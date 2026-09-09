"""Completed-only 4h features. No strategy signals, execution, or network access."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal, Context, localcontext
import hashlib
import json
import math
from pathlib import Path
import platform

import numpy as np
import pandas as pd

from research.data import AggregateCandle, SignalDataView, authorize, VERSION as DATA_VERSION
from research.spec import SYMBOLS, DEV_START, VALIDATION_START, Role, SplitRequest, fingerprint, policy

STEP = 14400000
START = DEV_START - 600*STEP
VERSION = 'completed-4h-features-v1'
MANIFEST_HASH = 'da2a0ec1f169ff055ef5ba39ac20a6f921b544ac83ada837cb174c844b9708f4'
HISTORY_HASHES = dict(zip(SYMBOLS, (
    '20cb2024d0d72f4f5fb9b62337c32e1c6db001790fbbdb03779dd488c47c7a34',
    '66ee284131d05d63f37607b6353069a78bd2bfbed49e73c59921baddba87a153',
    'd7edb589044e2f7c5bf02949bba3f98ddafcda89bfd73f350aff1dd249ce96e5')))
_ISSUED = object()


def _chronology(candles, symbol, start, count):
    if len(candles) != count: raise ValueError('Incomplete completed history')
    for i,c in enumerate(candles):
        if (type(c) is not AggregateCandle or c.symbol != symbol or c.timeframe != '4h'
                or c.open_time_ms != start+i*STEP or c.close_time_ms != start+(i+1)*STEP-1):
            raise ValueError('Mismatched, duplicate, incomplete or future history')


@dataclass(frozen=True)
class HistoryPackage:
    symbol: str
    candles: tuple[AggregateCandle, ...]
    history_fingerprint: str
    provenance_json: str
    _issuer: object = field(default=None,repr=False,compare=False)


def load_history(directory, symbol):
    """Explicit local read of the frozen 2023 package only; no evaluation."""
    if symbol not in SYMBOLS: raise PermissionError('Configured symbol required')
    root=Path(directory)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if fingerprint({k:v for k,v in manifest.items() if k!='fingerprint'}) != MANIFEST_HASH or manifest['fingerprint'] != MANIFEST_HASH:
        raise ValueError('Frozen Phase 6D0 manifest mismatch')
    document=json.loads((root/f'{symbol}-4h-history.json').read_text(encoding='utf-8'))
    meta=document['provenance']
    if (meta != manifest['symbols'][symbol] or meta['history_view_fingerprint'] != HISTORY_HASHES[symbol]
            or fingerprint({k:v for k,v in meta.items() if k!='history_view_fingerprint'}) != HISTORY_HASHES[symbol]
            or meta['role']!='HISTORY_ONLY' or meta['evaluable_boundaries']!=[]
            or meta['pre2024_evaluation_allowed'] is not False
            or meta['development_interval'] != [DEV_START,VALIDATION_START]
            or (meta['history_start_ms'],meta['history_end_ms'])!=(START,DEV_START)):
        raise ValueError('Frozen history contract mismatch')
    candles=tuple(AggregateCandle(**c) for c in document['candles'])
    _chronology(candles,symbol,START,600)
    if fingerprint([c.record() for c in candles]) != meta['normalized_4h_fingerprint']:
        raise ValueError('History candle fingerprint mismatch')
    return HistoryPackage(symbol,candles,meta['history_view_fingerprint'],json.dumps(meta,sort_keys=True),_ISSUED)


@dataclass(frozen=True)
class FeatureWindow:
    symbol: str
    open_time_ms: int
    completed: tuple[AggregateCandle, ...]
    visible_fingerprint: str
    _issuer: object = field(default=None,repr=False,compare=False)


def _window(symbol, timestamp, candles):
    """Private issuance; offline tests may call explicitly with synthetic candles."""
    if symbol not in SYMBOLS or type(timestamp) is not int or not DEV_START<=timestamp<VALIDATION_START or timestamp%STEP:
        raise PermissionError('Only aligned 2024 DEVELOPMENT boundaries')
    candles=tuple(candles)
    _chronology(candles,symbol,timestamp-600*STEP,600)
    visible=dict(symbol=symbol,timeframe='4h',open_time_ms=timestamp,completed=[c.record() for c in candles])
    return FeatureWindow(symbol,timestamp,candles,fingerprint(visible),_ISSUED)


@dataclass(frozen=True,init=False)
class FeatureView:
    """Owns authorized data; only at_open() output belongs at the feature boundary."""
    symbol: str
    _candles: tuple[AggregateCandle, ...] = field(repr=False)
    provenance: tuple[tuple[str,str], ...]

    def __init__(self, history, development):
        if type(history) is not HistoryPackage or history._issuer is not _ISSUED:
            raise PermissionError('Use verified Phase 6D0 history')
        if type(development) is not SignalDataView:
            raise PermissionError('Authorized Phase 6B2 view required, not raw data')
        meta=development.provenance
        request=SplitRequest(Role(meta['role']),meta['start_ms'],meta['end_ms'],meta['warmup_start_ms'])
        authorize(request,'4h')  # Before inspecting supplied development candles.
        symbol=history.symbol
        if (symbol not in SYMBOLS or history.history_fingerprint!=HISTORY_HASHES[symbol]
                or meta['symbol']!=symbol or meta['signal_timeframe']!='4h'
                or meta['execution_resolution']!='15m' or meta['warmup_signal_candles']!=99
                or meta['aggregation_version']!=DATA_VERSION or meta['policy_fingerprint']!=fingerprint(policy())):
            raise ValueError('Research view contract mismatch')
        hp=json.loads(history.provenance_json)
        if (type(history.candles) is not tuple or hp['history_view_fingerprint']!=history.history_fingerprint
                or fingerprint({k:v for k,v in hp.items() if k!='history_view_fingerprint'})!=history.history_fingerprint
                or fingerprint([c.record() for c in history.candles])!=hp['normalized_4h_fingerprint']):
            raise ValueError('History package was modified after verification')
        _chronology(history.candles,symbol,START,600)
        cs=tuple(development.candles)
        _chronology(cs,symbol,DEV_START-99*STEP,99+(VALIDATION_START-DEV_START)//STEP)
        if (fingerprint({k:v for k,v in meta.items() if k!='aggregate_fingerprint'})!=development.fingerprint
                or fingerprint([c.record() for c in cs])!=meta['normalized_aggregate_fingerprint']
                or history.candles[-99:]!=cs[:99]):
            raise ValueError('View integrity or overlapping warmup mismatch')
        object.__setattr__(self,'symbol',symbol)
        object.__setattr__(self,'_candles',history.candles+cs[99:])
        object.__setattr__(self,'provenance',(('history',history.history_fingerprint),('development',development.fingerprint)))

    def at_open(self, timestamp):
        if type(timestamp) is not int or not DEV_START<=timestamp<VALIDATION_START or timestamp%STEP:
            raise PermissionError('Warmup, non-boundary and validation/holdout evaluation forbidden')
        index=600+(timestamp-DEV_START)//STEP
        return _window(self.symbol,timestamp,self._candles[index-600:index])


@dataclass(frozen=True)
class FeatureParameters:
    ema_spans: tuple[int,...] = (9,20,21,50,100,200)
    trigger_rows: int = 99
    regime_rows: int = 600
    breakout_lookback: int = 20
    atr_period: int = 14
    atr_input_rows: int = 100

    def __post_init__(self):
        if (type(self.ema_spans) is not tuple or self.ema_spans!=(9,20,21,50,100,200)
                or any(type(x) is not int for x in self.ema_spans)
                or any(type(getattr(self,k)) is not int or getattr(self,k)!=v for k,v in
                    dict(trigger_rows=99,regime_rows=600,breakout_lookback=20,atr_period=14,atr_input_rows=100).items())):
            raise ValueError('Only frozen Phase 6D feature parameters allowed')


@dataclass(frozen=True)
class EMAValue:
    span: int
    completed_rows: int
    previous: Decimal
    latest: Decimal


@dataclass(frozen=True)
class BreakoutState:
    completed_close: Decimal
    preceding_high: Decimal
    preceding_low: Decimal
    bullish: bool
    bearish: bool
    previous_bullish: bool
    previous_bearish: bool
    fresh_bullish: bool
    fresh_bearish: bool


def _breakout(cs):
    high=max(c.high for c in cs[-21:-1]);low=min(c.low for c in cs[-21:-1])
    old_high=max(c.high for c in cs[-22:-2]);old_low=min(c.low for c in cs[-22:-2])
    bull,bear=cs[-1].close>high,cs[-1].close<low
    old_bull,old_bear=cs[-2].close>old_high,cs[-2].close<old_low
    return BreakoutState(cs[-1].close,high,low,bull,bear,old_bull,old_bear,bull and not old_bull,bear and not old_bear)


def _atr(cs):
    if len(cs)!=100: raise ValueError('ATR requires 100 completed candles including anchor')
    with localcontext(Context(prec=50)):
        tr=tuple(max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)) for p,c in zip(cs,cs[1:]))
        value=sum(tr[:14],Decimal(0))/14
        for item in tr[14:]: value=(13*value+item)/14
        return value


def _ribbon(values):
    if all(a>b for a,b in zip(values,values[1:])):return 'BULLISH'
    if all(a<b for a,b in zip(values,values[1:])):return 'BEARISH'
    return 'NEUTRAL'


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    open_time_ms: int
    timeframe: str
    completed_through_ms: int
    visible_fingerprint: str
    feature_version: str
    parameters: FeatureParameters
    emas: tuple[EMAValue,...]
    ribbon: str
    close_vs_ema200: str
    breakout: BreakoutState
    atr14: Decimal
    source_sha256: str
    software: tuple[tuple[str,str],...]
    snapshot_fingerprint: str


def calculate_features(window, parameters=FeatureParameters()):
    if type(window) is not FeatureWindow or window._issuer is not _ISSUED:
        raise PermissionError('An issued authorized feature window is required')
    if type(window.completed) is not tuple:raise ValueError('Immutable completed tuple required')
    if type(parameters) is not FeatureParameters or parameters!=FeatureParameters():
        raise ValueError('Frozen typed parameters required')
    checked=_window(window.symbol,window.open_time_ms,window.completed)
    if checked.visible_fingerprint!=window.visible_fingerprint:raise ValueError('Visible-window integrity failure')
    closes=[float(c.close) for c in window.completed]
    if any(not math.isfinite(v) or v<=0 for v in closes):raise ValueError('Unrepresentable indicator price')
    series=pd.Series(closes,dtype='float64')
    emas=[]
    for span in parameters.ema_spans:
        rows=99 if span in (9,21) else 600
        values=series.iloc[-rows:].ewm(span=span,adjust=False).mean()
        previous,latest=float(values.iloc[-2]),float(values.iloc[-1])
        if not all(math.isfinite(v) and v>0 for v in (previous,latest)):raise ValueError('Nonfinite EMA output')
        emas.append(EMAValue(span,rows,Decimal(str(previous)),Decimal(str(latest))))
    by_span={v.span:v.latest for v in emas}
    # Comparison uses the same float64 indicator boundary, not mixed precision.
    reference=Decimal(str(closes[-1]));ema200=by_span[200]
    payload=dict(symbol=window.symbol,open_time_ms=window.open_time_ms,timeframe='4h',
        completed_through_ms=window.open_time_ms-1,visible_fingerprint=window.visible_fingerprint,
        feature_version=VERSION,parameters=parameters,emas=tuple(emas),
        ribbon=_ribbon([by_span[s] for s in (20,50,100,200)]),
        close_vs_ema200='BULLISH' if reference>ema200 else 'BEARISH' if reference<ema200 else 'NEUTRAL',
        breakout=_breakout(window.completed),atr14=_atr(window.completed[-100:]),
        source_sha256=hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode('utf-8')).hexdigest(),
        software=(('python',platform.python_version()),('pandas',pd.__version__),('numpy',np.__version__)))
    record=dict(payload,parameters=asdict(parameters),emas=[asdict(v) for v in emas],breakout=asdict(payload['breakout']))
    return FeatureSnapshot(**payload,snapshot_fingerprint=fingerprint(record))
