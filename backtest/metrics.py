"""Read-only analytics over completed portfolio records and matching market data."""

from decimal import Decimal

from backtest.data import DataValidationError, STEP_MS
from backtest.portfolio import PRIORITY
from backtest.simulation import arithmetic

D = Decimal
METRICS_VERSION = "analytics-v1"


def _ratio(numerator, denominator, undefined):
    return {"value": numerator / denominator if denominator else None,
            "status": "DEFINED" if denominator else undefined}


@arithmetic
def trade_metrics(trades):
    """Wins are net outcomes; profit factor explicitly uses gross trade P&L."""
    trades = tuple(trades)
    net = [t.net_pnl for t in trades]
    winners, losers = [p for p in net if p > 0], [p for p in net if p < 0]
    gross_profits = sum((t.gross_pnl for t in trades if t.gross_pnl > 0), D(0))
    gross_losses = -sum((t.gross_pnl for t in trades if t.gross_pnl < 0), D(0))
    avg_win = sum(winners, D(0)) / len(winners) if winners else None
    avg_loss = -sum(losers, D(0)) / len(losers) if losers else None
    average = sum(net, D(0)) / len(net) if net else None
    win_run = loss_run = max_wins = max_losses = 0
    for pnl in net:
        win_run = win_run + 1 if pnl > 0 else 0
        loss_run = loss_run + 1 if pnl < 0 else 0
        max_wins, max_losses = max(max_wins, win_run), max(max_losses, loss_run)
    pf_status = "NO_TRADES" if not trades else "NO_GROSS_LOSSES" if gross_profits else "NO_GROSS_PROFITS_OR_LOSSES"
    ratio_status = "NO_TRADES" if not trades else "NO_WINNERS" if not winners else "NO_LOSERS"
    return dict(
        total_trades=len(trades), winning_trades=len(winners), losing_trades=len(losers),
        breakeven_trades=sum(p == 0 for p in net),
        win_rate_pct=D(len(winners)) / len(net) * 100 if net else None,
        gross_pnl=sum((t.gross_pnl for t in trades), D(0)), net_pnl=sum(net, D(0)),
        total_fees=sum((t.position.entry.fee + t.exit.fee for t in trades), D(0)),
        average_net_trade=average, expectancy_per_trade=average,
        best_trade=max(net) if net else None, worst_trade=min(net) if net else None,
        average_winner=avg_win, average_loser_magnitude=avg_loss,
        realized_win_loss_ratio=_ratio(avg_win, avg_loss, ratio_status) if winners and losers
        else {"value": None, "status": ratio_status},
        gross_realized_profits=gross_profits, gross_realized_losses=gross_losses,
        profit_factor=_ratio(gross_profits, gross_losses, pf_status),
        maximum_consecutive_wins=max_wins, maximum_consecutive_losses=max_losses,
    )


def operational_metrics(records, trades):
    counts = dict(total_evaluations=0, LONG=0, SHORT=0, NONE=0, actionable_signals=0,
                  accepted_entries=0, BLOCKED_OPEN_POSITION=0, BLOCKED_PRIORITY=0,
                  eligibility_rejections=0, STOP_LOSS=0, TAKE_PROFIT=0, END_OF_DATA=0,
                  dual_touch_exits=0)
    for record in records:
        signal = record.decision.signal
        if signal not in ("LONG", "SHORT", "NONE"):
            raise DataValidationError("Unknown recorded signal")
        counts["total_evaluations"] += 1
        counts[signal] += 1
        counts["actionable_signals"] += signal != "NONE"
        if record.disposition == "ACCEPTED":
            counts["accepted_entries"] += 1
        elif record.disposition == "REJECTED":
            counts["eligibility_rejections"] += 1
        elif record.disposition == "BLOCKED":
            if record.reason not in ("BLOCKED_OPEN_POSITION", "BLOCKED_PRIORITY"):
                raise DataValidationError("Unknown recorded blocking reason")
            counts[record.reason] += 1
        elif record.disposition != "NONE":
            raise DataValidationError("Unknown recorded disposition")
    for trade in trades:
        counts[trade.reason.value] += 1
        counts["dual_touch_exits"] += trade.dual_touch
    return counts


@arithmetic
def drawdown(curve, observed_through_ms=None):
    """Latest equal high-water mark starts an episode; equality recovers it."""
    if not curve or curve[0]["equity"] <= 0:
        raise DataValidationError("Drawdown requires a positive initial equity point")
    observed_through_ms = curve[-1]["timestamp_ms"] if observed_through_ms is None else observed_through_ms
    if observed_through_ms < curve[-1]["timestamp_ms"]:
        raise DataValidationError("Observation end precedes curve")
    peak, peak_time = curve[0]["equity"], curve[0]["timestamp_ms"]
    previous_time = peak_time
    active = None
    episodes = []
    for point in curve[1:]:
        value, timestamp = point["equity"], point["timestamp_ms"]
        if timestamp < previous_time:
            raise DataValidationError("Equity curve must be chronological")
        previous_time = timestamp
        if value >= peak:
            if active is not None:
                active["recovery_timestamp_ms"] = timestamp
                active["completed_duration_ms"] = timestamp - active["peak_timestamp_ms"]
                episodes.append(active)
                active = None
            peak, peak_time = value, timestamp
        else:
            if active is None:
                active = dict(peak_equity=peak, peak_timestamp_ms=peak_time,
                              trough_equity=value, trough_timestamp_ms=timestamp,
                              recovery_timestamp_ms=None, completed_duration_ms=None)
            elif value < active["trough_equity"]:
                active["trough_equity"], active["trough_timestamp_ms"] = value, timestamp
    if active is not None:
        episodes.append(active)
    for episode in episodes:
        episode["drawdown_amount"] = episode["peak_equity"] - episode["trough_equity"]
        episode["drawdown_pct"] = episode["drawdown_amount"] / episode["peak_equity"] * 100
        episode["unrecovered"] = episode["recovery_timestamp_ms"] is None
        episode["observed_duration_ms"] = (episode["recovery_timestamp_ms"]
                                           if not episode["unrecovered"] else observed_through_ms) - episode["peak_timestamp_ms"]
    worst_amount = max(episodes, key=lambda e: e["drawdown_amount"], default=None)
    worst_pct = max(episodes, key=lambda e: e["drawdown_pct"], default=None)
    return dict(max_drawdown_amount=worst_amount["drawdown_amount"] if worst_amount else D(0),
                max_drawdown_pct=worst_pct["drawdown_pct"] if worst_pct else D(0),
                worst_amount_episode=worst_amount, worst_pct_episode=worst_pct,
                maximum_completed_duration_ms=max((e["completed_duration_ms"] for e in episodes
                                                    if e["completed_duration_ms"] is not None), default=0),
                unrecovered_episode=active, episodes=episodes)


@arithmetic
def equity_curves(result, datasets):
    if set(datasets) != set(PRIORITY) or not result.boundaries:
        raise DataValidationError("Analytics require all source datasets and a nonempty run")
    expected = dict(result.dataset_fingerprints)
    for symbol in PRIORITY:
        if datasets[symbol].fingerprint != expected[symbol]:
            raise DataValidationError("Analytics dataset fingerprint mismatch")
    bars = {s: {c.open_time_ms: c for c in datasets[s].candles if c.symbol == s} for s in PRIORITY}
    start = result.boundaries[0].open_time_ms
    initial = dict(timestamp_ms=start, boundary_open_time_ms=start,
                   equity=result.config.starting_equity, trade_reference=None, observation="INITIAL")
    realized, mtm = [initial.copy()], [initial.copy()]
    active = None
    balance = result.config.starting_equity
    previous_boundary = None
    for boundary in result.boundaries:
        timestamp = boundary.open_time_ms
        if previous_boundary is not None and timestamp != previous_boundary + STEP_MS:
            raise DataValidationError("Portfolio boundaries must be contiguous")
        previous_boundary = timestamp
        if boundary.equity_before != balance or boundary.position_symbol_before != (active.decision.symbol if active else None):
            raise DataValidationError("Inconsistent portfolio lifecycle/equity")
        if boundary.created is not None:
            if active is not None:
                raise DataValidationError("Overlapping recorded positions")
            active = boundary.created
        if boundary.closed is not None:
            trade = boundary.closed
            if active != trade.position or trade.exit.candle_open_time_ms != timestamp:
                raise DataValidationError("Inconsistent recorded closure")
            balance += trade.net_pnl
            realized.append(dict(timestamp_ms=timestamp if trade.exit.phase == "OPEN" else timestamp + STEP_MS - 1,
                                 boundary_open_time_ms=timestamp, equity=balance,
                                 trade_reference=trade.position.decision.decision_id,
                                 observation="OPEN" if trade.exit.phase == "OPEN" else "CANDLE_CLOSE_OBSERVATION",
                                 recorded_exit_phase=trade.exit.phase))
            active = None
        if boundary.equity_after != balance or boundary.position_symbol_after != (active.decision.symbol if active else None):
            raise DataValidationError("Inconsistent boundary end state")
        unrealized, mark = D(0), None
        if active is not None:
            candle = bars[active.decision.symbol].get(timestamp)
            if candle is None:
                raise DataValidationError("Missing analytics mark candle")
            mark = candle.close
            unrealized = (mark - active.entry.price) * active.entry.quantity
            if active.decision.signal == "SHORT":
                unrealized = -unrealized
        mtm.append(dict(timestamp_ms=timestamp + STEP_MS - 1, boundary_open_time_ms=timestamp,
                        equity=balance + unrealized, realized_balance=balance, unrealized_pnl=unrealized,
                        mark_price=mark, position_symbol=active.decision.symbol if active else None,
                        trade_reference=active.decision.decision_id if active else None,
                        observation="CANDLE_CLOSE"))
    if active is not None or result.final_equity != balance:
        raise DataValidationError("Portfolio result must be closed and reconciled")
    return realized, mtm


@arithmetic
def analyze(result, datasets):
    realized, mtm = equity_curves(result, datasets)
    trades = result.trades
    records = tuple(d for b in result.boundaries for d in b.decisions)
    summary = trade_metrics(trades)
    summary.update(starting_equity=result.config.starting_equity, final_realized_equity=result.final_equity,
                   absolute_return=result.final_equity - result.config.starting_equity,
                   return_pct=(result.final_equity - result.config.starting_equity) / result.config.starting_equity * 100)
    by_symbol = {}
    for symbol in PRIORITY:
        symbol_trades = tuple(t for t in trades if t.position.decision.symbol == symbol)
        operations = operational_metrics((r for r in records if r.decision.symbol == symbol), symbol_trades)
        by_symbol[symbol] = dict(metrics=trade_metrics(symbol_trades), operations=operations,
                                 actionable_signals=operations["actionable_signals"],
                                 accepted_trades=operations["accepted_entries"])
    return dict(summary=summary, risk=dict(primary="MTM_CANDLE_CLOSE_DRAWDOWN",
                                          mtm=drawdown(mtm), realized=drawdown(realized, mtm[-1]["timestamp_ms"])),
                per_symbol=by_symbol, operations=operational_metrics(records, trades),
                realized_equity_curve=realized, mtm_equity_curve=mtm)
