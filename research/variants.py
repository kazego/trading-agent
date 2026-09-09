"""Two explicit 4h feature strategies. No scheduling, execution or registration."""
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
from typing import ClassVar, Protocol

from research.features import FeatureView, FeatureSnapshot, FeatureParameters, calculate_features
from research.spec import fingerprint
from research.strategy import SignalDecision


@dataclass(frozen=True)
class VariantParameters:
    trigger: str
    trigger_ema_spans: tuple[int,...]
    regime_ema_spans: tuple[int,...]
    breakout_lookback: int | None
    feature_parameters: FeatureParameters = FeatureParameters()


@dataclass(frozen=True)
class VariantDecision(SignalDecision):
    """Existing decision envelope extended with exact variant/feature provenance."""
    parameters: VariantParameters
    feature_fingerprint: str
    trigger: str


class FeatureStrategy(Protocol):
    """Feature-input counterpart of the existing typed research Strategy protocol."""
    def evaluate(self, snapshot: FeatureSnapshot) -> VariantDecision: ...


def _verify(view, snapshot):
    if type(snapshot) is not FeatureSnapshot:
        raise PermissionError('Typed feature snapshot required, not raw data or dictionaries')
    if snapshot.timeframe!='4h' or snapshot.symbol!=view.symbol:
        raise PermissionError('Matching authorized 4h snapshot required')
    # at_open enforces DEVELOPMENT and excludes warmup/current/future information.
    expected=calculate_features(view.at_open(snapshot.open_time_ms))
    if snapshot!=expected:
        raise ValueError('Snapshot differs from the authorized visible feature window')


def _decision(strategy, snapshot, signal, trigger, reason):
    software=dict(snapshot.software)
    payload=dict(symbol=snapshot.symbol,signal_timeframe='4h',open_time_ms=snapshot.open_time_ms,
        signal=signal,strategy_family=strategy.family,strategy_version=strategy.version,
        parameters=strategy.parameters,visible_fingerprint=snapshot.visible_fingerprint,
        completed_open_times_ms=tuple(snapshot.open_time_ms-i*14400000 for i in range(600,0,-1)),
        reason=reason,source_sha256=hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode('utf-8')).hexdigest(),
        python_version=software['python'],pandas_version=software['pandas'],numpy_version=software['numpy'],
        feature_fingerprint=snapshot.snapshot_fingerprint,trigger=trigger)
    identity=dict(payload,parameters=asdict(strategy.parameters))
    return VariantDecision(**payload,decision_id=fingerprint(identity))


@dataclass(frozen=True)
class RibbonStrategy:
    view: FeatureView
    family: ClassVar[str] = 'EMA_CROSSOVER_RIBBON'
    version: ClassVar[str] = 'ema9-21-ribbon20-50-100-200-4h-v1'
    parameters: ClassVar[VariantParameters] = VariantParameters('EMA_CROSSOVER',(9,21),(20,50,100,200),None)

    def __post_init__(self):
        if type(self.view) is not FeatureView:raise PermissionError('Explicit authorized FeatureView required')

    def evaluate(self, snapshot: FeatureSnapshot) -> VariantDecision:
        _verify(self.view,snapshot)
        emas={v.span:v for v in snapshot.emas}
        fast,slow=emas[9],emas[21]
        trigger='NONE'
        if fast.previous<=slow.previous and fast.latest>slow.latest:trigger='LONG'
        elif fast.previous>=slow.previous and fast.latest<slow.latest:trigger='SHORT'
        if trigger=='NONE':return _decision(self,snapshot,'NONE',trigger,'NO_CROSSOVER')
        matched=snapshot.ribbon==('BULLISH' if trigger=='LONG' else 'BEARISH')
        return _decision(self,snapshot,trigger if matched else 'NONE',trigger,
            'CROSSOVER_WITH_RIBBON' if matched else 'FILTERED_REGIME')


@dataclass(frozen=True)
class BreakoutStrategy:
    view: FeatureView
    family: ClassVar[str] = 'FRESH_BREAKOUT_TREND'
    version: ClassVar[str] = 'fresh-breakout20-ema200-4h-v1'
    parameters: ClassVar[VariantParameters] = VariantParameters('FRESH_CLOSE_BREAKOUT',(),(200,),20)

    def __post_init__(self):
        if type(self.view) is not FeatureView:raise PermissionError('Explicit authorized FeatureView required')

    def evaluate(self, snapshot: FeatureSnapshot) -> VariantDecision:
        _verify(self.view,snapshot)
        b=snapshot.breakout
        if b.fresh_bullish and b.fresh_bearish:raise ValueError('Contradictory breakout states')
        trigger='LONG' if b.fresh_bullish else 'SHORT' if b.fresh_bearish else 'NONE'
        if trigger=='NONE':return _decision(self,snapshot,'NONE',trigger,'NO_FRESH_BREAKOUT')
        matched=snapshot.close_vs_ema200==('BULLISH' if trigger=='LONG' else 'BEARISH')
        return _decision(self,snapshot,trigger if matched else 'NONE',trigger,
            'BREAKOUT_WITH_TREND' if matched else 'FILTERED_TREND')
