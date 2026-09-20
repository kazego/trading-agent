"""SQLite read-only snapshots; no client imports, new signals, writes or repair."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from backtest.data import SYMBOLS
from research.spec import fingerprint
from forward.model import LABEL, FOUR_HOURS
from forward.store import Store


@contextmanager
def connect(directory, manifest):
    path=(Path(directory)/'observer.sqlite').resolve()
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('SELECT fingerprint FROM identity').fetchall()!=[(manifest['manifest_fingerprint'],)]:
            raise ValueError('Database manifest binding mismatch')
        Store.verify(SimpleNamespace(db=db,manifest=manifest))  # SELECT-only existing validator.
        yield db
    finally: db.close()


def records(db, operations=None):
    observations=[json.loads(r[0]) for r in db.execute('SELECT payload FROM observations ORDER BY boundary_ms')]
    events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM events ORDER BY sequence')]
    receipts={}
    for e in events:
        if e.get('event') in ('RECEIPT','RECOVERED_RECEIPT'):
            key=e['observation_fingerprint']
            if key in receipts: raise ValueError('Duplicate persistence receipt')
            receipts[key]=e
    for o in observations:
        digest=fingerprint(o)
        receipt=receipts.pop(digest,None)
        if operations and 'operations_fingerprint' in o:
            if o['operations_fingerprint']!=operations['operations_fingerprint']:
                raise ValueError('Observation operational binding mismatch')
            if receipt and receipt['event']=='RECEIPT':
                tolerance=(operations['policy']['timely_ms'] if o['boundary_ms']>=operations['effective_boundary_ms'] else 120000)
                if receipt['tolerance_ms']!=tolerance: raise ValueError('Receipt tolerance changed')
        if receipt:
            if (o['timing']!='UNCONFIRMED' or receipt['boundary_ms']!=o['boundary_ms']
                    or receipt['observed_ms']<o['observed_ms']
                    or receipt['operations_fingerprint']!=o['operations_fingerprint']):
                raise ValueError('Invalid persistence receipt')
            from forward.timing import classify
            expected=('DELAYED_RECONSTRUCTED' if receipt['event']=='RECOVERED_RECEIPT'
                      else classify(o['boundary_ms'],receipt['observed_ms'],receipt['tolerance_ms']))
            if receipt['timing']!=expected: raise ValueError('Incorrect receipt classification')
            o=dict(o,effective_timing=expected,persistence_receipt=receipt)
        else: o=dict(o,effective_timing=o['timing'])
        yield o,events
    if receipts: raise ValueError('Orphan persistence receipt')


def report(directory, command, *, now_ms):
    from forward.manifest import load
    from forward.operations import LEGACY_SPEC, load_operations, verify_checkpoint
    if command not in ('status','latest','summary'): raise ValueError('Read-only command required')
    m=load(directory,read_only=True)
    ops=None
    if (Path(directory)/'operations-v1.json').exists(): ops=load_operations(directory,m)
    with connect(directory,m) as db:
        if ops: verify_checkpoint(db,ops['checkpoint'])
        obs=[o for o,e in records(db,ops)]
        events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM events ORDER BY sequence')]
        latest={s:t+900000 for s,t in db.execute('SELECT symbol,max(open_ms) FROM candles GROUP BY symbol')}
    blocked=(Path(directory)/'BLOCKED.json').exists() or any(e['event']=='BLOCKED' for e in events)
    common=min(latest.values()) if set(latest)==set(SYMBOLS) else None
    successes=[e['observed_ms'] for e in events if e['event']=='STOP' and 'observed_ms' in e]
    next_boundary=obs[-1]['boundary_ms']+FOUR_HOURS if obs else m['first_observation_ms']
    warnings=[]
    if not successes: warnings.append('LEGACY_LAST_SUCCESS_TIME_UNKNOWN')
    if any(o['effective_timing']=='UNCONFIRMED' for o in obs): warnings.append('UNCONFIRMED_PERSISTENCE_RECEIPT')
    if ops is None and fingerprint(m['spec'])==LEGACY_SPEC: warnings.append('OPERATIONS_MIGRATION_REQUIRED')
    expected_close=(now_ms-180000)//900000*900000
    stale=common is None or common<expected_close or now_ms>=next_boundary+300000
    last=events[-1]['event'] if events else None
    health=('BLOCKED' if blocked else 'MIGRATION_REQUIRED' if 'OPERATIONS_MIGRATION_REQUIRED' in warnings
            else last if last in ('RECOVERABLE','BACKOFF') else 'STALE' if stale else 'HEALTHY')
    base=dict(label=LABEL,manifest_fingerprint=m['manifest_fingerprint'],health=health,stale=stale,warnings=warnings)
    if command=='status':
        return dict(base,last_successful_poll_ms=max(successes) if successes else None,
            latest_candle_boundaries_ms=latest,common_completed_15m_boundary_ms=common,
            common_completed_4h_boundary_ms=common//FOUR_HOURS*FOUR_HOURS if common else None,
            next_unrecorded_4h_boundary_ms=next_boundary,scored_observations=len(obs),
            last_event=events[-1] if events else None,
            operations_fingerprint=ops['operations_fingerprint'] if ops else None)
    if command=='latest': return dict(base,observation=obs[-1] if obs else None)
    counts={s:{side:0 for side in ('LONG','SHORT','NONE')} for s in SYMBOLS}
    timing={}
    for o in obs:
        for d in o['decisions']: counts[d['symbol']][d['signal']]+=1
        key=o.get('operations_fingerprint','LEGACY_120000_MS')
        if 'operations_fingerprint' in o:
            if ops is None or key!=ops['operations_fingerprint']: raise ValueError('Unbound observation policy')
            tolerance=ops['policy']['timely_ms'] if o['boundary_ms']>=ops['effective_boundary_ms'] else 120000
            key+=':'+str(tolerance)+'_MS'
        bucket=timing.setdefault(key,{'ONLINE':0,'DELAYED_RECONSTRUCTED':0,'UNCONFIRMED':0})
        bucket[o['effective_timing']]+=1
    return dict(base,t0_ms=m['t0_ms'],scored_observations=len(obs),symbol_decisions=sum(sum(c.values()) for c in counts.values()),
        signals=counts,timing_by_policy=timing,first_boundary_ms=obs[0]['boundary_ms'] if obs else None,
        last_boundary_ms=obs[-1]['boundary_ms'] if obs else None,
        overdue_boundaries=max(0,(now_ms-300000-next_boundary)//FOUR_HOURS+1))


def safe_report(directory, command, *, now_ms):
    """Sanitized read failure; never latches or repairs from a reporting command."""
    try: return report(directory,command,now_ms=now_ms)
    except Exception:
        return dict(label=LABEL,health='INTEGRITY_OR_AUTHORIZATION_FAILURE',
                    warnings=['LOCAL_READ_VERIFICATION_FAILED'],read_only=True)
