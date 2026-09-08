"""Historical opening events. Only completed candles and the current open are exposed."""

from dataclasses import dataclass
from decimal import Decimal

from backtest.data import Candle, Dataset, DataValidationError, STEP_MS

HISTORY_ROWS = 99


@dataclass(frozen=True)
class OpeningEvent:
    symbol: str
    timeframe: str
    open_time_ms: int
    completed_through_ms: int
    opening_price: Decimal
    completed: tuple[Candle, ...]
    dataset_fingerprint: str


class HistoricalClock:
    """Replay one symbol independently. A fresh iteration restarts deterministically.

    The clock necessarily owns historical input including future bars; consumers
    receive OpeningEvent values, never a dataset or a current completed OHLC bar.
    This is a data-access boundary, not a sandbox against Python introspection.
    """

    def __init__(self, dataset: Dataset, symbol: str):
        metadata = dataset.provenance
        if symbol not in metadata["symbols"] or metadata["timeframe"] != "15m":
            raise DataValidationError("Clock requires a configured 15m symbol")
        if metadata["warmup_candles"] < HISTORY_ROWS:
            raise DataValidationError("Clock requires at least 99 completed warmup candles")
        self._candles = tuple(c for c in dataset.candles if c.symbol == symbol)
        self._start = metadata["start_ms"]
        self._end = metadata["end_ms"]
        self._fingerprint = dataset.fingerprint
        expected_start = self._start - metadata["warmup_candles"] * STEP_MS
        expected_count = (self._end - expected_start) // STEP_MS
        if len(self._candles) != expected_count or any(
            c.timeframe != "15m" or c.open_time_ms != expected_start + i * STEP_MS
            or c.close_time_ms >= metadata["as_of_ms"]
            for i, c in enumerate(self._candles)
        ):
            raise DataValidationError("Clock requires complete contiguous validated input")

    def __iter__(self):
        for index, candle in enumerate(self._candles):
            if self._start <= candle.open_time_ms < self._end:
                yield OpeningEvent(
                    candle.symbol, candle.timeframe, candle.open_time_ms,
                    candle.open_time_ms - 1, candle.open,
                    self._candles[index - HISTORY_ROWS:index], self._fingerprint,
                )
