"""Offline regressions for paper-accounting and malformed order boundaries."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from paper_execution import apply_pending_fills, fnum

TS = "2026-10-15T00:00:00+00:00"
BAD = (0, -1, "bad", "NaN", float("nan"), float("inf"), float("-inf"), True)


def base_state(held=False):
    return {
        "cash_sek": 8000 if held else 10000,
        "total_value_sek": 10000,
        "positions": ([{
            "s": "BTC", "entry": 100, "mark": 100, "qty": 20,
            "alloc_sek": 2000, "size_pct": 20, "size_pct_locked": 20,
        }] if held else []),
        "closed_trades": [],
    }


class NumericBoundaryTests(unittest.TestCase):
    def test_fnum_rejects_nonfinite_boolean_and_overflow(self):
        for value in ("NaN", "Infinity", "-Infinity", True, False,
                      float("nan"), float("inf"), 10 ** 1000, None, "bad"):
            with self.subTest(value_type=type(value).__name__):
                self.assertIsNone(fnum(value))
        self.assertEqual(fnum("12.5"), 12.5)
        self.assertEqual(fnum(0), 0)

    def test_invalid_quotes_do_not_buy_add_or_sell(self):
        for op in ("KÖP", "ADD", "SÄLJ"):
            for quote in BAD:
                with self.subTest(op=op, quote=quote):
                    state = base_state(held=op != "KÖP")
                    before = deepcopy(state)
                    state["pending_command"] = {"op": op, "s": "BTC", "entry": 100}
                    result = apply_pending_fills(state, {"BTC": quote}, TS)
                    self.assertEqual(result["positions"], before["positions"])
                    self.assertEqual(result["cash_sek"], before["cash_sek"])
                    self.assertEqual(result["closed_trades"], [])
                    self.assertEqual(result["fills_applied"][0]["skip"], "no_price")
                    json.dumps(result, allow_nan=False)

    def test_missing_quote_retains_explicit_fallback(self):
        state = base_state()
        state["pending_command"] = {"op": "KÖP", "s": "BTC", "entry": "100"}
        self.assertEqual(len(apply_pending_fills(state, {}, TS)["positions"]), 1)

    def test_invalid_fallback_does_not_buy(self):
        for quote in BAD:
            with self.subTest(quote=quote):
                state = base_state()
                state["pending_command"] = {"op": "KÖP", "s": "BTC", "entry": quote}
                self.assertEqual(apply_pending_fills(state, {}, TS)["positions"], [])

    def test_invalid_sizes_do_not_default_to_large_orders(self):
        for op in ("KÖP", "ADD"):
            for size in BAD:
                with self.subTest(op=op, size=size):
                    state = base_state(held=op == "ADD")
                    before = deepcopy(state)
                    state["pending_command"] = {"op": op, "s": "BTC", "size_pct": size}
                    result = apply_pending_fills(state, {"BTC": 100}, TS)
                    self.assertEqual(result["cash_sek"], before["cash_sek"])
                    self.assertEqual(result["positions"], before["positions"])
                    self.assertEqual(result["fills_applied"][0]["skip"], "invalid_size")

    def test_zero_size_is_not_replaced_by_trade_plan(self):
        state = base_state()
        state["trade_plans"] = {"BTC": {"entry": 100, "size_pct": 40}}
        state["pending_command"] = {"op": "KÖP", "s": "BTC", "size_pct": 0}
        self.assertEqual(apply_pending_fills(state, {"BTC": 100}, TS)["positions"], [])

    def test_invalid_portfolio_totals_raise_before_mutation(self):
        for field in ("cash_sek", "total_value_sek"):
            for value in ("NaN", float("inf"), -1):
                with self.subTest(field=field, value=value):
                    state = base_state()
                    state[field] = value
                    state["pending_command"] = {"op": "KÖP", "s": "BTC", "entry": 100}
                    before = deepcopy(state)
                    with self.assertRaises(ValueError):
                        apply_pending_fills(state, {"BTC": 100}, TS)
                    self.assertEqual(state, before)

    def test_bad_order_does_not_block_next_valid_order(self):
        state = base_state(held=True)
        state["pending_fills"] = [
            {"op": "SÄLJ", "s": "BTC", "frac": "bad"},
            {"op": "SÄLJ", "s": "BTC", "frac": 0.5},
        ]
        result = apply_pending_fills(state, {"BTC": 100}, TS)
        self.assertEqual(result["positions"][0]["qty"], 10)
        self.assertEqual(len(result["closed_trades"]), 1)

    def test_invalid_cost_configuration_fails_early(self):
        root = Path(__file__).resolve().parents[1]
        for key in ("PAPER_FEE_RATE", "PAPER_SLIPPAGE_RATE"):
            for value in ("NaN", "Infinity", "-0.1", "1", "bad"):
                with self.subTest(key=key, value=value):
                    env = dict(os.environ, PAPER_FEE_RATE="0.001", PAPER_SLIPPAGE_RATE="0.0005")
                    env[key] = value
                    proc = subprocess.run([sys.executable, "-c", "import paper_execution"],
                                          cwd=root, env=env, capture_output=True,
                                          text=True, timeout=10)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn(key, proc.stderr)


class FractionTests(unittest.TestCase):
    def test_zero_fraction_does_not_sell_everything(self):
        self.assert_invalid_fraction(0)

    def assert_invalid_fraction(self, fraction):
        state = base_state(held=True)
        before = deepcopy(state)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": fraction}
        result = apply_pending_fills(state, {"BTC": 110}, TS)
        self.assertEqual(result["positions"], before["positions"])
        self.assertEqual(result["cash_sek"], before["cash_sek"])
        self.assertEqual(result["closed_trades"], [])
        self.assertEqual(result["fills_applied"][0]["skip"], "invalid_fraction")

    def test_out_of_range_or_malformed_fractions_are_skipped(self):
        for fraction in BAD[1:] + (1.1, "0"):
            with self.subTest(fraction=fraction):
                self.assert_invalid_fraction(fraction)

    def test_near_full_sale_keeps_unsold_remainder(self):
        state = base_state(held=True)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": 0.999}
        result = apply_pending_fills(state, {"BTC": 110}, TS)
        self.assertEqual(len(result["positions"]), 1)
        self.assertAlmostEqual(result["positions"][0]["qty"], 0.02)
        self.assertAlmostEqual(result["positions"][0]["alloc_sek"], 2)
        self.assertAlmostEqual(result["closed_trades"][0]["qty"] +
                               result["positions"][0]["qty"], 20)

    def test_small_fraction_is_not_rounded_up_to_one_percent(self):
        state = base_state(held=True)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": 0.001}
        result = apply_pending_fills(state, {"BTC": 110}, TS)
        self.assertAlmostEqual(result["closed_trades"][0]["qty"], 0.02)
        self.assertAlmostEqual(result["positions"][0]["qty"], 19.98)

    def test_valid_fraction_string_is_supported(self):
        state = base_state(held=True)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": "0.5"}
        result = apply_pending_fills(state, {"BTC": 110}, TS)
        self.assertEqual(result["positions"][0]["qty"], 10)

    def test_full_sale_still_closes_position(self):
        state = base_state(held=True)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": 1}
        self.assertEqual(apply_pending_fills(state, {"BTC": 110}, TS)["positions"], [])

    def test_missing_fraction_still_means_full_sale(self):
        state = base_state(held=True)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC"}
        self.assertEqual(apply_pending_fills(state, {"BTC": 110}, TS)["positions"], [])


class HistoryIntegrityTests(unittest.TestCase):
    def test_more_than_200_trades_survive_repeated_noop_cycles(self):
        state = base_state()
        state["closed_trades"] = [{"id": i, "pnl_sek": 1 if i % 2 else -1}
                                  for i in range(250)]
        before = deepcopy(state["closed_trades"])
        for _ in range(3):
            apply_pending_fills(state, {}, TS)
            self.assertEqual(state["closed_trades"], before)
            self.assertEqual(state["wins"], 125)
            self.assertEqual(state["losses"], 125)
            self.assertEqual(state["trades"], 250)

    def test_legacy_records_and_precision_are_not_rewritten(self):
        state = base_state()
        state["closed_trades"] = [{"pnl": 1.23456}, {"pnl_sek": -0.98765},
                                  {"pnl_sek": 0, "pnl": 20}]
        before = deepcopy(state["closed_trades"])
        result = apply_pending_fills(state, {}, TS)
        self.assertEqual(result["closed_trades"], before)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)

    def test_new_sale_appends_without_changing_old_records(self):
        state = base_state(held=True)
        state["closed_trades"] = [{"id": i, "pnl": i / 7} for i in range(250)]
        before = deepcopy(state["closed_trades"])
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC"}
        result = apply_pending_fills(state, {"BTC": 110}, TS)
        self.assertEqual(result["closed_trades"][:-1], before)
        self.assertEqual(len(result["closed_trades"]), 251)
        self.assertEqual(result["closed_trades"][-1]["pnl"], result["closed_trades"][-1]["pnl_sek"])

    def test_consumed_sale_is_not_replayed(self):
        state = base_state(held=True)
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": 0.5}
        apply_pending_fills(state, {"BTC": 110}, TS)
        before = deepcopy(state)
        apply_pending_fills(state, {"BTC": 110}, TS)
        for key in ("cash_sek", "positions", "closed_trades", "wins", "losses"):
            self.assertEqual(state[key], before[key])


if __name__ == "__main__":
    unittest.main()
