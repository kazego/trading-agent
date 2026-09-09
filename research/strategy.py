"""Typed EMA decisions from authorized opening windows. No financial execution."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal
import hashlib
import math
from pathlib import Path
import platform
from typing import Protocol

import numpy as np
import pandas as pd

from backtest.data import DataValidationError, canonical_decimal, _hash
from backtest.strategy_adapter import Decision
from research.data import AggregateCandle, SignalOpening, STEPS, HISTORY
from research.spec import DEV_START, VALIDATION_START, SYMBOLS

FAMILY = "EMA_CROSSOVER"
VERSION = "research-ema-opening-v1"
_ISSUED = object()


@dataclass(frozen=True)
class EMAConfig:
    fast: int = 9
    slow: int = 21

    def __post_init__(self):
        if type(self.fast) is not int or type(self.slow) is not int or not 1 <= self.fast < self.slow:
            raise DataValidationError("EMA spans must be integers: 1 <= fast < slow")


@dataclass(frozen=True)
class OpeningWindow:
    symbol: str
    timeframe: str
    open_time_ms: int
    completed_through_ms: int
    completed: tuple[AggregateCandle, ...]
    opening_price: Decimal
    visible_fingerprint: str
    _issuer: object = field(default=None, repr=False, compare=False)


def _issued_window(opening: SignalOpening):
    """Private issuance used by the validated schedule; tests may explicitly use it.

    This token prevents accidental raw-data use, not deliberate Python introspection.
    """
    return OpeningWindow(opening.symbol, opening.timeframe, opening.open_time_ms,
                         opening.completed_through_ms, tuple(opening.completed), opening.opening_price,
                         opening.visible_fingerprint, _ISSUED)


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    signal_timeframe: str
    open_time_ms: int
    signal: str
    strategy_family: str
    strategy_version: str
    parameters: EMAConfig
    visible_fingerprint: str
    completed_open_times_ms: tuple[int, ...]
    reason: str
    source_sha256: str
    python_version: str
    pandas_version: str
    numpy_version: str
    decision_id: str


class Strategy(Protocol):
    def evaluate(self, window: OpeningWindow) -> SignalDecision: ...


def _frame(window):
    if not isinstance(window, OpeningWindow) or window._issuer is not _ISSUED:
        raise PermissionError("Use an opening window issued by the authorized research schedule")
    if (window.symbol not in SYMBOLS or window.timeframe not in STEPS
            or type(window.open_time_ms) is not int or not DEV_START <= window.open_time_ms < VALIDATION_START):
        raise PermissionError("Only 2024 DEVELOPMENT signal openings are authorized")
    step = STEPS[window.timeframe]
    if (window.open_time_ms % step or window.completed_through_ms != window.open_time_ms-1
            or len(window.completed) != HISTORY):
        raise DataValidationError("Exactly 99 completed aligned signal candles required")
    records = []
    for i,c in enumerate(window.completed):
        if (not isinstance(c, AggregateCandle) or c.symbol != window.symbol or c.timeframe != window.timeframe
                or c.open_time_ms != window.open_time_ms-(HISTORY-i)*step
                or c.close_time_ms != c.open_time_ms+step-1):
            raise DataValidationError("Mismatched, incomplete or future signal history")
        records.append(c.record())
    opening = canonical_decimal(window.opening_price)
    forming = dict(open=opening,high=opening,low=opening,close=opening,volume="0")
    visible = dict(symbol=window.symbol,timeframe=window.timeframe,open_time_ms=window.open_time_ms,
                   completed=records,forming=forming)
    if _hash(visible) != window.visible_fingerprint:
        raise DataValidationError("Visible-window fingerprint mismatch")
    records.append(dict(symbol=window.symbol,timeframe=window.timeframe,open_time_ms=window.open_time_ms,
                        close_time_ms=window.open_time_ms+step-1,**forming))
    frame = pd.DataFrame(records)
    for name in ("open","high","low","close","volume"):
        values = [float(row[name]) for row in records]
        if any(not math.isfinite(value) or value < 0 or (value == 0 and row[name] != "0")
               or (name != "volume" and value <= 0) for value,row in zip(values,records)):
            raise DataValidationError("Price/volume cannot be represented at the float64 strategy boundary")
        frame[name] = pd.Series(values,dtype="float64")
    return frame


@dataclass(frozen=True)
class EMACrossover:
    config: EMAConfig = EMAConfig()
    source_hash: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.config, EMAConfig):
            raise DataValidationError("Typed EMAConfig required")
        object.__setattr__(self, "source_hash", hashlib.sha256(
            Path(__file__).read_text(encoding="utf-8").encode("utf-8")).hexdigest())

    def evaluate(self, window: OpeningWindow) -> SignalDecision:
        frame = _frame(window)
        fast = frame["close"].ewm(span=self.config.fast,adjust=False).mean()
        slow = frame["close"].ewm(span=self.config.slow,adjust=False).mean()
        if len(frame) < self.config.slow+2:
            signal,reason = "NONE","INSUFFICIENT_EMA_HISTORY"
        elif fast.iloc[-3] <= slow.iloc[-3] and fast.iloc[-2] > slow.iloc[-2]:
            signal,reason = "LONG","CROSS_ABOVE"
        elif fast.iloc[-3] >= slow.iloc[-3] and fast.iloc[-2] < slow.iloc[-2]:
            signal,reason = "SHORT","CROSS_BELOW"
        else:
            signal,reason = "NONE","NO_CROSS"
        metadata = dict(symbol=window.symbol,signal_timeframe=window.timeframe,open_time_ms=window.open_time_ms,
            signal=signal,strategy_family=FAMILY,strategy_version=VERSION,parameters=self.config,
            visible_fingerprint=window.visible_fingerprint,
            completed_open_times_ms=tuple(c.open_time_ms for c in window.completed),reason=reason,
            source_sha256=self.source_hash,python_version=platform.python_version(),
            pandas_version=pd.__version__,numpy_version=np.__version__)
        identity = dict(metadata,parameters=asdict(self.config))
        return SignalDecision(**metadata,decision_id=_hash(identity))


@dataclass(frozen=True)
class ExecutionDecision(Decision):
    """Phase 5E compatibility envelope; timeframe is execution resolution (15m)."""
    signal_timeframe: str
    strategy_family: str
    reason: str
    signal_decision_id: str


def _execution_decision(signal, event):
    metadata = dict(symbol=signal.symbol,timeframe=event.timeframe,open_time_ms=signal.open_time_ms,
        completed_through_ms=signal.open_time_ms-1,signal=signal.signal,
        completed_open_times_ms=signal.completed_open_times_ms,dataset_fingerprint=event.dataset_fingerprint,
        window_fingerprint=signal.visible_fingerprint,strategy_version=signal.strategy_version,
        strategy_source_sha256=signal.source_sha256,adapter_version=VERSION,
        ema_spans=(signal.parameters.fast,signal.parameters.slow),pandas_version=signal.pandas_version,
        numpy_version=signal.numpy_version,python_version=signal.python_version,
        signal_timeframe=signal.signal_timeframe,strategy_family=signal.strategy_family,
        reason=signal.reason,signal_decision_id=signal.decision_id)
    return ExecutionDecision(**metadata,decision_id=_hash(metadata))
