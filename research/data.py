"""Development-only signal data views. No signals, execution, network or writes."""
from dataclasses import dataclass, asdict
from decimal import Decimal, Context, localcontext
import json
from pathlib import Path

from backtest.data import (Candle, Dataset, Source, DataValidationError, canonical_decimal,
                           ingest, _hash, _encoded)
from research.spec import (Role, SplitRequest, SYMBOLS, STEP_MS, DEV_START,
                           VALIDATION_START, policy, fingerprint)

VERSION = "research-aggregate-v1"
STEPS = {"15m": STEP_MS, "1h": 4*STEP_MS, "4h": 16*STEP_MS}
HISTORY = 99
DECEMBER_START = 1701388800000
ACQUISITION_HASH = "0b3249ee8b2038875b022dd41bf134c5c29c5c4bf2eda8b80c0c78adcb5af721"


def authorize(request, timeframe):
    """Phase 6B1 policy, with a stricter 6B2 development-only capability.

    No validation/holdout bypass is provided. Future validation support must
    integrate explicit registry promotion before broadening this capability.
    """
    if not isinstance(request, SplitRequest) or request.role != Role.DEVELOPMENT:
        raise PermissionError("Only DEVELOPMENT data access is implemented in Phase 6B2")
    if timeframe not in STEPS:
        raise DataValidationError("Unsupported signal timeframe")
    step = STEPS[timeframe]
    if (request.start_ms, request.end_ms) != (DEV_START, VALIDATION_START):
        raise PermissionError("Exact 2024 DEVELOPMENT interval required")
    if request.warmup_start_ms != request.start_ms-HISTORY*step:
        raise DataValidationError(f"Exactly {HISTORY} completed {timeframe} history candles required")
    if request.start_ms % step or request.end_ms % step:
        raise DataValidationError("Evaluation must align to signal buckets")


def exact_sum(values):
    values = tuple(values)
    if not values:
        return Decimal(0)
    # Enough significant digits for the whole integer span, fractional span and
    # possible carries. Do not inherit an ambient Decimal rounding precision.
    places = max(v.adjusted() for v in values)-min(v.as_tuple().exponent for v in values)+len(str(len(values)))+2
    with localcontext(Context(prec=max(28, places))):
        return sum(values, Decimal(0))


@dataclass(frozen=True)
class AggregateCandle:
    symbol: str
    timeframe: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        if self.symbol not in SYMBOLS or self.timeframe not in STEPS:
            raise DataValidationError("Unsupported aggregate symbol/timeframe")
        step = STEPS[self.timeframe]
        if (type(self.open_time_ms) is not int or type(self.close_time_ms) is not int
                or self.open_time_ms % step or self.close_time_ms != self.open_time_ms+step-1):
            raise DataValidationError("Aggregate must be UTC epoch aligned")
        for field in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, field, Decimal(canonical_decimal(getattr(self, field))))
        if (min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0
                or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise DataValidationError("Invalid aggregate OHLCV")

    def record(self):
        return {k: canonical_decimal(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


def aggregate_bucket(components, timeframe):
    """Pure aggregation of one bucket; completeness, order and identity mandatory."""
    if timeframe not in STEPS:
        raise DataValidationError("Unsupported signal timeframe")
    components = tuple(components)
    step = STEPS[timeframe]
    if len(components) != step//STEP_MS or any(not isinstance(c, Candle) for c in components):
        raise DataValidationError("Incomplete bucket or non-baseline component")
    first = components[0]
    if first.open_time_ms % step:
        raise DataValidationError("Misaligned first component")
    for i,c in enumerate(components):
        if (c.symbol != first.symbol or c.timeframe != "15m"
                or c.open_time_ms != first.open_time_ms+i*STEP_MS
                or c.close_time_ms != c.open_time_ms+STEP_MS-1):
            raise DataValidationError("Duplicate/gap/misaligned/mismatched component")
    return AggregateCandle(first.symbol, timeframe, first.open_time_ms, first.open_time_ms+step-1,
                           first.open, max(c.high for c in components), min(c.low for c in components),
                           components[-1].close, exact_sum(c.volume for c in components))


@dataclass(frozen=True)
class SignalOpening:
    symbol: str
    timeframe: str
    open_time_ms: int
    completed_through_ms: int
    completed: tuple[AggregateCandle, ...]
    opening_price: Decimal
    visible_fingerprint: str


@dataclass(frozen=True)
class SignalDataView:
    """Owns full historical data; pass only at_open() output to future consumers.

    This is an information boundary, not protection against Python introspection.
    No signal decisions are evaluated or emitted by this object.
    """
    candles: tuple[AggregateCandle, ...]
    provenance_json: str

    @property
    def provenance(self):
        return json.loads(self.provenance_json)

    @property
    def fingerprint(self):
        return self.provenance["aggregate_fingerprint"]

    @property
    def evaluation_candles(self):
        return self.candles[HISTORY:]

    def at_open(self, timestamp):
        meta = self.provenance
        step = STEPS[meta["signal_timeframe"]]
        if type(timestamp) is not int or not meta["start_ms"] <= timestamp < meta["end_ms"] or timestamp % step:
            raise DataValidationError("Not an evaluation signal opening; warmup cannot be evaluated")
        index = HISTORY+(timestamp-meta["start_ms"])//step
        completed = self.candles[index-HISTORY:index]
        if len(completed) != HISTORY or any(c.close_time_ms >= timestamp for c in completed):
            raise DataValidationError("Incomplete/future history")
        # Only the current OPEN is selected, never current aggregate H/L/C/V.
        opening = self.candles[index].open
        visible = dict(symbol=meta["symbol"], timeframe=meta["signal_timeframe"],
                       open_time_ms=timestamp, completed=[c.record() for c in completed],
                       forming=dict(open=canonical_decimal(opening), high=canonical_decimal(opening),
                                    low=canonical_decimal(opening), close=canonical_decimal(opening), volume="0"))
        return SignalOpening(meta["symbol"], meta["signal_timeframe"], timestamp, timestamp-1,
                             completed, opening, _hash(visible))


def build_view(dataset, symbol, timeframe, request):
    authorize(request, timeframe)  # Must precede any inspection of the supplied data.
    if not isinstance(dataset, Dataset) or symbol not in SYMBOLS:
        raise DataValidationError("Validated Phase 5B dataset and configured symbol required")
    meta = dataset.provenance
    if (meta["symbols"] != [symbol] or meta["timeframe"] != "15m" or meta["timestamp_unit"] != "ms"
            or (meta["start_ms"],meta["end_ms"]) != (request.start_ms,request.end_ms)
            or meta["as_of_ms"] != request.end_ms):
        raise DataValidationError("Source must be a development-only 15m dataset")
    if meta["duplicate_count"] or meta["excluded_incomplete_count"]:
        raise DataValidationError("Duplicate/incomplete components rejected for research aggregation")
    start = request.start_ms-meta["warmup_candles"]*STEP_MS
    expected = (request.end_ms-start)//STEP_MS
    if len(dataset.candles) != expected:
        raise DataValidationError("Missing/duplicate source components")
    for i,c in enumerate(dataset.candles):
        if (c.symbol != symbol or c.timeframe != "15m" or c.open_time_ms != start+i*STEP_MS
                or c.close_time_ms != c.open_time_ms+STEP_MS-1 or c.close_time_ms >= request.end_ms):
            raise DataValidationError("Source gap, duplicate, future or mismatched candle")
    if (_hash({k:v for k,v in meta.items() if k != "dataset_fingerprint"}) != dataset.fingerprint
            or _hash([c.record() for c in dataset.candles]) != meta["normalized_candle_fingerprint"]):
        raise DataValidationError("Source fingerprint mismatch")
    if start > request.warmup_start_ms:
        raise DataValidationError(f"Warmup deficient by {(start-request.warmup_start_ms)//STEP_MS} underlying 15m candles for {symbol} {timeframe}")
    selected = dataset.candles[(request.warmup_start_ms-start)//STEP_MS:]
    size = STEPS[timeframe]//STEP_MS
    if len(selected) % size:
        raise DataValidationError("Partial required signal bucket")
    aggregates = tuple(aggregate_bucket(selected[i:i+size],timeframe) for i in range(0,len(selected),size))
    provenance = dict(aggregation_version=VERSION, source_15m_dataset_fingerprint=dataset.fingerprint,
        source_normalized_candle_fingerprint=meta["normalized_candle_fingerprint"],
        policy_fingerprint=fingerprint(policy()), role=request.role.value, symbol=symbol, signal_timeframe=timeframe,
        execution_resolution="15m", utc_bucket_rule="floor(epoch_ms/signal_step_ms)*signal_step_ms",
        start_ms=request.start_ms, end_ms=request.end_ms, interval_semantics="[start,end)",
        warmup_start_ms=request.warmup_start_ms, warmup_end_ms=request.start_ms,
        warmup_signal_candles=HISTORY, warmup_components=HISTORY*size,
        components_per_bucket=size, signal_step_ms=STEPS[timeframe],
        evaluation_candle_count=len(aggregates)-HISTORY,
        normalized_aggregate_fingerprint=_hash([c.record() for c in aggregates]))
    provenance["aggregate_fingerprint"] = _hash(provenance)
    return SignalDataView(aggregates, _encoded(provenance).decode("ascii"))


def load_local_development(historical_root, symbol):
    """Read only Dec 2023 + 2024 normalized files; never inspect 2025 candle files.

    The old manifest is metadata only. Newly selected development data is validated
    independently, leaving all existing archives/manifests/datasets unchanged.
    """
    if symbol not in SYMBOLS:
        raise DataValidationError("Unsupported symbol")
    request = SplitRequest(Role.DEVELOPMENT,DEV_START,VALIDATION_START,DEV_START-99*STEPS["4h"])
    authorize(request,"4h")
    root = Path(historical_root)
    manifest = json.loads((root/"acquisition-manifest.json").read_text())
    if _hash({k:v for k,v in manifest.items() if k != "fingerprint"}) != manifest["fingerprint"] or manifest["fingerprint"] != ACQUISITION_HASH:
        raise DataValidationError("Approved acquisition manifest fingerprint mismatch")
    months = ["2023-12"]+[f"2024-{m:02d}" for m in range(1,13)]
    sources = []
    for month in months:
        entries = [r for r in manifest["archives"] if r["symbol"] == symbol and r["month"] == month]
        if len(entries) != 1:
            raise DataValidationError(f"Missing/duplicate local source metadata: {symbol} {month}")
        r = entries[0]
        path = root/"normalized"/symbol/f"{symbol}-15m-{month}.csv"
        if not path.is_file():
            raise DataValidationError(f"Missing local file: {symbol} {month}; no download attempted")
        sources.append(Source(path,symbol,r["url"],r["normalized_csv_sha256"]))
    december = ingest(sources[:1],symbols=(symbol,),start_ms=DECEMBER_START,end_ms=DEV_START,
                      as_of_ms=DEV_START,timestamp_unit="ms",warmup_candles=0)
    if december.provenance["duplicate_count"] or december.provenance["excluded_incomplete_count"]:
        raise DataValidationError("December source contains duplicate/incomplete candles")
    dataset = ingest(sources,symbols=(symbol,),start_ms=DEV_START,end_ms=VALIDATION_START,
                     as_of_ms=VALIDATION_START,timestamp_unit="ms",warmup_candles=99*16)
    return dataset, len(december.candles)
