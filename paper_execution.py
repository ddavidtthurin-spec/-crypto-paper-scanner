"""Deterministic paper-execution model with conservative trading costs."""
from __future__ import annotations

import os


def fnum(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


FEE_RATE = float(os.environ.get("PAPER_FEE_RATE", "0.001"))
SLIPPAGE_RATE = float(os.environ.get("PAPER_SLIPPAGE_RATE", "0.0005"))


def _normalize_command(command, state):
    if not isinstance(command, dict):
        return None
    out = dict(command)
    op = out.get("op") or out.get("cmd") or out.get("action")
    if isinstance(op, str):
        out["op"] = (
            op.upper().replace("KOP", "KÖP").replace("BUY", "KÖP")
            .replace("SELL", "SÄLJ").replace("SALJ", "SÄLJ")
        )
    symbol = out.get("s") or out.get("token") or out.get("sym") or out.get("ticker")
    if isinstance(symbol, str):
        out["s"] = symbol.upper().replace("-USDT", "").replace("USDT", "")
    if out.get("op") == "KÖP" and out.get("s") and not out.get("entry"):
        plan = ((state.get("trade_plans") or {}).get(out["s"]) or {})
        for key in ("entry", "stop", "tp1", "tp2"):
            out[key] = out.get(key) or plan.get(key)
        out["size_pct"] = out.get("size_pct") or plan.get("size_pct") or 45.0
    return out


def apply_pending_fills(state, price_now, ts):
    """Apply explicit queued commands once, including fee and slippage."""
    positions = list(state.get("positions") or [])
    cash = float(state.get("cash_sek") or 0)
    closed = list(state.get("closed_trades") or [])
    applied = []
    commands = list(state.get("pending_fills") or [])
    pending = state.get("pending_command") or state.get("last_command")
    if isinstance(pending, dict):
        commands.append(pending)
    commands = [c for c in (_normalize_command(c, state) for c in commands) if c]
    held = {p.get("s") for p in positions if p.get("s")}
    equity = float(
        state.get("total_value_sek")
        or (cash + sum(float(p.get("value_sek") or p.get("alloc_sek") or 0) for p in positions))
    )

    def market_price(symbol, fallback=None):
        return fnum(price_now.get(symbol)) or fnum(fallback)

    for command in commands:
        op, symbol = command.get("op"), command.get("s")
        if op == "KÖP" and symbol:
            if symbol in held:
                applied.append({"skip": "already_held", "s": symbol, "op": op})
                continue
            if len(positions) >= 2:
                applied.append({"skip": "max_pos", "s": symbol, "op": op})
                continue
            quote = market_price(symbol, command.get("entry"))
            if not quote:
                applied.append({"skip": "no_price", "s": symbol, "op": op})
                continue
            size_pct = min(45.0, max(10.0, fnum(command.get("size_pct")) or 45.0))
            budget = round(min(equity * size_pct / 100.0, cash * 0.995), 2)
            if budget < 50:
                applied.append({"skip": "no_cash", "s": symbol, "op": op})
                continue
            notional = budget / (1.0 + FEE_RATE)
            fee = budget - notional
            fill_price = quote * (1.0 + SLIPPAGE_RATE)
            qty = notional / fill_price
            positions.append({
                "s": symbol, "entry": fill_price, "signal_price": quote,
                "stop": fnum(command.get("stop")), "tp1": fnum(command.get("tp1")),
                "tp2": fnum(command.get("tp2")), "size_pct": size_pct,
                "size_pct_locked": size_pct, "size_pct_cmd": size_pct,
                "mtm_weight_pct": size_pct, "qty": qty, "alloc_sek": budget,
                "opened": ts, "mark": quote, "fills": 1,
                "entry_fee_sek": round(fee, 4), "entry_slippage_pct": SLIPPAGE_RATE * 100,
                "legs": [{"t": ts, "op": "KÖP", "signal_px": quote,
                          "px": fill_price, "qty": qty, "alloc_sek": budget,
                          "fee_sek": round(fee, 4), "size_pct": size_pct}],
            })
            cash -= budget
            held.add(symbol)
            applied.append({"fill": "KÖP", "s": symbol, "signal_price": quote,
                            "price": fill_price, "alloc": budget,
                            "fee": round(fee, 4), "size_pct": size_pct})
        elif op == "ADD" and symbol:
            position = next((p for p in positions if p.get("s") == symbol), None)
            if not position:
                applied.append({"skip": "add_not_held", "s": symbol, "op": op})
                continue
            quote = market_price(symbol, command.get("entry") or position.get("mark") or position.get("entry"))
            if not quote:
                applied.append({"skip": "no_price", "s": symbol, "op": op})
                continue
            add_pct = min(20.0, max(5.0, fnum(command.get("size_pct")) or 10.0))
            budget = round(min(equity * add_pct / 100.0, cash * 0.995), 2)
            total_pct = float(position.get("size_pct_locked") or position.get("size_pct") or 0) + add_pct
            if budget < 50 or total_pct > 45.0:
                applied.append({"skip": "risk_cap", "s": symbol, "op": op})
                continue
            notional = budget / (1.0 + FEE_RATE)
            fee = budget - notional
            fill_price = quote * (1.0 + SLIPPAGE_RATE)
            qty_add = notional / fill_price
            old_qty = float(position.get("qty") or 0)
            old_cost = float(position.get("alloc_sek") or 0)
            new_qty, new_cost = old_qty + qty_add, old_cost + budget
            position["entry"] = ((float(position.get("entry") or fill_price) * old_qty) + (fill_price * qty_add)) / new_qty
            position["qty"], position["alloc_sek"] = new_qty, new_cost
            position["size_pct"] = position["size_pct_locked"] = round(total_pct, 2)
            position["fills"] = int(position.get("fills") or 1) + 1
            legs = list(position.get("legs") or [])
            legs.append({"t": ts, "op": "ADD", "signal_px": quote, "px": fill_price,
                         "qty": qty_add, "alloc_sek": budget, "fee_sek": round(fee, 4),
                         "size_pct": add_pct})
            position["legs"] = legs
            cash -= budget
            applied.append({"fill": "ADD", "s": symbol, "signal_price": quote,
                            "price": fill_price, "alloc": budget,
                            "fee": round(fee, 4), "size_pct": add_pct})
        elif op == "SÄLJ" and symbol:
            position = next((p for p in positions if p.get("s") == symbol), None)
            if not position:
                applied.append({"skip": "not_held", "s": symbol, "op": op})
                continue
            fraction = min(1.0, max(0.01, float(command.get("frac") or 1.0)))
            quote = market_price(symbol, command.get("exit") or position.get("mark") or position.get("entry"))
            if not quote:
                applied.append({"skip": "no_price", "s": symbol, "op": op})
                continue
            fill_price = quote * (1.0 - SLIPPAGE_RATE)
            qty = float(position.get("qty") or 0) * fraction
            gross_proceeds = qty * fill_price
            exit_fee = gross_proceeds * FEE_RATE
            net_proceeds = gross_proceeds - exit_fee
            cost_basis = float(position.get("alloc_sek") or 0) * fraction
            pnl = net_proceeds - cost_basis
            closed.append({
                "s": symbol, "side": "LONG", "entry": position.get("entry"),
                "signal_exit": quote, "exit": fill_price, "qty": qty,
                "gross_proceeds_sek": round(gross_proceeds, 2),
                "exit_fee_sek": round(exit_fee, 4), "cost_basis_sek": round(cost_basis, 2),
                "pnl_sek": round(pnl, 2),
                "pct": round(100.0 * pnl / cost_basis, 3) if cost_basis else None,
                "opened": position.get("opened"), "closed": ts,
                "reason": command.get("anledning") or "SÄLJ",
            })
            cash += net_proceeds
            if fraction >= 0.999:
                positions = [p for p in positions if p.get("s") != symbol]
                held.discard(symbol)
            else:
                remain = 1.0 - fraction
                position["qty"] = float(position.get("qty") or 0) * remain
                position["alloc_sek"] = float(position.get("alloc_sek") or 0) * remain
                position["size_pct_locked"] = float(position.get("size_pct_locked") or position.get("size_pct") or 0) * remain
                position["size_pct"] = position["size_pct_locked"]
                if command.get("anledning") == "tp1_scale":
                    position["scaled_tp1"] = True
            applied.append({"fill": "SÄLJ", "s": symbol, "signal_price": quote,
                            "price": fill_price, "pnl": round(pnl, 2),
                            "fee": round(exit_fee, 4), "frac": fraction})

    for trade in closed:
        pnl = fnum(trade.get("pnl_sek"))
        if pnl is None:
            pnl = fnum(trade.get("pnl")) or 0.0
        trade["pnl"] = trade["pnl_sek"] = round(pnl, 2)
    state["positions"] = positions
    state["cash_sek"] = cash
    state["closed_trades"] = closed[-200:]
    state["wins"] = sum(1 for trade in closed if float(trade.get("pnl_sek") or 0) > 0)
    state["losses"] = sum(1 for trade in closed if float(trade.get("pnl_sek") or 0) < 0)
    state["open_trades"] = len(positions)
    state["trades"] = len(closed) + len(positions)
    state["pending_fills"] = []
    state["pending_command"] = None
    state["last_command"] = None
    state["fills_applied"] = applied[-20:]
    state["execution_model"] = {
        "fee_rate_pct": FEE_RATE * 100,
        "slippage_rate_pct_per_side": SLIPPAGE_RATE * 100,
        "mode": "paper_conservative",
    }
    return state
