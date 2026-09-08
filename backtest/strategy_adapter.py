"""Call the frozen EMA strategy on exactly the information available at an open."""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import platform

import numpy as np
import pandas as pd

import strategy
from backtest.clock import HISTORY_ROWS, OpeningEvent
from backtest.data import DataValidationError, STEP_MS, _hash, canonical_decimal

STRATEGY_VERSION = "frozen-ema-9-21-v1"
ADAPTER_VERSION = "opening-window-v1"
FINANCIAL_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Decision:
    symbol: str
    timeframe: str
    open_time_ms: int
    completed_through_ms: int
    signal: str
    completed_open_times_ms: tuple[int, ...]
    dataset_fingerprint: str
    window_fingerprint: str
    strategy_version: str
    strategy_source_sha256: str
    adapter_version: str
    ema_spans: tuple[int, int]
    pandas_version: str
    numpy_version: str
    python_version: str
    decision_id: str


def _visible_records(event):
    if event.timeframe != "15m" or len(event.completed) != HISTORY_ROWS:
        raise DataValidationError("Strategy evaluation requires exactly 99 completed candles")
    if event.completed_through_ms != event.open_time_ms - 1:
        raise DataValidationError("Invalid completed-candle boundary")
    for index, candle in enumerate(event.completed):
        expected = event.open_time_ms - (HISTORY_ROWS - index) * STEP_MS
        if (candle.symbol != event.symbol or candle.timeframe != event.timeframe
                or candle.open_time_ms != expected or candle.close_time_ms >= event.open_time_ms):
            raise DataValidationError("Invalid completed strategy window")
    records = [c.record() for c in event.completed]
    opening = canonical_decimal(event.opening_price)
    # Scheduled close time is known at open; eventual H/L/C/V are never accessed.
    records.append(dict(symbol=event.symbol, timeframe=event.timeframe,
                        open_time_ms=event.open_time_ms,
                        close_time_ms=event.open_time_ms + STEP_MS - 1,
                        open=opening, high=opening, low=opening, close=opening, volume="0"))
    return records


def _frame(records):
    frame = pd.DataFrame(records)
    for column in FINANCIAL_COLUMNS:
        values = [float(row[column]) for row in records]
        if any(not math.isfinite(value) or value < 0 or (value == 0 and row[column] != "0")
               for value, row in zip(values, records)) or (
                   column != "volume" and any(value <= 0 for value in values)):
            raise DataValidationError("Value cannot be represented as finite strategy float64")
        frame[column] = pd.Series(values, dtype="float64")
    return frame


class FrozenStrategyAdapter:
    """No indicators cached, no configuration mutations, no trade intents."""

    def __init__(self):
        # Normalize file newlines so Windows/Linux checkouts identify the same source.
        self._source_hash = hashlib.sha256(
            Path(strategy.__file__).read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()

    def evaluate(self, event: OpeningEvent) -> Decision:
        if (strategy.config.EMA_FAST, strategy.config.EMA_SLOW) != (9, 21):
            raise DataValidationError("Frozen strategy requires EMA 9/21 configuration")
        records = _visible_records(event)
        signal = strategy.get_signal(_frame(records))
        if signal not in ("LONG", "SHORT", "NONE"):
            raise DataValidationError("Unknown strategy signal")
        metadata = dict(
            symbol=event.symbol, timeframe=event.timeframe, open_time_ms=event.open_time_ms,
            completed_through_ms=event.completed_through_ms, signal=signal,
            completed_open_times_ms=tuple(c.open_time_ms for c in event.completed),
            dataset_fingerprint=event.dataset_fingerprint, window_fingerprint=_hash(records),
            strategy_version=STRATEGY_VERSION, strategy_source_sha256=self._source_hash,
            adapter_version=ADAPTER_VERSION, ema_spans=(9, 21), pandas_version=pd.__version__,
            numpy_version=np.__version__, python_version=platform.python_version(),
        )
        return Decision(**metadata, decision_id=_hash(metadata))
