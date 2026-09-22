"""B2 acceptance-time model. No candle prices, sizing, execution or activation."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal
import hashlib
from pathlib import Path

from backtest.data import SYMBOLS, _timestamp, canonical_decimal
from paper.model import DraftManifest, SourceObservation, LABEL, parse, implementation as b1_identity
from research.spec import canonical, fingerprint, digest


def policy():
    return dict(label=LABEL, version='live-0b-acceptance-v1', priority=list(SYMBOLS),
                entry_offset_ms=900000, deadline_margin_ms=60000, deadline_inclusive=True,
                prospective='S_STRICTLY_AFTER_START', source_available='STRICTLY_BEFORE_ACCEPTANCE',
                complete_batch=True, fallback=False, global_slots=1,
                durable_confirmation='POST_COMMIT_SAMPLE_AT_OR_BEFORE_DEADLINE',
                clock_corroboration='UTC_DELTA_GE_FLOOR_MONOTONIC_ELAPSED_MS',
                monotonic_anchor='BEFORE_ACCEPTANCE_UTC',
                unconfirmed='BLOCK_SLOT_NEVER_INFER_ACCEPTANCE')


def implementation():
    root = Path(__file__).resolve().parents[1]
    return fingerprint(dict(version='live-0b-b2-implementation-v1', b1=b1_identity(),
        sources={p: hashlib.sha256((root/p).read_text(encoding='utf-8').encode()).hexdigest()
                 for p in ('paper/acceptance.py', 'paper/reservations.py')}, policy=policy()))


def timestamp(value):
    if type(value) is not int:
        raise ValueError('Integer UTC milliseconds required')
    return _timestamp(value)


@dataclass(frozen=True)
class ProspectiveGate:
    """Future authorization input capability; no activation writer or default T0."""
    start_ms: int | None = None
    authorization: str | None = None

    def __post_init__(self):
        if self.start_ms is None:
            if self.authorization is not None:
                raise ValueError('Authorization without boundary')
        else:
            if timestamp(self.start_ms) % 14400000:
                raise ValueError('4h prospective boundary required')
            digest(self.authorization)

    def permits(self, boundary):
        return self.start_ms is not None and boundary > self.start_ms


@dataclass(frozen=True)
class AcceptanceBinding:
    draft: DraftManifest
    prospective: ProspectiveGate = field(default_factory=ProspectiveGate)
    implementation_id: str = field(default_factory=implementation)

    def __post_init__(self):
        if type(self.draft) is not DraftManifest or type(self.prospective) is not ProspectiveGate:
            raise ValueError('Explicit typed B2 binding required')
        if self.implementation_id != implementation():
            raise ValueError('B2 implementation mismatch')

    @property
    def record(self):
        return dict(label=LABEL, version='live-0b-b2-binding-v1', draft=self.draft.record,
                    draft_id=self.draft.id, prospective=asdict(self.prospective),
                    implementation=self.implementation_id, policy_fingerprint=fingerprint(policy()))

    @property
    def id(self):
        return fingerprint(self.record)

    @property
    def equity(self):
        # No accounting in B2. Only the committed initial-capital basis exists.
        return canonical(dict(label=LABEL, basis='INITIAL_REALIZED_BALANCE', draft_id=self.draft.id,
                              amount=self.draft.config.starting_equity, currency='USDT'))


@dataclass(frozen=True)
class Candidate:
    symbol: str
    direction: str
    atr14: str
    signal_fingerprint: str

    def __post_init__(self):
        if (self.symbol not in SYMBOLS or self.direction not in ('LONG', 'SHORT', 'NONE')
                or type(self.atr14) is not str or canonical_decimal(self.atr14) != self.atr14
                or Decimal(self.atr14) < 0):
            raise ValueError('Invalid candidate projection')
        digest(self.signal_fingerprint)


@dataclass(frozen=True)
class BoundaryBatch:
    """Whitelisted arbitration view: no raw payload, context or future-price field."""
    boundary_ms: int
    candidates: tuple[Candidate, ...]
    timing: str
    available_ms: int
    has_receipt: bool
    source_id: str
    evidence_id: str
    observation_id: str
    receipt_id: str | None

    def __post_init__(self):
        if (type(self.candidates) is not tuple or any(type(c) is not Candidate for c in self.candidates)
                or [c.symbol for c in self.candidates] != list(SYMBOLS)
                or timestamp(self.boundary_ms) % 14400000 or timestamp(self.available_ms) < self.boundary_ms
                or self.timing not in ('ONLINE', 'DELAYED_RECONSTRUCTED', 'UNCONFIRMED')
                or type(self.has_receipt) is not bool or self.has_receipt != (self.receipt_id is not None)):
            raise ValueError('Malformed complete batch projection')
        for value in (self.source_id, self.evidence_id, self.observation_id):
            digest(value)
        if self.receipt_id is not None:
            digest(self.receipt_id)

    @classmethod
    def from_evidence(cls, evidence):
        if type(evidence) is not SourceObservation:
            raise ValueError('Typed verified source evidence required')
        # Recheck even when input came from deserialization or another process.
        SourceObservation(evidence.source, evidence.observation, evidence.receipt)
        o = parse(evidence.observation)
        r = parse(evidence.receipt) if evidence.receipt else None
        return cls(o['boundary_ms'], tuple(Candidate(d['symbol'], d['signal'], d['atr14'],
                   d['signal_fingerprint']) for d in o['decisions']), evidence.effective_timing,
                   r['observed_ms'] if r else o['observed_ms'], r is not None,
                   evidence.source.id, evidence.id, fingerprint(o), fingerprint(r) if r else None)


def decide(binding, batch, *, accepted_ms, slot):
    """Pure arbitration. Only AcceptanceStore supplies this view in the normal path."""
    timestamp(accepted_ms)
    if type(binding) is not AcceptanceBinding or type(batch) is not BoundaryBatch:
        raise ValueError('Typed arbitration inputs required')
    if ([c.symbol for c in batch.candidates] != list(SYMBOLS)
            or batch.source_id != binding.draft.source.id
            or batch.boundary_ms % 14400000 or accepted_ms < batch.boundary_ms
            or batch.available_ms >= accepted_ms):
        raise ValueError('Incomplete, inconsistent or not-yet-available batch')
    if slot not in ('EMPTY', 'PENDING', 'OPEN', 'BLOCKED'):
        raise ValueError('Unknown slot state')
    entry = batch.boundary_ms + 900000
    deadline = entry - 60000
    dispositions = []
    selected = None
    for candidate in batch.candidates:
        if candidate.direction == 'NONE':
            reason = 'NONE'
        elif batch.timing == 'DELAYED_RECONSTRUCTED':
            reason = 'MISSED_DELAYED_SIGNAL'
        elif not batch.has_receipt or batch.timing != 'ONLINE':
            reason = 'BLOCKED_UNCONFIRMED_SOURCE'
        elif accepted_ms > deadline:
            reason = 'MISSED_ACCEPTANCE_DEADLINE'
        elif binding.prospective.start_ms is None:
            reason = 'BLOCKED_NOT_ACTIVATED'
        elif not binding.prospective.permits(batch.boundary_ms):
            reason = 'OUTSIDE_PROSPECTIVE_INTERVAL'
        elif binding.draft.rules_fingerprint is None:
            reason = 'BLOCKED_CONFIGURATION'
        elif Decimal(candidate.atr14) <= 0:
            reason = 'REJECTED_INVALID_ATR'
        elif slot != 'EMPTY':
            reason = 'BLOCKED_GLOBAL_SLOT'
        elif selected is not None:
            reason = 'BLOCKED_PRIORITY'
        else:
            selected = candidate
            reason = 'SELECTED_PENDING_CONFIRMATION'
        dispositions.append(dict(symbol=candidate.symbol, direction=candidate.direction, reason=reason))
    reservation = None
    if selected:
        reservation = dict(label=LABEL, version='live-0b-pending-entry-v1',
            binding=binding.id, config=binding.draft.config.id,
            implementation=binding.implementation_id, rules=binding.draft.rules_fingerprint,
            source_identity=batch.source_id, evidence=batch.evidence_id,
            observation=batch.observation_id, receipt=batch.receipt_id,
            symbol=selected.symbol, direction=selected.direction, atr14=selected.atr14,
            signal_fingerprint=selected.signal_fingerprint, signal_boundary_ms=batch.boundary_ms,
            acceptance_ms=accepted_ms, entry_boundary_ms=entry, deadline_ms=deadline,
            equity_basis=binding.equity, equity_reference=fingerprint(binding.equity))
        reservation = dict(reservation, reservation_id=fingerprint(reservation))
    body = dict(label=LABEL, version='live-0b-boundary-decision-v1', binding=binding.id,
                boundary_ms=batch.boundary_ms, acceptance_ms=accepted_ms,
                entry_boundary_ms=entry, deadline_ms=deadline,
                evidence=batch.evidence_id, slot_at_acceptance=slot,
                dispositions=dispositions, reservation=reservation)
    return dict(body, decision_id=fingerprint(body))
