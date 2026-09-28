"""Snapshot reports using SQLite mode=ro and query_only; no source/network reads."""
from contextlib import contextmanager
from decimal import Decimal as D, localcontext
import sqlite3

from paper.operations import ROOT,load,paths
from paper.ledger import PaperLedger
from paper.model import LABEL,parse
from research.spec import canonical


@contextmanager
def connect(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()!=('ok',): raise ValueError('Database integrity')
        yield db
    finally: db.close()


def report(run_id, command, *, root=ROOT, now_ms):
    if command not in ('status','latest','trades','missed','summary'): raise ValueError('Unknown report')
    auth=load(run_id,root); control,path=paths(run_id,root)
    warnings=[]
    for directory in (control,path):
        if (directory/'BLOCKED.json').exists(): warnings.append(parse((directory/'BLOCKED.json').read_bytes().rstrip(b'\n')))
    base=dict(label=LABEL,run_id=run_id,activation=auth.id,t0_ms=auth.record['t0_ms'],warnings=warnings)
    invocations=[]
    if (control/'operations.sqlite').exists():
        with connect(control/'operations.sqlite') as db:
            invocations=db.execute('SELECT * FROM invocations ORDER BY sequence').fetchall()
    base.update(last_attempt=invocations[-1] if invocations else None,
                last_success=next((r for r in reversed(invocations) if r[2] is not None and r[3]!='BLOCKED'),None))
    if not (path/'paper.sqlite').exists():
        if invocations or path.exists():
            return dict(base,health='BLOCKED' if warnings else 'INCOMPLETE_OR_MISSING_LEDGER',
                        realized_equity=None,slot=None)
        return dict(base,health='BLOCKED' if warnings else 'AUTHORIZED_NOT_STARTED',realized_equity='1000',slot=['EMPTY',None,None])
    with connect(path/'paper.sqlite') as db:
        # Reuse B4's pure verification routine without constructing a writable Store,
        # acquiring its lock, initializing schemas or invoking its latching guard.
        identity_row=db.execute('SELECT digest,payload FROM identity').fetchone()
        identity=parse(identity_row[1].encode())
        from paper.journal import implementation,HISTORICAL
        from research.spec import fingerprint
        expected=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=implementation(),
            historical=HISTORICAL,binding=auth.binding.record,rules=auth.rules.record,
            activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
            initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=auth.record['t0_ms']))
        if identity!=expected or identity_row[0]!=fingerprint(expected): raise ValueError('Ledger activation binding mismatch')
        verifier=object.__new__(PaperLedger)
        verifier._db=db; verifier.identity=identity; verifier.identity_id=identity_row[0]
        verifier.binding=auth.binding; verifier.rules=auth.rules
        state=verifier._verify()
    with localcontext() as ctx:
        ctx.prec=50
        trades=[]
        for owner,t in state.trades.items():
            r=state.reservations[owner]['reservation']; e=next(e for e in state.accounting if e['owner']==owner)
            trades.append(dict(owner=owner,symbol=r['symbol'],direction=r['direction'],entry=t.position.record['entry'],
                **t.record,fees=t.position.entry.fee+t.exit.fee,equity_before=e['equity_before'],equity_after=e['equity_after']))
        decisions=[state.result(t) for t in sorted(state.batches)]
        dispositions=[d for item in decisions for d in item['effective_dispositions']]
        counts={}
        for d in dispositions: counts[d['reason']]=counts.get(d['reason'],0)+1
        mtm=None
        if state.slot[0]=='OPEN':
            owner=state.slot[1]; p=state.positions[owner]
            keys=[(stamp,key) for (o,stamp),key in state.processed.items() if o==owner]
            if keys:
                stamp,key=max(keys); close=D(state.candles[key]['candle']['close'])
                sign=D(1) if p.pending.signal=='LONG' else D(-1)
                mtm=dict(open_ms=stamp,equity=state.equity+sign*(close-p.entry.price)*p.entry.quantity,
                         semantics='CANDLE_CLOSE_PRICE_ONLY_ENTRY_FEES_DEFERRED')
        peak=D('1000'); dd=D(0)
        for e in state.accounting:
            value=D(e['equity_after']); peak=max(peak,value); dd=max(dd,(peak-value)/peak)
        summary=dict(realized_equity=state.equity,closed_trades=len(trades),
            wins=sum(t.net_pnl>0 for t in state.trades.values()),losses=sum(t.net_pnl<0 for t in state.trades.values()),
            gross_pnl=sum((t.gross_pnl for t in state.trades.values()),D(0)),
            net_pnl=sum((t.net_pnl for t in state.trades.values()),D(0)),
            fees=sum((t.position.entry.fee+t.exit.fee for t in state.trades.values()),D(0)),
            realized_drawdown_fraction=dd,mtm_drawdown=None,mtm_limitation='No complete persisted portfolio MTM series',
            dispositions=counts,selected_reservations=len(state.reservations),
            accepted=sum(c['status']=='PENDING_ENTRY' for c in state.confirmations.values()),
            online_actionable=sum(d['signal']!='NONE' for batch in state.batches.values()
                if batch['source']['receipt'] and batch['source']['receipt']['timing']=='ONLINE'
                for d in batch['source']['observation']['decisions']))
        if command=='summary': return canonical(dict(base,**summary))
        if command=='trades': return canonical(dict(base,trades=trades))
        if command=='missed':
            return canonical(dict(base,decisions=[x for x in decisions if any(d['reason'] not in ('NONE','PENDING_ENTRY') for d in x['effective_dispositions'])],
                                  rejections=[r for r in state.reservations.values() if r['state'].startswith('REJECTED')]))
        if command=='latest':
            return canonical(dict(base,decision=decisions[-1] if decisions else None,
                source=state.batches[max(state.batches)]['source'] if state.batches else None,
                reservations=state.reservations,positions={k:v.record for k,v in state.positions.items()},trades=trades))
        last=base['last_success']
        health='BLOCKED' if warnings else 'STALE' if not last or now_ms-last[2]>1200000 else 'HEALTHY'
        progress=parse(last[6].encode()) if last and last[6] else None
        stale_data=not progress or any(v['last_open_ms'] is None or now_ms-v['last_open_ms']-900000>1200000 for v in progress.values())
        if health=='HEALTHY' and stale_data: health='STALE_SOURCE'
        if not warnings and base['last_attempt'] and base['last_attempt'][3]=='RUNNING':
            health='INCOMPLETE_OR_RUNNING_INVOCATION'
        if state.slot[0]=='BLOCKED': health='BLOCKED'
        elif not warnings and any(r['state']=='UNCONFIRMED_ACCEPTANCE' for r in state.reservations.values()):
            health='UNRESOLVED_ACCEPTANCE'
        return canonical(dict(base,health=health,realized_equity=state.equity,mtm=mtm,slot=state.slot,
            financial_available_ms=state.financial_available_ms,state_available_ms=state.state_available_ms,
            cursors={k:v.last_processed_ms for k,v in state.positions.items()},
            source_boundary_ms=last[4] if last else None,source_progress=progress,source_stale=stale_data,
            unresolved_exposure=state.slot[0]!='EMPTY',
            overdue_source=not last or last[4] is None or now_ms-last[4]>14400000+1200000))
