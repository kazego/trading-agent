"""Explicit LIVE-0A.1 operational migration. Never activates on import/read."""
import json
import os
from pathlib import Path
import subprocess

from research.spec import encoded, fingerprint
from forward.model import LABEL, FOUR_HOURS

LEGACY_COMMIT = '9880a91f6fef772a69e696e3bee77cb40e151d9c'
LEGACY_MANIFEST = 'a36d985436303dea4d477e76718cefa9ec6d3155df74d98e2850efcd0139bf6b'
LEGACY_SPEC = '12204ae6263144597640579a5aa6cd31599316341ca56b8bf9019a8d08d407b1'
SEMANTIC_SOURCES = ('forward/model.py','backtest/__init__.py','research/__init__.py',
    'backtest/data.py','research/data.py','research/features.py','research/spec.py','runtime.py','errors.py')


def policy():
    return dict(version='live-0a-operations-v1', poll_interval_ms=900000,
        poll_offset_ms=180000, timely_ms=300000, tolerance_inclusive=True,
        persistence='post-commit clock sample; append-only receipt',
        unconfirmed='UNCONFIRMED until conservative delayed recovery receipt',
        clock_disagreement_ms=30000, max_time_request_ms=10000,
        temporary_http=[408,429,500,502,503,504], next_attempt='next scheduled interval',
        retry_after='persist and respect; no in-process retry', critical='durable BLOCKED; never auto-clear')


def verify_legacy(record):
    from forward.manifest import ROOT
    if record['manifest_fingerprint'] != LEGACY_MANIFEST or fingerprint(record['spec']) != LEGACY_SPEC:
        raise ValueError('Unrecognized historical manifest')
    # Operational modules may change only under the separately approved binding.
    # Numerical/context/data kernels stay whole-file locked to the reviewed commit.
    for path in SEMANTIC_SOURCES:
        old = subprocess.run(['git','show',f'{LEGACY_COMMIT}:{path}'],cwd=ROOT,
                             capture_output=True,check=True).stdout.decode('utf-8')
        if (ROOT/path).read_text(encoding='utf-8') != old.replace('\r\n','\n'):
            raise ValueError('Frozen forward candidate/data/context implementation changed')


def checkpoint(db):
    observations = [[t,d] for t,d in db.execute('SELECT boundary_ms,digest FROM observations ORDER BY boundary_ms')]
    # Prefix digests allow future append-only growth but detect rewritten evidence.
    def prefix(table, columns, order):
        rows = [list(r) for r in db.execute(f'SELECT {columns} FROM {table} ORDER BY {order}')]
        return dict(count=len(rows),fingerprint=fingerprint(rows))
    return dict(observations=observations,
        candles=prefix('candles','symbol,open_ms,payload,digest,received_ms','symbol,open_ms'),
        events=prefix('events','sequence,payload','sequence'),
        candle_ends={s:t for s,t in db.execute('SELECT symbol,max(open_ms) FROM candles GROUP BY symbol')})


def verify_checkpoint(db, saved):
    obs = [[t,d] for t,d in db.execute('SELECT boundary_ms,digest FROM observations ORDER BY boundary_ms LIMIT ?',
                                    (len(saved['observations']),))]
    if obs != saved['observations']: raise ValueError('Historical observations changed')
    rows = []
    for s,end in sorted(saved['candle_ends'].items()):
        rows.extend([list(r) for r in db.execute('SELECT symbol,open_ms,payload,digest,received_ms FROM candles WHERE symbol=? AND open_ms<=? ORDER BY open_ms',(s,end))])
    if dict(count=len(rows),fingerprint=fingerprint(rows)) != saved['candles']:
        raise ValueError('Historical candle prefix changed')
    events=[list(r) for r in db.execute('SELECT sequence,payload FROM events ORDER BY sequence LIMIT ?', (saved['events']['count'],))]
    if dict(count=len(events),fingerprint=fingerprint(events)) != saved['events']:
        raise ValueError('Historical event prefix changed')


def plan(directory, *, effective_ms, now_ms):
    from forward.manifest import load, implementation
    from forward.readout import connect
    m=load(directory, read_only=True)
    verify_legacy(m)
    if type(effective_ms) is not int or effective_ms % FOUR_HOURS or effective_ms < now_ms+300000:
        raise ValueError('Explicit future 4h boundary at least five minutes ahead required')
    with connect(directory,m) as db:
        cp=checkpoint(db)
        if any(json.loads(r[0])['event']=='BLOCKED' for r in db.execute('SELECT payload FROM events')):
            raise ValueError('Cannot migrate a BLOCKED run')
    if (Path(directory)/'BLOCKED.json').exists(): raise ValueError('Run is BLOCKED')
    if cp['observations'] and effective_ms<=cp['observations'][-1][0]:
        raise ValueError('Existing observations cannot be relabeled')
    body=dict(label=LABEL,version='operations-v1',manifest_fingerprint=m['manifest_fingerprint'],
        implementation=implementation(),effective_boundary_ms=effective_ms,policy=policy(),
        policy_fingerprint=fingerprint(policy()),checkpoint=cp)
    return dict(body,operations_fingerprint=fingerprint(body))


def bind(directory, *, effective_ms, now_ms, approved_implementation, approved_plan):
    from runtime import DatabaseLock
    lock=DatabaseLock(Path(directory)/'observer.sqlite')
    lock.acquire()
    try:
        record=plan(directory,effective_ms=effective_ms,now_ms=now_ms)
        if record['implementation']!=approved_implementation or record['operations_fingerprint']!=approved_plan:
            raise ValueError('Operational plan requires exact explicit approval')
        with (Path(directory)/'operations-v1.json').open('xb') as f:
            f.write(encoded(record)+b'\n'); f.flush(); os.fsync(f.fileno())
        return record
    finally: lock.release()


def load_operations(directory, manifest):
    from forward.manifest import implementation
    raw=(Path(directory)/'operations-v1.json').read_bytes()
    r=json.loads(raw)
    required={'label','version','manifest_fingerprint','implementation','effective_boundary_ms',
              'policy','policy_fingerprint','checkpoint','operations_fingerprint'}
    if (set(r)!=required or raw!=encoded(r)+b'\n' or r['label']!=LABEL or r['version']!='operations-v1'
            or fingerprint({k:v for k,v in r.items() if k!='operations_fingerprint'})!=r['operations_fingerprint']
            or r['manifest_fingerprint']!=manifest['manifest_fingerprint'] or r['implementation']!=implementation()
            or r['policy']!=policy() or r['policy_fingerprint']!=fingerprint(policy())
            or type(r['effective_boundary_ms']) is not int or r['effective_boundary_ms']%FOUR_HOURS
            or r['effective_boundary_ms']<manifest['first_observation_ms']
            or any(t>=r['effective_boundary_ms'] for t,d in r['checkpoint']['observations'])):
        raise ValueError('Operational authorization/identity mismatch')
    return r


def block(directory, code='CRITICAL'):
    """Independent durable latch even when the database/manifest cannot be opened."""
    p=Path(directory)
    if not p.is_dir(): return
    try:
        with (p/'BLOCKED.json').open('xb') as f:
            f.write(encoded(dict(label=LABEL,state='BLOCKED',code=code))+b'\n')
            f.flush(); os.fsync(f.fileno())
    except FileExistsError: pass
