#!/usr/bin/env python3
"""Create a GitHub notification issue for a new paper-trade action."""
import json
import os
import subprocess
from pathlib import Path

signal = json.loads(Path("signal.json").read_text(encoding="utf-8"))
if signal.get("status") != "action":
    raise SystemExit(0)

cmd = signal.get("command") or {}
moves = signal.get("stop_moves") or []
op = cmd.get("op") or ("FLYTTA STOPLOSS" if moves else "SIGNAL")
token = cmd.get("s") or (moves[0].get("s") if moves else "PORTFÖLJ")
if op == "SÄLJ" and float(cmd.get("frac") or 1) == 0.5:
    op = "SÄLJ HÄLFTEN"

lines = [
    "## Manuellt pappershandelskommando",
    "",
    f"- **Kommando:** {op} {token}",
]
if cmd:
    lines += [
        f"- **Signalpris:** {cmd.get('signal_price')}",
        f"- **Papperspris:** {cmd.get('paper_price')}",
        f"- **Storlek:** {cmd.get('size_sek')} SEK ({cmd.get('size_pct')} %)",
        f"- **Modellerad avgift:** {cmd.get('fee_sek')} SEK",
        f"- **Stoploss:** {cmd.get('stop')}",
        f"- **TP1 / TP2:** {cmd.get('tp1')} / {cmd.get('tp2')}",
        f"- **Anledning:** {cmd.get('anledning')}",
    ]
for move in moves:
    lines += [
        "",
        f"### FLYTTA STOPLOSS {move.get('s')}",
        f"Från **{move.get('from')}** till **{move.get('to')}** vid aktuellt pris **{move.get('mark')}** (trailing/vinstlåsning).",
    ]
lines += ["", "> Detta är pappershandel. Ingen riktig börsorder har lagts."]

env = dict(os.environ)
env["GH_TOKEN"] = os.environ["GITHUB_TOKEN"]
owner = os.environ.get("GITHUB_REPOSITORY", "/").split("/", 1)[0]
args = ["gh", "issue", "create", "--title", f"Papperssignal: {op} {token}", "--body", "\n".join(lines)]
if owner:
    args += ["--assignee", owner]
subprocess.run(
    args,
    check=True,
    env=env,
)
