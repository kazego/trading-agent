"""Configuration validation without credentials, files, or exchange access."""

import unittest
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import config
from errors import ConfigurationError


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_legacy_compatibility(self):
        settings = config.load_settings()
        expected = {
            "USE_TESTNET": True, "TRADING_ENABLED": False,
            "SYMBOLS": ("BTCUSDT", "ETHUSDT", "ADAUSDT"),
            "POSITION_MODE": "ONE_WAY", "MARKET_TYPE": "USDT_M", "TIMEFRAME": "15m",
            "EMA_FAST": 9, "EMA_SLOW": 21, "LEVERAGE": 3,
            "RISK_PER_TRADE_PCT": 1.0, "STOP_LOSS_PCT": 1.5, "TAKE_PROFIT_PCT": 3.0,
            "MAX_OPEN_POSITIONS": 1, "POLL_INTERVAL_SECONDS": 60, "MAX_LEVERAGE": 3,
            "MAX_POSITION_NOTIONAL_USDT": Decimal("1000"), "MAX_DAILY_LOSS_PCT": 3.0,
            "MAX_CONSECUTIVE_LOSSES": 3, "MAX_DATA_AGE_SECONDS": 120,
            "MANAGEMENT_POLL_SECONDS": 5, "API_TIMEOUT_SECONDS": 10, "MAX_READ_RETRIES": 3,
            "ENTRY_CONFIRM_TIMEOUT_SECONDS": 15, "PROTECTION_TIMEOUT_SECONDS": 10,
            "DATABASE_PATH": config.PROJECT_ROOT / "data" / "bot.sqlite3",
            "LOG_DIRECTORY": config.PROJECT_ROOT / "logs",
            "MAX_POSITION_QTY": {"BTCUSDT": Decimal("0.01"), "ETHUSDT": Decimal("0.1"), "ADAUSDT": Decimal("1000")},
        }
        self.assertEqual(set(expected), {item.name for item in fields(config.Settings)})
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(settings, name), value)
                self.assertEqual(getattr(config, name), value)
        self.assertEqual(config.SYMBOL, "BTCUSDT")

    def test_testnet_only_and_strict_boolean(self):
        for value in (False, 0, 1, "True", None):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                replace(config.Settings(), USE_TESTNET=value).validate()
        for value in (0, 1, "False", None):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                replace(config.Settings(), TRADING_ENABLED=value).validate()
        self.assertTrue(replace(config.Settings(), TRADING_ENABLED=True).validate().TRADING_ENABLED)

    def test_watchlist_is_fixed_and_ordered(self):
        for symbols in ((), ("BTCUSDT",), ("BTCUSDT", "ETHUSDT", "ETHUSDT"),
                        ("BTCUSDT", "ETHUSDT", "XRPUSDT"), tuple(reversed(config.FIXED_SYMBOLS)),
                        ["BTCUSDT", "ETHUSDT", "ADAUSDT"], ("btcusdt", "ETHUSDT", "ADAUSDT")):
            with self.subTest(symbols=symbols), self.assertRaises(ConfigurationError):
                replace(config.Settings(), SYMBOLS=symbols).validate()

    def test_fixed_account_and_strategy_contract(self):
        for name, value in (("POSITION_MODE", "HEDGE"), ("MARKET_TYPE", "COIN_M"),
                            ("TIMEFRAME", "1m"), ("EMA_FAST", 8), ("EMA_SLOW", 22),
                            ("MAX_OPEN_POSITIONS", 2), ("MAX_OPEN_POSITIONS", True)):
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                replace(config.Settings(), **{name: value}).validate()

    def test_positive_integer_ranges(self):
        for name in ("LEVERAGE", "MAX_LEVERAGE", "MAX_CONSECUTIVE_LOSSES", "POLL_INTERVAL_SECONDS",
                     "MAX_DATA_AGE_SECONDS", "MANAGEMENT_POLL_SECONDS", "API_TIMEOUT_SECONDS",
                     "ENTRY_CONFIRM_TIMEOUT_SECONDS", "PROTECTION_TIMEOUT_SECONDS"):
            for value in (0, -1, True, 1.5, "10", float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError):
                    replace(config.Settings(), **{name: value}).validate()

    def test_retry_count_can_be_zero_but_not_negative_or_noninteger(self):
        replace(config.Settings(), MAX_READ_RETRIES=0).validate()
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                replace(config.Settings(), MAX_READ_RETRIES=value).validate()

    def test_percentages_and_notional_reject_invalid_numbers(self):
        percentages = ("RISK_PER_TRADE_PCT", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "MAX_DAILY_LOSS_PCT")
        for name in percentages + ("MAX_POSITION_NOTIONAL_USDT",):
            for value in (0, -1, True, "secret-value", float("inf"), float("nan"), Decimal("NaN")):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError) as caught:
                    replace(config.Settings(), **{name: value}).validate()
                self.assertNotIn("secret-value", str(caught.exception))
        for name in percentages:
            for value in (100, 101):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError):
                    replace(config.Settings(), **{name: value}).validate()

    def test_quantity_caps_are_per_symbol_and_complete(self):
        for quantities in (None, Decimal("1"), {}, {"BTCUSDT": Decimal("1")},
                           {**config.MAX_POSITION_QTY, "XRPUSDT": Decimal("1")}):
            with self.subTest(quantities=quantities), self.assertRaises(ConfigurationError):
                replace(config.Settings(), MAX_POSITION_QTY=quantities).validate()
        for symbol in config.FIXED_SYMBOLS:
            for value in (0, -1, float("nan"), float("inf"), True, "1"):
                with self.subTest(symbol=symbol, value=value), self.assertRaises(ConfigurationError):
                    replace(config.Settings(), MAX_POSITION_QTY={**config.MAX_POSITION_QTY, symbol: value}).validate()

    def test_numeric_types_remain_compatible_with_unchanged_risk_code(self):
        for name in ("RISK_PER_TRADE_PCT", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT"):
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                replace(config.Settings(), **{name: Decimal("1")}).validate()
        with self.assertRaises(ConfigurationError):
            replace(config.Settings(), MAX_READ_RETRIES=10 ** 1000).validate()

    def test_inconsistent_limits_rejected(self):
        for changes in ({"LEVERAGE": 4}, {"RISK_PER_TRADE_PCT": 4.0},
                        {"ENTRY_CONFIRM_TIMEOUT_SECONDS": 9}, {"PROTECTION_TIMEOUT_SECONDS": 9},
                        {"MAX_DATA_AGE_SECONDS": 9}, {"MANAGEMENT_POLL_SECONDS": 121}):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                replace(config.Settings(), **changes).validate()

    def test_paths_are_explicit_and_nonconflicting(self):
        for name in ("DATABASE_PATH", "LOG_DIRECTORY"):
            for value in ("logs", Path("relative"), config.PROJECT_ROOT / ".." / "logs"):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError):
                    replace(config.Settings(), **{name: value}).validate()
        with self.assertRaises(ConfigurationError):
            replace(config.Settings(), DATABASE_PATH=config.PROJECT_ROOT / "data.txt").validate()
        with self.assertRaises(ConfigurationError):
            replace(config.Settings(), LOG_DIRECTORY=config.DATABASE_PATH).validate()

    def test_settings_do_not_require_or_represent_credentials(self):
        with patch.object(config, "API_KEY", "synthetic-key"), patch.object(config, "API_SECRET", "synthetic-secret"):
            settings = config.load_settings()
            self.assertNotIn("synthetic", repr(settings))
            self.assertFalse(hasattr(settings, "API_KEY"))
            self.assertFalse(hasattr(settings, "API_SECRET"))
        with patch.object(config, "API_KEY", ""), patch.object(config, "API_SECRET", ""):
            config.load_settings()

    def test_startup_snapshot_validates_exported_values_and_copies_caps(self):
        with patch.object(config, "USE_TESTNET", False), self.assertRaises(ConfigurationError):
            config.load_settings()
        with patch.object(config, "SYMBOL", "ETHUSDT"), self.assertRaises(ConfigurationError):
            config.load_settings()
        quantities = dict(config.MAX_POSITION_QTY)
        with patch.object(config, "MAX_POSITION_QTY", quantities):
            settings = config.load_settings()
        quantities["BTCUSDT"] = Decimal("99")
        self.assertEqual(settings.MAX_POSITION_QTY["BTCUSDT"], Decimal("0.01"))
        with self.assertRaises(TypeError):
            settings.MAX_POSITION_QTY["BTCUSDT"] = Decimal("99")
        with self.assertRaises(FrozenInstanceError):
            settings.TRADING_ENABLED = True
