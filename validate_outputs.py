#!/usr/bin/env python3
"""Validate a consistent completed scan before publishing canonical state."""
from pathlib import Path

from state_io import read_json


def validate_outputs(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    state = read_json(root / "crypto_daytrade_state.json")
    out = read_json(root / "scan_live.json")
    signal = read_json(root / "signal.json")
    if not all(isinstance(item, dict) for item in (state, out, signal)):
        raise ValueError("All scanner outputs must be JSON objects")
    for key in ("struct80", "candidate_book", "expectancy_log"):
        if key not in state:
            raise ValueError(f"Missing state field: {key}")
    if not isinstance(state["struct80"], list) or len(state["struct80"]) != 80:
        raise ValueError("Expected 80 structure rows")
    if (state.get("health") or {}).get("status") != "ok":
        raise ValueError("Scan health is not ok")
    if state.get("version") != "3.4.0":
        raise ValueError("Unexpected scanner version")
    if not state.get("updated") or state["updated"] != out.get("ts"):
        raise ValueError("State and scan output have different snapshot timestamps")
    portfolio = signal.get("portfolio")
    if not isinstance(portfolio, dict):
        raise ValueError("Signal portfolio is missing")
    for key in ("cash_sek", "total_value_sek", "positions"):
        if key not in portfolio or key not in state or portfolio[key] != state[key]:
            raise ValueError(f"Signal portfolio does not match state: {key}")
    if signal.get("status") not in ("action", "no_action"):
        raise ValueError("Unknown signal status")
    has_action = bool(signal.get("command") or signal.get("stop_moves"))
    if (signal["status"] == "action") != has_action:
        raise ValueError("Signal status disagrees with its actions")
    return state


if __name__ == "__main__":
    validate_outputs()
