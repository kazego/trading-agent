"""Phase 6A read-only attribution. Never executes a strategy or simulation."""
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import pickle

from backtest.data import STEP_MS, _hash, _encoded
from backtest.simulation import arithmetic
from backtest.portfolio import _canonical
from backtest.metrics import trade_metrics

VERSION = "baseline-diagnostics-v1"
BASELINE = "61b496687c440a505349635255e58986b18bca48c9607f0def04d383688177be"
DEFINITIONS = {
    "calendar": "UTC exit candle opening boundary; intrabar exits have no invented timestamp. Boundary lies within the same month.",
    "duration": "Lower bound is exit-open minus entry-open; upper bound adds 15 minutes for INTRABAR, exact CLOSE uses +899999ms. Percentiles use linear interpolation; no intrabar timestamp invented.",
    "excursions": "Use full candles from entry through strictly before exit candle, then exit open and exit reference price. CLOSE includes full exit candle. INTRABAR/OPEN exclude exit H/L; INTRABAR excursions are conservative observed lower bounds, not exact lifecycle extrema. Fill slippage is reported separately, not market excursion.",
    "excursion_thresholds": "Meaningful MFE >=0.5 initial stop-price distance; near TP >=90% of initial target distance; large MAE >=0.5 stop-price distance. Count only known eligible observations; not inferred exit-candle ordering.",
    "stop_runs": "Maximal consecutive STOP_LOSS runs in portfolio closure order, length >=2; each member counted once.",
    "rapid_opposites": "Consecutive actual trades of the same symbol with opposite direction and next entry 0..4 boundaries after prior exit boundary; attribution counts the later trade once.",
    "flip_sequences": "Three consecutive actual same-symbol entries alternate directions and span <=16 boundaries; count overlapping windows, P&L uses union of member trades once.",
    "loss_clusters": "UTC exit dates with >=3 net-losing trades; count and P&L include losing trades on those dates only.",
    "concentration": "Worst/best N sorted by net P&L then decision ID. Five percent uses ceiling of trade count. Percent of net loss is signed contribution divided by total negative net P&L.",
    "costs": "Gross already includes slipped fills. Reference-fill P&L adds back exact recorded slippage at fixed quantities and exit references; it is NOT a zero-cost strategy rerun because sizing, protection levels and arbitration remain cost-conditioned.",
}


def date(ms, fmt):
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime(fmt)


def duration(t):
    delta = t.exit.candle_open_time_ms - t.position.entry.candle_open_time_ms
    if t.exit.phase == "CLOSE":
        delta += STEP_MS - 1
    return D(delta) / 60000, D(delta + (STEP_MS if t.exit.phase == "INTRABAR" else 0)) / 60000


@arithmetic
def distribution(values):
    values = sorted(D(v) for v in values)
    if not values:
        return dict(count=0, mean=None, min=None, max=None, p25=None, median=None, p75=None, p90=None)
    def percentile(p):
        index = D(len(values)-1)*p
        low = int(index)
        return values[low] + (values[min(low+1,len(values)-1)]-values[low])*(index-low)
    return dict(count=len(values),mean=sum(values)/len(values),min=values[0],max=values[-1],
                p25=percentile(D('.25')),median=percentile(D('.5')),p75=percentile(D('.75')),p90=percentile(D('.9')))


@arithmetic
def stats(trades):
    trades = tuple(trades)
    result = trade_metrics(trades)
    result['average_gross_trade'] = result['gross_pnl']/len(trades) if trades else None
    result['exits'] = dict(Counter(t.reason.value for t in trades))
    result['duration_lower_minutes'] = distribution(duration(t)[0] for t in trades)
    result['duration_upper_minutes'] = distribution(duration(t)[1] for t in trades)
    return result


def grouped(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {k:stats(v) for k,v in sorted(groups.items())}


@arithmetic
def cost_decomposition(trades, starting):
    gross = sum((t.gross_pnl for t in trades), D(0))
    net = sum((t.net_pnl for t in trades), D(0))
    fees = sum((t.position.entry.fee+t.exit.fee for t in trades),D(0))
    slip = D(0)
    for t in trades:
        for f in (t.position.entry,t.exit):
            slip += (f.price-f.reference_price if f.side=='BUY' else f.reference_price-f.price)*f.quantity
    if gross-fees != net:
        raise ValueError('Cost reconciliation failed')
    return dict(gross_pnl=gross,net_pnl=net,fees=fees,gross_minus_net=gross-net,
                recorded_fill_slippage=slip,total_recorded_friction=fees+slip,
                fixed_fill_reference_pnl=gross+slip,fees_pct_start=fees/starting*100,
                fees_per_trade=fees/len(trades) if trades else None,
                gross_expectancy=gross/len(trades) if trades else None,
                net_expectancy=net/len(trades) if trades else None)


@arithmetic
def payoff(trades):
    m=trade_metrics(trades)
    w,l=m['average_winner'],m['average_loser_magnitude']
    exits=Counter(t.reason.value for t in trades)
    return dict(exits=dict(exits),exit_pct={k:D(v)/len(trades)*100 for k,v in exits.items()},
                sl_tp_ratio=D(exits['STOP_LOSS'])/exits['TAKE_PROFIT'] if exits['TAKE_PROFIT'] else None,
                nominal_break_even_pct=D(100)/3,actual_win_rate_pct=m['win_rate_pct'],
                average_net_winner=w,average_net_loser_magnitude=l,
                realized_win_loss_ratio=w/l if w is not None and l else None,
                realized_break_even_pct=l/(w+l)*100 if w is not None and l else None)


@arithmetic
def excursion(t, bars):
    p=t.position; entry=p.entry.price; start=p.entry.candle_open_time_ms; end=t.exit.candle_open_time_ms
    prices=[entry]
    for timestamp in range(start,end,STEP_MS):
        c=bars[timestamp]
        prices.extend((c.high,c.low))
    last=bars[end]
    prices.extend((last.open,t.exit.reference_price))
    if t.exit.phase=='CLOSE':
        prices.extend((last.high,last.low))
    long=p.decision.signal=='LONG'
    favorable=max(D(0), max(prices)-entry if long else entry-min(prices))
    adverse=max(D(0), entry-min(prices) if long else max(prices)-entry)
    stop_distance=abs(entry-p.stop_loss)
    target_distance=abs(entry-p.take_profit)
    return dict(reference=p.decision.decision_id,symbol=p.decision.symbol,direction=p.decision.signal,
                outcome='WIN' if t.net_pnl>0 else 'LOSS' if t.net_pnl<0 else 'BREAKEVEN',
                reason=t.reason.value,exit_extrema_excluded=t.exit.phase!='CLOSE',
                mfe_price=favorable,mae_price=adverse,mfe_pct=favorable/entry*100,mae_pct=adverse/entry*100,
                mfe_usdt=favorable*p.entry.quantity,mae_usdt=adverse*p.entry.quantity,
                mfe_R=favorable*p.entry.quantity/p.planned_stop_risk,
                mae_R=adverse*p.entry.quantity/p.planned_stop_risk,
                meaningful_mfe=favorable>=stop_distance*D('.5'),near_target=favorable>=target_distance*D('.9'),
                large_mae=adverse>=stop_distance*D('.5'))


def excursion_summary(records):
    return {field:distribution(r[field] for r in records) for field in
            ('mfe_pct','mae_pct','mfe_R','mae_R','mfe_usdt','mae_usdt')}


def whipsaw(trades):
    trades=list(trades); runs=[]; current=[]
    for i,t in enumerate(trades):
        if t.reason.value=='STOP_LOSS':
            current.append(i)
        else:
            if len(current)>=2: runs.append(current)
            current=[]
    if len(current)>=2: runs.append(current)
    by_symbol=defaultdict(list)
    for i,t in enumerate(trades): by_symbol[t.position.decision.symbol].append(i)
    rapid=[]; flips=[]
    for seq in by_symbol.values():
        for a,b in zip(seq,seq[1:]):
            if trades[a].position.decision.signal != trades[b].position.decision.signal and 0 <= trades[b].position.entry.candle_open_time_ms-trades[a].exit.candle_open_time_ms <= 4*STEP_MS:
                rapid.append([a,b])
        for a,b,c in zip(seq,seq[1:],seq[2:]):
            ds=[trades[x].position.decision.signal for x in (a,b,c)]
            if ds[0]!=ds[1] and ds[1]!=ds[2] and trades[c].position.entry.candle_open_time_ms-trades[a].position.entry.candle_open_time_ms<=16*STEP_MS:
                flips.append([a,b,c])
    days=defaultdict(list)
    for i,t in enumerate(trades):
        if t.net_pnl<0: days[date(t.exit.candle_open_time_ms,'%Y-%m-%d')].append(i)
    clusters=[v for v in days.values() if len(v)>=3]
    def record(patterns, later=False):
        members=sorted({i for p in patterns for i in (p[-1:] if later else p)})
        return dict(pattern_count=len(patterns),trade_count=len(members),
                    net_pnl=sum((trades[i].net_pnl for i in members),D(0)),
                    references=[trades[i].position.decision.decision_id for i in members])
    return dict(stop_runs=record(runs),maximum_stop_run=max(map(len,runs),default=0),
                rapid_opposites=record(rapid,True),flip_sequences=record(flips),loss_days=record(clusters))


@arithmetic
def blocking(boundaries):
    counts=Counter(); groups={k:defaultdict(Counter) for k in ('symbol','direction','year','month')}
    occupied=0
    for b in boundaries:
        occupied += b.position_symbol_before is not None
        for r in b.decisions:
            if r.decision.signal=='NONE': continue
            label=r.reason if r.disposition=='BLOCKED' else r.disposition.value
            counts[label]+=1
            for name,key in dict(symbol=r.decision.symbol,direction=r.decision.signal,
                    year=date(b.open_time_ms,'%Y'),month=date(b.open_time_ms,'%Y-%m')).items():
                groups[name][key][label]+=1
    total=sum(counts.values())
    return dict(actionable=total,counts=dict(counts),percentages={k:D(v)/total*100 for k,v in counts.items()} if total else {},
                groups={k:{g:dict(v) for g,v in sorted(d.items())} for k,d in groups.items()},
                evaluation_boundaries=len(boundaries),occupied_at_open=occupied,
                occupied_at_open_pct=D(occupied)/len(boundaries)*100 if boundaries else None)


@arithmetic
def concentration(trades):
    ordered=sorted(trades,key=lambda t:(t.net_pnl,t.position.decision.decision_id))
    total=sum((t.net_pnl for t in trades),D(0)); result={}
    for n,label in ((5,'5'),(10,'10'),((len(trades)+19)//20,'5pct')):
        for side,subset in (('worst',ordered[:n]),('best',ordered[-n:] if n else [])):
            pnl=sum((t.net_pnl for t in subset),D(0))
            result[side+'_'+label]=dict(trades=len(subset),net_pnl=pnl,signed_pct_total_net=pnl/total*100 if total else None)
    return result


@arithmetic
def diagnose(result,datasets):
    trades=result.trades
    direction=lambda t:t.position.decision.signal
    symbol=lambda t:t.position.decision.symbol
    year=lambda t:date(t.exit.candle_open_time_ms,'%Y')
    month=lambda t:date(t.exit.candle_open_time_ms,'%Y-%m')
    months=grouped(trades,month)
    # Include all evaluation months, including zero-trade months.
    for b in result.boundaries:
        months.setdefault(date(b.open_time_ms,'%Y-%m'),None)
    months={k:v if v is not None else stats(()) for k,v in sorted(months.items())}
    running=D(0); streak=longest=0
    for m in months.values():
        running+=m['net_pnl']; m['cumulative_net_pnl']=running
        streak=streak+1 if m['net_pnl']<0 else 0; longest=max(longest,streak)
    bars={s:{c.open_time_ms:c for c in d.candles} for s,d in datasets.items()}
    excursions=[excursion(t,bars[symbol(t)]) for t in trades]
    excursion_groups={}
    for field in ('symbol','direction','outcome'):
        excursion_groups[field]={key:excursion_summary([r for r in excursions if r[field]==key]) for key in sorted({r[field] for r in excursions})}
    output=dict(version=VERSION,baseline_report_fingerprint=BASELINE,definitions=DEFINITIONS,
        diagnostic_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        dataset_fingerprints={s:d.fingerprint for s,d in datasets.items()},
        direction=grouped(trades,direction),symbol_direction=grouped(trades,lambda t:symbol(t)+' '+direction(t)),
        years=grouped(trades,year),months=months,year_symbol=grouped(trades,lambda t:year(t)+' '+symbol(t)),
        year_direction=grouped(trades,lambda t:year(t)+' '+direction(t)),
        year_symbol_direction=grouped(trades,lambda t:year(t)+' '+symbol(t)+' '+direction(t)),
        calendar_summary=dict(best_month=max(months,key=lambda k:months[k]['net_pnl']),worst_month=min(months,key=lambda k:months[k]['net_pnl']),
            longest_losing_month_sequence=longest,profitable_months=sum(m['net_pnl']>0 for m in months.values()),month_count=len(months)),
        costs=cost_decomposition(trades,result.config.starting_equity),payoff=payoff(trades),
        durations=dict(overall=stats(trades),by_outcome=grouped(trades,lambda t:'WIN' if t.net_pnl>0 else 'LOSS' if t.net_pnl<0 else 'BREAKEVEN'),
                       by_direction=grouped(trades,direction),by_symbol=grouped(trades,symbol)),
        excursions=dict(overall=excursion_summary(excursions),groups=excursion_groups,records=excursions,
            stopped_with_meaningful_mfe=sum(r['reason']=='STOP_LOSS' and r['meaningful_mfe'] for r in excursions),
            stopped_near_target=sum(r['reason']=='STOP_LOSS' and r['near_target'] for r in excursions),
            winners_large_mae=sum(r['outcome']=='WIN' and r['large_mae'] for r in excursions)),
        whipsaw=whipsaw(trades),blocking=blocking(result.boundaries),concentration=concentration(trades))
    output=_canonical(output)
    output['diagnostic_fingerprint']=_hash(output)
    return output


def human(report):
    lines=['PHASE 6A: baseline-001 diagnostics', 'Existing actual trades only; no counterfactual portfolio.',
           'Baseline fingerprint: '+report['baseline_report_fingerprint'], 'Diagnostic fingerprint: '+report['diagnostic_fingerprint']]
    for section in ('direction','symbol_direction','years','months'):
        lines.append('\n'+section.upper()+' (UTC exit attribution)')
        for name,m in report[section].items():
            lines.append(f"{name}: trades={m['total_trades']}, gross={m['gross_pnl']}, net={m['net_pnl']}, fees={m['total_fees']}, win%={m['win_rate_pct']}, PF={m['profit_factor']['value']}")
    for section in ('calendar_summary','costs','payoff','concentration'):
        lines.extend(['\n'+section.upper(),json.dumps(report[section],sort_keys=True)])
    lines.extend(['\nDURATION (minutes; lower/upper bounds, not invented exit times)',
        json.dumps({k:report['durations']['overall'][k] for k in ('duration_lower_minutes','duration_upper_minutes')},sort_keys=True),
        '\nEXCURSIONS (conservative observed lower bounds for intrabar exits)',json.dumps(report['excursions']['overall'],sort_keys=True),
        '\nWHIPSAW',json.dumps({k:({a:b for a,b in v.items() if a!='references'} if isinstance(v,dict) else v) for k,v in report['whipsaw'].items()},sort_keys=True),
        '\nBLOCKING',json.dumps({k:v for k,v in report['blocking'].items() if k!='groups'},sort_keys=True),
        '\nDEFINITIONS',json.dumps(report['definitions'],sort_keys=True),
        '\nOBSERVATIONS',
        f"LONG net={report['direction']['LONG']['net_pnl']} USDT; SHORT net={report['direction']['SHORT']['net_pnl']} USDT. These are actual executed trades, not direction-only portfolios.",
        f"2024 net={report['years']['2024']['net_pnl']}; 2025 net={report['years']['2025']['net_pnl']} USDT.",
        f"Stopped trades with observed meaningful MFE={report['excursions']['stopped_with_meaningful_mfe']}; stopped near target={report['excursions']['stopped_near_target']}; winners with large MAE={report['excursions']['winners_large_mae']}.",
        f"Already losing before recorded fees: {D(report['costs']['gross_pnl'])<0}. Fixed-fill reference P&L before recorded slippage/fees={report['costs']['fixed_fill_reference_pnl']} USDT; this is not a zero-cost strategy result.",
        'Direction/year/month figures attribute executed trades only. Blocking measures availability, not missed profits.',
        'Gross P&L already includes slippage. Fixed-fill reference P&L removes recorded fill slippage only; it does not reconstruct a zero-cost strategy.',
        'Exit-candle ordering is unknown; excursion threshold counts are conservative observed counts. No market regimes were inferred.',
        'Funding and liquidation excluded. Original MTM is candle-close sampled, not intrabar risk.',
        '\nHYPOTHESES FOR FUTURE TESTING',
        'Turnover, direction asymmetry and reversal patterns may deserve controlled testing. These attributions do not show that disabling a direction, changing SL/TP or allowing more positions improves performance.'])
    return '\n'.join(lines)+'\n'


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnose-existing-baseline',action='store_true',required=True)
    parser.parse_args()
    root=Path('artifacts/baseline-001'); output=root/'diagnostics'
    if output.exists(): raise FileExistsError('Diagnostics already exist; refusing overwrite')
    files=[p for p in root.rglob('*') if p.is_file()]
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    baseline=json.loads((root/'financial/report.json').read_text())
    if _hash({k:v for k,v in baseline.items() if k!='report_fingerprint'})!=BASELINE: raise ValueError('Baseline hash mismatch')
    from backtest.baseline import load_inputs
    datasets,_,_,_=load_inputs(root)
    # Only the preserved locally generated checkpoint is loaded, never external pickle inputs.
    with (root/'financial/first-result.pickle').open('rb') as stream: result=pickle.load(stream)
    if result.fingerprint!=baseline['manifest']['portfolio_result_fingerprint']: raise ValueError('Checkpoint fingerprint mismatch')
    report=diagnose(result,datasets)
    if before!={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}: raise ValueError('Baseline files changed')
    output.mkdir()
    with (output/'report.json').open('xb') as stream: stream.write(_encoded(report)+b'\n')
    with (output/'report.txt').open('x',encoding='utf-8',newline='\n') as stream: stream.write(human(report))
    print(human(report),flush=True)


if __name__=='__main__': main()
