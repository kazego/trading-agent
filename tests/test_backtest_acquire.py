"""Offline-only tests for the fixed public archive plan and normalization."""

from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backtest import acquire
from backtest.data import DataValidationError, STEP_MS

T = 1704067200000


def archive_bytes(spec, unit="ms", rows=2, member=None, malformed=None):
    lines = []
    for i in range(rows):
        opening, closing = T + i * STEP_MS, T + (i + 1) * STEP_MS - 1
        if unit == "us":
            opening, closing = opening * 1000, closing * 1000 + 999
        elif unit == "scaled_us":
            opening, closing = opening * 1000, closing * 1000
        elif unit == "seconds":
            opening, closing = opening // 1000, closing // 1000
        lines.append(",".join(map(str, [opening, 100, 101, 99, 100, 1, closing, 0, 0, 0, 0, 0])))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr(member or spec.filename.replace(".zip", ".csv"), malformed if malformed is not None else "\n".join(lines) + "\n")
    return output.getvalue()


def checksum(content, spec):
    return (hashlib.sha256(content).hexdigest() + "  " + spec.filename + "\n").encode("ascii")


class AcquisitionTests(unittest.TestCase):
    def test_fixed_bounds_and_exact_official_url(self):
        plan = acquire.plan()
        self.assertEqual(len(plan), 75)
        self.assertEqual({s.symbol for s in plan}, {"BTCUSDT", "ETHUSDT", "ADAUSDT"})
        self.assertEqual(plan[0].month, "2023-12")
        self.assertEqual(plan[-1].month, "2025-12")
        self.assertEqual(acquire.END_MS - acquire.START_MS, 731 * 86400000)
        self.assertEqual(acquire.Archive("BTCUSDT", "2024-01").url,
                         "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2024-01.zip")

    def test_unapproved_inputs_rejected(self):
        for symbol, month, timeframe in (("SOLUSDT", "2024-01", "15m"), ("BTCUSDT", "2026-01", "15m"),
                                          ("BTCUSDT", "2023-11", "15m"), ("BTCUSDT", "2024-01", "1m"),
                                          ("../BTCUSDT", "2024-01", "15m")):
            with self.assertRaises(DataValidationError):
                acquire.Archive(symbol, month, timeframe)

    def test_checksum_exact_name_and_bytes(self):
        spec = acquire.Archive("BTCUSDT", "2024-01")
        raw = archive_bytes(spec)
        self.assertEqual(acquire.verify_checksum(raw, checksum(raw, spec), spec), hashlib.sha256(raw).hexdigest())
        for bad in (b"invalid", b"a" * 513, checksum(raw, spec).replace(b"BTCUSDT", b"ETHUSDT")):
            with self.assertRaises(DataValidationError):
                acquire.verify_checksum(raw, bad, spec)
        with self.assertRaises(DataValidationError):
            acquire.verify_checksum(raw + b"changed", checksum(raw, spec), spec)

    def test_explicit_timestamp_contracts_normalize_identically(self):
        spec = acquire.Archive("BTCUSDT", "2024-01")
        expected, label, count = acquire.normalize_archive(archive_bytes(spec), spec)
        self.assertEqual((label, count), ("milliseconds", 2))
        for unit, label in (("us", "microseconds_interval_end"), ("scaled_us", "microseconds_scaled_milliseconds")):
            actual, actual_label, count = acquire.normalize_archive(archive_bytes(spec, unit), spec)
            self.assertEqual(actual, expected)
            self.assertEqual(actual_label, label)

    def test_unknown_units_wrong_month_and_mixed_units_fail(self):
        spec = acquire.Archive("BTCUSDT", "2024-01")
        with self.assertRaises(DataValidationError):
            acquire.normalize_archive(archive_bytes(spec, "seconds"), spec)
        wrong = acquire.Archive("BTCUSDT", "2024-02")
        with self.assertRaises(DataValidationError):
            acquire.normalize_archive(archive_bytes(wrong), wrong)
        mixed = f"{T},100,101,99,100,1,{T + STEP_MS - 1},0,0,0,0,0\n{(T + STEP_MS)*1000},100,101,99,100,1,{(T + 2*STEP_MS)*1000 - 1},0,0,0,0,0\n"
        with self.assertRaises(DataValidationError):
            acquire.normalize_archive(archive_bytes(spec, malformed=mixed), spec)

    def test_malformed_unsafe_and_oversized_zip(self):
        spec = acquire.Archive("BTCUSDT", "2024-01")
        for raw in (b"not a zip", archive_bytes(spec, member="../../escape.csv"),
                    archive_bytes(spec, malformed=""), archive_bytes(spec, malformed="1,2,3")):
            with self.assertRaises(DataValidationError):
                acquire.normalize_archive(raw, spec)
        with patch.object(acquire, "MAX_CSV_BYTES", 1), self.assertRaises(DataValidationError):
            acquire.normalize_archive(archive_bytes(spec), spec)

    def test_unsafe_paths_and_exclusive_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            for part in ("..", "a/b", "C:\\outside", "a:b", "/absolute"):
                with self.assertRaises(DataValidationError):
                    acquire._safe_path(directory, part)
            path = acquire._safe_path(directory, "valid.json")
            acquire._save_or_match(path, b"original")
            acquire._save_or_match(path, b"original")
            with self.assertRaises(DataValidationError):
                acquire._save_or_match(path, b"changed")
            self.assertEqual(path.read_bytes(), b"original")

    def test_arbitrary_network_url_rejected_before_request(self):
        with patch.object(acquire.urllib.request, "build_opener") as opener:
            for url in ("http://data.binance.vision/", "https://fapi.binance.com/fapi/v1/time",
                        "https://example.com/a.zip", "https://data.binance.vision/arbitrary.zip"):
                with self.assertRaises(DataValidationError):
                    acquire._fetch(url, 512)
            opener.assert_not_called()

    def test_download_bounds_and_no_redirect(self):
        url = acquire.plan()[0].url
        for body in (b"", b"12345"):
            response = io.BytesIO(body)
            response.status = 200
            response.geturl = lambda: url
            with patch.object(acquire.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(DataValidationError):
                    acquire._fetch(url, 4)
        with self.assertRaises(DataValidationError):
            acquire._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_deterministic_manifest_reuses_cache_and_phase5b_validation(self):
        specs = tuple(acquire.Archive(s, "2024-01") for s in acquire.SYMBOLS)
        raw = {spec.url: archive_bytes(spec) for spec in specs}
        responses = dict(raw)
        responses.update({spec.url + ".CHECKSUM": checksum(raw[spec.url], spec) for spec in specs})
        with tempfile.TemporaryDirectory() as directory, patch.object(acquire, "plan", return_value=specs), \
                patch.object(acquire, "END_MS", T + 2 * STEP_MS), patch.object(acquire, "WARMUP", 0), \
                patch.object(acquire, "_fetch", side_effect=lambda url, limit: responses[url]) as fetch, \
                redirect_stdout(io.StringIO()):
            first = acquire.acquire_and_validate(Path(directory) / "first")
            self.assertEqual(fetch.call_count, 6)
            again = acquire.acquire_and_validate(Path(directory) / "first")
            self.assertEqual(fetch.call_count, 6)
            relocated = acquire.acquire_and_validate(Path(directory) / "second")
            self.assertEqual(first, again)
            self.assertEqual(first, relocated)
            self.assertFalse(first["financial_run_performed"])
            for result in first["datasets"].values():
                self.assertEqual(result["candle_count"], 2)
                self.assertEqual(result["provenance"]["duplicate_count"], 0)

    def test_ambiguous_microsecond_close_rejected(self):
        spec = acquire.Archive("BTCUSDT", "2024-01")
        line = f"{T*1000},100,101,99,100,1,{(T + STEP_MS)*1000 - 123},0,0,0,0,0\n"
        with self.assertRaises(DataValidationError):
            acquire.normalize_archive(archive_bytes(spec, malformed=line), spec)

    def test_normalization_does_not_bypass_phase5b_ohlc_or_gap_checks(self):
        spec = acquire.Archive("BTCUSDT", "2024-01")
        cases = [f"{T},100,98,99,100,1,{T + STEP_MS - 1},0,0,0,0,0\n",
                 f"{T},100,101,99,100,1,{T + STEP_MS - 1},0,0,0,0,0\n"
                 f"{T + 2*STEP_MS},100,101,99,100,1,{T + 3*STEP_MS - 1},0,0,0,0,0\n"]
        for rows in cases:
            normalized, _, _ = acquire.normalize_archive(archive_bytes(spec, malformed=rows), spec)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "normalized.csv"
                path.write_bytes(normalized)
                with self.assertRaises(DataValidationError):
                    acquire.ingest([acquire.Source(path, "BTCUSDT", "synthetic")], symbols=("BTCUSDT",),
                                   start_ms=T, end_ms=T + 3*STEP_MS, as_of_ms=T + 3*STEP_MS,
                                   timestamp_unit="ms", warmup_candles=0)


if __name__ == "__main__":
    unittest.main()
