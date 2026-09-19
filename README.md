# Crypto paper scanner v3.4

30-minute paper-trading scanner. It never submits exchange orders.

The GitHub workflow runs `run_scanner.py`, validates complete JSON output, and
only then commits the updated state. `signal.json` contains the latest manual
action for the user. A GitHub issue is created only for a buy, sell, half-sell,
or stop-loss move, so GitHub can notify the repository owner. The initial paper
balance is SEK 10,000 and no real exchange order is ever submitted.

Entries require completed-candle breakout structure, aligned 4H/1H EMA trend,
acceptable RSI, minimum ADX and volume/price-quality checks. Position size risks
at most 1.25% of current equity and is capped at 45% exposure. Paper fills model
0.10% exchange fees and 0.05% slippage per side. These assumptions can be
overridden with `PAPER_FEE_RATE` and `PAPER_SLIPPAGE_RATE`.
