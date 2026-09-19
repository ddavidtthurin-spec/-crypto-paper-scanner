import unittest

from paper_execution import FEE_RATE, SLIPPAGE_RATE, apply_pending_fills
from run_scanner import choose_command


class ExecutionTests(unittest.TestCase):
    def base_state(self):
        return {
            "cash_sek": 10_000,
            "total_value_sek": 10_000,
            "start_kassa_sek": 10_000,
            "positions": [],
            "closed_trades": [],
        }

    def test_buy_uses_equity_weight_and_costs(self):
        state = self.base_state()
        state["pending_command"] = {
            "op": "KÖP", "s": "BTC", "size_pct": 40,
            "entry": 100, "stop": 95, "tp1": 108, "tp2": 114,
        }
        result = apply_pending_fills(state, {"BTC": 100}, "2026-01-01T00:00:00+00:00")
        position = result["positions"][0]
        self.assertAlmostEqual(position["alloc_sek"], 4000.0, places=2)
        self.assertAlmostEqual(result["cash_sek"], 6000.0, places=2)
        self.assertAlmostEqual(position["entry"], 100 * (1 + SLIPPAGE_RATE), places=8)
        self.assertGreater(position["entry_fee_sek"], 0)

    def test_half_sale_is_not_repeated_and_reduces_locked_size(self):
        state = self.base_state()
        state["cash_sek"] = 6000
        state["positions"] = [{
            "s": "BTC", "entry": 100, "qty": 40, "alloc_sek": 4000,
            "size_pct": 40, "size_pct_locked": 40, "tp1": 110,
        }]
        state["pending_command"] = {"op": "SÄLJ", "s": "BTC", "frac": 0.5, "anledning": "tp1_scale"}
        result = apply_pending_fills(state, {"BTC": 110}, "2026-01-01T01:00:00+00:00")
        position = result["positions"][0]
        self.assertTrue(position["scaled_tp1"])
        self.assertEqual(position["qty"], 20)
        self.assertEqual(position["size_pct_locked"], 20)
        self.assertGreater(result["closed_trades"][0]["exit_fee_sek"], 0)

    def test_duplicate_buy_is_skipped(self):
        state = self.base_state()
        state["positions"] = [{"s": "BTC", "qty": 1, "alloc_sek": 4000}]
        state["pending_command"] = {"op": "KÖP", "s": "BTC", "entry": 100, "size_pct": 40}
        result = apply_pending_fills(state, {"BTC": 100}, "2026-01-01T00:00:00+00:00")
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["fills_applied"][0]["skip"], "already_held")

    def test_add_respects_total_exposure_cap(self):
        state = self.base_state()
        state["cash_sek"] = 6000
        state["positions"] = [{"s": "BTC", "entry": 100, "qty": 40,
                               "alloc_sek": 4000, "size_pct": 40,
                               "size_pct_locked": 40}]
        state["pending_command"] = {"op": "ADD", "s": "BTC", "size_pct": 10}
        result = apply_pending_fills(state, {"BTC": 105}, "2026-01-01T00:00:00+00:00")
        self.assertEqual(result["fills_applied"][0]["skip"], "risk_cap")


class DecisionTests(unittest.TestCase):
    def setup(self):
        return {"s": "ETH", "why": "RANGE_BREAK_HOLD", "q": 9, "volr4": 1.5,
                "plan": {"entry": 100, "stop": 96, "tp1": 106.4, "tp2": 111.2, "risk_pct": 4}}

    def state(self):
        return {"positions": [], "regime": "TREND_UP", "flow": "RISK_ON",
                "rotation": {"slots_free": 2}}

    def test_risk_based_size(self):
        command = choose_command(self.state(), {"a_setups": [self.setup()]})
        self.assertEqual(command["size_pct"], 31.25)

    def test_held_symbol_is_not_rebought(self):
        state = self.state()
        state["positions"] = [{"s": "ETH"}]
        self.assertIsNone(choose_command(state, {"a_setups": [self.setup()]}))

    def test_risk_off_blocks_new_buy(self):
        state = self.state()
        state["flow"] = "RISK_OFF"
        self.assertIsNone(choose_command(state, {"a_setups": [self.setup()]}))


if __name__ == "__main__":
    unittest.main()
