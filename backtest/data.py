"""Strict local USD-M 15m archive ingestion; no live application imports or network."""

import csv
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import zipfile

SYMBOLS = ("BTCUSDT", "ETHUSDT", "ADAUSDT")
STEP_MS = 900_000
VERSION = "usd-m-15m-v1"
HEADER = ("open_time", "open", "high", "low", "close", "volume", "close_time",
          "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore")


class DataValidationError(ValueError):
    """Invalid or incomplete historical input; no usable dataset is returned."""


def canonical_decimal(value):
    """Normalize without Decimal.normalize(), which can round under ambient context."""
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise DataValidationError("Financial values require decimal strings, Decimal or int")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise DataValidationError("Invalid decimal") from exc
    if not number.is_finite():
        raise DataValidationError("Non-finite decimal")
    if number == 0:
        return "0"
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _timestamp(value):
    # Explicit millisecond contract, bounded to 2000..2100 to reject s/us/ns inputs.
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or not 946684800000 <= value < 4102444800000:
        raise DataValidationError("Timestamp must be UTC epoch milliseconds in 2000..2100")
    return value


@dataclass(frozen=True)
class Candle:
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
        if self.symbol not in SYMBOLS or self.timeframe != "15m":
            raise DataValidationError("Unsupported symbol/timeframe")
        for name in ("open_time_ms", "close_time_ms"):
            object.__setattr__(self, name, _timestamp(getattr(self, name)))
        if self.open_time_ms % STEP_MS or self.close_time_ms != self.open_time_ms + STEP_MS - 1:
            raise DataValidationError("Invalid candle alignment or close time")
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, Decimal(canonical_decimal(getattr(self, name))))
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise DataValidationError("Prices must be positive and volume nonnegative")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise DataValidationError("Malformed OHLC bounds")

    def record(self):
        return {key: canonical_decimal(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class Source:
    path: Path
    symbol: str
    identity: str
    checksum_sha256: str | None = None


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


@dataclass(frozen=True)
class Dataset:
    candles: tuple[Candle, ...]
    # JSON text makes the provenance immutable alongside frozen candles.
    provenance_json: str

    @property
    def provenance(self):
        return json.loads(self.provenance_json)

    @property
    def fingerprint(self):
        return self.provenance["dataset_fingerprint"]

    def write(self, path):
        """Create an exclusive JSON artifact; never open/overwrite a live database."""
        path = Path(path)
        if path.suffix.lower() != ".json":
            raise DataValidationError("Historical artifacts require a .json filename")
        payload = {"provenance": self.provenance, "candles": [c.record() for c in self.candles]}
        with path.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(_encoded(payload).decode("ascii") + "\n")


def _rows(source):
    raw = Path(source.path).read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if source.checksum_sha256 is not None and source.checksum_sha256.lower() != checksum:
        raise DataValidationError("Source checksum mismatch")
    member = None
    if Path(source.path).suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
                    raise DataValidationError("Archive must contain exactly one CSV")
                member = members[0].filename
                raw_csv = archive.read(member)
        except zipfile.BadZipFile as exc:
            raise DataValidationError("Invalid ZIP archive") from exc
    elif Path(source.path).suffix.lower() == ".csv":
        raw_csv = raw
    else:
        raise DataValidationError("Only local CSV or single-CSV ZIP files are supported")
    try:
        rows = list(csv.reader(io.StringIO(raw_csv.decode("utf-8-sig")), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise DataValidationError("Invalid CSV encoding/format") from exc
    if rows and tuple(rows[0]) == HEADER:
        rows = rows[1:]
    if not rows:
        raise DataValidationError("Empty source")
    candles = []
    previous = None
    for row in rows:
        if len(row) != 12:
            raise DataValidationError("Expected twelve Binance kline fields")
        candle = Candle(source.symbol, "15m", row[0], row[6], *row[1:6])
        if previous is not None and candle.open_time_ms < previous:
            raise DataValidationError("Source rows are out of chronological order")
        previous = candle.open_time_ms
        candles.append(candle)
    return candles, {"identity": source.identity, "symbol": source.symbol,
                     "sha256": checksum, "supplied_sha256": source.checksum_sha256.lower()
                     if source.checksum_sha256 is not None else None, "archive_member": member}


def ingest(sources, *, symbols, start_ms, end_ms, as_of_ms, timestamp_unit,
           timeframe="15m", warmup_candles=0):
    """Validate all input, then select warmup + complete [start,end) candles.

    Source-file order is immaterial; rows within each file must already be ordered.
    Completion means close_time_ms < as_of_ms. No data is padded or downloaded.
    """
    symbols = tuple(symbols)
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in SYMBOLS for s in symbols):
        raise DataValidationError("Invalid configured symbols")
    symbols = tuple(s for s in SYMBOLS if s in symbols)
    if timeframe != "15m" or timestamp_unit != "ms":
        raise DataValidationError("Only explicit ms timestamps and 15m candles are supported")
    start_ms, end_ms, as_of_ms = map(_timestamp, (start_ms, end_ms, as_of_ms))
    if start_ms % STEP_MS or end_ms % STEP_MS or start_ms >= end_ms:
        raise DataValidationError("Interval must be aligned and nonempty")
    if type(warmup_candles) is not int or warmup_candles < 0:
        raise DataValidationError("Warmup must be a nonnegative integer")
    required_start = _timestamp(start_ms - warmup_candles * STEP_MS)
    by_symbol = {s: {} for s in symbols}
    provenance, identities = [], set()
    duplicates = incomplete = 0
    for source in sources:
        if source.symbol not in symbols or not isinstance(source.identity, str) or not source.identity.strip():
            raise DataValidationError("Source must identify a configured symbol and nonempty identity")
        if source.identity in identities:
            raise DataValidationError("Source identities must be unique")
        identities.add(source.identity)
        candles, metadata = _rows(source)
        provenance.append(metadata)
        for candle in candles:
            bucket = by_symbol[candle.symbol]
            old = bucket.get(candle.open_time_ms)
            if old is not None:
                if old != candle:
                    raise DataValidationError("Conflicting duplicate candle")
                duplicates += 1
            else:
                bucket[candle.open_time_ms] = candle
    selected = []
    for symbol, bucket in by_symbol.items():
        ordered = sorted(bucket.values(), key=lambda c: c.open_time_ms)
        if any(b.open_time_ms - a.open_time_ms != STEP_MS for a, b in zip(ordered, ordered[1:])):
            raise DataValidationError("Missing candle/gap in source data")
        complete = {c.open_time_ms: c for c in ordered if c.close_time_ms < as_of_ms}
        incomplete += len(ordered) - len(complete)
        for timestamp in range(required_start, end_ms, STEP_MS):
            if timestamp not in complete:
                raise DataValidationError("Missing complete interval/warmup candle")
            selected.append(complete[timestamp])
    selected.sort(key=lambda c: (c.open_time_ms, SYMBOLS.index(c.symbol)))
    records = [c.record() for c in selected]
    metadata = {"schema_version": VERSION, "normalization_version": VERSION,
                "symbols": list(symbols), "timeframe": timeframe, "timestamp_unit": "ms",
                "start_ms": start_ms, "end_ms": end_ms, "as_of_ms": as_of_ms,
                "warmup_candles": warmup_candles, "duplicate_count": duplicates,
                "excluded_incomplete_count": incomplete,
                "sources": sorted(provenance, key=lambda p: p["identity"]),
                "normalized_candle_fingerprint": _hash(records)}
    metadata["dataset_fingerprint"] = _hash(metadata)
    return Dataset(tuple(selected), _encoded(metadata).decode("ascii"))
