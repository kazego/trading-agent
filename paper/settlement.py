"""Pure B3 paper settlement transitions. No loop, database writes or live orders."""
from dataclasses import dataclass, asdict, replace
from decimal import Decimal as D
import hashlib
from pathlib import Path

from backtest.data import canonical_decimal
from backtest.simulation import (arithmetic, _round, slipped_price, size_quantity,
    process_bar, Fill, SimulationConfig, CostMode, SizingError, SymbolRules, EXCLUSIONS)
from paper.acceptance import AcceptanceBinding, BoundaryBatch, decide, implementation as b2_identity
from paper.reservations import AcceptanceStore
from paper.model import LABEL, SourceObservation, parse
from paper.market import RuleSnapshot, CandleEvidence
from research.spec import canonical, encoded, fingerprint, digest


def implementation():
    root = Path(__file__).resolve().parents[1]
    return fingerprint(dict(version='paper-b3-implementation-v1', b2=b2_identity(),
        sources={p: hashlib.sha256((root/p).read_text(encoding='utf-8').encode()).hexdigest()
                 for p in ('paper/market.py', 'paper/settlement.py', 'backtest/simulation.py', 'research/protection.py')}))


def costs():
    # Fractions are frozen research assumptions, not actual exchange charges.
    return SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE,
                            entry_fee_rate=D('0.0005'), exit_fee_rate=D('0.0005'), slippage_bps=D('2'))


@dataclass(frozen=True)
class ConfirmedEntry:
    binding: AcceptanceBinding
    evidence: SourceObservation
    result: bytes
    equity_basis: bytes | None = None

    def __post_init__(self):
        if type(self.binding) is not AcceptanceBinding or type(self.evidence) is not SourceObservation:
            raise ValueError('Exact B2 binding/evidence required')
        if self.binding.implementation_id != b2_identity():
            raise ValueError('B2 implementation identity changed')
        r = parse(self.result)
        decision = decide(self.binding, BoundaryBatch.from_evidence(self.evidence),
                          accepted_ms=r['acceptance_ms'], slot='EMPTY', equity_basis=self.equity_basis)
        reservation = decision['reservation']
        if reservation is None or r.get('confirmation') is None:
            raise ValueError('Timing-confirmed pending entry required')
        c = r['confirmation']
        expected_confirmation = AcceptanceStore._confirmation(reservation, c['durable_ms'], c['elapsed_ns'])
        if expected_confirmation['status'] != 'PENDING_ENTRY':
            raise ValueError('Late, blocked or unconfirmed acceptance')
        dispositions = parse(encoded(decision['dispositions']))
        for d in dispositions:
            if d['symbol'] == reservation['symbol']:
                d['reason'] = 'PENDING_ENTRY'
        expected = dict(decision, confirmation=expected_confirmation, reservation_state='PENDING_ENTRY',
                        effective_dispositions=dispositions)
        if r != expected:
            raise ValueError('B2 decision/confirmation/provenance mismatch')

    @property
    def reservation(self):
        return parse(self.result)['reservation']

    @property
    def id(self):
        return fingerprint(dict(label=LABEL, version='paper-confirmed-entry-v1',
                                result=parse(self.result), evidence=self.evidence.id, binding=self.binding.id))

    @property
    def symbol(self):
        return self.reservation['symbol']

    @property
    def signal(self):
        return self.reservation['direction']


@dataclass(frozen=True)
class PaperPosition:
    pending: ConfirmedEntry
    rules: RuleSnapshot
    entry_evidence: CandleEvidence
    entry: Fill
    stop_loss: D
    take_profit: D
    requested_risk: D
    planned_stop_risk: D
    utilization: D
    last_processed_ms: int | None = None
    history_digest: str | None = None

    def __post_init__(self):
        if (type(self.pending) is not ConfirmedEntry or type(self.rules) is not RuleSnapshot
                or type(self.entry_evidence) is not CandleEvidence or type(self.entry) is not Fill):
            raise ValueError('Typed paper position provenance required')
        for name in ('stop_loss', 'take_profit', 'requested_risk', 'planned_stop_risk', 'utilization'):
            value = D(canonical_decimal(getattr(self, name)))
            if value <= 0:
                raise ValueError('Positive paper financial value required')
            object.__setattr__(self, name, value)
        if self.utilization > 1 or self.planned_stop_risk > self.requested_risk:
            raise ValueError('Paper risk budget exceeded')
        if self.last_processed_ms is None:
            if self.history_digest is not None:
                raise ValueError('Unexpected processing digest')
        else:
            if (type(self.last_processed_ms) is not int or self.last_processed_ms % 900000
                    or self.last_processed_ms < self.entry.candle_open_time_ms):
                raise ValueError('Invalid completed-candle cursor')
            digest(self.history_digest)

    @property
    def decision(self):
        # Structural interface used by pure historical process_bar/close_position.
        # This is the real paper instruction, never a fabricated research Decision.
        return self.pending

    @property
    def record(self):
        return canonical(dict(label=LABEL, version='paper-open-v1', implementation=implementation(),
            pending=self.pending.id, rules=self.rules.id, entry_evidence=self.entry_evidence.id,
            entry=asdict(self.entry), stop_loss=self.stop_loss, take_profit=self.take_profit,
            requested_risk=self.requested_risk, planned_stop_risk=self.planned_stop_risk,
            utilization=self.utilization, last_processed_ms=self.last_processed_ms, history_digest=self.history_digest))

    @property
    def id(self):
        return fingerprint(self.record)


@dataclass(frozen=True)
class PaperTrade:
    position: PaperPosition
    exit_evidence: CandleEvidence
    exit: Fill
    reason: str
    dual_touch: bool
    gross_pnl: D
    net_pnl: D
    entry_slippage_cost: D
    exit_slippage_cost: D

    def __post_init__(self):
        if (type(self.position) is not PaperPosition or type(self.exit_evidence) is not CandleEvidence
                or type(self.exit) is not Fill or type(self.dual_touch) is not bool
                or self.reason not in ('STOP_LOSS', 'TAKE_PROFIT')):
            raise ValueError('Typed protection exit required; no forced end-of-data exit')
        for name in ('gross_pnl', 'net_pnl', 'entry_slippage_cost', 'exit_slippage_cost'):
            value = D(canonical_decimal(getattr(self, name)))
            if name.endswith('slippage_cost') and value < 0:
                raise ValueError('Negative adverse slippage attribution')
            object.__setattr__(self, name, value)

    @property
    def record(self):
        return canonical(dict(label=LABEL, version='paper-closed-v1', position=self.position.id,
            exit_evidence=self.exit_evidence.id, exit=asdict(self.exit), reason=self.reason,
            dual_touch=self.dual_touch, gross_pnl=self.gross_pnl, net_pnl=self.net_pnl,
            entry_slippage_cost=self.entry_slippage_cost, exit_slippage_cost=self.exit_slippage_cost,
            slippage_attribution='INFORMATIONAL_ALREADY_IN_FILLS', limitations=EXCLUSIONS))

    @property
    def id(self):
        return fingerprint(self.record)


@dataclass(frozen=True)
class Transition:
    instruction_id: str
    status: str
    code: str
    position: PaperPosition | None = None
    trade: PaperTrade | None = None
    evidence_id: str | None = None

    def __post_init__(self):
        digest(self.instruction_id)
        if self.evidence_id is not None:
            digest(self.evidence_id)
        if self.status not in ('OPEN', 'CLOSED', 'UNRESOLVED', 'REJECTED_SIZING', 'REJECTED_EXECUTION'):
            raise ValueError('Unknown paper transition')
        if (self.status == 'OPEN' and type(self.position) is not PaperPosition
                or self.status == 'CLOSED' and type(self.trade) is not PaperTrade
                or self.status != 'CLOSED' and self.trade is not None
                or self.status in ('CLOSED', 'REJECTED_SIZING', 'REJECTED_EXECUTION') and self.position is not None):
            raise ValueError('Inconsistent paper transition payload')

    @property
    def slot_state(self):
        if self.status in ('REJECTED_SIZING', 'REJECTED_EXECUTION', 'CLOSED'):
            return 'FREE'
        return 'OPEN' if self.status == 'OPEN' else 'UNRESOLVED'

    @property
    def record(self):
        return dict(label=LABEL, version='paper-transition-v1', implementation=implementation(),
                    instruction_id=self.instruction_id, status=self.status, code=self.code,
                    slot_state=self.slot_state, position=self.position.record if self.position else None,
                    trade=self.trade.record if self.trade else None, evidence_id=self.evidence_id)

    @property
    def id(self):
        return fingerprint(self.record)


@arithmetic
def atr_levels(price, direction, atr, rules):
    # Same explicit ATR formula as frozen ATRProtection.levels; no research view
    # is constructed and no identity validation is bypassed. Rounding is reused.
    price, atr = D(canonical_decimal(price)), D(canonical_decimal(atr))
    if price <= 0 or atr <= 0 or direction not in ('LONG', 'SHORT') or type(rules) is not SymbolRules:
        raise SizingError('Invalid ATR protection inputs')
    long = direction == 'LONG'
    stop = _round(price-2*atr if long else price+2*atr, rules.tick_size, not long)
    target = _round(price+4*atr if long else price-4*atr, rules.tick_size, long)
    if min(stop, target) <= 0 or not (stop < price < target if long else target < price < stop):
        raise SizingError('Invalid ATR protection levels')
    return stop, target


@arithmetic
def settle(pending, evidence, rules):
    if type(pending) is not ConfirmedEntry:
        raise ValueError('Confirmed B2 instruction required')
    # Revalidate immutable financial instruction before every transition.
    ConfirmedEntry(pending.binding, pending.evidence, pending.result, pending.equity_basis)
    r = pending.reservation
    def result(status, code, position=None):
        return Transition(pending.id, status, code, position=position,
                          evidence_id=evidence.id if type(evidence) is CandleEvidence else None)
    if type(rules) is not RuleSnapshot or rules.id != r['rules']:
        return result('UNRESOLVED', 'RULE_BINDING_MISMATCH')
    if evidence is None:
        return result('UNRESOLVED', 'MISSING_ENTRY_CANDLE')
    if (type(evidence) is not CandleEvidence or evidence.source != pending.binding.draft.source
            or evidence.candle.symbol != pending.symbol or evidence.candle.open_time_ms != r['entry_boundary_ms']):
        return result('UNRESOLVED', 'ENTRY_CANDLE_IDENTITY_MISMATCH')
    CandleEvidence(evidence.source, evidence.candle, evidence.row_digest, evidence.received_ms)
    rule = rules.for_symbol(pending.symbol)
    equity, atr = D(r['equity_basis']['amount']), D(r['atr14'])
    # Only OPEN participates in entry settlement, never the eventual H/L/C/V.
    side = 'BUY' if pending.signal == 'LONG' else 'SELL'
    price = slipped_price(evidence.candle.open, side, costs())
    try:
        stop, target = atr_levels(price, pending.signal, atr, rule)
    except SizingError:
        return result('REJECTED_EXECUTION', 'INVALID_PROTECTION')
    try:
        quantity, actual = size_quantity(equity, price, stop, pending.signal, rule, costs(), price_distance_risk=True)
    except SizingError:
        return result('REJECTED_SIZING', 'QUANTITY_NOTIONAL_OR_MARGIN_CONSTRAINT')
    requested = equity*D('0.01')
    if actual > requested or actual != quantity*abs(price-stop):
        raise ValueError('Planned risk reconciliation failure')
    entry = Fill(side, r['entry_boundary_ms'], 'OPEN', evidence.candle.open, price,
                 quantity, price*quantity*costs().entry_rate)
    position = PaperPosition(pending, rules, evidence, entry, stop, target, requested, actual, actual/requested)
    return result('OPEN', 'ENTRY_SETTLED', position)


@arithmetic
def protect(position, evidence):
    if type(position) is not PaperPosition:
        raise ValueError('Typed paper position required')
    # Reconcile the immutable financial position with its originating instruction.
    original = settle(position.pending, position.entry_evidence, position.rules).position
    if original is None or replace(position, last_processed_ms=None, history_digest=None) != original:
        raise ValueError('Paper position financial identity mismatch')
    expected = (position.entry.candle_open_time_ms if position.last_processed_ms is None
                else position.last_processed_ms+900000)
    def unresolved(code):
        return Transition(position.pending.id, 'UNRESOLVED', code, position=position)
    if evidence is None:
        return unresolved('AWAITING_NEXT_CANDLE')
    if (type(evidence) is not CandleEvidence or evidence.source != position.pending.binding.draft.source
            or evidence.candle.symbol != position.pending.symbol or evidence.candle.open_time_ms != expected):
        return unresolved('CANDLE_CHRONOLOGY_OR_IDENTITY_MISMATCH')
    CandleEvidence(evidence.source, evidence.candle, evidence.row_digest, evidence.received_ms)
    if expected == position.entry.candle_open_time_ms and evidence != position.entry_evidence:
        return unresolved('ENTRY_CANDLE_REVISION')
    closed = process_bar(position, evidence.candle, costs())
    if closed is not None:
        trade = PaperTrade(position, evidence, closed.exit, closed.reason.value, closed.dual_touch,
            closed.gross_pnl, closed.net_pnl,
            abs(position.entry.price-position.entry.reference_price)*position.entry.quantity,
            abs(closed.exit.price-closed.exit.reference_price)*closed.exit.quantity)
        return Transition(position.pending.id, 'CLOSED', closed.reason.value, trade=trade, evidence_id=evidence.id)
    updated = replace(position, last_processed_ms=expected,
                      history_digest=fingerprint(dict(previous=position.history_digest, candle=evidence.id)))
    return Transition(position.pending.id, 'OPEN', 'PROTECTIONS_NOT_TRIGGERED', position=updated, evidence_id=evidence.id)
