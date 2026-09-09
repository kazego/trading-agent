"""Explicit frozen ATR protection binding; no runner, registry or data acquisition."""
from dataclasses import asdict, dataclass
from decimal import Decimal as D
import hashlib
from pathlib import Path

from backtest.data import DataValidationError, canonical_decimal
from backtest.simulation import Position, SymbolRules, SizingError, arithmetic, _round
from research.features import FeatureSnapshot, FeatureView, calculate_features
from research.schedule import ResearchSchedule
from research.spec import fingerprint


@dataclass(frozen=True)
class ATRRiskRecord:
    policy_family: str
    policy_version: str
    atr_period: int
    stop_multiple: int
    target_multiple: int
    atr: D
    feature_fingerprint: str
    decision_id: str
    symbol: str
    decision_time_ms: int
    entry_fill: D
    rounded_stop: D
    rounded_target: D
    quantity: D
    requested_risk: D
    actual_planned_risk: D
    risk_utilization_ratio: D
    rules_fingerprint: str
    config_fingerprint: str
    source_sha256: str
    identity: str


@dataclass(frozen=True)
class ATRPosition(Position):
    protection: ATRRiskRecord


def _binding(decision, event):
    return fingerprint(dict(decision=asdict(decision), symbol=event.symbol,
        timeframe=event.timeframe, open_time_ms=event.open_time_ms,
        completed_through_ms=event.completed_through_ms, opening_price=event.opening_price,
        dataset_fingerprint=event.dataset_fingerprint))


@dataclass(frozen=True, init=False)
class ATRProtection:
    """Issued before execution, after checking the authorized feature and EMA views.

    Execution retains no candles or feature view and never recomputes ATR.
    Python introspection is outside this accidental-misuse boundary.
    """
    atr: D
    feature_fingerprint: str
    decision_binding: str
    source_sha256: str

    def __init__(self, view, snapshot, schedule, event):
        if type(view) is not FeatureView or type(snapshot) is not FeatureSnapshot or type(schedule) is not ResearchSchedule:
            raise PermissionError('Authorized feature view, snapshot and explicit EMA schedule required')
        if (snapshot.symbol != event.symbol or snapshot.open_time_ms != event.open_time_ms
                or snapshot.timeframe != '4h' or view.symbol != event.symbol):
            raise PermissionError('ATR must match the accepted 4h EMA entry boundary and symbol')
        atr = D(canonical_decimal(snapshot.atr14))
        if atr <= 0:
            raise DataValidationError('ATR must be finite and positive')
        completed = view.at_open(event.open_time_ms)
        if snapshot != calculate_features(completed):
            raise DataValidationError('Feature content/fingerprint mismatch')
        window = schedule.window_at(event.symbol, event.open_time_ms)
        if window is None or window.timeframe != '4h' or window.completed != completed.completed[-99:]:
            raise PermissionError('EMA and ATR completed history must agree')
        decision = schedule(event)  # Existing 9/21 adapter and source/open validation.
        if decision is None or decision.signal not in ('LONG', 'SHORT'):
            raise DataValidationError('An actionable existing EMA9/21 entry is required')
        object.__setattr__(self, 'atr', atr)
        object.__setattr__(self, 'feature_fingerprint', snapshot.snapshot_fingerprint)
        object.__setattr__(self, 'decision_binding', _binding(decision, event))
        object.__setattr__(self, 'source_sha256', hashlib.sha256(
            Path(__file__).read_text(encoding='utf-8').encode('utf-8')).hexdigest())

    def validate(self, decision, event, config):
        if _binding(decision, event) != self.decision_binding:
            raise DataValidationError('ATR binding differs from accepted EMA entry')
        if config.risk_fraction != D('0.01') or config.leverage != D('3'):
            raise DataValidationError('Frozen research policy requires 1% risk and 3x cap')

    @arithmetic
    def levels(self, entry_price, signal, rules):
        if type(rules) is not SymbolRules:
            raise DataValidationError('Validated symbol rules required')
        entry_price = D(canonical_decimal(entry_price))
        if entry_price <= 0 or signal not in ('LONG', 'SHORT'):
            raise DataValidationError('Positive entry fill and direction required')
        long = signal == 'LONG'
        stop = _round(entry_price - 2*self.atr if long else entry_price + 2*self.atr,
                      rules.tick_size, not long)
        target = _round(entry_price + 4*self.atr if long else entry_price - 4*self.atr,
                        rules.tick_size, long)
        if min(stop, target) <= 0 or not (stop < entry_price < target if long else target < entry_price < stop):
            raise SizingError('ATR/tick rules cannot represent valid protections')
        return stop, target

    @arithmetic
    def record(self, position, rules, config):
        requested = position.equity_before * config.risk_fraction
        actual = position.entry.quantity * abs(position.entry.price-position.stop_loss)
        if actual > requested or actual != position.planned_stop_risk:
            raise SizingError('ATR planned risk exceeds budget or fails reconciliation')
        values = dict(policy_family='WILDER_ATR_PROTECTION', policy_version='atr14-2x-4x-v1',
            atr_period=14, stop_multiple=2, target_multiple=4, atr=self.atr,
            feature_fingerprint=self.feature_fingerprint, decision_id=position.decision.decision_id,
            symbol=position.decision.symbol, decision_time_ms=position.decision.open_time_ms,
            entry_fill=position.entry.price, rounded_stop=position.stop_loss, rounded_target=position.take_profit,
            quantity=position.entry.quantity, requested_risk=requested, actual_planned_risk=actual,
            risk_utilization_ratio=actual/requested, rules_fingerprint=fingerprint(asdict(rules)),
            config_fingerprint=fingerprint(asdict(config)), source_sha256=self.source_sha256)
        record = ATRRiskRecord(**values, identity=fingerprint(values))
        return ATRPosition(position.decision, position.entry, position.stop_loss, position.take_profit,
                           position.equity_before, position.planned_stop_risk, record)
