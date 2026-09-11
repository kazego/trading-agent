"""Bounded public collection and synchronized observations; no trade simulation."""
import time

from backtest.data import SYMBOLS, STEP_MS
from forward import public_feed
from forward.model import LABEL, signal_snapshot, context_snapshot
from forward.manifest import load
from forward.store import Store


def observe_ready(store, *, observed_ms):
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
        store.observation(record)
        count += 1
    return count


def poll(directory):
    """EXPLICIT network entry point. Never called on import or automatically."""
    manifest = load(directory)  # Identity checked BEFORE any network request.
    store = Store(directory, manifest)
    try:
        if store.db.execute("SELECT 1 FROM events WHERE payload LIKE '%\"event\":\"BLOCKED\"%' LIMIT 1").fetchone():
            raise ValueError('Run is blocked; preserve evidence for operator review')
        store.event('START')
        now = public_feed.server_time()
        local_now = time.time_ns()//1_000_000
        if abs(local_now-now) > 30000 or now < manifest['created_ms']:
            raise ValueError('Clock disagreement; observation blocked')
        end = now//STEP_MS*STEP_MS
        if end < manifest['history_start_ms']:
            raise ValueError('Manifest history is in the future')
        for symbol in SYMBOLS:
            for _ in range(10):  # At most 30 bounded kline pages per invocation.
                start = store.next_candle(symbol)
                if start >= end: break
                page = public_feed.closed_klines(symbol,start,end,now=now)
                store.append(symbol,page,received_ms=max(now,time.time_ns()//1_000_000))
        observed_ms = max(now, time.time_ns()//1_000_000)
        count = observe_ready(store,observed_ms=observed_ms)
        store.event('STOP')
        return dict(label=LABEL, observations_written=count, next_boundary_ms=store.next_boundary(),
                    collected_through_ms=min(store.next_candle(s) for s in SYMBOLS)-1)
    except Exception:
        store.event('BLOCKED')
        raise ValueError('Observer blocked; preserve run and inspect integrity before further use') from None
    finally:
        store.close()
