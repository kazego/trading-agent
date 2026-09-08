"""Deterministic JSON and text reports. No simulation, network or automatic writes."""

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from backtest.data import DataValidationError, _encoded, _hash
from backtest.metrics import METRICS_VERSION, analyze
from backtest.portfolio import PRIORITY, _canonical
from backtest.simulation import arithmetic

REPORT_VERSION = "report-v1"
ASSUMPTIONS = (
    "Net after modeled trading costs only; funding and liquidation excluded. Not complete Futures account returns.",
    "PRIMARY risk: MTM drawdown sampled at 15m candle closes; worst intrabar excursion may be greater.",
    "MTM = closed-trade realized balance + price-only unrealized P&L. Hypothetical exit fees excluded.",
    "Entry fees of open positions remain deferred until closure, consistent with the supplied realized-balance formula.",
    "REALIZED drawdown is reported separately and is not complete mark-to-market portfolio risk.",
    "Profit factor uses positive/negative GROSS trade P&L (after fill slippage, before fees); wins and expectancy use NET P&L.",
)


def _source_hash(path):
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BacktestReport:
    canonical_json: str

    @property
    def fingerprint(self):
        return json.loads(self.canonical_json)["report_fingerprint"]

    def write(self, path):
        """Explicit exclusive creation only; report generation itself does not write."""
        path = Path(path)
        if path.suffix.lower() != ".json":
            raise DataValidationError("Report output requires .json")
        with path.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(self.canonical_json)

    @arithmetic
    def text(self):
        document = json.loads(self.canonical_json)
        summary, risk = document["summary"], document["risk"]
        def number(value):
            return "N/A" if value is None else format(Decimal(value), ".4f")
        pf = summary["profit_factor"]
        config = document["manifest"]["configuration"]
        lines = ["BACKTEST RESULT", "-------------------------------",
                 "Initial equity (USDT): " + number(summary["starting_equity"]),
                 "Final realized equity (USDT): " + number(summary["final_realized_equity"]),
                 "Gross / net P&L (USDT): " + number(summary["gross_pnl"]) + " / " + number(summary["net_pnl"]),
                 "Return (%): " + number(summary["return_pct"]),
                 "Trades / wins / losses / breakeven: " + " / ".join(str(summary[k]) for k in
                     ("total_trades", "winning_trades", "losing_trades", "breakeven_trades")),
                 "Win rate (%): " + number(summary["win_rate_pct"]),
                 "Gross profit factor: " + number(pf["value"]) + " (" + pf["status"] + ")",
                 "Net expectancy (USDT/trade): " + number(summary["expectancy_per_trade"]),
                 "Total fees (USDT): " + number(summary["total_fees"]),
                 "PRIMARY max MTM drawdown (USDT / %): " + number(risk["mtm"]["max_drawdown_amount"]) + " / " + number(risk["mtm"]["max_drawdown_pct"]),
                 "Max REALIZED drawdown (USDT / %): " + number(risk["realized"]["max_drawdown_amount"]) + " / " + number(risk["realized"]["max_drawdown_pct"])]
        for symbol in PRIORITY:
            metrics = document["per_symbol"][symbol]["metrics"]
            lines.append(symbol + ": trades=" + str(metrics["total_trades"]) + ", net USDT=" + number(metrics["net_pnl"]))
        lines += ["Cost mode: " + config["cost_mode"],
                  "Configured entry/exit fee rates: " + config["entry_fee_rate"] + " / " + config["exit_fee_rate"],
                  "Configured adverse slippage (bps): " + config["slippage_bps"],
                  "Inactive costs are disabled by the selected cost mode.",
                  "Report fingerprint: " + document["report_fingerprint"], *document["assumptions"]]
        return "\n".join(lines) + "\n"


@arithmetic
def build_report(result, datasets, *, code_revision=None):
    """code_revision is optional operator-supplied provenance, never inferred via Git."""
    if code_revision is not None and (not isinstance(code_revision, str) or not code_revision.strip()):
        raise DataValidationError("Code revision must be a nonempty string or None")
    analytics = analyze(result, datasets)
    decisions = [r.decision for b in result.boundaries for r in b.decisions]
    def unique(name):
        values = {getattr(d, name) for d in decisions}
        if len(values) != 1:
            raise DataValidationError("Inconsistent decision provenance: " + name)
        return next(iter(values))
    sources = {symbol: datasets[symbol].provenance for symbol in PRIORITY}
    intervals = {(m["start_ms"], m["end_ms"], m["timeframe"]) for m in sources.values()}
    if len(intervals) != 1:
        raise DataValidationError("Report source intervals must align")
    start, end, timeframe = next(iter(intervals))
    manifest = dict(
        portfolio_result_fingerprint=result.fingerprint, code_revision=code_revision,
        code_revision_provenance="OPERATOR_SUPPLIED" if code_revision else "UNAVAILABLE",
        strategy_source_sha256=unique("strategy_source_sha256"),
        report_source_sha256=_source_hash(Path(__file__)),
        metrics_source_sha256=_source_hash(Path(__file__).with_name("metrics.py")),
        symbols=PRIORITY, symbol_priority=result.priority, timeframe=timeframe,
        evaluation_interval=dict(start_ms=start, end_ms=end, semantics="[start,end)"),
        processed_through_boundary_ms=result.boundaries[-1].open_time_ms,
        terminal_state=result.terminal_state,
        warmup_policy=dict(completed_rows=99, forming_rows=1, window_rows=100,
                           ema_recomputed_each_window=True),
        strategy_version=unique("strategy_version"), ema_spans=unique("ema_spans"),
        strategy_adapter_version=unique("adapter_version"),
        python_version=unique("python_version"), pandas_version=unique("pandas_version"),
        numpy_version=unique("numpy_version"),
        configuration=asdict(result.config), risk_pct=result.config.risk_fraction * 100,
        max_leverage=result.config.leverage, max_concurrent_positions=1,
        stop_loss_pct=result.config.stop_fraction * 100, take_profit_pct=result.config.target_fraction * 100,
        effective_entry_fee_rate=result.config.entry_rate, effective_exit_fee_rate=result.config.exit_rate,
        effective_slippage_bps=result.config.slippage_bps if result.config.cost_mode == "FEES_AND_SLIPPAGE" else Decimal(0),
        symbol_rules=tuple(asdict(r) for r in result.rules), datasets=sources,
        portfolio_version=result.portfolio_version, execution_version=result.execution_version,
        metrics_version=METRICS_VERSION, report_version=REPORT_VERSION,
    )
    document = _canonical(dict(manifest=manifest, **analytics, assumptions=ASSUMPTIONS,
                               trades=[dict(reference=t.position.decision.decision_id, record=asdict(t)) for t in result.trades]))
    document["manifest"]["manifest_fingerprint"] = _hash(document["manifest"])
    document["report_fingerprint"] = _hash(document)
    return BacktestReport(_encoded(document).decode("ascii") + "\n")
