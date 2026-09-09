"""Read-only DEVELOPMENT analytics; no execution or automatic selection."""
from datetime import datetime, timezone
from decimal import Decimal as D

from backtest.metrics import analyze, trade_metrics
from backtest.simulation import arithmetic
from research.spec import canonical, fingerprint, policy


@arithmetic
def outcome_metrics(trades):
    trades = tuple(trades)
    metrics = trade_metrics(trades)
    profits = sum((t.net_pnl for t in trades if t.net_pnl > 0), D(0))
    losses = -sum((t.net_pnl for t in trades if t.net_pnl < 0), D(0))
    metrics['net_profit_factor'] = dict(value=profits/losses if losses else None,
        status='DEFINED' if losses else 'NO_NET_LOSSES' if profits else 'NO_NET_OUTCOMES')
    metrics['gross_expectancy'] = metrics['gross_pnl']/len(trades) if trades else None
    return metrics


def eligibility(metrics, quarters, drawdown_pct, *, integrity, insolvent):
    reasons = []
    if metrics['expectancy_per_trade'] is None or D(metrics['expectancy_per_trade']) <= 0:
        reasons.append('NONPOSITIVE_NET_EXPECTANCY')
    pf = metrics['net_profit_factor']
    if not (pf['status'] == 'NO_NET_LOSSES' or pf['value'] is not None and D(pf['value']) > 1):
        reasons.append('NET_PF_NOT_ABOVE_ONE')
    if len(quarters) != 4 or any(q['total_trades'] == 0 for q in quarters.values()):
        reasons.append('INACTIVE_QUARTER')
    if sum(D(q['net_pnl']) > 0 for q in quarters.values()) < 3:
        reasons.append('FEWER_THAN_THREE_POSITIVE_QUARTERS')
    if D(drawdown_pct) >= 25: reasons.append('MTM_DRAWDOWN_CAP')
    if not integrity: reasons.append('INTEGRITY_OR_RECONCILIATION_FAILURE')
    if insolvent: reasons.append('INSOLVENCY')
    weak = metrics['total_trades'] < 100
    return dict(status='INELIGIBLE' if reasons else 'WEAK_EVIDENCE' if weak else 'ELIGIBLE',
        reasons=reasons + (['BELOW_100_TRADE_EVIDENCE_TARGET'] if weak else []),
        evidence_target=100, evidence_target_is_hard_rejection=False, weaker_evidence=weak)


@arithmetic
def build_scorecard(result, datasets, identity):
    analytics = analyze(result, datasets)
    trades = result.trades
    metrics = outcome_metrics(trades)
    metrics.update({k: analytics['summary'][k] for k in
        ('starting_equity','final_realized_equity','return_pct')})
    months = {f'2024-{m:02d}': [] for m in range(1,13)}
    quarters = {f'2024-Q{q}': [] for q in range(1,5)}
    for t in trades:
        date = datetime.fromtimestamp(t.exit.candle_open_time_ms//1000, timezone.utc)
        if date.year != 2024: raise ValueError('Only DEVELOPMENT outcomes allowed')
        months[f'2024-{date.month:02d}'].append(t)
        quarters[f'2024-Q{(date.month-1)//3+1}'].append(t)
    months = {k: outcome_metrics(v) for k,v in months.items()}
    quarters = {k: outcome_metrics(v) for k,v in quarters.items()}
    fees = metrics['total_fees']
    slippage = sum((abs(f.price-f.reference_price)*f.quantity for t in trades
                    for f in (t.position.entry,t.exit)), D(0))
    turnover = sum((f.price*f.quantity for t in trades for f in (t.position.entry,t.exit)), D(0))
    reconciliation = (metrics['net_pnl'] == result.final_equity-result.config.starting_equity
        and metrics['gross_pnl']-fees == metrics['net_pnl']
        and analytics['operations']['accepted_entries'] == len(trades)
        and all(t.gross_pnl-t.position.entry.fee-t.exit.fee == t.net_pnl for t in trades))
    if not reconciliation: raise ValueError('Research result reconciliation failure')
    insolvent = result.terminal_state == 'INSOLVENT' or any(p['equity'] <= 0 for p in analytics['mtm_equity_curve'])
    attribution = {f'{s}:{direction}': outcome_metrics(t for t in trades
        if t.position.decision.symbol == s and t.position.decision.signal == direction)
        for s in result.priority for direction in ('LONG','SHORT')}
    document = dict(version='research-scorecard-v1', identity=identity, performance=metrics,
        risk=dict(**analytics['risk'], insolvency=insolvent),
        costs=dict(modeled_fees=fees, recorded_fill_slippage=slippage,
            friction_pct_starting_equity=(fees+slippage)/result.config.starting_equity*100,
            filled_notional=turnover, turnover_over_starting_equity=turnover/result.config.starting_equity,
            trades_per_development_day=D(len(trades))/366),
        temporal=dict(months=months, quarters=quarters,
            profitable_quarters=sum(q['net_pnl']>0 for q in quarters.values()),
            profitable_month_fraction=D(sum(m['net_pnl']>0 for m in months.values()))/12),
        attribution=dict(symbol=analytics['per_symbol'], symbol_direction=attribution,
            direction={d:outcome_metrics(t for t in trades if t.position.decision.signal==d) for d in ('LONG','SHORT')}),
        operations=dict(**analytics['operations'],
            occupied_boundary_fraction=D(sum(b.position_symbol_before is not None or b.created is not None
                for b in result.boundaries))/len(result.boundaries)),
        integrity='RECONCILED', eligibility=eligibility(metrics,quarters,
            analytics['risk']['mtm']['max_drawdown_pct'],integrity=True,insolvent=insolvent),
        policy=policy(), realized_equity_curve=analytics['realized_equity_curve'],
        mtm_equity_curve=analytics['mtm_equity_curve'])
    document = canonical(document)
    return dict(document, scorecard_fingerprint=fingerprint(document))


def compare_batch(specs, cards):
    """Predeclared three-way Pareto comparison, never promotion or scalar ranking."""
    specs = tuple(specs)
    if len(specs)!=3 or {s.signal_timeframe for s in specs}!={'15m','1h','4h'}:
        raise ValueError('Exactly the approved three concepts required')
    if len({(s.strategy_version,s.source_hashes,s.software_versions) for s in specs})!=1:
        raise ValueError('Comparison requires identical strategy/source/software across the batch')
    indexed = {c['identity']['experiment_fingerprint']:c for c in cards}
    if len(cards)!=3 or set(indexed)!={s.id for s in specs}: raise ValueError('Exact batch results required')
    rows=[]
    for s in sorted(specs,key=lambda s: ('15m','1h','4h').index(s.signal_timeframe)):
        c=indexed[s.id]
        if fingerprint({k:v for k,v in c.items() if k!='scorecard_fingerprint'})!=c['scorecard_fingerprint']:
            raise ValueError('Scorecard integrity failure')
        rows.append(dict(timeframe=s.signal_timeframe, **{k:c[k] for k in
            ('identity','performance','risk','costs','temporal','attribution','operations','eligibility')}))
    def vector(row):
        p=row['performance']
        return (D(p['return_pct']), D(p['expectancy_per_trade']) if p['expectancy_per_trade'] is not None else None,
                -D(row['risk']['mtm']['max_drawdown_pct']))
    dominance=[]
    for a in rows:
        for b in rows:
            x,y=vector(a),vector(b)
            if a is not b and None not in x+y and all(i>=j for i,j in zip(x,y)) and any(i>j for i,j in zip(x,y)):
                dominance.append([a['timeframe'],b['timeframe']])
    doc=dict(version='research-comparison-v1', rows=rows, dominates=dominance,
        pareto_axes=['return_pct','net_expectancy','negative_mtm_drawdown_pct'], automatic_promotion=False,
        notice='Correlated experiments on the same observed 2024 DEVELOPMENT period; stronger results are not independent confirmation. 2025 is previously observed locked validation; 2026 remains untouched true holdout.')
    return dict(doc,comparison_fingerprint=fingerprint(doc))
