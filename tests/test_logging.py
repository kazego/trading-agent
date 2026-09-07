import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from errors import LoggingUnavailableError
from logging_config import (LOGGER_NAME, LOG_MAX_BYTES, LOG_BACKUP_COUNT, JsonFormatter,
                            Redactor, SafeStreamHandler, configure_logging)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "logs"

    def tearDown(self):
        logger = logging.getLogger(LOGGER_NAME)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        self.temp.cleanup()

    def records(self):
        return [json.loads(line) for line in (self.directory / "bot.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_persistent_json_utc_and_context(self):
        run_id = "a55779dd-0123-4567-89ab-123456789abc"
        logger = configure_logging(self.directory, console=False, run_id=run_id)
        logger.warning("A synthetic decision\nwith newline", extra={
            "event_type": "risk_rejected", "symbol": "ETHUSDT", "decision_id": "decision-1",
            "trade_id": "trade-1", "order_id": "order-1",
        })
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["severity"], "WARNING")
        self.assertEqual(record["event_type"], "risk_rejected")
        self.assertEqual(record["run_id"], run_id)
        self.assertEqual(record["symbol"], "ETHUSDT")
        self.assertEqual(record["decision_id"], "decision-1")
        self.assertEqual(record["trade_id"], "trade-1")
        self.assertEqual(record["order_id"], "order-1")
        self.assertTrue(record["timestamp"].endswith("Z"))
        self.assertEqual(datetime.fromisoformat(record["timestamp"]).utcoffset(), timedelta(0))

    def test_defaults_include_optional_null_fields(self):
        logger = configure_logging(self.directory, console=False)
        logger.info("Started")
        record = self.records()[0]
        self.assertEqual(record["event_type"], "application")
        for name in ("symbol", "decision_id", "trade_id", "order_id", "run_id"):
            self.assertIsNone(record[name])

    def test_secrets_scrubbed_from_file_console_context_and_exception(self):
        output = io.StringIO()
        with redirect_stderr(output):
            logger = configure_logging(self.directory, secrets=("synthetic-key", "synthetic-secret"))
            logger.info("key=%s secret=%s", "synthetic-key", "synthetic-secret", extra={
                "event_type": "example", "order_id": "synthetic-key",
                "context": {"nested": [{"api_key": "unregistered-value", "password": "short-pass"}],
                            "safe": "retained"},
            })
            try:
                raise RuntimeError("synthetic-secret Authorization: Bearer access-value")
            except RuntimeError:
                logger.exception("Request failed")
        for text in ((self.directory / "bot.jsonl").read_text(encoding="utf-8"), output.getvalue()):
            for secret in ("synthetic-key", "synthetic-secret", "unregistered-value", "short-pass", "access-value"):
                self.assertNotIn(secret, text)
            self.assertIn("[REDACTED]", text)
            self.assertIn("retained", text)
        self.assertEqual(self.records()[1]["exception"]["type"], "RuntimeError")

    def test_credential_patterns_and_known_values(self):
        redactor = Redactor(("known-with spaces",))
        cases = [
            ('API_KEY="short value"', "short value"),
            ("{'apiSecret': 'another value'}", "another value"),
            ("Authorization: Basic YWJjZA==", "YWJjZA=="),
            ("https://user:pass@example.invalid/path?signature=signed&token=abc", "user:pass"),
            ("https://example.invalid/?signature=signed&token=abc", "signed"),
            ("BINANCE_API_SECRET=private", "private"),
            ("known-with spaces", "known-with spaces"),
            ("opaque " + "a" * 64, "a" * 64),
        ]
        for text, forbidden in cases:
            with self.subTest(text=text):
                self.assertNotIn(forbidden, redactor.text(text))

    def test_structured_secrets_and_safe_percent_interpolation(self):
        logger = configure_logging(self.directory, console=False)
        logger.info("API_KEY=%s", "short-key")
        logger.info("Count=%d name=%s", 2, "safe")
        logger.info("%(password)s", {"password": "private"})
        logger.info({"token": "nested-secret", "count": 1})
        records = self.records()
        self.assertEqual(records[0]["message"], "API_KEY=[REDACTED]")
        self.assertEqual(records[1]["message"], "Count=2 name=safe")
        self.assertEqual(records[2]["message"], "[REDACTED]")
        self.assertEqual(records[3]["message"], {"token": "[REDACTED]", "count": 1})

    def test_rotation_configuration_and_actual_rollover(self):
        logger = configure_logging(self.directory, console=False)
        handler = logger.handlers[0]
        self.assertEqual(handler.maxBytes, LOG_MAX_BYTES)
        self.assertEqual(handler.backupCount, LOG_BACKUP_COUNT)
        logger = configure_logging(self.directory, console=False, max_bytes=400, backup_count=2,
                                   secrets=("synthetic-secret",))
        for index in range(10):
            logger.info("record %d synthetic-secret", index)
        self.assertTrue((self.directory / "bot.jsonl.1").exists())
        self.assertTrue((self.directory / "bot.jsonl.2").exists())
        self.assertFalse((self.directory / "bot.jsonl.3").exists())
        for path in self.directory.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("synthetic-secret", text)
            for line in text.splitlines():
                json.loads(line)

    def test_reconfiguration_has_no_duplicates_or_root_propagation(self):
        root_handlers = list(logging.getLogger().handlers)
        configure_logging(self.directory, console=False)
        logger = configure_logging(self.directory, console=False)
        logger.info("Once")
        self.assertEqual(len(logger.handlers), 1)
        self.assertEqual(len(self.records()), 1)
        self.assertFalse(logger.propagate)
        self.assertEqual(logging.getLogger().handlers, root_handlers)

    def test_initialization_failure_omits_raw_os_error(self):
        with patch.object(Path, "mkdir", side_effect=OSError("API_SECRET=private-value")):
            with self.assertRaises(LoggingUnavailableError) as caught:
                configure_logging(self.directory)
        self.assertNotIn("private-value", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_write_and_rotation_failures_do_not_dump_record(self):
        output = io.StringIO()
        logger = configure_logging(self.directory, console=False, max_bytes=1)
        logger.info("Seed the nonempty file before forcing rollover")
        with redirect_stderr(output), patch.object(logger.handlers[0], "doRollover", side_effect=OSError("private-value")):
            logger.error("password=raw-secret")
        self.assertNotIn("raw-secret", output.getvalue())
        self.assertNotIn("private-value", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["event_type"], "logging_write_failure")

    def test_console_failure_does_not_dump_record(self):
        class BrokenStream:
            def write(self, text):
                raise OSError("private-stream-value")

        handler = SafeStreamHandler(BrokenStream())
        handler.setFormatter(JsonFormatter())
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "password=raw-secret", (), None)
        output = io.StringIO()
        with redirect_stderr(output):
            handler.handle(record)
        self.assertNotIn("raw-secret", output.getvalue())
        self.assertNotIn("private-stream-value", output.getvalue())

    def test_formatting_failure_does_not_dump_arguments(self):
        logger = configure_logging(self.directory, console=False)
        logger.error("Broken %d", "raw-secret")
        self.assertEqual(self.records()[0]["event_type"], "logging_format_failure")
        self.assertNotIn("raw-secret", (self.directory / "bot.jsonl").read_text(encoding="utf-8"))

    def test_untrusted_objects_are_not_stringified(self):
        class PrivateObject:
            def __str__(self):
                raise AssertionError("must not stringify")

        logger = configure_logging(self.directory, console=False)
        logger.info(PrivateObject(), extra={"context": {"object": PrivateObject()}})
        record = self.records()[0]
        self.assertEqual(record["message"], "[UNSUPPORTED]")
        self.assertEqual(record["context"]["object"], "[UNSUPPORTED]")

    def test_invalid_rotation_limits_fail_explicitly(self):
        for kwargs in ({"max_bytes": 0}, {"max_bytes": True}, {"backup_count": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LoggingUnavailableError):
                configure_logging(self.directory, **kwargs)
