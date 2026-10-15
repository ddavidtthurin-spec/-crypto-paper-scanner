"""No-network regressions for decision boundaries and strict state I/O."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import run_scanner
from state_io import read_json, write_json
from validate_outputs import validate_outputs


def setup(symbol="ETH", q=9):
    return {"s": symbol, "why": "RANGE_BREAK_HOLD", "q": q, "volr4": 1.5,
            "plan": {"entry": 100, "stop": 96, "tp1": 106.4, "tp2": 111.2, "risk_pct": 4}}


def state():
    return {"positions": [], "rotation": {"slots_free": 2}, "flow": "RISK_ON", "regime": "TREND_UP"}


class DecisionBoundaryTests(unittest.TestCase):
    def test_numeric_sale_fraction_is_not_converted_to_full_sale(self):
        for fraction in (0.001, 0.5, 0.999, "0.25"):
            with self.subTest(fraction=fraction):
                s = state()
                s["exit_now"] = {"op": "SÄLJ", "s": "BTC", "frac": fraction}
                self.assertEqual(run_scanner.choose_command(s, {})["frac"], float(fraction))

    def test_invalid_exit_fraction_cannot_trigger_sale_or_buy(self):
        for fraction in (0, -1, 1.01, "bad", "NaN", True, False):
            with self.subTest(fraction=fraction):
                s = state()
                s["exit_now"] = {"op": "SÄLJ", "s": "BTC", "frac": fraction}
                self.assertIsNone(run_scanner.choose_command(s, {"a_setups": [setup()]}))

    def test_legacy_sale_labels_are_preserved(self):
        for label, fraction in ((None, 1), ("HELT", 1), ("HÄLFTEN", 0.5), ("HALFTEN", 0.5)):
            with self.subTest(label=label):
                s = state()
                s["exit_now"] = {"op": "SÄLJ", "s": "BTC", "frac": label}
                self.assertEqual(run_scanner.choose_command(s, {})["frac"], fraction)

    def test_invalid_risk_does_not_create_maximum_buy(self):
        for risk in ("NaN", float("inf"), -1, 0, "bad", True):
            with self.subTest(risk=risk):
                row = setup()
                row["plan"]["risk_pct"] = risk
                self.assertIsNone(run_scanner.choose_command(state(), {"a_setups": [row]}))

    def test_malformed_candidate_does_not_hide_valid_candidate(self):
        bad = setup("BTC", 20)
        bad["plan"]["stop"] = float("nan")
        command = run_scanner.choose_command(state(), {"a_setups": [None, [], bad, setup()]})
        self.assertEqual(command["s"], "ETH")
        self.assertEqual(command["size_pct"], 31.25)

    def test_invalid_price_levels_are_rejected(self):
        for key, value in (("stop", 101), ("entry", 0), ("tp1", 99), ("tp2", 105),
                           ("tp1", "NaN"), ("entry", True)):
            with self.subTest(key=key, value=value):
                row = setup()
                row["plan"][key] = value
                self.assertIsNone(run_scanner.choose_command(state(), {"a_setups": [row]}))

    def test_nonfinite_ranking_and_bad_slots_do_not_crash(self):
        bad = setup("BTC", "NaN")
        self.assertEqual(run_scanner.choose_command(state(), {"a_setups": [bad, setup()]})["s"], "ETH")
        s = state()
        s["rotation"]["slots_free"] = "bad"
        self.assertIsNone(run_scanner.choose_command(s, {"a_setups": [setup()]}))


class StrictJsonTests(unittest.TestCase):
    def test_nonfinite_json_input_is_rejected(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            for value in ("NaN", "Infinity", "-Infinity", "1e400"):
                with self.subTest(value=value):
                    p.write_text('{"value":' + value + '}')
                    with self.assertRaises(ValueError):
                        read_json(p)

    def test_failed_write_preserves_existing_file_and_cleans_temporary(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            write_json(p, {"cash": 10000})
            original = p.read_bytes()
            for value in (float("nan"), float("inf"), object()):
                with self.subTest(value_type=type(value).__name__):
                    with self.assertRaises((ValueError, TypeError)):
                        write_json(p, {"cash": value})
                    self.assertEqual(p.read_bytes(), original)
                    self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_unicode_and_finite_values_round_trip(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            value = {"op": "SÄLJ", "qty": 0.001, "closed": [], "optional": None}
            write_json(p, value)
            self.assertEqual(read_json(p), value)


class SnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.s = {"updated": "2026-10-15T12:00:00+00:00", "struct80": [{}] * 80,
                  "candidate_book": {}, "expectancy_log": {}, "health": {"status": "ok"},
                  "version": "3.4.0", "cash_sek": 10000, "total_value_sek": 10000,
                  "positions": []}
        self.out = {"ts": self.s["updated"]}
        self.signal = {"status": "no_action", "command": None, "stop_moves": [],
                       "portfolio": {key: self.s[key] for key in ("cash_sek", "total_value_sek", "positions")}}

    def save(self):
        for name, value in (("crypto_daytrade_state.json", self.s),
                            ("scan_live.json", self.out), ("signal.json", self.signal)):
            write_json(self.root / name, value)

    def test_consistent_outputs_pass(self):
        self.save()
        self.assertEqual(validate_outputs(self.root), self.s)

    def test_different_scan_timestamp_is_rejected(self):
        self.out["ts"] = "old"
        self.save()
        with self.assertRaisesRegex(ValueError, "timestamp"):
            validate_outputs(self.root)

    def test_stale_signal_portfolio_is_rejected(self):
        self.signal["portfolio"]["cash_sek"] = 9999
        self.save()
        with self.assertRaisesRegex(ValueError, "portfolio"):
            validate_outputs(self.root)

    def test_action_status_must_match_payload(self):
        self.signal["status"] = "action"
        self.save()
        with self.assertRaisesRegex(ValueError, "status"):
            validate_outputs(self.root)

    def test_existing_health_and_structure_requirements_remain(self):
        for field, value in (("version", "wrong"), ("struct80", []), ("health", {"status": "degraded"})):
            with self.subTest(field=field):
                old = self.s[field]
                self.s[field] = value
                self.save()
                with self.assertRaises(ValueError):
                    validate_outputs(self.root)
                self.s[field] = old

    def test_runner_rejects_mismatched_scan_outputs_before_decision(self):
        self.out["ts"] = "old"
        self.save()
        with patch.object(run_scanner, "STATE", self.root / "crypto_daytrade_state.json"), \
             patch.object(run_scanner, "OUT", self.root / "scan_live.json"), \
             patch.object(run_scanner.subprocess, "run") as process:
            process.return_value.returncode = 0
            process.return_value.stdout = "DONE"
            with self.assertRaisesRegex(RuntimeError, "snapshots"):
                run_scanner.run_scan()


if __name__ == "__main__":
    unittest.main()
