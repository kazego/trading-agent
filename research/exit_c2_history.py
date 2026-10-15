"""Isolated A/C2 historical integration. No CLI, acquisition or operational store.

Availability model is IDEALIZED: completed bars available at close; signal receipt
S+3m, decision S+4m, confirmation +1ms; reversal decision after close receipt +1ms.
These assumptions are not reconstructed forward timing evidence. No forced exit.
"""
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from backtest.data import Candle, SYMBOLS
from backtest.simulation import arithmetic
from forward.model import HISTORY, completed, signal_snapshot
from research.exit_c2 import Coordinator, Signal, STEP, FOUR_H
from research.spec import canonical, fingerprint


@dataclass(frozen=True)
class History:
    start: int
    end: int
    candles: tuple
    provenance: str

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or self.start % FOUR_H or self.end % FOUR_H or self.end <= self.start:
            raise ValueError('Aligned nonempty evaluation interval required')
        object.__setattr__(self, 'candles', tuple(self.candles))
        if not isinstance(self.provenance, str) or not self.provenance:
            raise ValueError('Explicit provenance required')
        if any(type(c) is not Candle or c.symbol not in SYMBOLS for c in self.candles):
            raise ValueError('Only validated candles')
        for symbol in SYMBOLS:
            completed(tuple(c for c in self.candles if c.symbol == symbol), symbol,
                      self.start-HISTORY, self.end)

    @property
    def identity(self):
        return fingerprint(dict(start=self.start, end=self.end, provenance=self.provenance,
                                candles=[c.record() for c in self.candles]))

    def coverage(self):
        return dict(start=self.start, end=self.end, warmup_start=self.start-HISTORY,
                    per_symbol=(self.end-self.start+HISTORY)//STEP, gaps=0, duplicates=0,
                    identity=self.identity)


def load_reviewed_files(root, files, *, start, end):
    """Read only explicit JSON archives beneath a strictly resolved research root.

    files = [(relative_path, independently reviewed raw SHA256), ...]. Each JSON
    must contain `candles` records in Candle.record format. No SQLite, discovery,
    downloads or writes. Hash approval/provenance is the caller's responsibility;
    this is not an authorization to use an unreviewed research period.
    """
    root = Path(root).resolve(strict=True)
    rows, inventory, seen = [], [], set()
    for relative, expected in files:
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts or rel.suffix.lower() != '.json':
            raise ValueError('Relative JSON research files only')
        path = (root/rel).resolve(strict=True)
        if not path.is_relative_to(root) or path in seen:
            raise ValueError('Escaped or duplicate archive')
        seen.add(path)
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError('Reviewed SHA256 required')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Archive hash mismatch')
        body = json.loads(raw)
        rows.extend(Candle(**r) for r in body['candles'])
        inventory.append(dict(path=rel.as_posix(), sha256=expected))
    # Files may be month partitions; reject duplicates rather than deduplicating.
    rows.sort(key=lambda c: (SYMBOLS.index(c.symbol), c.open_time_ms))
    return History(start, end, tuple(rows), json.dumps(inventory, sort_keys=True))


def signals_at(series, boundary):
    """Only preceding completed context reaches the unchanged signal kernel."""
    result = []
    for symbol in SYMBOLS:
        cs = series[symbol]
        offset = (boundary-HISTORY-cs[0].open_time_ms)//STEP
        snap = signal_snapshot(cs[offset:offset+HISTORY//STEP], symbol, boundary)
        result.append(Signal(symbol, boundary, snap['signal'], D(snap['atr14']),
                             boundary+180000, 'ONLINE', snap['signal_fingerprint']))
    return tuple(result)


def _samples(t):
    return (dict(utc_ms=t, before_ns=0, after_ns=0),
            dict(utc_ms=t+1, before_ns=1000000, after_ns=1000000))


@arithmetic
def run(history, rules, policy):
    """Caller-selected in-memory evaluation; does not load or write any file."""
    if type(history) is not History:
        raise ValueError('Validated History required')
    series = {s: tuple(c for c in history.candles if c.symbol == s) for s in SYMBOLS}
    coordinator = Coordinator(policy, rules, history.start-FOUR_H)
    marks = [(history.start, D('1000'))]
    for opening in range(history.start, history.end, STEP):
        if opening % FOUR_H == 0:
            coordinator.observe(signals_at(series, opening), *_samples(opening+240000))
        selected = (coordinator.pending[0].symbol if coordinator.pending else
                    coordinator.position.decision.symbol if coordinator.position else None)
        if selected and (not coordinator.pending or coordinator.pending[1] == opening):
            candle = series[selected][(opening-series[selected][0].open_time_ms)//STEP]
            old_reversal = coordinator.reversal
            coordinator.bar(candle, opening+STEP)
            if coordinator.reversal and coordinator.reversal != old_reversal:
                coordinator.decide_reversal(*_samples(opening+STEP+1))
        value = coordinator.equity
        if coordinator.position:
            p = coordinator.position
            close = series[p.decision.symbol][(opening-series[p.decision.symbol][0].open_time_ms)//STEP].close
            value += (close-p.entry.price)*p.entry.quantity*(1 if p.decision.direction == 'LONG' else -1)-p.entry.fee
        marks.append((opening+STEP, value))
    return coordinator, report(coordinator, marks, history)


@arithmetic
def report(c, marks, history):
    """MTM at 15m CLOSE only, entry fees deducted; no hypothetical exit fees.

    Slippage is embedded in fills and reported informationally, never deducted
    twice. Realized equity follows B4 (open entry fee realized upon closure).
    """
    trades = c.trades
    pnls = [D(t['net_pnl']) for t in trades]
    fills = [f for t in trades for f in (t['position']['entry'], t['exit'])]
    if c.position:
        fills.append(asdict(c.position.entry))
    fees = sum((D(f['fee']) for f in fills), D(0))
    slip = sum((abs(D(f['price'])-D(f['reference_price']))*D(f['quantity']) for f in fills), D(0))
    gains = sum((v for v in pnls if v > 0), D(0))
    losses = -sum((v for v in pnls if v < 0), D(0))
    peak, dd, dd_fraction = marks[0][1], D(0), D(0)
    for _, value in marks:
        peak = max(peak, value)
        dd = max(dd, peak-value)
        dd_fraction = max(dd_fraction, (peak-value)/peak)
    exposure = sum(t['exit']['candle_open_time_ms']-t['position']['entry']['candle_open_time_ms'] for t in trades)
    exposure_upper = exposure + STEP*sum(t['exit']['phase']=='INTRABAR' for t in trades)
    if c.position:
        duration = history.end-c.position.entry.candle_open_time_ms
        exposure += duration
        exposure_upper += duration
    events = [json.loads(raw) for raw in c.journal]
    return canonical(dict(policy=c.policy, history=history.coverage(), rules=c.rules.id,
        availability='IDEALIZED_COMPLETED_BARS_S+3m_RECEIPT_S+4m_DECISION',
        initial_equity='1000', realized_equity=c.equity, final_mtm_equity=marks[-1][1],
        realized_net_pnl=sum(pnls,D(0)), mtm_net_pnl=marks[-1][1]-D(1000),
        max_drawdown_close_mtm=dd, max_drawdown_fraction=dd_fraction,
        closed_trades=len(trades), exit_reasons=dict(Counter(t['reason'] for t in trades)),
        profit_factor=gains/losses if losses else None,
        expectancy=sum(pnls,D(0))/len(pnls) if pnls else None,
        fees=fees, embedded_slippage=slip, exposure_ms_lower=exposure,
        exposure_ms_upper=exposure_upper,
        exposure_fraction_lower=D(exposure)/(history.end-history.start),
        reversal_commitments=sum(e['kind']=='REVERSAL_COMMITTED' for e in events),
        open_position=asdict(c.position) if c.position else None,
        pending_entry=c.snapshot()['pending'], slot=c.slot, marks=marks,
        dispositions=dict(Counter(e['reason'] for e in events if e['kind']=='DISPOSITION'))))


def compare(history, rules):
    return {policy: run(history, rules, policy) for policy in ('A', 'C2')}
