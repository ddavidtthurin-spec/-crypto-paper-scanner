#!/usr/bin/env python3
"""Validated decision runner for scan_v33.py (paper trading only)."""
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from paper_execution import fnum
from state_io import read_json, write_json

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "crypto_daytrade_state.json"
OUT = ROOT / "scan_live.json"
SIGNAL = ROOT / "signal.json"
SCANNER = ROOT / "scan_v33.py"



def run_scan():
    env = dict(os.environ, SCANNER_DATA_DIR=str(ROOT), PYTHONUNBUFFERED="1")
    proc = subprocess.run(
        [sys.executable, str(SCANNER)], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=12 * 60,
    )
    if proc.returncode != 0 or "DONE" not in proc.stdout:
        raise RuntimeError((proc.stderr or proc.stdout or "scanner failed")[-2000:])
    state, out = read_json(STATE), read_json(OUT)
    if not isinstance(state, dict) or not isinstance(out, dict):
        raise RuntimeError("scanner outputs must be JSON objects")
    if not state.get("updated") or state["updated"] != out.get("ts"):
        raise RuntimeError("state and scan output belong to different snapshots")
    required = ("struct80", "candidate_book", "expectancy_log")
    missing = [key for key in required if key not in state]
    if missing:
        raise RuntimeError("incomplete state: " + ", ".join(missing))
    return state, out


def choose_command(state, out):
    exit_now = state.get("exit_now") or {}
    if exit_now.get("op") == "SÄLJ" and exit_now.get("s"):
        raw_fraction = exit_now.get("frac")
        label = str(raw_fraction).strip().upper()
        fraction = (1.0 if raw_fraction is None or label == "HELT" else
                    0.5 if label in ("HÄLFTEN", "HALFTEN") else fnum(raw_fraction))
        if fraction is None or not 0 < fraction <= 1:
            return None
        return {
            "op": "SÄLJ", "s": exit_now["s"], "frac": fraction,
            "exit": exit_now.get("px"), "anledning": exit_now.get("reason") or "exit_now",
        }
    rotation = state.get("rotation") or {}
    if rotation.get("rotate") and rotation.get("weakest"):
        return {
            "op": "SÄLJ", "s": rotation["weakest"], "frac": 1.0,
            "anledning": rotation.get("reason") or "rotation",
        }
    if (fnum(rotation.get("slots_free")) or 0) <= 0:
        return None
    if state.get("regime") == "RISK_OFF" or state.get("flow") == "RISK_OFF":
        return None
    held = {p.get("s") for p in (state.get("positions") or []) if p.get("s")}
    setups = []
    for row in out.get("a_setups") or []:
        if not isinstance(row, dict) or not isinstance(row.get("s"), str) or not row["s"]:
            continue
        if row["s"] in held or row.get("why") != "RANGE_BREAK_HOLD":
            continue
        plan = row.get("plan")
        if not isinstance(plan, dict):
            continue
        levels = [fnum(plan.get(key)) for key in ("stop", "entry", "tp1", "tp2")]
        if any(value is None or value <= 0 for value in levels):
            continue
        if not levels[0] < levels[1] < levels[2] <= levels[3]:
            continue
        risk_pct = fnum(plan.get("risk_pct"))
        if risk_pct is None or risk_pct <= 0:
            continue
        size_pct = round(min(45.0, 125.0 / risk_pct), 2)
        if size_pct < 15.0:
            continue
        setups.append((row, size_pct))
    if not setups:
        return None
    setups.sort(key=lambda item: (fnum(item[0].get("q")) or 0,
                                 fnum(item[0].get("volr4")) or 0), reverse=True)
    best, size_pct = setups[0]
    plan = best["plan"]
    # Keep the existing 1.25% signal-risk budget and 45% exposure cap.
    return {
        "op": "KÖP", "s": best["s"], "size_pct": size_pct,
        "entry": plan["entry"], "stop": plan["stop"],
        "tp1": plan["tp1"], "tp2": plan["tp2"],
        "anledning": f"RANGE_BREAK_HOLD q={best.get('q')}",
    }


def enrich_command(command, state):
    """Attach the exact paper size and risk levels used in the manual alert."""
    if not command:
        return None
    command = dict(command)
    equity = float(state.get("total_value_sek") or state.get("start_kassa_sek") or 10000)
    if command.get("op") == "KÖP":
        size_pct = float(command.get("size_pct") or 45.0)
        command["size_sek"] = round(equity * size_pct / 100.0, 2)
        command["size_pct"] = size_pct
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
    previous_cycle = before.get("cycle_completed_at") or before.get("updated")
    before_positions = {p.get("s"): deepcopy(p) for p in before.get("positions", []) if p.get("s")}
    state, out = run_scan()
    command = enrich_command(choose_command(state, out), state)
    if command:
        state["pending_command"] = command
        write_json(STATE, state)
        state, out = run_scan()
        fills = [f for f in (state.get("fills_applied") or []) if f.get("fill")]
        matching = next((f for f in fills if f.get("s") == command.get("s") and f.get("fill") == command.get("op")), None)
        if not matching:
            command = None
        else:
            command["paper_price"] = matching.get("price") or command.get("paper_price")
            command["signal_price"] = matching.get("signal_price")
            command["size_sek"] = matching.get("alloc") or command.get("size_sek")
            command["fee_sek"] = matching.get("fee")

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
    cycle_completed = datetime.now(timezone.utc).isoformat()
    health = state.get("health") if isinstance(state.get("health"), dict) else {}
    try:
        prior = datetime.fromisoformat(previous_cycle)
        current = datetime.fromisoformat(cycle_completed)
        health["interval_minutes"] = round((current - prior).total_seconds() / 60.0, 2)
    except (TypeError, ValueError):
        health["interval_minutes"] = None
    health["cycle_completed"] = cycle_completed
    state["health"] = health
    state["cycle_completed_at"] = cycle_completed
    write_json(STATE, state)
    write_json(SIGNAL, signal)
    print(json.dumps(signal, ensure_ascii=False))


if __name__ == "__main__":
    main()
