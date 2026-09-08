"""Offline portfolio scheduling, shared equity and unchanged information boundaries."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
from itertools import permutations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backtest.clock import HistoricalClock
from backtest.data import DataValidationError, Source, STEP_MS, _encoded, _hash, ingest
from backtest.portfolio import Disposition, PRIORITY, simulate_portfolio
from backtest.simulation import CostMode, ExitReason, SimulationConfig, SymbolRules, simulate
from backtest.strategy_adapter import FrozenStrategyAdapter

T = 1704067200000
START = T + 99 * STEP_MS


def datasets(signals=None, bars=None, length=3):
    signals = signals or dict.fromkeys(PRIORITY, "LONG")
    bars = bars or {}
    result = {}
    with tempfile.TemporaryDirectory() as directory:
        for symbol in PRIORITY:
            signal = signals.get(symbol, "NONE")
            tail = [90, 110] if signal == "LONG" else [110, 90] if signal == "SHORT" else [100, 100]
            closes = [100] * 97 + tail
            values = [(100, max(100, c), min(100, c), c) for c in closes]
            values += bars.get(symbol, [(100, 101, 99, 100)] * length)
            rows = [",".join(map(str, [T + i * STEP_MS, *ohlc, 1, T + (i + 1) * STEP_MS - 1,
                                      0, 0, 0, 0, 0])) for i, ohlc in enumerate(values)]
            path = Path(directory) / (symbol + ".csv")
            path.write_text("\n".join(rows) + "\n", encoding="ascii")
            result[symbol] = ingest([Source(path, symbol, "portfolio-synthetic-" + symbol)],
                                    symbols=(symbol,), start_ms=START,
                                    end_ms=T + len(values) * STEP_MS, as_of_ms=T + len(values) * STEP_MS,
                                    timestamp_unit="ms", warmup_candles=99)
    return result


def rules(maximum="1"):
    return {s: SymbolRules(s, "synthetic-assumed-v1", D("0.01"), D("0.001"),
                           D("0.001"), D(maximum)) for s in PRIORITY}


def scripted(signals):
    """Isolate scheduling with explicit test-only signals; still construct real windows."""
    original = FrozenStrategyAdapter.evaluate
    def evaluate(adapter, event):
        decision = original(adapter, event)
        signal = signals.get(((event.open_time_ms - START) // STEP_MS, event.symbol), "NONE")
        return replace(decision, signal=signal, decision_id=_hash([decision.decision_id, signal]))
    return patch.object(FrozenStrategyAdapter, "evaluate", evaluate)


class PortfolioTests(unittest.TestCase):
    def test_all_long_btc_wins(self):
        result = simulate_portfolio(datasets(length=1), rules())
        first = result.boundaries[0]
        self.assertEqual(first.priority_winner, "BTCUSDT")
        self.assertEqual([r.disposition for r in first.decisions],
                         [Disposition.ACCEPTED, Disposition.BLOCKED, Disposition.BLOCKED])
        self.assertEqual([r.reason for r in first.decisions], [None, "BLOCKED_PRIORITY", "BLOCKED_PRIORITY"])
        self.assertEqual(len(result.trades), 1)

    def test_eth_beats_ada_and_ada_alone(self):
        for active, winner in (({"ETHUSDT": "LONG", "ADAUSDT": "LONG"}, "ETHUSDT"),
                               ({"ADAUSDT": "LONG"}, "ADAUSDT")):
            with self.subTest(winner=winner):
                result = simulate_portfolio(datasets(signals=active, length=1), rules())
                self.assertEqual(result.boundaries[0].priority_winner, winner)
                self.assertEqual(result.trades[0].position.decision.symbol, winner)

    def test_none_is_recorded_not_blocked(self):
        result = simulate_portfolio(datasets(signals={s: "NONE" for s in PRIORITY}), rules())
        self.assertEqual(result.trades, ())
        self.assertTrue(all(d.disposition == Disposition.NONE for b in result.boundaries for d in b.decisions))
        self.assertEqual(result.final_equity, D(1000))

    def test_all_input_orderings_identical(self):
        data, filters = datasets(), rules()
        baseline = simulate_portfolio(data, filters)
        for order in permutations(PRIORITY):
            with self.subTest(order=order):
                result = simulate_portfolio({s: data[s] for s in order},
                                            {s: filters[s] for s in reversed(order)})
                self.assertEqual(result, baseline)
                self.assertEqual(result.fingerprint, baseline.fingerprint)

    def test_held_position_blocks_all_actionable_symbols(self):
        with scripted({(i, s): "LONG" for i in range(3) for s in PRIORITY}):
            result = simulate_portfolio(datasets(), rules())
        self.assertEqual(len(result.trades), 1)
        for record in result.boundaries[1:]:
            self.assertTrue(all(d.reason == "BLOCKED_OPEN_POSITION" for d in record.decisions))
            self.assertIsNone(record.created)

    def test_intrabar_exit_cannot_enable_same_boundary_entry(self):
        data = datasets(bars={"BTCUSDT": [(100, 101, 99, 100), (100, 101, 98, 100), (100, 101, 99, 100)]})
        with scripted({(0, "BTCUSDT"): "LONG", (1, "ETHUSDT"): "LONG", (2, "ETHUSDT"): "LONG"}):
            result = simulate_portfolio(data, rules())
        middle = result.boundaries[1]
        self.assertEqual(middle.closed.reason, ExitReason.STOP_LOSS)
        self.assertEqual(middle.closed.exit.phase, "INTRABAR")
        self.assertIsNone(middle.created)
        self.assertEqual(middle.decisions[1].reason, "BLOCKED_OPEN_POSITION")
        self.assertEqual(result.boundaries[2].created.decision.symbol, "ETHUSDT")

    def test_opening_gap_exit_also_blocks_same_boundary(self):
        data = datasets(bars={"BTCUSDT": [(100, 101, 99, 100), (97, 98, 96, 97), (100, 101, 99, 100)]})
        with scripted({(0, "BTCUSDT"): "LONG", (1, "ETHUSDT"): "LONG", (2, "ADAUSDT"): "LONG"}):
            result = simulate_portfolio(data, rules())
        self.assertEqual(result.boundaries[1].closed.exit.phase, "OPEN")
        self.assertIsNone(result.boundaries[1].created)
        self.assertEqual(result.boundaries[1].decisions[1].reason, "BLOCKED_OPEN_POSITION")
        self.assertEqual(result.boundaries[2].priority_winner, "ADAUSDT")

    def test_entry_candle_exit_cannot_rearbitrate(self):
        data = datasets(length=2, bars={"BTCUSDT": [(100, 104, 98, 100)] * 2})
        with scripted({(i, s): "LONG" for i in range(2) for s in PRIORITY}):
            result = simulate_portfolio(data, rules())
        self.assertEqual(len(result.trades), 2)
        for boundary in result.boundaries:
            self.assertEqual(boundary.closed.reason, ExitReason.STOP_LOSS)
            self.assertEqual(sum(d.disposition == Disposition.ACCEPTED for d in boundary.decisions), 1)
            self.assertTrue(all(d.reason == "BLOCKED_PRIORITY" for d in boundary.decisions[1:]))

    def test_shared_equity_winner_and_loser_change_next_size(self):
        cases = [((100, 103, 99, 100), "1019.998", "6.799"),
                 ((100, 101, 98.5, 100), "990.001", "6.600")]
        for first_bar, equity, quantity in cases:
            with self.subTest(equity=equity):
                data = datasets(length=2, bars={"BTCUSDT": [first_bar, (100, 101, 99, 100)]})
                with scripted({(0, "BTCUSDT"): "LONG", (1, "ETHUSDT"): "LONG"}):
                    result = simulate_portfolio(data, rules("100"))
                first, second = result.boundaries
                self.assertEqual(first.equity_after, D(equity))
                self.assertEqual(second.equity_before, D(equity))
                self.assertEqual(second.created.equity_before, D(equity))
                self.assertEqual(second.created.entry.quantity, D(quantity))
                self.assertLessEqual(second.created.planned_stop_risk, D(equity) * D("0.01"))

    def test_insolvency_terminal_no_later_entry(self):
        data = datasets(bars={"BTCUSDT": [(100, 101, 99, 100), (2000, 2001, 1999, 2000), (100, 101, 99, 100)]})
        with scripted({(0, "BTCUSDT"): "SHORT", (1, "ETHUSDT"): "LONG", (2, "ETHUSDT"): "LONG"}):
            result = simulate_portfolio(data, rules())
        self.assertEqual(result.terminal_state, "INSOLVENT")
        self.assertEqual(result.final_equity, D(-900))
        self.assertEqual(len(result.boundaries), 2)
        self.assertEqual(result.boundaries[-1].decisions[1].reason, "BLOCKED_OPEN_POSITION")

    def test_missing_extra_symbols_and_rules_fail_before_evaluation(self):
        data, filters = datasets(), rules()
        for bad_data, bad_rules in (({s: data[s] for s in PRIORITY[:2]}, filters),
                                    (data, {s: filters[s] for s in PRIORITY[:2]}),
                                    (dict(data, DOGEUSDT=data["BTCUSDT"]), filters),
                                    (data, dict(filters, BTCUSDT=filters["ETHUSDT"]))):
            with self.subTest(keys=list(bad_data)), patch.object(FrozenStrategyAdapter, "evaluate") as call:
                with self.assertRaises(DataValidationError):
                    simulate_portfolio(bad_data, bad_rules)
                call.assert_not_called()

    def test_gaps_misalignment_timeframe_and_warmup_fail_closed(self):
        base = datasets()
        original = base["ADAUSDT"]
        variants = [replace(original, candles=original.candles[:100] + original.candles[101:])]
        for changes in ({"start_ms": START + STEP_MS}, {"end_ms": START + STEP_MS},
                        {"timeframe": "1h"}, {"warmup_candles": 98}):
            metadata = original.provenance | changes
            variants.append(replace(original, provenance_json=_encoded(metadata).decode("ascii")))
        for variant in variants:
            with self.subTest(provenance=variant.provenance), self.assertRaises(DataValidationError):
                simulate_portfolio(dict(base, ADAUSDT=variant), rules())

    def test_end_of_data_net_equity_once_and_matches_5d(self):
        data = datasets(signals={"BTCUSDT": "LONG"}, length=2,
                        bars={"BTCUSDT": [(100, 101, 99, 100), (100, 101, 99, 101)]})
        config = SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE, entry_fee_rate=D("0.0005"),
                                  exit_fee_rate=D("0.0005"), slippage_bps=D(10))
        result = simulate_portfolio(data, rules(), config)
        single = simulate(data["BTCUSDT"], "BTCUSDT", rules()["BTCUSDT"], config)
        self.assertEqual(result.trades, single.trades)
        self.assertEqual(result.final_equity, single.final_equity)
        self.assertEqual(result.final_equity, D(1000) + result.trades[0].net_pnl)
        self.assertEqual(result.boundaries[0].equity_after, D(1000))
        self.assertEqual(result.trades[0].reason, ExitReason.END_OF_DATA)
        self.assertEqual(result.boundaries[-1].state_after, "FLAT")

    def test_rejected_higher_priority_then_tradeable_candidate(self):
        filters = rules()
        filters["BTCUSDT"] = replace(filters["BTCUSDT"], min_notional=D(1000))
        result = simulate_portfolio(datasets(length=1), filters)
        first = result.boundaries[0]
        self.assertEqual(first.decisions[0].disposition, Disposition.REJECTED)
        self.assertEqual(first.priority_winner, "ETHUSDT")
        self.assertEqual(first.decisions[2].reason, "BLOCKED_PRIORITY")

    def test_all_rejected_recorded(self):
        filters = {s: replace(r, min_notional=D(1000)) for s, r in rules().items()}
        result = simulate_portfolio(datasets(length=1), filters)
        self.assertTrue(all(d.disposition == Disposition.REJECTED for d in result.boundaries[0].decisions))
        self.assertEqual(result.trades, ())
        self.assertIsNone(result.boundaries[0].priority_winner)

    def test_invariant_and_complete_records(self):
        with scripted({(i, s): "LONG" for i in range(3) for s in PRIORITY}):
            result = simulate_portfolio(datasets(), rules())
        active = None
        for record in result.boundaries:
            self.assertEqual(record.position_symbol_before, active)
            self.assertEqual(tuple(d.decision.symbol for d in record.decisions), PRIORITY)
            if record.created is not None:
                self.assertIsNone(active)
                active = record.created.decision.symbol
            if record.closed is not None:
                self.assertEqual(active, record.closed.position.decision.symbol)
                active = None
            self.assertEqual(record.position_symbol_after, active)
            self.assertEqual(record.equity_after, record.equity_before + (record.closed.net_pnl if record.closed else D(0)))
        self.assertIsNone(active)

    def test_determinism_and_no_source_mutation(self):
        data, filters = datasets(), rules()
        before = {s: (d.candles, d.provenance_json) for s, d in data.items()}
        first = simulate_portfolio(data, filters)
        with localcontext() as context:
            context.prec = 6
            second = simulate_portfolio(data, filters)
        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(before, {s: (d.candles, d.provenance_json) for s, d in data.items()})

    def test_post_decision_ohlc_changes_outcome_not_opening_arbitration(self):
        a = simulate_portfolio(datasets(length=1, bars={"BTCUSDT": [(100, 101, 98, 99)]}), rules())
        b = simulate_portfolio(datasets(length=1, bars={"BTCUSDT": [(100, 104, 99, 103)]}), rules())
        for left, right in zip(a.boundaries[0].decisions, b.boundaries[0].decisions):
            self.assertEqual(left.decision.signal, right.decision.signal)
            self.assertEqual(left.decision.window_fingerprint, right.decision.window_fingerprint)
            self.assertEqual(left.disposition, right.disposition)
        self.assertEqual(a.boundaries[0].priority_winner, b.boundaries[0].priority_winner)
        self.assertNotEqual(a.trades[0].reason, b.trades[0].reason)

    def test_future_edits_leave_earlier_visible_decisions_unchanged(self):
        before = datasets()
        after = datasets(bars={"ETHUSDT": [(100, 101, 99, 100), (100, 101, 99, 100), (100, 900, 1, 800)]})
        left = simulate_portfolio(before, rules())
        right = simulate_portfolio(after, rules())
        for a, b in zip(left.boundaries[:2], right.boundaries[:2]):
            for x, y in zip(a.decisions, b.decisions):
                self.assertEqual(x.decision.signal, y.decision.signal)
                self.assertEqual(x.decision.window_fingerprint, y.decision.window_fingerprint)
                self.assertEqual(x.disposition, y.disposition)
        # Real (unpatched) Phase 5C decisions match exactly for each dataset.
        for symbol in PRIORITY:
            direct = tuple(FrozenStrategyAdapter().evaluate(e) for e in HistoricalClock(before[symbol], symbol))
            actual = tuple(next(r.decision for r in b.decisions if r.decision.symbol == symbol) for b in left.boundaries)
            self.assertEqual(direct, actual)

    def test_selected_symbol_uses_own_open_and_rules(self):
        data = datasets(length=1, bars={"ETHUSDT": [(200, 201, 199, 200)]})
        filters = rules()
        filters["BTCUSDT"] = replace(filters["BTCUSDT"], min_notional=D(1000))
        filters["ETHUSDT"] = replace(filters["ETHUSDT"], tick_size=D("0.5"), max_quantity=D(2))
        result = simulate_portfolio(data, filters)
        position = result.boundaries[0].created
        self.assertEqual(position.decision.symbol, "ETHUSDT")
        self.assertEqual(position.entry.price, D(200))
        self.assertEqual(position.entry.quantity, D(2))
        self.assertEqual(position.stop_loss, D(197))
        self.assertEqual(position.take_profit, D(206))

    def test_all_opening_decisions_precede_intrabar_processing(self):
        import backtest.portfolio as portfolio
        original_evaluate = FrozenStrategyAdapter.evaluate
        original_process = portfolio.process_bar
        calls = []
        def evaluate(adapter, event):
            calls.append((event.open_time_ms, "decision", event.symbol))
            return original_evaluate(adapter, event)
        def process(position, candle, config):
            calls.append((candle.open_time_ms, "exit", candle.symbol))
            return original_process(position, candle, config)
        with patch.object(FrozenStrategyAdapter, "evaluate", evaluate), patch.object(portfolio, "process_bar", process):
            simulate_portfolio(datasets(), rules())
        for timestamp in (START, START + STEP_MS, START + 2 * STEP_MS):
            current = [entry for entry in calls if entry[0] == timestamp]
            self.assertEqual([entry[1] for entry in current[:3]], ["decision"] * 3)
            self.assertEqual([entry[2] for entry in current[:3]], list(PRIORITY))
            self.assertEqual(current[3][1], "exit")


if __name__ == "__main__":
    unittest.main()
