"""Manual analytics examples plus read-only reporting of real synthetic runs."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from backtest.data import DataValidationError, STEP_MS
from backtest.metrics import analyze, drawdown, trade_metrics
from backtest.portfolio import PRIORITY, simulate_portfolio
from backtest.report import build_report
from backtest.simulation import CostMode, SimulationConfig
from backtest.strategy_adapter import FrozenStrategyAdapter
from test_backtest_portfolio import datasets, rules, scripted, START


def manual_trades(gross, fee="0"):
    fee = D(fee)
    return tuple(SimpleNamespace(gross_pnl=D(pnl), net_pnl=D(pnl) - fee * 2,
                                 position=SimpleNamespace(entry=SimpleNamespace(fee=fee)),
                                 exit=SimpleNamespace(fee=fee)) for pnl in gross)


class MetricsReportTests(unittest.TestCase):
    def run_data(self, **kwargs):
        data = datasets(**kwargs)
        return data, simulate_portfolio(data, rules())

    def test_manual_counts_quality_and_streaks(self):
        result = trade_metrics(manual_trades([10, 20, -5, -10, 0, 15]))
        self.assertEqual((result["total_trades"], result["winning_trades"], result["losing_trades"],
                          result["breakeven_trades"]), (6, 3, 2, 1))
        self.assertEqual(result["win_rate_pct"], D(50))
        self.assertEqual(result["net_pnl"], D(30))
        self.assertEqual(result["profit_factor"], {"value": D(3), "status": "DEFINED"})
        self.assertEqual(result["expectancy_per_trade"], D(5))
        self.assertEqual(result["average_winner"], D(15))
        self.assertEqual(result["average_loser_magnitude"], D("7.5"))
        self.assertEqual(result["realized_win_loss_ratio"]["value"], D(2))
        self.assertEqual(result["maximum_consecutive_wins"], 2)
        self.assertEqual(result["maximum_consecutive_losses"], 2)
        self.assertEqual((result["best_trade"], result["worst_trade"]), (D(20), D(-10)))

    def test_breakeven_resets_streaks(self):
        result = trade_metrics(manual_trades([5, 0, 5, -1, 0, -1]))
        self.assertEqual(result["maximum_consecutive_wins"], 1)
        self.assertEqual(result["maximum_consecutive_losses"], 1)

    def test_profit_factor_uses_gross_while_expectancy_uses_net(self):
        result = trade_metrics(manual_trades([10, -5], fee="1"))
        self.assertEqual(result["profit_factor"]["value"], D(2))
        self.assertEqual(result["expectancy_per_trade"], D("0.5"))
        self.assertEqual(result["total_fees"], D(4))
        self.assertEqual(result["gross_pnl"] - result["total_fees"], result["net_pnl"])

    def test_zero_trades(self):
        data, result = self.run_data(signals={s: "NONE" for s in PRIORITY})
        metrics = analyze(result, data)
        summary = metrics["summary"]
        self.assertEqual(summary["total_trades"], 0)
        self.assertEqual(summary["profit_factor"]["status"], "NO_TRADES")
        for field in ("win_rate_pct", "average_net_trade", "expectancy_per_trade", "best_trade", "worst_trade"):
            self.assertIsNone(summary[field])
        self.assertEqual(summary["return_pct"], D(0))
        self.assertEqual(metrics["risk"]["mtm"]["max_drawdown_amount"], D(0))
        self.assertEqual(len(metrics["realized_equity_curve"]), 1)

    def test_ratio_edge_cases(self):
        for pnls, pf_value, pf_status, ratio_status in (([1, 2], None, "NO_GROSS_LOSSES", "NO_LOSERS"),
                                                       ([-1, -2], D(0), "DEFINED", "NO_WINNERS"),
                                                       ([0, 0], None, "NO_GROSS_PROFITS_OR_LOSSES", "NO_WINNERS")):
            with self.subTest(pnls=pnls):
                metrics = trade_metrics(manual_trades(pnls))
                self.assertEqual(metrics["profit_factor"], {"value": pf_value, "status": pf_status})
                self.assertEqual(metrics["realized_win_loss_ratio"]["status"], ratio_status)
                self.assertIsNone(metrics["realized_win_loss_ratio"]["value"])

    def test_realized_curve_changes_at_closures_only(self):
        data = datasets(length=2, bars={"BTCUSDT": [(100, 103, 99, 100), (100, 101, 99, 100)],
                                       "ETHUSDT": [(100, 101, 99, 100), (100, 101, 98, 100)]})
        with scripted({(0, "BTCUSDT"): "LONG", (1, "ETHUSDT"): "LONG"}):
            result = simulate_portfolio(data, rules())
        curve = analyze(result, data)["realized_equity_curve"]
        self.assertEqual([p["equity"] for p in curve], [D(1000), D(1003), D("1001.5")])
        self.assertEqual(curve[1]["timestamp_ms"], START + STEP_MS - 1)
        self.assertEqual(curve[1]["observation"], "CANDLE_CLOSE_OBSERVATION")
        self.assertEqual(curve[1]["trade_reference"], result.trades[0].position.decision.decision_id)

    def test_long_mtm_and_realized_drawdown_distinction(self):
        data, result = self.run_data(signals={"BTCUSDT": "LONG"}, bars={"BTCUSDT": [
            (100, 101, 99, 101), (100, 101, 99, "99.5"), (100, 101, 99, 100)]})
        metrics = analyze(result, data)
        self.assertEqual([p["equity"] for p in metrics["mtm_equity_curve"]],
                         [D(1000), D(1001), D("999.5"), D(1000)])
        self.assertEqual(metrics["risk"]["mtm"]["max_drawdown_amount"], D("1.5"))
        self.assertEqual(metrics["risk"]["realized"]["max_drawdown_amount"], D(0))
        self.assertEqual(metrics["risk"]["primary"], "MTM_CANDLE_CLOSE_DRAWDOWN")

    def test_short_mtm_sign(self):
        data, result = self.run_data(signals={"BTCUSDT": "SHORT"}, bars={"BTCUSDT": [
            (100, 101, 99, 99), (100, 101, 99, "100.5"), (100, 101, 99, 100)]})
        curve = analyze(result, data)["mtm_equity_curve"]
        self.assertEqual([p["equity"] for p in curve], [D(1000), D(1001), D("999.5"), D(1000)])

    def test_open_position_fee_convention_then_actual_closure(self):
        data = datasets(signals={"BTCUSDT": "LONG"}, length=2,
                        bars={"BTCUSDT": [(100, 101, 99, 101), (100, 101, 99, 100)]})
        config = SimulationConfig(cost_mode=CostMode.FEES_ONLY, entry_fee_rate=D("0.0005"), exit_fee_rate=D("0.0005"))
        result = simulate_portfolio(data, rules(), config)
        metrics = analyze(result, data)
        self.assertEqual(metrics["mtm_equity_curve"][1]["equity"], D(1001))
        self.assertEqual(metrics["mtm_equity_curve"][-1]["equity"], D("999.9"))
        self.assertEqual(metrics["summary"]["total_fees"], D("0.1"))

    def test_drawdown_peak_trough_recovery_and_duration(self):
        curve = [dict(timestamp_ms=i * 1000, equity=D(e)) for i, e in enumerate([100, 120, 90, 110, 120, 100])]
        risk = drawdown(curve)
        self.assertEqual(risk["max_drawdown_amount"], D(30))
        self.assertEqual(risk["max_drawdown_pct"], D(25))
        worst = risk["worst_amount_episode"]
        self.assertEqual((worst["peak_timestamp_ms"], worst["trough_timestamp_ms"], worst["recovery_timestamp_ms"]),
                         (1000, 2000, 4000))
        self.assertEqual(risk["maximum_completed_duration_ms"], 3000)
        self.assertTrue(risk["unrecovered_episode"]["unrecovered"])
        self.assertIsNone(risk["unrecovered_episode"]["recovery_timestamp_ms"])

    def test_unrecovered_duration_observed_to_run_end(self):
        risk = drawdown([dict(timestamp_ms=0, equity=D(100)), dict(timestamp_ms=10, equity=D(90))], observed_through_ms=100)
        self.assertEqual(risk["unrecovered_episode"]["observed_duration_ms"], 100)
        self.assertEqual(risk["maximum_completed_duration_ms"], 0)

    def test_equal_peaks_and_equal_troughs_are_deterministic(self):
        risk = drawdown([dict(timestamp_ms=i, equity=D(e)) for i, e in enumerate([100, 100, 90, 90, 100])])
        episode = risk["worst_amount_episode"]
        self.assertEqual(episode["peak_timestamp_ms"], 1)
        self.assertEqual(episode["trough_timestamp_ms"], 2)
        self.assertEqual(episode["recovery_timestamp_ms"], 4)

    def test_amount_and_percentage_can_have_different_worst_episodes(self):
        risk = drawdown([dict(timestamp_ms=i, equity=D(e)) for i, e in enumerate([100, 50, 1000, 900])])
        self.assertEqual(risk["max_drawdown_amount"], D(100))
        self.assertEqual(risk["max_drawdown_pct"], D(50))
        self.assertNotEqual(risk["worst_amount_episode"], risk["worst_pct_episode"])

    def test_per_symbol_totals_reconcile(self):
        data = datasets(bars={s: [(100, 104, 98, 100)] * 3 for s in PRIORITY})
        with scripted({(i, s): "LONG" for i, s in enumerate(PRIORITY)}):
            result = simulate_portfolio(data, rules())
        metrics = analyze(result, data)
        for field in ("total_trades", "winning_trades", "losing_trades", "breakeven_trades", "gross_pnl", "net_pnl", "total_fees"):
            self.assertEqual(sum(m["metrics"][field] for m in metrics["per_symbol"].values()), metrics["summary"][field])
        for field, total in metrics["operations"].items():
            self.assertEqual(sum(m["operations"][field] for m in metrics["per_symbol"].values()), total)

    def test_operational_blocks_rejections_and_exit_reasons(self):
        data = datasets()
        filters = rules()
        filters["BTCUSDT"] = replace(filters["BTCUSDT"], min_notional=D(1000))
        with scripted({(i, s): "LONG" for i in range(3) for s in PRIORITY}):
            result = simulate_portfolio(data, filters)
        operations = analyze(result, data)["operations"]
        self.assertEqual(operations["total_evaluations"], 9)
        self.assertEqual(operations["eligibility_rejections"], 1)
        self.assertEqual(operations["accepted_entries"], 1)
        self.assertEqual(operations["BLOCKED_PRIORITY"], 1)
        self.assertEqual(operations["BLOCKED_OPEN_POSITION"], 6)
        self.assertEqual(operations["END_OF_DATA"], 1)
        for bar, reason, dual in (((100, 104, 98, 100), "STOP_LOSS", 1), ((100, 104, 99, 103), "TAKE_PROFIT", 0)):
            data, result = self.run_data(length=1, bars={"BTCUSDT": [bar]})
            ops = analyze(result, data)["operations"]
            self.assertEqual(ops[reason], 1)
            self.assertEqual(ops["dual_touch_exits"], dual)

    def test_json_bytes_and_fingerprints_repeat(self):
        data, result = self.run_data()
        first = build_report(result, data)
        with localcontext() as context:
            context.prec = 6
            second = build_report(result, dict(reversed(list(data.items()))))
        self.assertEqual(first.canonical_json.encode("ascii"), second.canonical_json.encode("ascii"))
        self.assertEqual(first.fingerprint, second.fingerprint)
        parsed = json.loads(first.canonical_json)
        self.assertEqual(parsed["summary"]["starting_equity"], "1000")
        self.assertNotIn("NaN", first.canonical_json)
        self.assertNotIn("Infinity", first.canonical_json)

    def test_manifest_configuration_change_changes_fingerprint(self):
        data, result = self.run_data()
        original = build_report(result, data)
        config = SimulationConfig(cost_mode=CostMode.FEES_ONLY, entry_fee_rate=D("0.0005"), exit_fee_rate=D("0.0005"))
        changed = build_report(simulate_portfolio(data, rules(), config), data)
        left, right = json.loads(original.canonical_json), json.loads(changed.canonical_json)
        self.assertNotEqual(left["manifest"]["manifest_fingerprint"], right["manifest"]["manifest_fingerprint"])
        self.assertNotEqual(original.fingerprint, changed.fingerprint)
        self.assertEqual(right["manifest"]["max_concurrent_positions"], 1)
        self.assertEqual(right["manifest"]["symbol_priority"], list(PRIORITY))

    def test_report_write_exclusive_and_human_output(self):
        data, result = self.run_data()
        report = build_report(result, data, code_revision="operator-supplied-revision")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            report.write(path)
            self.assertEqual(path.read_bytes(), report.canonical_json.encode("ascii"))
            with self.assertRaises(FileExistsError):
                report.write(path)
            with self.assertRaises(DataValidationError):
                report.write(Path(directory) / "live.sqlite")
        text = report.text()
        self.assertIn("PRIMARY max MTM drawdown", text)
        self.assertIn("REALIZED drawdown", text)
        self.assertIn("funding and liquidation excluded", text)
        self.assertIn("15m candle closes", text)
        self.assertIn("BTCUSDT", text)

    def test_reporting_is_read_only_and_never_evaluates_strategy(self):
        data, result = self.run_data()
        before = result.fingerprint
        source = {s: (d.candles, d.provenance_json) for s, d in data.items()}
        with patch.object(FrozenStrategyAdapter, "evaluate", side_effect=AssertionError("Analytics must not evaluate strategy")):
            build_report(result, data).text()
        self.assertEqual(before, result.fingerprint)
        self.assertEqual(source, {s: (d.candles, d.provenance_json) for s, d in data.items()})
        self.assertEqual(result, simulate_portfolio(data, rules()))

    def test_mismatched_source_and_lifecycle_rejected(self):
        data, result = self.run_data()
        changed = datasets(bars={"BTCUSDT": [(100, 104, 99, 103)] * 3})
        with self.assertRaises(DataValidationError):
            build_report(result, changed)
        bad = replace(result, final_equity=D(1234))
        with self.assertRaises(DataValidationError):
            analyze(bad, data)

    def test_gross_profit_with_net_loss_is_not_a_net_winner(self):
        metrics = trade_metrics(manual_trades([1], fee="1"))
        self.assertEqual(metrics["winning_trades"], 0)
        self.assertEqual(metrics["losing_trades"], 1)
        self.assertEqual(metrics["profit_factor"]["status"], "NO_GROSS_LOSSES")
        self.assertEqual(metrics["net_pnl"], D(-1))

    def test_negative_terminal_equity_drawdown_not_clipped(self):
        data = datasets(bars={"BTCUSDT": [(100, 101, 99, 100), (2000, 2001, 1999, 2000), (100, 101, 99, 100)]})
        with scripted({(0, "BTCUSDT"): "SHORT"}):
            result = simulate_portfolio(data, rules())
        metrics = analyze(result, data)
        self.assertEqual(metrics["summary"]["return_pct"], D(-190))
        self.assertEqual(metrics["risk"]["mtm"]["max_drawdown_pct"], D(190))
        self.assertEqual(len(metrics["mtm_equity_curve"]), 3)

    def test_report_fingerprint_verifies_canonical_content(self):
        from backtest.data import _hash
        data, result = self.run_data()
        report = build_report(result, data)
        document = json.loads(report.canonical_json)
        fingerprint = document.pop("report_fingerprint")
        self.assertEqual(fingerprint, _hash(document))
        manifest = document["manifest"].copy()
        manifest_fingerprint = manifest.pop("manifest_fingerprint")
        self.assertEqual(manifest_fingerprint, _hash(manifest))


if __name__ == "__main__":
    unittest.main()
