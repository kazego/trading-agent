"""Offline regression tests for the original EMA strategy; no Binance imports."""

import unittest
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal

import config
import strategy


def candles(closes):
    """Synthetic oldest-first candles, including the final forming candle."""
    return pd.DataFrame({"close": closes})


class StrategyRegressionTests(unittest.TestCase):
    def test_baseline_parameters(self):
        self.assertEqual(config.EMA_FAST, 9)
        self.assertEqual(config.EMA_SLOW, 21)

    def test_indicator_golden_values_freeze_spans_and_adjust_false(self):
        # Hand-calculated recursive EMA values, independent of pandas.ewm.
        result = strategy.add_indicators(candles([100.0, 110.0, 90.0, 120.0]))
        expected_fast = [100.0, 102.0, 99.6, 103.68]
        expected_slow = [100.0, 1110 / 11, 12090 / 121, 135420 / 1331]
        for column, expected in (("ema_fast", expected_fast), ("ema_slow", expected_slow)):
            with self.subTest(column=column):
                for actual, value in zip(result[column], expected):
                    self.assertAlmostEqual(actual, value, places=12)

    def test_long_from_strict_crossover(self):
        self.assertEqual(strategy.get_signal(candles([100.0] * 97 + [90, 110, 100])), "LONG")

    def test_short_from_strict_crossover(self):
        self.assertEqual(strategy.get_signal(candles([100.0] * 97 + [110, 90, 100])), "SHORT")

    def test_none_without_a_crossover(self):
        for closes in ([100.0] * 100, list(range(1, 101)), list(range(100, 0, -1))):
            with self.subTest(closes=closes[-3:]):
                self.assertEqual(strategy.get_signal(candles(closes)), "NONE")

    def test_insufficient_history_even_when_latest_closed_price_crosses(self):
        for length in range(23):
            closes = [100.0] * length
            if length >= 3:
                closes[-2] = 110.0
            with self.subTest(length=length):
                self.assertEqual(strategy.get_signal(candles(closes)), "NONE")

    def test_exact_minimum_history_is_23_rows(self):
        for close, expected in ((110.0, "LONG"), (90.0, "SHORT")):
            with self.subTest(expected=expected):
                self.assertEqual(strategy.get_signal(candles([100.0] * 21 + [close, 100])), expected)

    def test_previous_equality_allows_long_and_short(self):
        for close, expected in ((110.0, "LONG"), (90.0, "SHORT")):
            with self.subTest(expected=expected):
                self.assertEqual(strategy.get_signal(candles([100.0] * 98 + [close, 100])), expected)

    def test_exact_comparison_boundaries(self):
        # Isolate comparisons from floating-point EMA generation. End-to-end
        # crossover tests above still exercise the real indicator calculation.
        cases = [
            (99.0, 101.0, "LONG"),
            (100.0, 101.0, "LONG"),
            (101.0, 99.0, "SHORT"),
            (100.0, 99.0, "SHORT"),
            (99.0, 100.0, "NONE"),
            (101.0, 100.0, "NONE"),
            (100.0, 100.0, "NONE"),
            (101.0, 102.0, "NONE"),
            (99.0, 98.0, "NONE"),
        ]
        for previous_fast, current_fast, expected in cases:
            indicated = candles([100.0] * 100)
            indicated["ema_slow"] = 100.0
            indicated["ema_fast"] = 100.0
            indicated.loc[97, "ema_fast"] = previous_fast
            indicated.loc[98, "ema_fast"] = current_fast
            with self.subTest(previous=previous_fast, current=current_fast):
                with patch.object(strategy, "add_indicators", return_value=indicated):
                    self.assertEqual(strategy.get_signal(candles([100.0] * 100)), expected)

    def test_forming_candle_crossover_is_ignored(self):
        for forming_close in (1.0, 10000.0):
            with self.subTest(forming_close=forming_close):
                self.assertEqual(strategy.get_signal(candles([100.0] * 99 + [forming_close])), "NONE")

    def test_forming_candle_does_not_change_closed_candle_signal(self):
        for previous, current, expected in ((90, 110, "LONG"), (110, 90, "SHORT")):
            for forming_close in (1.0, 100.0, 10000.0):
                with self.subTest(expected=expected, forming_close=forming_close):
                    frame = candles([100.0] * 97 + [previous, current, forming_close])
                    self.assertEqual(strategy.get_signal(frame), expected)

    def test_crossover_one_closed_candle_earlier_is_not_repeated(self):
        for previous, current in ((90, 110), (110, 90)):
            with self.subTest(current=current):
                frame = candles([100.0] * 96 + [previous, current, current, 100])
                self.assertEqual(strategy.get_signal(frame), "NONE")

    def test_candle_selection_is_positional_not_index_label_based(self):
        frame = candles([100.0] * 97 + [90, 110, 100])
        frame.index = pd.date_range("2026-01-01", periods=100, freq="15min", tz="UTC")
        self.assertEqual(strategy.get_signal(frame), "LONG")

    def test_get_signal_does_not_mutate_input(self):
        for closes in ([100.0] * 97 + [90, 110, 100], [100.0] * 97 + [110, 90, 100],
                       [100.0] * 100, [100.0] * 22):
            for existing_indicators in (False, True):
                frame = candles(closes)
                frame["volume"] = 7
                frame.index = pd.Index(range(1000, 1000 + len(frame)), name="candle")
                frame.attrs["source"] = "regression-fixture"
                if existing_indicators:
                    frame["ema_fast"] = -1.0
                    frame["ema_slow"] = -2.0
                before = frame.copy(deep=True)
                with self.subTest(length=len(frame), existing_indicators=existing_indicators):
                    strategy.get_signal(frame)
                    assert_frame_equal(frame, before, check_exact=True)
                    self.assertEqual(frame.attrs, before.attrs)

    def test_add_indicators_returns_copy_and_preserves_original_columns(self):
        frame = pd.DataFrame({"close": [100.0, 110.0], "volume": [2, 3]})
        before = frame.copy(deep=True)
        result = strategy.add_indicators(frame)
        self.assertIsNot(result, frame)
        assert_frame_equal(frame, before, check_exact=True)
        assert_frame_equal(result[before.columns], before, check_exact=True)
        self.assertEqual(list(result.columns), ["close", "volume", "ema_fast", "ema_slow"])


if __name__ == "__main__":
    unittest.main()
