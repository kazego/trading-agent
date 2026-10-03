"""Bounded local consumer. No collector, daemon or network dependency."""
import sqlite3
import subprocess
import time
from pathlib import Path

from paper.operations import ROOT, paths, exclusive, policy
from paper.model import LABEL
from paper.source import SourceAdapter
from paper.market import CandleSource
from paper.ledger import PaperLedger
from paper.journal import source_available
from paper.verified_source import capture
from paper.authorization import load_execution
from research.spec import encoded, fingerprint
from runtime import DatabaseLock


def utc_ms(): return time.time_ns()//1000000


def source_progress(source):
    """Read existing source rows only, after normal source authorization."""
    from forward.manifest import load as source_manifest
    from forward.readout import connect
    from backtest.data import SYMBOLS
    result={}
    with connect(source._directory,source_manifest(source._directory)) as db:
        for symbol in SYMBOLS:
            first,last,count=db.execute('SELECT min(open_ms),max(open_ms),count(*) FROM candles WHERE symbol=?',(symbol,)).fetchone()
            if count and (first%900000 or last%900000 or count!=(last-first)//900000+1):
                raise ValueError('Source candle continuity failure')
            result[symbol]=dict(first_open_ms=first,last_open_ms=last,count=count)
    return result


def consume(run_id, *, root=ROOT, clock=utc_ms):
    start_mono=time.monotonic()  # Budget includes authorization and source verification.
    # Missing authorization must not create even an empty operational directory.
    root=Path(root)
    control,ledger_path=paths(run_id,root)
    if not (control/'activation.json').is_file():
        raise ValueError('Not activated; executable processing refused')
    for filename in ('consumer.lock','operations.sqlite'):
        path=control/filename
        if path.is_symlink() or path.exists() and path.stat().st_nlink!=1:
            raise ValueError('Aliased operational file forbidden')
    lock=DatabaseLock(control/'consumer')
    lock.acquire()  # Overlap refusal must not latch BLOCKED on the legitimate owner.
    db=None; ledger=None; invocation=None
    try:
        if (control/'BLOCKED.json').exists(): raise ValueError('Consumer is BLOCKED')
        auth=load_execution(run_id,root)
        started=clock()
        if started<auth.record['t0_ms']:
            return dict(label=LABEL,state='WAITING_FOR_T0')
        db=sqlite3.connect(control/'operations.sqlite')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS invocations(sequence INTEGER PRIMARY KEY, started_ms INTEGER NOT NULL, finished_ms INTEGER, state TEXT NOT NULL, source_boundary_ms INTEGER, transitions INTEGER NOT NULL, source_progress TEXT)')
        prior=db.execute('SELECT count(*) FROM invocations').fetchone()[0]
        if (prior or ledger_path.exists()) and not (ledger_path/'paper.sqlite').is_file():
            raise ValueError('Previously initialized or interrupted ledger is missing; never recreate')
        if (ledger_path/'paper.sqlite').exists():
            probe=sqlite3.connect((ledger_path/'paper.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
            try:
                if probe.execute('SELECT count(*) FROM identity').fetchone()!=(1,):
                    raise ValueError('Existing ledger has no unique identity')
            finally: probe.close()
        cursor=db.execute("INSERT INTO invocations(started_ms,state,transitions) VALUES(?,'RUNNING',0)",(started,))
        invocation=cursor.lastrowid; db.commit()
        source_path=root/'artifacts'/'forward-paper'/'live-0a-001'
        source=SourceAdapter(source_path,auth.source)
        market=CandleSource(source_path,auth.source)
        verified=capture(source_path,auth.source,timeout=max(0.001,min(15,
            policy()['max_runtime_seconds']-(time.monotonic()-start_mono))))
        items=verified.snapshot()
        progress=verified.progress
        boundaries=[o.boundary_ms for o in items]
        if boundaries!=sorted(set(boundaries)): raise ValueError('Source chronology/duplicates')
        eligible=[o for o in items if o.boundary_ms>auth.record['t0_ms']]
        expected=auth.record['t0_ms']+14400000
        for item in eligible:
            if item.boundary_ms!=expected: raise ValueError('Missing prospective source boundary')
            expected+=14400000
        ledger=PaperLedger(ledger_path,auth.binding,auth.rules,source,market,
            verified_source=verified,authorization=auth.ledger_authorization)
        state=ledger.reconcile(); ledger._sources(state)
        count=0
        def authorization_guard():
            auth.guard()
        def budget():
            return count<policy()['max_transitions'] and time.monotonic()-start_mono<policy()['max_runtime_seconds']
        def drain(before):
            nonlocal state,count
            while state.slot[0] in ('PENDING','OPEN') and budget():
                owner=state.slot[1]; reservation=state.reservations[owner]
                if reservation['state']!='PENDING_ENTRY' and state.slot[0]=='PENDING': break
                r=reservation['reservation']
                if state.slot[0]=='PENDING': stamp=r['entry_boundary_ms']
                else:
                    p=state.positions[owner]
                    stamp=p.entry.candle_open_time_ms if p.last_processed_ms is None else p.last_processed_ms+900000
                if stamp>=before: return True
                item=verified.at(r['symbol'],stamp)
                if item is None:
                    # A later existing candle proves a gap; never skip it.
                    last=progress[r['symbol']]['last_open_ms']
                    if last is not None and last>=stamp:
                        raise ValueError('Missing execution candle')
                    return True
                if item.received_ms>clock(): raise ValueError('Source receipt is in the future')
                authorization_guard()
                if state.slot[0]=='PENDING': ledger.settle_entry(owner)
                else: ledger.process_candle(owner,stamp)
                count+=1; state=ledger.reconcile()
            return budget()
        for item in eligible:
            if item.boundary_ms in state.batches: continue
            if not budget() or not drain(item.boundary_ms): break
            if source_available(item.record)>=clock(): break
            authorization_guard()
            auth.accept(ledger,item.boundary_ms)
            count+=1; state=ledger.reconcile()
        else:
            drain(clock()//900000*900000)
        verified.guard()
        # Revalidate immutable consumer authorization and source bytes before exit.
        auth.guard()
        finished=clock()
        if finished<started: raise ValueError('Consumer clock regressed')
        health='BUDGET_REACHED' if not budget() else ('WAITING' if not count else 'HEALTHY')
        if state.slot[0]=='BLOCKED' or any(r['state']=='UNCONFIRMED_ACCEPTANCE' for r in state.reservations.values()):
            health='UNRESOLVED_EXPOSURE'
        latest=boundaries[-1] if boundaries else None
        db.execute('UPDATE invocations SET finished_ms=?,state=?,source_boundary_ms=?,transitions=?,source_progress=? WHERE sequence=?',
                   (finished,health,latest,count,encoded(progress).decode(),invocation)); db.commit()
        return dict(label=LABEL,state=health,transitions=count,source_boundary_ms=latest)
    except subprocess.TimeoutExpired:
        # Bounded historical read exhausted its allowance; no unverified evidence
        # is executable. This is not evidence corruption and does not clear latches.
        if db is not None and invocation is not None:
            db.rollback()
            db.execute("UPDATE invocations SET finished_ms=?,state='BUDGET_REACHED' WHERE sequence=?",(clock(),invocation));db.commit()
        return dict(label=LABEL,state='BUDGET_REACHED',transitions=0)
    except Exception:
        # Never repair/release exposure; B4 owns financial durability.
        if control.exists():
            try: exclusive(control/'BLOCKED.json',dict(label=LABEL,state='BLOCKED',code='AUTHORIZATION_SOURCE_OR_LEDGER_FAILURE'))
            except FileExistsError: pass
        if db is not None and invocation is not None:
            db.rollback()
            db.execute("UPDATE invocations SET state='BLOCKED' WHERE sequence=?",(invocation,)); db.commit()
        raise
    finally:
        if ledger is not None: ledger.close()
        if db is not None: db.close()
        lock.release()
