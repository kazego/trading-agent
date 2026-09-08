"""Offline clock and frozen-strategy equivalence; no trading entry point."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from pandas.testing import assert_frame_equal

import strategy
from backtest.clock import HistoricalClock
from backtest.data import DataValidationError, Source, STEP_MS, ingest
from backtest.strategy_adapter import FrozenStrategyAdapter

T = 1704067200000


class ClockStrategyTests(unittest.TestCase):
    def dataset(self, closes=None, *, symbol="BTCUSDT", warmup=99, changes=None):
        closes = closes if closes is not None else [100] * 110
        changes = changes or {}
        rows = []
        for index, close in enumerate(closes):
            values = dict(open="100", high=str(max(100, close)), low=str(min(100, close)),
                          close=str(close), volume="1")
            values.update(changes.get(index, {}))
            rows.append(",".join([str(T + index * STEP_MS),
                                  *[values[k] for k in ("open", "high", "low", "close", "volume")],
                                  str(T + (index + 1) * STEP_MS - 1), "0", "0", "0", "0", "0"]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.csv"
            path.write_text("\n".join(rows) + "\n", encoding="ascii")
            return ingest([Source(path, symbol, "synthetic-" + symbol)], symbols=(symbol,),
                          start_ms=T + warmup * STEP_MS, end_ms=T + len(closes) * STEP_MS,
                          as_of_ms=T + len(closes) * STEP_MS, timestamp_unit="ms",
                          warmup_candles=warmup)

    def test_clock_half_open_progression_and_replay(self):
        clock = HistoricalClock(self.dataset(), "BTCUSDT")
        events = list(clock)
        self.assertEqual(events, list(clock))
        self.assertEqual([e.open_time_ms for e in events],
                         [T + i * STEP_MS for i in range(99, 110)])
        for event in events:
            self.assertEqual(event.completed_through_ms, event.open_time_ms - 1)
            self.assertEqual(len(event.completed), 99)
            self.assertEqual(event.completed[-1].close_time_ms, event.completed_through_ms)

    def test_warmup_blocks_before_strategy_called(self):
        with patch.object(strategy, "get_signal") as call:
            for warmup in (0, 22, 98):
                with self.subTest(warmup=warmup), self.assertRaises(DataValidationError):
                    HistoricalClock(self.dataset(warmup=warmup), "BTCUSDT")
            call.assert_not_called()

    def test_extra_warmup_still_yields_99_history_rows(self):
        event = next(iter(HistoricalClock(self.dataset(warmup=105), "BTCUSDT")))
        self.assertEqual(event.completed[0].open_time_ms, T + 6 * STEP_MS)
        self.assertEqual(len(event.completed), 99)

    def test_clock_rejects_wrong_symbol_and_broken_dataset(self):
        dataset = self.dataset()
        with self.assertRaises(DataValidationError):
            HistoricalClock(dataset, "ETHUSDT")
        with self.assertRaises(DataValidationError):
            HistoricalClock(replace(dataset, candles=dataset.candles[1:]), "BTCUSDT")

    def test_equivalence_long_short_none_and_previous_equality(self):
        cases = [([100] * 97 + [90, 110, 100], "LONG"),
                 ([100] * 97 + [110, 90, 100], "SHORT"),
                 ([100] * 100, "NONE"),
                 ([100] * 98 + [110, 100], "LONG"),
                 ([100] * 98 + [90, 100], "SHORT")]
        for closes, expected in cases:
            with self.subTest(expected=expected, tail=closes[-3:]):
                event = next(iter(HistoricalClock(self.dataset(closes), "BTCUSDT")))
                direct = strategy.get_signal(pd.DataFrame({"close": [float(x) for x in closes]}))
                decision = FrozenStrategyAdapter().evaluate(event)
                self.assertEqual(decision.signal, expected)
                self.assertEqual(decision.signal, direct)

    def test_repeated_rolling_equivalence(self):
        closes = [100 + ((i * 17) % 31) - 15 for i in range(160)]
        adapter = FrozenStrategyAdapter()
        for index, event in enumerate(HistoricalClock(self.dataset(closes), "BTCUSDT"), 99):
            direct = pd.DataFrame({"close": [float(x) for x in closes[index-99:index]] + [100.0]})
            original = strategy.get_signal
            with patch.object(strategy, "get_signal", wraps=original) as call:
                decision = adapter.evaluate(event)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0]["close"].tolist(), direct["close"].tolist())
            self.assertEqual(decision.signal, strategy.get_signal(direct))

    def test_exact_frame_and_minus_three_minus_two_mapping(self):
        event = next(iter(HistoricalClock(self.dataset(), "BTCUSDT")))
        original = strategy.get_signal
        with patch.object(strategy, "get_signal", wraps=original) as call:
            FrozenStrategyAdapter().evaluate(event)
        frame = call.call_args.args[0]
        self.assertEqual(len(frame), 100)
        self.assertEqual(frame.iloc[-3].open_time_ms, event.open_time_ms - 2 * STEP_MS)
        self.assertEqual(frame.iloc[-2].open_time_ms, event.open_time_ms - STEP_MS)
        self.assertEqual(frame.iloc[-1].open_time_ms, event.open_time_ms)
        self.assertEqual(frame.iloc[-1].volume, 0)
        for column in ("open", "high", "low", "close"):
            self.assertEqual(frame.iloc[-1][column], 100.0)
            self.assertEqual(str(frame[column].dtype), "float64")

    def test_future_edits_do_not_change_prior_signal_or_visible_window(self):
        before = self.dataset()
        after = self.dataset(changes={100: dict(open="400", high="999", low="1", close="800")})
        adapter = FrozenStrategyAdapter()
        first = adapter.evaluate(next(iter(HistoricalClock(before, "BTCUSDT"))))
        second = adapter.evaluate(next(iter(HistoricalClock(after, "BTCUSDT"))))
        self.assertEqual(first.signal, second.signal)
        self.assertEqual(first.window_fingerprint, second.window_fingerprint)
        self.assertEqual(first.completed_open_times_ms, second.completed_open_times_ms)
        self.assertNotEqual(first.dataset_fingerprint, second.dataset_fingerprint)

    def test_forming_hlcv_invisible_until_completed(self):
        baseline = self.dataset()
        changed = self.dataset(changes={99: dict(high="1000", low="1", close="900", volume="99999")})
        first_events = list(HistoricalClock(baseline, "BTCUSDT"))
        next_events = list(HistoricalClock(changed, "BTCUSDT"))
        adapter = FrozenStrategyAdapter()
        a, b = adapter.evaluate(first_events[0]), adapter.evaluate(next_events[0])
        self.assertEqual(a.signal, b.signal)
        self.assertEqual(a.window_fingerprint, b.window_fingerprint)
        self.assertNotEqual(adapter.evaluate(first_events[1]).window_fingerprint,
                            adapter.evaluate(next_events[1]).window_fingerprint)

    def test_next_open_visible_only_at_next_step(self):
        events = iter(HistoricalClock(self.dataset(changes={100: dict(open="777", high="777")}), "BTCUSDT"))
        first = next(events)
        self.assertEqual(first.opening_price, Decimal("100"))
        self.assertTrue(all(c.open_time_ms < first.open_time_ms for c in first.completed))
        self.assertFalse(hasattr(first, "dataset"))
        self.assertFalse(hasattr(first, "high"))
        self.assertEqual(next(events).opening_price, Decimal("777"))

    def test_deterministic_decisions_and_independent_symbols(self):
        for symbol in ("BTCUSDT", "ETHUSDT", "ADAUSDT"):
            clock = HistoricalClock(self.dataset(symbol=symbol), symbol)
            first = [FrozenStrategyAdapter().evaluate(e) for e in clock]
            second = [FrozenStrategyAdapter().evaluate(e) for e in clock]
            self.assertEqual(first, second)
            self.assertEqual({d.symbol for d in first}, {symbol})
            self.assertEqual(len({d.decision_id for d in first}), len(first))
            self.assertTrue(all(d.signal == "NONE" for d in first))

    def test_changed_config_rejected_without_mutating_it(self):
        event = next(iter(HistoricalClock(self.dataset(), "BTCUSDT")))
        with patch.object(strategy.config, "EMA_FAST", 8):
            with self.assertRaises(DataValidationError):
                FrozenStrategyAdapter().evaluate(event)
            self.assertEqual(strategy.config.EMA_FAST, 8)

    def test_invalid_events_and_unknown_signal_fail_closed(self):
        event = next(iter(HistoricalClock(self.dataset(), "BTCUSDT")))
        for invalid in (replace(event, completed=event.completed[:-1]),
                        replace(event, completed_through_ms=event.open_time_ms),
                        replace(event, completed=tuple(reversed(event.completed)))):
            with self.assertRaises(DataValidationError):
                FrozenStrategyAdapter().evaluate(invalid)
        with patch.object(strategy, "get_signal", return_value="BUY"):
            with self.assertRaises(DataValidationError):
                FrozenStrategyAdapter().evaluate(event)

    def test_strategy_float_overflow_and_underflow_rejected(self):
        event = next(iter(HistoricalClock(self.dataset(), "BTCUSDT")))
        for price in (Decimal("1e400"), Decimal("1e-400"), Decimal("0")):
            with self.subTest(price=price), self.assertRaises(DataValidationError):
                FrozenStrategyAdapter().evaluate(replace(event, opening_price=price))

    def test_fresh_frame_each_evaluation_and_no_input_mutation(self):
        event = next(iter(HistoricalClock(self.dataset(), "BTCUSDT")))
        original = strategy.get_signal
        with patch.object(strategy, "get_signal", wraps=original) as call:
            first = FrozenStrategyAdapter().evaluate(event)
            second = FrozenStrategyAdapter().evaluate(event)
        left, right = [entry.args[0] for entry in call.call_args_list]
        self.assertIsNot(left, right)
        assert_frame_equal(left, right, check_exact=True)
        self.assertNotIn("ema_fast", left)
        self.assertEqual(first, second)
        self.assertTrue(all(isinstance(c.close, Decimal) for c in event.completed))


if __name__ == "__main__":
    unittest.main()
