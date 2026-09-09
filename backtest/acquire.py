"""Bounded public-archive acquisition for baseline-001. Never runs a backtest."""

import argparse
import calendar
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import re
import urllib.request
import zipfile

from backtest.data import DataValidationError, HEADER, SYMBOLS, STEP_MS, Source, _encoded, _hash, ingest

VERSION = "baseline-001-acquisition-v1"
HOST = "https://data.binance.vision"
MONTHS = ("2023-12",) + tuple(f"{year}-{month:02}" for year in (2024, 2025) for month in range(1, 13))
START_MS = 1704067200000
END_MS = 1767225600000
WARMUP = 99
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "baseline-001" / "historical"
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_CHECKSUM_BYTES = 512


@dataclass(frozen=True)
class Archive:
    symbol: str
    month: str
    timeframe: str = "15m"

    def __post_init__(self):
        if self.symbol not in SYMBOLS or self.month not in MONTHS or self.timeframe != "15m":
            raise DataValidationError("Only baseline-001 symbols/months/15m are allowed")

    @property
    def filename(self):
        return f"{self.symbol}-15m-{self.month}.zip"

    @property
    def url(self):
        return f"{HOST}/data/futures/um/monthly/klines/{self.symbol}/15m/{self.filename}"


def plan():
    return tuple(Archive(symbol, month) for symbol in SYMBOLS for month in MONTHS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataValidationError("Archive redirects are not permitted")


def _fetch(url, limit):
    allowed = {a.url + suffix for a in plan() for suffix in ("", ".CHECKSUM")}
    if url not in allowed:
        raise DataValidationError("URL is outside the fixed official archive plan")
    request = urllib.request.Request(url, headers={"User-Agent": "trading-agent-baseline-001", "Accept-Encoding": "identity"})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
        if response.status != 200 or response.geturl() != url:
            raise DataValidationError("Unexpected archive response")
        content = response.read(limit + 1)
    if not content or len(content) > limit:
        raise DataValidationError("Empty or oversized archive response")
    return content


def _safe_path(root, *parts):
    root = Path(root).resolve()
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in (".", "..") for p in parts):
        raise DataValidationError("Unsafe artifact path component")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise DataValidationError("Artifact path escapes selected root")
    return path


def _save_or_match(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size != len(content) or path.read_bytes() != content:
            raise DataValidationError("Existing artifact differs; refusing overwrite")
    else:
        with path.open("xb") as stream:
            stream.write(content)


def _cached_or_fetch(path, url, limit):
    if path.exists():
        if not 0 < path.stat().st_size <= limit:
            raise DataValidationError("Invalid cached file size")
        return path.read_bytes()
    content = _fetch(url, limit)
    _save_or_match(path, content)
    return content


def checksum_digest(content, archive):
    if not 0 < len(content) <= MAX_CHECKSUM_BYTES:
        raise DataValidationError("Invalid checksum file size")
    try:
        text = content.decode("ascii").strip()
    except UnicodeError as exc:
        raise DataValidationError("Invalid checksum encoding") from exc
    match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?" + re.escape(archive.filename), text)
    if match is None:
        raise DataValidationError("Checksum must name the exact expected archive")
    return match[1].lower()


def verify_checksum(content, checksum, archive):
    digest = checksum_digest(checksum, archive)
    if hashlib.sha256(content).hexdigest() != digest:
        raise DataValidationError("Official archive SHA-256 mismatch")
    return digest


def normalize_archive(content, archive):
    """Explicit unit recognition against the named month, never magnitude alone."""
    if not 0 < len(content) <= MAX_ZIP_BYTES:
        raise DataValidationError("Invalid archive size")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zipped:
            entries = zipped.infolist()
            expected = archive.filename.removesuffix(".zip") + ".csv"
            if (len(entries) != 1 or entries[0].filename != expected
                    or entries[0].file_size > MAX_CSV_BYTES or entries[0].flag_bits & 1):
                raise DataValidationError("Unexpected archive member/size/encryption")
            raw = zipped.read(entries[0])
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), strict=True))
    except (zipfile.BadZipFile, UnicodeError, csv.Error) as exc:
        raise DataValidationError("Malformed public archive") from exc
    if rows and tuple(rows[0]) == HEADER:
        rows = rows[1:]
    if not rows:
        raise DataValidationError("Empty kline archive")
    year, month = map(int, archive.month.split("-"))
    first = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp()) * 1000
    end = first + calendar.monthrange(year, month)[1] * 86400000
    units = set()
    for row in rows:
        if len(row) != 12 or not all(row[i].isascii() and row[i].isdigit() for i in (0, 6)):
            raise DataValidationError("Invalid twelve-column timestamp fields")
        opening, closing = int(row[0]), int(row[6])
        if first <= opening < end and closing == opening + STEP_MS - 1:
            unit = "milliseconds"
        elif first * 1000 <= opening < end * 1000 and opening % 1000 == 0:
            if closing == opening + STEP_MS * 1000 - 1:
                unit = "microseconds_interval_end"
            elif closing == opening + (STEP_MS - 1) * 1000:
                unit = "microseconds_scaled_milliseconds"
            else:
                raise DataValidationError("Ambiguous microsecond close timestamp")
            opening, closing = opening // 1000, closing // 1000
        else:
            raise DataValidationError("Unknown timestamp units or wrong archive month")
        if opening % STEP_MS or not first <= opening < closing < end:
            raise DataValidationError("Timestamp outside monthly 15m contract")
        units.add(unit)
        row[0], row[6] = str(opening), str(closing)
    if len(units) != 1:
        raise DataValidationError("Mixed timestamp conventions within archive")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    normalized = output.getvalue().encode("utf-8")
    return normalized, next(iter(units)), len(rows)


def acquire_and_validate(root=DEFAULT_ROOT):
    """Exactly 75 archives/checksums; resume identical local files; no financial run."""
    root = Path(root)
    sources = {s: [] for s in SYMBOLS}
    archive_records = []
    for archive in plan():
        checksum_path = _safe_path(root, "raw", archive.symbol, archive.filename + ".CHECKSUM")
        archive_path = _safe_path(root, "raw", archive.symbol, archive.filename)
        checksum = _cached_or_fetch(checksum_path, archive.url + ".CHECKSUM", MAX_CHECKSUM_BYTES)
        checksum_digest(checksum, archive)  # Reject invalid checksum before downloading ZIP.
        content = _cached_or_fetch(archive_path, archive.url, MAX_ZIP_BYTES)
        digest = verify_checksum(content, checksum, archive)
        normalized, unit, rows = normalize_archive(content, archive)
        normalized_path = _safe_path(root, "normalized", archive.symbol, archive.filename.removesuffix(".zip") + ".csv")
        _save_or_match(normalized_path, normalized)
        normalized_digest = hashlib.sha256(normalized).hexdigest()
        sources[archive.symbol].append(Source(normalized_path, archive.symbol, archive.url, normalized_digest))
        archive_records.append(dict(symbol=archive.symbol, month=archive.month, url=archive.url,
                                    checksum_url=archive.url + ".CHECKSUM", source_sha256=digest,
                                    checksum_file_sha256=hashlib.sha256(checksum).hexdigest(),
                                    normalized_csv_sha256=normalized_digest, timestamp_contract=unit,
                                    source_rows=rows, purpose="WARMUP_SOURCE" if archive.month == "2023-12" else "EVALUATION_SOURCE"))
        print(f"Validated archive {archive.symbol} {archive.month}: {rows} rows, {unit}", flush=True)
    datasets = {}
    for symbol in SYMBOLS:
        dataset = ingest(sources[symbol], symbols=(symbol,), start_ms=START_MS, end_ms=END_MS,
                         as_of_ms=END_MS, timestamp_unit="ms", warmup_candles=WARMUP)
        datasets[symbol] = dict(candle_count=len(dataset.candles), evaluation_candle_count=(END_MS - START_MS) // STEP_MS,
                                provenance=dataset.provenance)
        print(f"Phase 5B validation passed: {symbol}, {len(dataset.candles)} selected candles", flush=True)
    manifest = dict(acquisition_version=VERSION, run_id="baseline-001", financial_run_performed=False,
                    status="DATA_VALIDATED_AWAITING_COST_APPROVAL", start_ms=START_MS, end_ms=END_MS,
                    warmup_candles=WARMUP, warmup_start_ms=START_MS - WARMUP * STEP_MS,
                    archives=archive_records, datasets=datasets)
    manifest["fingerprint"] = _hash(manifest)
    _save_or_match(_safe_path(root, "acquisition-manifest.json"), _encoded(manifest) + b"\n")
    pending = dict(run_id="baseline-001", status="NOT_RUN", ema_fast=9, ema_slow=21, timeframe="15m",
                   symbols=SYMBOLS, priority=SYMBOLS, starting_equity="1000", risk_pct="1", max_leverage="3",
                   max_concurrent_positions=1, sl_pct="1.5", tp_pct="3", dual_touch="STOP_LOSS",
                   entry="NEXT_CANDLE_OPEN", funding=False, liquidation=False,
                   cost_mode=None, entry_fee_rate=None, exit_fee_rate=None, slippage_bps=None,
                   symbol_rule_assumptions=None, acquisition_fingerprint=manifest["fingerprint"])
    _save_or_match(_safe_path(root, "baseline-001-pending.json"), _encoded(pending) + b"\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-public-baseline-001", action="store_true", required=True)
    parser.parse_args()
    manifest = acquire_and_validate()
    print("Data validated. Financial study NOT RUN: research fees/slippage require operator approval.")
    print("Acquisition fingerprint: " + manifest["fingerprint"])


if __name__ == "__main__":
    main()
