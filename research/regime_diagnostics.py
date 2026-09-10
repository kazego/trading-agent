"""Phase 6D4 attribution of preserved trades only; no signal or execution calls."""
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

from backtest.diagnostics import date, duration, excursion, distribution, concentration
from backtest.simulation import Fill, ExitReason, arithmetic
from research.data import load_local_development, build_view
from research.features import FeatureView, calculate_features, load_history, VERSION as FEATURE_VERSION
from research.spec import DEV_START, VALIDATION_START, SYMBOLS, Role, SplitRequest, canonical, encoded, fingerprint

VERSION = 'phase6d4-entry-regime-diagnostics-v1'
EXPERIMENT = '0c8d02a7bb3a2e9bb02c9e8b9c36e6c248581d7c95b2b3e3f91572faa7dd5851'
RUN = '37de98cc08420c945dece486ec0295b144dc3824b44fad1705367d08f4d096b7'
RESULT = '726a515e0fbb95a9b3281afe5d179c55f0d89e6dd9c5d20d77f1796342de6632'
MANIFEST = 'd78fbaa2af3b120ac3e2d567779f7ba7e862b4ae3406471dcc3a0b7abfa101b6'
TRADE_HASH = '7aae300aae0a45e7281054b9e83c4813238aac2b99ce8a9010bd99d3b1853d3b'
DEFINITIONS = dict(
    classification='PREDECLARED DIAGNOSTIC; not strategy performance or statistical significance',
    calendar='Core and ADA month/quarter attribution uses UTC exit candle opening, matching Phase 6C; entry month/quarter are separate entry-known fields.',
    slope='Latest completed EMA200 minus previous completed EMA200 in the same frozen 600-row window; sign only.',
    ribbon='Strict bullish/bearish order; otherwise EQUALITY_NEUTRAL if any pair equals, else MIXED.',
    partitions='Full-population normalized ATR quartiles: linear interpolation (n-1)*p, ties assigned to lower interval. Descriptive only, not candidate thresholds.',
    duration='Minutes: lower/upper bounds from existing diagnostic duration(); intrabar exit adds 15 minutes only to upper bound.',
    excursions='Existing conservative excursion(): full bars strictly before exit; exit open/reference only for OPEN/INTRABAR; full final bar only for CLOSE. No unknown post-exit extrema.',
    pnl='Gross already includes fill slippage; fees=gross-net. Wins/losses use net. Gross and net PF use their respective positive/negative trade populations.',
    concentration='Signed contribution/total net; may exceed 100%. Best/worst N tie-break by decision ID; ceil(5%*n). Exclusions subtract existing attribution, not counterfactual reruns.',
    overlap='Each table partitions the population; tables and cohort comparisons overlap and must not be summed together.',
    outcomes='Duration, exits, P&L and MFE/MAE are post-trade only and cannot enter the entry feature namespace.')
REGIMES = ('ribbon','ema200_relation','ema200_slope','volatility_quartile','bullish_breakout','bearish_breakout')


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def verify_source(folder):
    manifest=json.loads((folder/'run-manifest.json').read_text())
    if (manifest['manifest_fingerprint'] != MANIFEST or
            fingerprint({k:v for k,v in manifest.items() if k!='manifest_fingerprint'}) != MANIFEST or
            manifest['identity']['run_fingerprint'] != RUN):
        raise ValueError('Frozen Phase 6C 4h manifest mismatch')
    for name,expected in manifest['file_sha256'].items():
        if name not in ('approved-inputs.json','first-result.json','scorecard.json','report.txt') or sha(folder/name)!=expected:
            raise ValueError('Preserved artifact bytes differ')
    saved=json.loads((folder/'first-result.json').read_text())
    if saved['result_fingerprint']!=RESULT or fingerprint(saved['result'])!=RESULT:
        raise ValueError('Preserved result fingerprint differs')
    raw=tuple(b['closed'] for b in saved['result']['boundaries'] if b['closed'] is not None)
    if fingerprint(raw)!=TRADE_HASH:raise ValueError('Authoritative trade population differs')
    trades=tuple(decode_trade(t) for t in raw)
    reconcile(trades,authoritative=True)
    return trades,manifest


def decode_trade(t):
    # A read-only projection of authenticated-by-checksum local JSON, not a simulator.
    p=t['position'];d=p['decision']
    return NS(position=NS(decision=NS(**d),entry=Fill(**p['entry']),
        stop_loss=D(p['stop_loss']),take_profit=D(p['take_profit']),planned_stop_risk=D(p['planned_stop_risk'])),
        trade_fingerprint=fingerprint(t),exit=Fill(**t['exit']),reason=ExitReason(t['reason']),gross_pnl=D(t['gross_pnl']),net_pnl=D(t['net_pnl']))


@arithmetic
def reconcile(trades, *, authoritative=False):
    ids=[]
    for t in trades:
        d=t.position.decision;entry=t.position.entry.candle_open_time_ms;end=t.exit.candle_open_time_ms
        if (d.symbol not in SYMBOLS or d.signal not in ('LONG','SHORT') or
                not DEV_START<=entry<=end<VALIDATION_START or entry%14400000 or
                d.open_time_ms!=entry or d.signal_timeframe!='4h'):
            raise PermissionError('Only accepted 2024 4h entry boundaries permitted')
        ids.append(d.decision_id)
        gross=(t.exit.price-t.position.entry.price)*t.position.entry.quantity
        if d.signal=='SHORT':gross=-gross
        if (gross!=t.gross_pnl or t.gross_pnl-t.position.entry.fee-t.exit.fee!=t.net_pnl
                or t.exit.quantity!=t.position.entry.quantity):raise ValueError('Trade financial reconciliation failed')
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate trade identities')
    if authoritative:
        ada=[t for t in trades if t.position.decision.symbol=='ADAUSDT' and t.position.decision.signal=='SHORT']
        if (len(trades)!=174 or len(ada)!=29 or sum(t.gross_pnl for t in ada)!=D('189.93465862')
                or sum(t.net_pnl for t in ada)!=D('172.46507486225')):
            raise ValueError('Canonical 174 / ADA SHORT reconciliation failed')


def quarter(timestamp):
    return date(timestamp,'%Y')+'-Q'+str((int(date(timestamp,'%m'))-1)//3+1)


@arithmetic
def feature_fields(snapshot):
    emas={e.span:e for e in snapshot.emas};e=emas[200];close=snapshot.breakout.completed_close
    if close<=0 or snapshot.atr14<=0:raise ValueError('Positive completed close/ATR required')
    ribbon=[emas[k].latest for k in (20,50,100,200)]
    state=snapshot.ribbon
    if state=='NEUTRAL':state='EQUALITY_NEUTRAL' if len(set(ribbon))<4 else 'MIXED'
    slope=e.latest-e.previous
    b=snapshot.breakout
    return dict(symbol=snapshot.symbol,boundary_ms=snapshot.open_time_ms,
        feature_fingerprint=snapshot.snapshot_fingerprint,visible_fingerprint=snapshot.visible_fingerprint,
        ribbon=state,ema200_relation=snapshot.close_vs_ema200,
        ema200_slope='RISING' if slope>0 else 'FALLING' if slope<0 else 'FLAT',
        ema200_slope_value=slope,normalized_atr=snapshot.atr14/close,
        signed_ema200_distance=(close-e.latest)/close,
        bullish_breakout='FRESH' if b.fresh_bullish else 'STALE' if b.bullish else 'FALSE',
        bearish_breakout='FRESH' if b.fresh_bearish else 'STALE' if b.bearish else 'FALSE',
        entry_month=date(snapshot.open_time_ms,'%Y-%m'),entry_quarter=quarter(snapshot.open_time_ms))


def entry_features(view,symbol,timestamp):
    if type(view) is not FeatureView or view.symbol!=symbol:raise PermissionError('Matching authorized feature view required')
    return feature_fields(calculate_features(view.at_open(timestamp)))


@arithmetic
def summary(rows):
    outcomes=[r['outcome'] for r in rows];n=len(rows)
    def total(key):return sum((o[key] for o in outcomes),D(0))
    def pf(key):
        positive=sum((o[key] for o in outcomes if o[key]>0),D(0))
        negative=-sum((o[key] for o in outcomes if o[key]<0),D(0))
        return dict(value=positive/negative if negative else None,
            status='DEFINED' if negative else 'NO_TRADES' if not n else 'NO_LOSSES' if positive else 'NO_NONZERO_OUTCOMES')
    wins=sum(o['net_pnl']>0 for o in outcomes);losses=sum(o['net_pnl']<0 for o in outcomes)
    return dict(trades=n,wins=wins,losses=losses,breakeven=n-wins-losses,
        win_rate_pct=D(wins)/n*100 if n else None,gross_pnl=total('gross_pnl'),net_pnl=total('net_pnl'),
        fees=total('fees'),gross_pf=pf('gross_pnl'),net_pf=pf('net_pnl'),
        gross_expectancy=total('gross_pnl')/n if n else None,net_expectancy=total('net_pnl')/n if n else None,
        median_duration_lower_minutes=distribution(o['duration_lower_minutes'] for o in outcomes)['median'],
        median_duration_upper_minutes=distribution(o['duration_upper_minutes'] for o in outcomes)['median'],
        median_conservative_mfe_pct=distribution(o['mfe_pct'] for o in outcomes)['median'],
        median_conservative_mae_pct=distribution(o['mae_pct'] for o in outcomes)['median'])


def groups(rows,key,labels=()):
    buckets={k:[] for k in labels}
    for r in rows:buckets.setdefault(key(r),[]).append(r)
    return {k:summary(v) for k,v in sorted(buckets.items())}


def partition(value,cutpoints):
    return 'Q'+str(1+sum(value>p for p in cutpoints))


def cohort(rows,name):
    def member(r):
        e=r['entry'];s=e['symbol'];d=e['direction']
        if name=='ALL':return True
        if name=='NON_ADA_SHORT':return not(s=='ADAUSDT' and d=='SHORT')
        return s+' '+d==name
    return [r for r in rows if member(r)]


def regime_value(row,field):
    return row['descriptive'][field] if field=='volatility_quartile' else row['entry'][field]


def comparisons(rows):
    # Enumerate every observed category before inspecting outcome signs.
    return {field:{value:{name:summary([r for r in cohort(rows,name) if regime_value(r,field)==value])
        for name in ('ADAUSDT SHORT','ADAUSDT LONG','BTCUSDT SHORT','ETHUSDT SHORT','NON_ADA_SHORT','ALL')}
        for value in sorted({regime_value(r,field) for r in rows})} for field in REGIMES}


@arithmetic
def temporal(rows):
    months=groups(rows,lambda r:r['outcome']['month'],[f'2024-{m:02d}' for m in range(1,13)])
    quarters=groups(rows,lambda r:r['outcome']['quarter'],[f'2024-Q{q}' for q in range(1,5)])
    ordered=sorted(rows,key=lambda r:(r['outcome']['exit_boundary_ms'],r['trade_id']))
    profitable=[i for i,r in enumerate(ordered) if r['outcome']['net_pnl']>0]
    gaps=[dict(nonprofitable_trades=b-a-1,exit_boundary_gap_days=D(ordered[b]['outcome']['exit_boundary_ms']-
        ordered[a]['outcome']['exit_boundary_ms'])/86400000,from_trade=ordered[a]['trade_id'],to_trade=ordered[b]['trade_id'])
        for a,b in zip(profitable,profitable[1:])]
    return dict(months=months,quarters=quarters,profitable_months=sum(m['net_pnl']>0 for m in months.values()),
        profitable_quarters=sum(q['net_pnl']>0 for q in quarters.values()),
        longest_nonprofitable_sequence_between_winners=max(gaps,key=lambda x:(x['nonprofitable_trades'],x['from_trade']),default=None),
        longest_exit_boundary_gap_between_winners=max(gaps,key=lambda x:(x['exit_boundary_gap_days'],x['from_trade']),default=None),
        leading_nonprofitable_trades=profitable[0] if profitable else len(rows),
        trailing_nonprofitable_trades=len(rows)-1-profitable[-1] if profitable else len(rows))


@arithmetic
def concentration_report(rows,trades):
    total=summary(rows)['net_pnl'];ada=summary(cohort(rows,'ADAUSDT SHORT'))['net_pnl']
    calendar=temporal(rows)
    def best(name):
        k=min(calendar[name],key=lambda k:(-calendar[name][k]['net_pnl'],k));p=calendar[name][k]['net_pnl']
        return dict(period=k,net_pnl=p,share_pct=p/total*100 if total else None,result_excluding=total-p)
    return dict(total_net_pnl=total,ada_short_net_pnl=ada,ada_short_share_pct=ada/total*100 if total else None,
        result_excluding_ada_short=total-ada,best_month=best('months'),best_quarter=best('quarters'),
        tails=concentration(trades))


@arithmetic
def analyze(trades,views,bars,provenance):
    trades=tuple(trades);reconcile(trades)
    rows=[]
    for t in trades:
        d=t.position.decision;entry=entry_features(views[d.symbol],d.symbol,d.open_time_ms)
        entry['direction']=d.signal
        e=excursion(t,bars[d.symbol]);lower,upper=duration(t)
        outcome=dict(gross_pnl=t.gross_pnl,net_pnl=t.net_pnl,fees=t.position.entry.fee+t.exit.fee,
            exit_reason=t.reason.value,exit_boundary_ms=t.exit.candle_open_time_ms,exit_phase=t.exit.phase,
            month=date(t.exit.candle_open_time_ms,'%Y-%m'),quarter=quarter(t.exit.candle_open_time_ms),
            duration_lower_minutes=lower,duration_upper_minutes=upper,
            **{k:e[k] for k in ('mfe_pct','mae_pct','mfe_price','mae_price','mfe_usdt','mae_usdt','exit_extrema_excluded')})
        rows.append(dict(trade_id=d.decision_id,source_trade_fingerprint=t.trade_fingerprint,entry=entry,outcome=outcome))
    rows.sort(key=lambda r:(r['entry']['boundary_ms'],r['trade_id']))
    dist=distribution(r['entry']['normalized_atr'] for r in rows)
    cuts=[dist[k] for k in ('p25','median','p75')]
    for r in rows:r['descriptive']=dict(volatility_quartile=partition(r['entry']['normalized_atr'],cuts))
    tables=dict(symbol=groups(rows,lambda r:r['entry']['symbol']),direction=groups(rows,lambda r:r['entry']['direction']),
        symbol_direction=groups(rows,lambda r:r['entry']['symbol']+' '+r['entry']['direction']))
    tables.update({field+'_direction':groups(rows,lambda r,f=field:regime_value(r,f)+' '+r['entry']['direction']) for field in REGIMES})
    ada=cohort(rows,'ADAUSDT SHORT')
    output=dict(version=VERSION,definitions=DEFINITIONS,provenance=provenance,
        implementation_sha256=hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode()).hexdigest(),
        specification_fingerprint=fingerprint(dict(version=VERSION,definitions=DEFINITIONS,regimes=REGIMES)),
        trade_ids_fingerprint=fingerprint([r['trade_id'] for r in rows]),summary=summary(rows),
        volatility_distribution=dist,quartile_cutpoints=cuts,tables=tables,calendar=temporal(rows),
        ada_short=dict(summary=summary(ada),calendar=temporal(ada),
            regimes={f:groups(ada,lambda r,f=f:regime_value(r,f)) for f in REGIMES},
            distance_from_ema200=distribution(r['entry']['signed_ema200_distance'] for r in ada),
            duration_lower_minutes=distribution(r['outcome']['duration_lower_minutes'] for r in ada),
            exits=groups(ada,lambda r:r['outcome']['exit_reason']),
            mfe_pct=distribution(r['outcome']['mfe_pct'] for r in ada),mae_pct=distribution(r['outcome']['mae_pct'] for r in ada)),
        same_regime_comparison=comparisons(rows),concentration=concentration_report(rows,trades),records=rows)
    output=canonical(output);output['diagnostic_fingerprint']=fingerprint(output)
    return output


def human(report):
    lines=['PHASE 6D4 — PREDECLARED DIAGNOSTIC, NOT A STRATEGY',
        'Source run: '+report['provenance']['run_fingerprint'],
        'Diagnostic fingerprint: '+report['diagnostic_fingerprint'],
        'Calendar: UTC exit attribution. Entry-known and post-trade fields are separate.']
    for section,table in [('SYMBOL x DIRECTION',report['tables']['symbol_direction']),
                          ('ADA SHORT QUARTERS',report['ada_short']['calendar']['quarters']),
                          ('ADA SHORT MONTHS',report['ada_short']['calendar']['months'])]:
        lines.append('\n'+section)
        for name,m in table.items():lines.append(f"{name}: n={m['trades']}, gross={m['gross_pnl']}, net={m['net_pnl']}, net expectancy={m['net_expectancy']}")
    lines.extend(['\nCONCENTRATION',json.dumps(report['concentration'],sort_keys=True),
        '\nAll regime categories and cross-cohort summaries are in report.json; no outcome-based category selection.',
        'No statistical significance, causal conclusion or strategy authorization is implied.'])
    return '\n'.join(lines)+'\n'


def write_report(report,output):
    if fingerprint({k:v for k,v in report.items() if k!='diagnostic_fingerprint'})!=report['diagnostic_fingerprint']:
        raise ValueError('Diagnostic fingerprint mismatch')
    output=Path(output);output.mkdir()  # Existing/partial directory is never overwritten.
    with (output/'report.json').open('xb') as stream:stream.write(encoded(report)+b'\n')
    with (output/'report.txt').open('x',encoding='utf-8',newline='\n') as stream:stream.write(human(report))


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnose-existing-4h',action='store_true',required=True)
    parser.parse_args()
    root=Path('artifacts');folder=root/'research'/EXPERIMENT/'development'
    output=root/'research'/'phase6d4-diagnostics'
    if output.exists():raise FileExistsError('Preserved diagnostics already exist')
    trades,manifest=verify_source(folder)
    views={};bars={};sources={}
    for symbol in SYMBOLS:
        dataset,_=load_local_development(root/'baseline-001'/'historical',symbol)
        if dataset.fingerprint!=manifest['identity']['dataset_fingerprints'][symbol]:raise ValueError('Dataset identity mismatch')
        request=SplitRequest(Role.DEVELOPMENT,DEV_START,VALIDATION_START,DEV_START-99*14400000)
        signal_view=build_view(dataset,symbol,'4h',request)
        if signal_view.fingerprint!=manifest['identity']['view_fingerprints'][symbol]:raise ValueError('4h view identity mismatch')
        history=load_history(root/'research-data'/'phase6d0-warmup-v1',symbol)
        views[symbol]=FeatureView(history,signal_view)
        bars[symbol]={c.open_time_ms:c for c in dataset.candles if DEV_START<=c.open_time_ms<VALIDATION_START}
        sources[symbol]=dict(dataset=dataset.fingerprint,view=signal_view.fingerprint,history=history.history_fingerprint)
    provenance=dict(run_fingerprint=RUN,result_fingerprint=RESULT,trade_population_fingerprint=TRADE_HASH,
        feature_version=FEATURE_VERSION,feature_source_sha256=hashlib.sha256(
            Path('research/features.py').read_text(encoding='utf-8').encode()).hexdigest(),sources=sources)
    report=analyze(trades,views,bars,provenance)
    verify_source(folder)  # Verify authoritative financial artifacts again before publishing.
    write_report(report,output)
    print(human(report))


if __name__=='__main__':main()
