"""Import the real bot with a fake Binance module: no SDK or credentials used."""

import importlib.util
import io
import json
import logging
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import config
from errors import ApplicationError, ConfigurationError, LoggingUnavailableError
from logging_config import LOGGER_NAME


class StartupTests(unittest.TestCase):
    def setUp(self):
        enums = types.ModuleType("binance.enums")
        enums.SIDE_BUY = "BUY"
        enums.SIDE_SELL = "SELL"
        client_module = types.ModuleType("binance_client")
        self.factory = Mock(name="offline_factory")
        client_module.BinanceFuturesClient = self.factory
        # No import of the actual Binance adapter or external SDK.
        with patch.dict(sys.modules, {"binance": types.ModuleType("binance"),
                                      "binance.enums": enums, "binance_client": client_module}):
            spec = importlib.util.spec_from_file_location("offline_bot", config.PROJECT_ROOT / "bot.py")
            self.bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bot)

    def tearDown(self):
        self.close_logs()

    def close_logs(self):
        logger = logging.getLogger(LOGGER_NAME)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()

    def test_invalid_config_precedes_logging_and_client(self):
        with patch.object(config, "USE_TESTNET", False), patch.object(self.bot, "configure_logging") as setup:
            with self.assertRaises(ConfigurationError):
                self.bot.run_bot()
        setup.assert_not_called()
        self.factory.assert_not_called()

    def test_logging_initialization_failure_precedes_client(self):
        with patch.object(self.bot, "configure_logging", side_effect=LoggingUnavailableError("Unavailable")):
            with self.assertRaises(LoggingUnavailableError):
                self.bot.run_bot()
        self.factory.assert_not_called()

    def test_offline_startup_shutdown_write_json(self):
        fake = self.factory.return_value
        fake.get_symbol_precision.return_value = (3, 2)
        fake.get_open_position.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "logs"
            with patch.object(config, "LOG_DIRECTORY", directory), patch.object(config, "API_KEY", ""), \
                    patch.object(config, "API_SECRET", ""), redirect_stderr(io.StringIO()):
                self.bot.run_bot()
            records = [json.loads(line) for line in (directory / "bot.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["event_type"], "startup")
            self.assertIn("phase2_scope", [record["event_type"] for record in records])
            self.assertEqual(records[-1]["event_type"], "shutdown")
            self.assertTrue(records[0]["run_id"])
            self.assertEqual({record["run_id"] for record in records}, {records[0]["run_id"]})
            self.assertEqual([call[0] for call in fake.method_calls], ["get_symbol_precision", "get_open_position"])
            self.close_logs()  # Close file handles before Windows temporary cleanup.

    def test_client_startup_error_is_logged_and_reraised_without_raw_message(self):
        self.factory.side_effect = RuntimeError("API_SECRET=synthetic-private")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "logs"
            with patch.object(config, "LOG_DIRECTORY", directory), patch.object(config, "API_KEY", ""), \
                    patch.object(config, "API_SECRET", ""), redirect_stderr(io.StringIO()):
                with self.assertRaises(ApplicationError) as caught:
                    self.bot.run_bot()
            self.assertNotIn("synthetic-private", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            text = (directory / "bot.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("synthetic-private", text)
            self.assertIn("startup_error", text)
            self.close_logs()
