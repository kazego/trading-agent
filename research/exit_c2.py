"""In-memory A/C2 coordination only. No loader, runner, clock, files or activation.

Signals are explicit inputs from a future verified completed-4h adapter. This
module does not certify that a caller's signal is an EMA cross. Synthetic tests
may supply signals directly; that is not permission to evaluate real datasets.
Every receipt/confirmation time is supplied, never read from a system clock.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal as D

from backtest.data import Candle, SYMBOLS, _timestamp, canonical_decimal
from backtest.simulation import Fill, SizingError, arithmetic, process_bar, size_quantity, slipped_price
from paper.clock_v3 import confirmation
from paper.market import RuleSnapshot
from paper.settlement import atr_levels, costs
from research.spec import canonical, encoded, fingerprint

STEP = 900000
FOUR_H = 14400000


@dataclass(frozen=True)
class Signal:
    symbol: str
    boundary_ms: int
    direction: str
    atr: D
    receipt_ms: int
    timing: str
    evidence: str

    def __post_init__(self):
        _timestamp(self.boundary_ms); _timestamp(self.receipt_ms)
        object.__setattr__(self, 'atr', D(canonical_decimal(self.atr)))
        if (self.symbol not in SYMBOLS or self.boundary_ms % FOUR_H
                or self.direction not in ('NONE', 'LONG', 'SHORT')
                or self.atr < 0 or self.direction != 'NONE' and self.atr <= 0
                or self.receipt_ms < self.boundary_ms
                or self.timing not in ('ONLINE', 'DELAYED_RECONSTRUCTED', 'UNCONFIRMED')
                or len(self.evidence) != 64 or any(c not in '0123456789abcdef' for c in self.evidence)):
            raise ValueError('Invalid completed signal evidence')

    @property
    def signal(self):
        return self.direction  # Structural interface for shared protection math.

    @property
    def id(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class Position:
    decision: Signal
    entry: Fill
    stop_loss: D
    take_profit: D
    planned_stop_risk: D


class Coordinator:
    """Same base for A and C2. Experimental receipts emulate durable commits.

    One object owns one portfolio. An operation is atomic in memory and identical
    retries return the original trace. There is intentionally no disk persistence
    or real-order/partial-fill API. Missing evidence keeps the slot unresolved.
    """
    def __init__(self, policy, rules, start_ms):
        if policy not in ('A', 'C2') or type(rules) is not RuleSnapshot:
            raise ValueError('Explicit experimental policy and frozen rules required')
        _timestamp(start_ms)
        if start_ms % FOUR_H:
            raise ValueError('Aligned prospective start required')
        self.policy, self.rules, self.start_ms = policy, rules, start_ms
        self.config = costs()
        self.equity = D('1000')
        self.position = self.pending = self.intent = self.reversal = None
        self.cursor = None
        self.now = start_ms
        self.last_boundary = None
        self.trades = []
        self._journal = []
        self._requests = {}

    @property
    def slot(self):
        return 'OPEN' if self.position else 'PENDING' if self.pending else 'EMPTY'

    @property
    def journal(self):
        return tuple(self._journal)  # Canonical immutable bytes, not mutable dicts.

    @property
    def specification(self):
        """Research identity only, never an operational authorization."""
        return canonical(dict(version='experimental-exit-a-c2-memory-v1', policy=self.policy,
            start_ms=self.start_ms, rules=self.rules.record, costs=asdict(self.config),
            signal_contract='NEW_COMPLETED_4H_EMA9_21_CROSS_FROM_VERIFIED_ADAPTER',
            normal_entry_offset_ms=STEP, close_deadline_offset_ms=840000,
            close_open_offset_ms=STEP, reversal_deadline_offset_ms=2640000,
            reversal_open_offset_ms=3*STEP, priority=SYMBOLS, fallback=False,
            max_positions=1, signal_fill_confirmation='COMPLETED_15M_RECEIPT',
            end_of_data='PRESERVE_UNRESOLVED_NO_FORCED_CLOSE',
            limitations=['SYNTHETIC_INPUT_INTERFACE', 'NO_DURABLE_STORAGE',
                         'FULL_FILLS_ONLY', 'NO_REAL_DATA_ADAPTER']))

    def snapshot(self):
        return canonical(dict(equity=self.equity, slot=self.slot,
            position=asdict(self.position) if self.position else None,
            pending=(asdict(self.pending[0]),self.pending[1]) if self.pending else None,
            intent=asdict(self.intent) if self.intent else None,
            reversal=(asdict(self.reversal[0]),self.reversal[1]) if self.reversal else None,
            cursor=self.cursor, trades=self.trades))

    def _event(self, kind, **data):
        body = canonical(dict(sequence=len(self._journal)+1,
            previous=fingerprint(self._journal[-1].decode()) if self._journal else None,
            kind=kind, available_ms=self.now, **data))
        self._journal.append(encoded(body))

    def _run(self, key, payload, now, action):
        digest = fingerprint(payload)
        if key in self._requests:
            old, result = self._requests[key]
            if old != digest:
                raise ValueError('Conflicting retry')
            return result
        _timestamp(now)
        if now < self.now:
            raise ValueError('Availability chronology regression')
        before = deepcopy(self.__dict__)
        index = len(self._journal)
        try:
            self.now = now
            self._event('INPUT', request=key, payload=payload)
            action()
            if self.position and self.pending:
                raise ValueError('Two global exposures')
            result = tuple(self._journal[index:])
            self._requests[key] = digest, result
            return result
        except BaseException:
            self.__dict__.clear(); self.__dict__.update(before)
            raise

    def _confirmed(self, signal, deadline, acceptance, durable):
        r = dict(reservation_id=signal.id, acceptance_ms=acceptance['utc_ms'], deadline_ms=deadline)
        result = confirmation(r, acceptance, durable)
        self._event('CLOCK_CONFIRMATION', signal=signal.id, result=result)
        if result['status'] == 'BLOCKED_CLOCK':
            raise ValueError('Invalid synthetic clock evidence; stop experiment')
        return result['status'] == 'PENDING_ENTRY'

    def observe(self, signals, acceptance, durable):
        """Complete three-symbol batch; no prices are accepted by this method."""
        if (type(signals) is not tuple or len(signals) != 3
                or any(type(s) is not Signal for s in signals)
                or tuple(s.symbol for s in signals) != SYMBOLS
                or len({s.boundary_ms for s in signals}) != 1):
            raise ValueError('Exact complete priority-ordered batch required')
        boundary = signals[0].boundary_ms
        def action():
            _timestamp(acceptance['utc_ms'])
            if acceptance['utc_ms'] > durable['utc_ms']:
                raise ValueError('Confirmation UTC regression')
            if self.last_boundary is not None and boundary <= self.last_boundary:
                raise ValueError('Nonchronological signal boundary')
            # Do not decide against a position whose preceding bars are missing.
            if self.position and (self.cursor is None or self.cursor + STEP < boundary):
                raise ValueError('Unresolved execution candles before signal')
            self.last_boundary = boundary
            winner = None
            for s in signals:
                reason = None
                if s.direction == 'NONE': reason = 'NONE'
                elif boundary <= self.start_ms: reason = 'PRE_START'
                elif s.receipt_ms >= acceptance['utc_ms']: reason = 'SOURCE_NOT_AVAILABLE'
                elif s.timing != 'ONLINE': reason = 'MISSED_DELAYED_SIGNAL'
                elif acceptance['utc_ms'] > boundary+840000: reason = 'MISSED_ACCEPTANCE_DEADLINE'
                elif winner is not None: reason = 'BLOCKED_PRIORITY'
                elif self.position or self.pending:
                    opposite = (self.policy == 'C2' and self.position and not self.intent
                        and self.position.decision.symbol == s.symbol
                        and self.position.decision.direction != s.direction)
                    if opposite:
                        if self._confirmed(s, boundary+840000, acceptance, durable):
                            self.intent = s
                            self._event('CLOSE_INTENT', signal=s.id, position=self.position.decision.id,
                                        close_open_ms=boundary+STEP, reversal_open_ms=boundary+3*STEP)
                            reason = 'CLOSE_PENDING'
                        else: reason = 'MISSED_ACCEPTANCE_DEADLINE'
                    else: reason = 'BLOCKED_GLOBAL_SLOT'
                else:
                    winner = s
                    if self._confirmed(s, boundary+840000, acceptance, durable):
                        self.pending = (s, boundary+STEP)
                        reason = 'PENDING_ENTRY'
                    else: reason = 'MISSED_ACCEPTANCE_DEADLINE'
                self._event('DISPOSITION', signal=s.id, symbol=s.symbol, boundary_ms=boundary, reason=reason)
        return self._run('SIGNAL:'+str(boundary), dict(signals=[asdict(s) for s in signals],
            acceptance=acceptance, durable=durable), durable['utc_ms'], action)

    @arithmetic
    def _open(self, signal, candle):
        rule = self.rules.for_symbol(signal.symbol)
        side = 'BUY' if signal.direction == 'LONG' else 'SELL'
        price = slipped_price(candle.open, side, self.config)
        try:
            stop, target = atr_levels(price, signal.direction, signal.atr, rule)
        except SizingError:
            self._event('REJECTED_EXECUTION', signal=signal.id); return
        if not self.rules.prices_allowed(signal.symbol, price, stop, target):
            self._event('REJECTED_EXECUTION', signal=signal.id); return
        try:
            quantity, risk = size_quantity(self.equity, price, stop, signal.direction,
                rule, self.config, price_distance_risk=True)
        except SizingError:
            self._event('REJECTED_SIZING', signal=signal.id); return
        fill = Fill(side, candle.open_time_ms, 'OPEN', candle.open, price, quantity,
                    price*quantity*self.config.entry_rate)
        self.position = Position(signal, fill, stop, target, risk)
        self.cursor = None
        self._event('ENTRY', signal=signal.id, position=asdict(self.position))

    @arithmetic
    def _close(self, fill, reason, dual_touch=False):
        p = self.position
        gross = (fill.price-p.entry.price)*p.entry.quantity*(1 if p.decision.direction == 'LONG' else -1)
        net = gross-p.entry.fee-fill.fee
        trade = canonical(dict(position=asdict(p), exit=asdict(fill), reason=reason,
            dual_touch=dual_touch, gross_pnl=gross, net_pnl=net,
            equity_before=self.equity, equity_after=self.equity+net))
        self.trades.append(trade); self.equity += net
        self._event('CLOSE_CONFIRMED', trade=trade)
        intent = self.intent
        self.position = self.intent = None; self.cursor = None
        self._event('SLOT_RELEASED', owner=p.decision.id)
        if reason == 'OPPOSITE_SIGNAL':
            self.reversal = (intent, self.now)
        elif intent:
            self._event('C2_CANCELLED_PROTECTION', signal=intent.id)

    @arithmetic
    def bar(self, candle, received_ms):
        """Settlement uses exact next complete candle; cannot replace a missing bar."""
        if type(candle) is not Candle or received_ms < candle.close_time_ms+1:
            raise ValueError('Only completed candle receipts')
        def action():
            if self.pending:
                signal, opening = self.pending
                if (candle.symbol, candle.open_time_ms) != (signal.symbol, opening):
                    raise ValueError('Wrong precommitted entry candle')
                self.pending = None
                self._open(signal, candle)
            elif self.position:
                expected = self.cursor+STEP
                if (candle.symbol, candle.open_time_ms) != (self.position.decision.symbol, expected):
                    raise ValueError('Missing/conflicting execution candle')
            else:
                raise ValueError('No exposure to settle')
            p = self.position
            if not p: return
            # On the scheduled close opening, expose only OPEN to gap checks.
            due = self.intent and candle.open_time_ms == self.intent.boundary_ms+STEP
            if self.intent and candle.open_time_ms > self.intent.boundary_ms+STEP:
                raise ValueError('Never move a committed close opening')
            checked = (Candle(candle.symbol,'15m',candle.open_time_ms,candle.close_time_ms,
                candle.open,candle.open,candle.open,candle.open,D(0)) if due else candle)
            trade = process_bar(p, checked, self.config)
            if trade:
                self._close(trade.exit, trade.reason.value, trade.dual_touch)
            elif due:
                side = 'SELL' if p.decision.direction == 'LONG' else 'BUY'
                price = slipped_price(candle.open, side, self.config)
                fill = Fill(side,candle.open_time_ms,'OPEN',candle.open,price,p.entry.quantity,
                            price*p.entry.quantity*self.config.exit_rate)
                self._close(fill,'OPPOSITE_SIGNAL')
            else:
                self.cursor = candle.open_time_ms
                self._event('BAR', owner=p.decision.id, candle=candle.record())
        return self._run('BAR:'+candle.symbol+':'+str(candle.open_time_ms),
            dict(candle=candle.record(), received_ms=received_ms), received_ms, action)

    def decide_reversal(self, acceptance, durable):
        """Independent decision. No entry price; cannot create generic authority."""
        if not self.reversal:
            raise ValueError('Confirmed signal closure required')
        s, closed_ms = self.reversal
        def action():
            if self.position or self.pending or acceptance['utc_ms'] < closed_ms:
                raise ValueError('Closure/account/slot not available')
            if self._confirmed(s,s.boundary_ms+2640000,acceptance,durable):
                self.pending = (s,s.boundary_ms+3*STEP)
                self._event('REVERSAL_COMMITTED', signal=s.id, entry_open_ms=s.boundary_ms+3*STEP,
                            frozen_atr=s.atr, equity=self.equity)
            else:
                self._event('REVERSAL_EXPIRED',signal=s.id)
        return self._run('REVERSE:'+s.id,dict(acceptance=acceptance,durable=durable),durable['utc_ms'],action)
