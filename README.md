# Crypto paper scanner

Hourly paper-trading scanner. It never submits exchange orders.

The GitHub workflow runs `run_scanner.py`, validates complete JSON output, and
only then commits the updated state. `signal.json` contains the latest manual
action for the user. A GitHub issue is created only for a buy, sell, half-sell,
or stop-loss move, so GitHub can notify the repository owner. The initial paper
balance is SEK 10,000 and no real exchange order is ever submitted.
