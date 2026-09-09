"""Offline checks for the approved research inputs and one-run guard."""
import copy
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from backtest.baseline import config, normalize_rules, capture_rules, run, write_json
from backtest.data import DataValidationError, SYMBOLS


def payload():
    return {"symbols": [dict(symbol=s, contractType="PERPETUAL", quoteAsset="USDT",
        marginAsset="USDT", status="TRADING", filters=[
            dict(filterType="PRICE_FILTER", tickSize="0.10"),
            dict(filterType="LOT_SIZE", minQty="0.001", maxQty="1000", stepSize="0.001"),
            dict(filterType="MARKET_LOT_SIZE", minQty="0.001", maxQty="120", stepSize="0.001"),
            dict(filterType="MIN_NOTIONAL", notional="50")]) for s in SYMBOLS]}


class BaselineTests(unittest.TestCase):
    def test_approved_costs(self):
        c = config()
        self.assertEqual((c.starting_equity, c.entry_rate, c.exit_rate, c.slippage_bps),
                         tuple(map(Decimal, ("1000", ".0005", ".0005", "2"))))
        self.assertEqual(c.cost_mode.value, "FEES_AND_SLIPPAGE")

    def test_market_intersection_and_decimal_canonicalization(self):
        data = payload()
        original = copy.deepcopy(data)
        rules = normalize_rules(data)
        self.assertEqual(rules["normalized_rules"]["BTCUSDT"]["max_quantity"], "120")
        self.assertEqual(rules["normalized_rules"]["BTCUSDT"]["tick_size"], "0.1")
        self.assertEqual(data, original)

    def test_deterministic_order_independent_fingerprint(self):
        data = payload()
        expected = normalize_rules(data)
        data["symbols"].reverse()
        for s in data["symbols"]:
            s["filters"].reverse()
        self.assertEqual(expected, normalize_rules(data))
        self.assertNotIn("retrieved_at_utc", expected)

    def test_missing_duplicate_and_different_grid_fail(self):
        for change in (lambda d: d["symbols"].pop(),
                       lambda d: d["symbols"].append(d["symbols"][0]),
                       lambda d: d["symbols"][0]["filters"].pop(),
                       lambda d: d["symbols"][0]["filters"][2].update(stepSize="0.002")):
            with self.subTest(change=change):
                data = payload()
                change(data)
                with self.assertRaises(DataValidationError):
                    normalize_rules(data)

    def test_float_and_nonfinite_rejected(self):
        for value in (0.1, "NaN", "Infinity", "0"):
            data = payload()
            data["symbols"][0]["filters"][0]["tickSize"] = value
            with self.assertRaises(DataValidationError):
                normalize_rules(data)

    def test_claimed_run_never_executes_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "financial").mkdir()
            with patch("backtest.baseline.load_inputs", side_effect=AssertionError("Must not load")):
                with self.assertRaises(FileExistsError):
                    run(root)

    def test_snapshot_and_artifacts_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "research-rules-v1.json"
            write_json(path, {"original": True})
            with self.assertRaises(FileExistsError):
                write_json(path, {})
            with patch("urllib.request.build_opener", side_effect=AssertionError("No network")):
                with self.assertRaises(FileExistsError):
                    capture_rules(root)
            self.assertEqual(path.read_text(), '{"original":true}\n')
