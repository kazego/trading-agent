"""Synthetic offline archive contracts; never import any live trading module."""

from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from backtest.data import (Candle, DataValidationError, HEADER, Source, STEP_MS,
                           canonical_decimal, ingest)

FIXTURES = Path(__file__).parent / "fixtures" / "historical"
T = 1704067200000


class HistoricalDataTests(unittest.TestCase):
    def run_fixture(self, name="valid", **overrides):
        options = dict(symbols=("BTCUSDT",), start_ms=T, end_ms=T + 3 * STEP_MS,
                       as_of_ms=T + 3 * STEP_MS, timestamp_unit="ms")
        options.update(overrides)
        return ingest([Source(FIXTURES / (name + ".csv"), "BTCUSDT", name)], **options)

    def test_valid_contiguous_typed_candles(self):
        result = self.run_fixture()
        self.assertEqual(len(result.candles), 3)
        self.assertIsInstance(result.candles[0], Candle)
        self.assertEqual(result.candles[0].open, Decimal("100"))
        self.assertEqual(result.candles[0].record()["open"], "100")

    def test_bad_fixtures_fail_closed(self):
        for name in ("malformed_ohlc", "wrong_units", "gap", "conflicting_duplicate"):
            with self.subTest(name=name), self.assertRaises(DataValidationError):
                self.run_fixture(name)

    def test_identical_duplicate_count_and_normalized_hash(self):
        result = self.run_fixture("identical_duplicate")
        self.assertEqual(result.provenance["duplicate_count"], 1)
        self.assertEqual(result.provenance["normalized_candle_fingerprint"],
                         self.run_fixture().provenance["normalized_candle_fingerprint"])
        self.assertNotEqual(result.fingerprint, self.run_fixture().fingerprint)

    def test_incomplete_tail_excluded(self):
        result = self.run_fixture("incomplete")
        self.assertEqual(len(result.candles), 3)
        self.assertEqual(result.provenance["excluded_incomplete_count"], 1)

    def test_requested_incomplete_candle_rejected(self):
        with self.assertRaises(DataValidationError):
            self.run_fixture("incomplete", end_ms=T + 4 * STEP_MS)

    def test_completion_boundary(self):
        with self.assertRaises(DataValidationError):
            self.run_fixture(as_of_ms=T + 3 * STEP_MS - 1)

    def test_half_open_interval(self):
        result = self.run_fixture(start_ms=T + STEP_MS, end_ms=T + 2 * STEP_MS)
        self.assertEqual([c.open_time_ms for c in result.candles], [T + STEP_MS])

    def test_warmup_included_and_recorded(self):
        result = self.run_fixture(start_ms=T + 2 * STEP_MS, warmup_candles=2)
        self.assertEqual(len(result.candles), 3)
        self.assertEqual(result.provenance["warmup_candles"], 2)

    def test_insufficient_warmup_fixture(self):
        with self.assertRaises(DataValidationError):
            self.run_fixture("insufficient_warmup", warmup_candles=1)

    def test_request_validation(self):
        for overrides in (dict(symbols=("SOLUSDT",)), dict(symbols=()),
                          dict(symbols=("BTCUSDT", "BTCUSDT")), dict(timeframe="1h"),
                          dict(timestamp_unit="us"), dict(start_ms=T + 1),
                          dict(end_ms=T), dict(warmup_candles=-1),
                          dict(warmup_candles=True), dict(as_of_ms=T * 1000)):
            with self.subTest(overrides=overrides), self.assertRaises(DataValidationError):
                self.run_fixture(**overrides)

    def test_candle_validation(self):
        baseline = dict(symbol="BTCUSDT", timeframe="15m", open_time_ms=T,
                        close_time_ms=T + STEP_MS - 1, open="100", high="102",
                        low="99", close="101", volume="1")
        for changes in (dict(open=100.0), dict(open="NaN"), dict(high="Infinity"),
                        dict(low="0"), dict(volume="-1"), dict(close="103"),
                        dict(close_time_ms=T + STEP_MS), dict(open_time_ms=T // 1000),
                        dict(timeframe="1m"), dict(symbol="DOGEUSDT")):
            with self.subTest(changes=changes), self.assertRaises(DataValidationError):
                Candle(**(baseline | changes))

    def test_canonical_decimal_independent_of_context(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(canonical_decimal("123456789.12345678900"), "123456789.123456789")
        self.assertEqual(canonical_decimal("-0.000"), "0")
        self.assertEqual(canonical_decimal("1E+3"), "1000")

    def test_three_symbols_and_source_order_invariance(self):
        symbols = ("BTCUSDT", "ETHUSDT", "ADAUSDT")
        manifest = json.loads((FIXTURES / "multi_symbol.json").read_text())
        sources = [Source(FIXTURES / manifest[s], s, "synthetic-" + s) for s in symbols]
        options = dict(symbols=symbols, start_ms=T, end_ms=T + 3 * STEP_MS,
                       as_of_ms=T + 3 * STEP_MS, timestamp_unit="ms")
        result = ingest(sources, **options)
        reversed_result = ingest(list(reversed(sources)), **options)
        self.assertEqual(result.fingerprint, reversed_result.fingerprint)
        self.assertEqual([c.symbol for c in result.candles[:3]], list(symbols))
        self.assertEqual(len(result.candles), 9)
        with self.assertRaises(DataValidationError):
            ingest(sources[:2], **options)

    def test_write_exclusive_canonical_json(self):
        result = self.run_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "historical.json"
            result.write(path)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["provenance"]["dataset_fingerprint"], result.fingerprint)
            self.assertEqual(saved["candles"][0]["open"], "100")
            with self.assertRaises(FileExistsError):
                result.write(path)
            with self.assertRaises(DataValidationError):
                result.write(Path(directory) / "live.sqlite")

    def test_repeated_run_and_provenance_immutability(self):
        result = self.run_fixture()
        result.provenance["start_ms"] = 0
        self.assertEqual(result, self.run_fixture())
        with self.assertRaises(AttributeError):
            result.candles[0].open = Decimal("1")

    def test_local_zip_header_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("candles.csv", ",".join(HEADER) + "\n" +
                                 (FIXTURES / "valid.csv").read_text())
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            options = dict(symbols=("BTCUSDT",), start_ms=T, end_ms=T + 3 * STEP_MS,
                           as_of_ms=T + 3 * STEP_MS, timestamp_unit="ms")
            result = ingest([Source(path, "BTCUSDT", "zip", checksum)], **options)
            self.assertEqual(result.provenance["sources"][0]["sha256"], checksum)
            self.assertEqual(len(result.candles), 3)
            with self.assertRaises(DataValidationError):
                ingest([Source(path, "BTCUSDT", "zip", "0" * 64)], **options)

    def test_out_of_order_and_row_shape(self):
        original = (FIXTURES / "valid.csv").read_text().splitlines()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            for rows in (list(reversed(original)), ["1,2"], [], ["unrecognized,header"]):
                path.write_text("\n".join(rows))
                with self.subTest(rows=rows), self.assertRaises(DataValidationError):
                    ingest([Source(path, "BTCUSDT", "bad")], symbols=("BTCUSDT",),
                           start_ms=T, end_ms=T + 3 * STEP_MS,
                           as_of_ms=T + 3 * STEP_MS, timestamp_unit="ms")

    def test_hash_changes_with_values_and_contract(self):
        first = self.run_fixture()
        changed = self.run_fixture(start_ms=T + STEP_MS)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.csv"
            path.write_text((FIXTURES / "valid.csv").read_text().replace("100.00", "100.5"))
            changed = ingest([Source(path, "BTCUSDT", "valid")], symbols=("BTCUSDT",),
                             start_ms=T, end_ms=T + 3 * STEP_MS,
                             as_of_ms=T + 3 * STEP_MS, timestamp_unit="ms")
            self.assertNotEqual(first.provenance["normalized_candle_fingerprint"],
                                changed.provenance["normalized_candle_fingerprint"])

    def test_overlapping_files_deduplicate_independent_of_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "copy.csv"
            copy.write_bytes((FIXTURES / "valid.csv").read_bytes())
            options = dict(symbols=("BTCUSDT",), start_ms=T, end_ms=T + 3 * STEP_MS,
                           as_of_ms=T + 3 * STEP_MS, timestamp_unit="ms")
            copied = ingest([Source(copy, "BTCUSDT", "valid")], **options)
            self.assertEqual(copied.fingerprint, self.run_fixture().fingerprint)
            sources = [Source(copy, "BTCUSDT", "copy"),
                       Source(FIXTURES / "valid.csv", "BTCUSDT", "valid")]
            self.assertEqual(ingest(sources, **options).provenance["duplicate_count"], 3)
            with self.assertRaises(DataValidationError):
                ingest(sources + [sources[0]], **options)

    def test_zip_multiple_members_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multiple.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("a.csv", "")
                archive.writestr("b.csv", "")
            with self.assertRaises(DataValidationError):
                ingest([Source(path, "BTCUSDT", "multiple")], symbols=("BTCUSDT",),
                       start_ms=T, end_ms=T + STEP_MS, as_of_ms=T + STEP_MS,
                       timestamp_unit="ms")


if __name__ == "__main__":
    unittest.main()
