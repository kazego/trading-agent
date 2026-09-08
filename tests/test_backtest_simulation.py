"""Hand-calculated execution contracts and offline end-to-end boundary checks."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backtest.clock import HistoricalClock
from backtest.data import Candle, DataValidationError, Source, STEP_MS, ingest
from backtest.simulation import (CostMode, ExitReason, Fill, SimulationConfig, SymbolRules,
                                 SizingError, close_position, open_position, process_bar,
                                 protections, simulate, size_quantity, slipped_price)
from backtest.strategy_adapter import FrozenStrategyAdapter

T = 1704067200000


def dataset(signal="LONG", bars=None):
    closes = [100] * 97 + ([90, 110] if signal == "LONG" else [110, 90])
    historical = [(100, max(100, c), min(100, c), c) for c in closes]
    rows = []
    for i, (opening, high, low, close) in enumerate(historical + (bars or [(100, 101, 99, 100)])):
        rows.append(",".join(map(str, [T + i * STEP_MS, opening, high, low, close, 1,
                                      T + (i + 1) * STEP_MS - 1, 0, 0, 0, 0, 0])))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.csv"
        path.write_text("\n".join(rows) + "\n", encoding="ascii")
        return ingest([Source(path, "BTCUSDT", "execution-fixture")], symbols=("BTCUSDT",),
                      start_ms=T + 99 * STEP_MS, end_ms=T + len(rows) * STEP_MS,
                      as_of_ms=T + len(rows) * STEP_MS, timestamp_unit="ms", warmup_candles=99)


def rules(**changes):
    return replace(SymbolRules("BTCUSDT", "synthetic-assumed-v1", D("0.01"), D("0.001"),
                               D("0.001"), D("1")), **changes)


class ExecutionTests(unittest.TestCase):
    def position(self, signal="LONG", config=SimulationConfig()):
        event = next(iter(HistoricalClock(dataset(signal), "BTCUSDT")))
        decision = FrozenStrategyAdapter().evaluate(event)
        self.assertEqual(decision.signal, signal)
        return open_position(decision, event, D("1000"), rules(), config)

    def bar(self, opening="100", high="101", low="99", close="100", offset=0):
        timestamp = T + (99 + offset) * STEP_MS
        return Candle("BTCUSDT", "15m", timestamp, timestamp + STEP_MS - 1,
                      opening, high, low, close, "1")

    def test_long_and_short_exact_stop_target_results(self):
        cases = [("LONG", "98.5", "101", "98.5", "100", ExitReason.STOP_LOSS, "-1.5"),
                 ("LONG", "100", "103", "99", "100", ExitReason.TAKE_PROFIT, "3"),
                 ("SHORT", "100", "101.5", "99", "100", ExitReason.STOP_LOSS, "-1.5"),
                 ("SHORT", "100", "101", "97", "100", ExitReason.TAKE_PROFIT, "3")]
        for signal, opening, high, low, close, reason, pnl in cases:
            with self.subTest(signal=signal, reason=reason):
                position = self.position(signal)
                trade = process_bar(position, self.bar(opening, high, low, close), SimulationConfig())
                self.assertEqual(position.entry.price, D(100))
                self.assertEqual(position.entry.quantity, D(1))
                self.assertEqual(trade.reason, reason)
                self.assertEqual(trade.gross_pnl, D(pnl))
                self.assertEqual(trade.net_pnl, D(pnl))

    def test_dual_touch_stop_first_both_sides(self):
        for signal in ("LONG", "SHORT"):
            trade = process_bar(self.position(signal), self.bar(high="104", low="96"), SimulationConfig())
            self.assertEqual(trade.reason, ExitReason.STOP_LOSS)
            self.assertTrue(trade.dual_touch)
            self.assertEqual(trade.gross_pnl, D("-1.5"))

    def test_stop_gaps_use_worse_open_both_sides(self):
        for signal, opening, high, low, expected in (("LONG", "97", "100", "96", "-3"),
                                                    ("SHORT", "103", "104", "100", "-3")):
            trade = process_bar(self.position(signal), self.bar(opening, high, low, opening, 1), SimulationConfig())
            self.assertEqual(trade.exit.reference_price, D(opening))
            self.assertEqual(trade.gross_pnl, D(expected))
            self.assertEqual(trade.exit.phase, "OPEN")

    def test_target_gap_no_improvement_and_opening_order_wins(self):
        for signal, opening, target in (("LONG", "105", "103"), ("SHORT", "95", "97")):
            trade = process_bar(self.position(signal), self.bar(opening, "106", "94", "100", 1), SimulationConfig())
            self.assertEqual(trade.reason, ExitReason.TAKE_PROFIT)
            self.assertEqual(trade.exit.reference_price, D(target))
            self.assertFalse(trade.dual_touch)

    def test_fee_example(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_ONLY, entry_fee_rate=D("0.0005"), exit_fee_rate=D("0.0005"))
        trade = process_bar(self.position(config=config), self.bar(high="103"), config)
        self.assertEqual(trade.position.entry.fee, D("0.05"))
        self.assertEqual(trade.exit.fee, D("0.0515"))
        self.assertEqual(trade.gross_pnl, D(3))
        self.assertEqual(trade.net_pnl, D("2.8985"))

    def test_cost_modes_and_adverse_buy_sell(self):
        for mode, expected_price, expected_fee in ((CostMode.GROSS, "100", "0"),
                                                  (CostMode.FEES_ONLY, "100", "0.1"),
                                                  (CostMode.FEES_AND_SLIPPAGE, "100.1", "0.1001")):
            config = SimulationConfig(cost_mode=mode, entry_fee_rate=D("0.001"), slippage_bps=D(10))
            position = self.position(config=config)
            self.assertEqual(position.entry.price, D(expected_price))
            self.assertEqual(position.entry.fee, D(expected_fee))
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, slippage_bps=D(10))
        self.assertEqual(slipped_price(D(100), "SELL", config), D("99.9"))
        self.assertEqual(slipped_price(D(100), "BUY", config), D("100.1"))

    def test_protections_from_slipped_entry_and_away_rounding(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, slippage_bps=D(10))
        long, short = self.position(config=config), self.position("SHORT", config)
        self.assertEqual((long.stop_loss, long.take_profit), (D("98.59"), D("103.11")))
        self.assertEqual((short.stop_loss, short.take_profit), (D("101.40"), D("96.90")))

    def test_slippage_all_exit_reasons_and_sides(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, slippage_bps=D(10))
        for signal, expected in (("LONG", "102.897"), ("SHORT", "103.103")):
            for reason in ExitReason:
                trade = close_position(self.position(signal, config), D(103), T + 100 * STEP_MS,
                                       "CLOSE", reason, False, config)
                self.assertEqual(trade.exit.price, D(expected))

    def test_gap_reference_receives_slippage(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, slippage_bps=D(10))
        trade = process_bar(self.position(config=config), self.bar("97", "100", "96", "97", 1), config)
        self.assertEqual(trade.exit.reference_price, D(97))
        self.assertEqual(trade.exit.price, D("96.903"))

    def test_baseline_size_hand_calculated(self):
        quantity, risk = size_quantity(D(1000), D(100), D("98.5"), "LONG", rules(max_quantity=D(100)), SimulationConfig())
        self.assertEqual(quantity, D("6.666"))
        self.assertEqual(risk, D("9.999"))

    def test_size_includes_costs_and_rounded_stop(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE,
                                  entry_fee_rate=D("0.0005"), exit_fee_rate=D("0.0005"), slippage_bps=D(10))
        entry = D("100.1")
        stop, _ = protections(entry, "LONG", rules(), config)
        quantity, risk = size_quantity(D(1000), entry, stop, "LONG", rules(max_quantity=D(100)), config)
        stop_fill = stop * D("0.999")
        per_unit = entry - stop_fill + entry * D("0.0005") + stop_fill * D("0.0005")
        self.assertEqual(risk, quantity * per_unit)
        self.assertLessEqual(risk, D(10))
        self.assertGreater((quantity + D("0.001")) * per_unit, D(10))

    def test_leverage_margin_cap_and_entry_fee_reserve(self):
        config = SimulationConfig(risk_fraction=D(1), cost_mode=CostMode.FEES_ONLY, entry_fee_rate=D("0.01"))
        quantity, _ = size_quantity(D(1000), D(100), D("98.5"), "LONG", rules(max_quantity=D(100)), config)
        self.assertEqual(quantity, D("29.126"))
        self.assertLessEqual(quantity * D(100) / D(3) + quantity * D(100) * D("0.01"), D(1000))
        quantity, _ = size_quantity(D(1000), D(100), D("98.5"), "LONG", rules(max_quantity=D(100)), SimulationConfig(risk_fraction=D(1)))
        self.assertEqual(quantity, D(30))

    def test_max_quantity_and_lot_rounding(self):
        quantity, _ = size_quantity(D(1000), D(100), D("98.5"), "LONG",
                                    rules(max_quantity=D("1.2345")), SimulationConfig())
        self.assertEqual(quantity, D("1.234"))

    def test_min_quantity_notional_insufficient_margin_and_equity(self):
        for equity, filters in ((D(1000), rules(min_quantity=D(2), max_quantity=D(2), min_notional=D(1000))),
                                (D("0.01"), rules(min_quantity=D(1))),
                                (D(1000), rules(min_quantity=D(10), max_quantity=D(20))),
                                (D(0), rules()), (D(-1), rules())):
            with self.subTest(equity=equity, filters=filters), self.assertRaises(SizingError):
                size_quantity(equity, D(100), D("98.5"), "LONG", filters, SimulationConfig())

    def test_invalid_rules_config_and_financial_floats(self):
        for change in (dict(tick_size=D(0)), dict(quantity_step=D(-1)), dict(min_quantity=D(0)),
                       dict(max_quantity=D("0.0001")), dict(version=""), dict(min_notional=D(-1)), dict(tick_size=0.01)):
            with self.subTest(change=change), self.assertRaises(DataValidationError):
                rules(**change)
        for change in (dict(starting_equity=D(0)), dict(leverage=D(4)), dict(risk_fraction=D(0)),
                       dict(slippage_bps=D(-1)), dict(slippage_bps=D(10000)),
                       dict(entry_fee_rate=D(-1)), dict(exit_fee_rate=D(1)), dict(starting_equity=1000.0)):
            with self.subTest(change=change), self.assertRaises(DataValidationError):
                SimulationConfig(**change)
        with self.assertRaises(DataValidationError):
            Fill("BUY", T, "OPEN", 100.0, D(100), D(1), D(0))

    def test_impossible_protection_tick_rejected(self):
        with self.assertRaises(SizingError):
            protections(D(100), "LONG", rules(tick_size=D(200)), SimulationConfig())

    def test_end_of_data_with_costs_and_equity(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, entry_fee_rate=D("0.0005"),
                                  exit_fee_rate=D("0.0005"), slippage_bps=D(10))
        result = simulate(dataset(bars=[(100, 101, 99, 101)]), "BTCUSDT", rules(), config)
        trade = result.trades[0]
        self.assertEqual(trade.reason, ExitReason.END_OF_DATA)
        self.assertEqual(trade.exit.price, D("100.899"))
        self.assertEqual(result.final_equity, D(1000) + trade.net_pnl)
        self.assertIn("funding and liquidation excluded", result.limitations)

    def test_entry_candle_exit_and_no_intrabar_reentry(self):
        result = simulate(dataset(bars=[(100, 104, 98, 100)]), "BTCUSDT", rules())
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].reason, ExitReason.STOP_LOSS)
        self.assertTrue(result.trades[0].dual_touch)
        self.assertEqual(result.trades[0].exit.candle_open_time_ms, result.trades[0].position.entry.candle_open_time_ms)

    def test_held_position_gap_exit_blocks_same_boundary_reentry(self):
        data = dataset(bars=[(100, 101, 99, 100), (97, 98, 96, 97)])
        original = FrozenStrategyAdapter.evaluate
        def always_long(adapter, event):
            return replace(original(adapter, event), signal="LONG")
        with patch.object(FrozenStrategyAdapter, "evaluate", always_long):
            result = simulate(data, "BTCUSDT", rules())
        self.assertEqual(len(result.decisions), 2)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit.reference_price, D(97))

    def test_none_and_rejections(self):
        data = dataset()
        original = FrozenStrategyAdapter.evaluate
        def no_signal(adapter, event):
            return replace(original(adapter, event), signal="NONE")
        with patch.object(FrozenStrategyAdapter, "evaluate", no_signal):
            result = simulate(data, "BTCUSDT", rules())
        self.assertEqual(result.trades, ())
        self.assertEqual(result.final_equity, D(1000))
        result = simulate(data, "BTCUSDT", rules(min_notional=D(1000)))
        self.assertEqual(len(result.rejections), 1)
        self.assertEqual(result.trades, ())

    def test_deterministic_replay_independent_decimal_context(self):
        data = dataset()
        first = simulate(data, "BTCUSDT", rules())
        with localcontext() as context:
            context.prec = 6
            context.rounding = "ROUND_DOWN"
            second = simulate(data, "BTCUSDT", rules())
        self.assertEqual(first, second)

    def test_lookahead_outcome_changes_without_decision_change(self):
        stopped = dataset(bars=[(100, 101, 98, 99)])
        target = dataset(bars=[(100, 104, 99, 103)])
        a = simulate(stopped, "BTCUSDT", rules())
        b = simulate(target, "BTCUSDT", rules())
        self.assertEqual(a.decisions[0].signal, b.decisions[0].signal)
        self.assertEqual(a.decisions[0].window_fingerprint, b.decisions[0].window_fingerprint)
        self.assertEqual(a.trades[0].position.entry, b.trades[0].position.entry)
        self.assertNotEqual(a.trades[0].reason, b.trades[0].reason)
        direct = FrozenStrategyAdapter().evaluate(next(iter(HistoricalClock(stopped, "BTCUSDT"))))
        self.assertEqual(a.decisions[0], direct)

    def test_minimum_notional_equality_and_margin_rejection(self):
        quantity, _ = size_quantity(D(1000), D(100), D("98.5"), "LONG",
                                    rules(min_notional=D(100)), SimulationConfig())
        self.assertEqual(quantity, D(1))
        with self.assertRaises(SizingError):
            size_quantity(D(30), D(100), D("98.5"), "LONG", rules(min_quantity=D(1)),
                          SimulationConfig(risk_fraction=D(1)))

    def test_insolvency_from_short_gap_stops_replay(self):
        data = dataset("SHORT", bars=[(100, 101, 99, 100), (2000, 2001, 1999, 2000),
                                       (2000, 2001, 1999, 2000)])
        result = simulate(data, "BTCUSDT", rules())
        self.assertTrue(result.insolvent)
        self.assertEqual(result.final_equity, D(-900))
        self.assertEqual(len(result.decisions), 2)
        self.assertEqual(result.trades[0].gross_pnl, D(-1900))

    def test_heavily_slipped_entry_immediate_adverse_open(self):
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, slippage_bps=D(200))
        position = self.position(config=config)
        trade = process_bar(position, self.bar(), config)
        self.assertEqual(trade.reason, ExitReason.STOP_LOSS)
        self.assertEqual(trade.exit.reference_price, D(100))
        self.assertEqual(trade.exit.price, D(98))

    def test_execution_does_not_mutate_dataset_or_clock_decisions(self):
        data = dataset(bars=[(100, 101, 99, 100), (100, 104, 99, 103)])
        original_candles, provenance = data.candles, data.provenance_json
        expected = tuple(FrozenStrategyAdapter().evaluate(event) for event in HistoricalClock(data, "BTCUSDT"))
        result = simulate(data, "BTCUSDT", rules())
        self.assertEqual(result.decisions, expected)
        self.assertEqual(data.candles, original_candles)
        self.assertEqual(data.provenance_json, provenance)

    def test_wrong_rules_and_event_identity_rejected(self):
        data = dataset()
        with self.assertRaises(DataValidationError):
            simulate(data, "BTCUSDT", rules(symbol="ETHUSDT"))
        event = next(iter(HistoricalClock(data, "BTCUSDT")))
        decision = FrozenStrategyAdapter().evaluate(event)
        with self.assertRaises(DataValidationError):
            open_position(replace(decision, open_time_ms=event.open_time_ms + STEP_MS), event,
                          D(1000), rules(), SimulationConfig())


if __name__ == "__main__":
    unittest.main()
