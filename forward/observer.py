"""Bounded public collection and synchronized observations; no trade simulation."""
import time
import json
from pathlib import Path

from backtest.data import SYMBOLS, STEP_MS
from forward import public_feed
from forward.model import LABEL, signal_snapshot, context_snapshot
from forward.manifest import load
from forward.store import Store


def observe_ready(store, *, observed_ms, clock=None, operations=None):
    """Only durable, completed data. Signal calculation precedes context creation."""
    count = 0
    while store.next_boundary() <= min(store.next_candle(s) for s in SYMBOLS):
        boundary = store.next_boundary()
        if boundary > observed_ms: raise ValueError('Future observation denied')
        histories = {s:store.history(s,boundary) for s in SYMBOLS}
        decisions = [signal_snapshot(histories[s],s,boundary) for s in SYMBOLS]
        contexts = {s:context_snapshot(histories[s],s,boundary) for s in SYMBOLS}
        # All signals are recorded. Priority is metadata only until an execution
        # phase exists: no accepted/blocked entries, sizing, positions or P&L.
        record = dict(label=LABEL, manifest_fingerprint=store.manifest['manifest_fingerprint'],
            boundary_ms=boundary, observed_ms=observed_ms,
            timing=('ONLINE' if observed_ms-boundary <= store.manifest['spec']['max_online_lateness_ms']
                    else 'DELAYED_RECONSTRUCTED'),
            evidence_role='PROSPECTIVE_INTERVAL', decisions=decisions, descriptive_context=contexts)
        store.observation(record,clock=clock,operations=operations)
        count += 1
    return count


def poll(directory):
    """EXPLICIT network entry point. Never called on import or automatically."""
    from forward.operations import block, load_operations, verify_checkpoint
    from forward.timing import Clock
    from errors import RuntimeOwnershipError
    directory=Path(directory)
    if (directory/'BLOCKED.json').exists():
        raise ValueError('Run is BLOCKED; no network access')
    store=None
    try:
        manifest=load(directory)  # Exact migration binding BEFORE network.
        operations=load_operations(directory,manifest) if (directory/'operations-v1.json').exists() else None
        store=Store(directory,manifest)
        if operations: verify_checkpoint(store.db,operations['checkpoint'])
        from forward.readout import records
        list(records(store.db,operations))  # Validate existing persistence receipts.
        if store.db.execute("SELECT 1 FROM events WHERE payload LIKE '%\"event\":\"BLOCKED\"%' LIMIT 1").fetchone():
            raise ValueError('Run is blocked; preserve evidence for operator review')
        wall=time.time_ns()//1_000_000
        # Backoff is a durable event; normal scheduled calls do not bypass it.
        events=[json.loads(r[0]) for r in store.db.execute('SELECT payload FROM events ORDER BY sequence')]
        retry=max((e.get('retry_not_before_ms',0) for e in events),default=0)
        if wall<retry:
            store.event('BACKOFF',observed_ms=wall,retry_not_before_ms=retry)
            return dict(label=LABEL,health='BACKOFF',retry_not_before_ms=retry)
        store.event('START',observed_ms=wall)
        request_start=time.monotonic_ns()
        now = public_feed.server_time()
        local_now = time.time_ns()//1_000_000
        if abs(local_now-now) > 30000 or now < manifest['created_ms']:
            raise ValueError('Clock disagreement; observation blocked')
        clock=Clock(now,request_start) if operations else None
        end = now//STEP_MS*STEP_MS
        if end < manifest['history_start_ms']:
            raise ValueError('Manifest history is in the future')
        for symbol in SYMBOLS:
            for _ in range(10):  # At most 30 bounded kline pages per invocation.
                start = store.next_candle(symbol)
                if start >= end: break
                page = public_feed.closed_klines(symbol,start,end,now=now)
                store.append(symbol,page,received_ms=clock.now() if clock else max(now,time.time_ns()//1_000_000))
        if clock:
            clock.reconcile(public_feed.server_time())
            # Recover missing receipts WITHOUT regenerating or updating decisions.
            confirmed={e['observation_fingerprint'] for e in events if e['event'] in ('RECEIPT','RECOVERED_RECEIPT')}
            for raw,digest in store.db.execute('SELECT payload,digest FROM observations ORDER BY boundary_ms').fetchall():
                r=json.loads(raw)
                if r['timing']=='UNCONFIRMED' and digest not in confirmed:
                    stamp=clock.now()
                    if stamp<r['observed_ms']: raise ValueError('Recovery clock precedes observation')
                    store.event('RECOVERED_RECEIPT',observed_ms=stamp,boundary_ms=r['boundary_ms'],
                        observation_fingerprint=digest,timing='DELAYED_RECONSTRUCTED',
                        operations_fingerprint=r['operations_fingerprint'],code='COMMIT_TIME_UNKNOWN')
        observed_ms = clock.now() if clock else max(now,time.time_ns()//1_000_000)
        count = observe_ready(store,observed_ms=observed_ms,clock=clock,operations=operations)
        store.event('STOP',observed_ms=clock.now() if clock else time.time_ns()//1_000_000)
        return dict(label=LABEL, observations_written=count, next_boundary_ms=store.next_boundary(),
                    collected_through_ms=min(store.next_candle(s) for s in SYMBOLS)-1)
    except RuntimeOwnershipError:
        return dict(label=LABEL,health='BUSY',observations_written=0)
    except public_feed.TemporaryPublicError as exc:
        try:
            if store is None: raise ValueError('No durable journal')
            stamp=time.time_ns()//1_000_000
            store.event('RECOVERABLE',observed_ms=stamp,code='TEMPORARY_PUBLIC_FAILURE',
                        retry_not_before_ms=stamp+exc.retry_after_s*1000)
            return dict(label=LABEL,health='RECOVERABLE',observations_written=0,
                        retry_not_before_ms=stamp+exc.retry_after_s*1000)
        except Exception:
            block(directory,'RECOVERY_JOURNAL_FAILURE')
            raise ValueError('Cannot durably record recoverable failure') from None
    except Exception:
        if not directory.exists(): raise
        block(directory)
        if store:
            try: store.event('BLOCKED',observed_ms=time.time_ns()//1_000_000)
            except Exception: pass  # Independent fsynced latch remains authoritative.
        raise ValueError('Observer blocked; preserve run and inspect integrity before further use') from None
    finally:
        if store: store.close()
