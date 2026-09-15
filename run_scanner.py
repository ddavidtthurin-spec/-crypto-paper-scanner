#!/usr/bin/env python3
"""Validated decision runner for scan_v33.py (paper trading only)."""
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "crypto_daytrade_state.json"
OUT = ROOT / "scan_live.json"
SIGNAL = ROOT / "signal.json"
SCANNER = ROOT / "scan_v33.py"


def read_json(path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def run_scan():
    env = dict(os.environ, SCANNER_DATA_DIR=str(ROOT), PYTHONUNBUFFERED="1")
    proc = subprocess.run(
        [sys.executable, str(SCANNER)], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=12 * 60,
    )
    if proc.returncode != 0 or "DONE" not in proc.stdout:
        raise RuntimeError((proc.stderr or proc.stdout or "scanner failed")[-2000:])
    state, out = read_json(STATE), read_json(OUT)
    required = ("struct80", "candidate_book", "expectancy_log")
    missing = [key for key in required if key not in state]
    if missing:
        raise RuntimeError("incomplete state: " + ", ".join(missing))
    return state, out


def choose_command(state, out):
    exit_now = state.get("exit_now") or {}
    if exit_now.get("op") == "SÄLJ" and exit_now.get("s"):
        half = str(exit_now.get("frac") or "").upper() in ("HÄLFTEN", "HALFTEN")
        return {
            "op": "SÄLJ", "s": exit_now["s"], "frac": 0.5 if half else 1.0,
            "exit": exit_now.get("px"), "anledning": exit_now.get("reason") or "exit_now",
        }
    rotation = state.get("rotation") or {}
    if rotation.get("rotate") and rotation.get("weakest"):
        return {
            "op": "SÄLJ", "s": rotation["weakest"], "frac": 1.0,
            "anledning": rotation.get("reason") or "rotation",
        }
    if int(rotation.get("slots_free") or 0) <= 0:
        return None
    setups = [
        row for row in (out.get("a_setups") or [])
        if row.get("why") == "RANGE_BREAK_HOLD"
        and isinstance(row.get("plan"), dict)
        and all(row["plan"].get(k) is not None for k in ("entry", "stop", "tp1", "tp2"))
    ]
    if not setups:
        return None
    setups.sort(key=lambda row: (row.get("q") or 0, row.get("volr4") or 0), reverse=True)
    best, plan = setups[0], setups[0]["plan"]
    return {
        "op": "KÖP", "s": best["s"], "size_pct": 45.0,
        "entry": plan["entry"], "stop": plan["stop"],
        "tp1": plan["tp1"], "tp2": plan["tp2"],
        "anledning": f"RANGE_BREAK_HOLD q={best.get('q')}",
    }


def enrich_command(command, state):
    """Attach the exact paper size and risk levels used in the manual alert."""
    if not command:
        return None
    command = dict(command)
    start_cash = float(state.get("start_kassa_sek") or 10000)
    if command.get("op") == "KÖP":
        command["size_sek"] = round(start_cash * 0.45, 2)
        command["size_pct"] = 45.0
        command["paper_price"] = command.get("entry")
        return command
    if command.get("op") == "SÄLJ":
        pos = next((p for p in state.get("positions") or [] if p.get("s") == command.get("s")), None)
        if pos:
            frac = float(command.get("frac") or 1.0)
            command["paper_price"] = command.get("exit") or pos.get("mark") or pos.get("entry")
            command["size_sek"] = round(float(pos.get("alloc_sek") or 0) * frac, 2)
            command["size_pct"] = round(float(pos.get("size_pct") or 0) * frac, 2)
            command["stop"] = pos.get("stop")
            command["tp1"] = pos.get("tp1")
            command["tp2"] = pos.get("tp2")
    return command


def main():
    before = read_json(STATE) if STATE.exists() else {}
    before_positions = {p.get("s"): deepcopy(p) for p in before.get("positions", []) if p.get("s")}
    state, out = run_scan()
    command = enrich_command(choose_command(state, out), state)
    if command:
        state["pending_command"] = command
        write_json(STATE, state)
        state, out = run_scan()

    sold = command.get("s") if command and command.get("op") == "SÄLJ" else None
    stop_moves = []
    for pos in state.get("positions") or []:
        sym = pos.get("s")
        old = (before_positions.get(sym) or {}).get("stop")
        new = pos.get("stop")
        if sym != sold and old is not None and new is not None and float(new) > float(old):
            stop_moves.append({"s": sym, "from": old, "to": new, "mark": pos.get("mark")})

    signal = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "status": "action" if command or stop_moves else "no_action",
        "command": command,
        "stop_moves": stop_moves,
        "portfolio": {
            "cash_sek": state.get("cash_sek"),
            "total_value_sek": state.get("total_value_sek"),
            "positions": state.get("positions") or [],
        },
    }
    write_json(SIGNAL, signal)
    print(json.dumps(signal, ensure_ascii=False))


if __name__ == "__main__":
    main()
