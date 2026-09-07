"""
Configuración del bot. Las claves de API NUNCA deben escribirse acá directamente:
se leen desde variables de entorno para evitar exponerlas por accidente
(ej: al subir el código a GitHub).

Cómo generar las API keys de TESTNET (recomendado para probar sin plata real):
1. Entrá a https://testnet.binancefuture.com
2. Logueate con GitHub (no necesitás cuenta real de Binance)
3. Generá API Key y Secret Key desde el panel

Para setear las variables de entorno en Linux/Mac:
    export BINANCE_API_KEY="tu_api_key"
    export BINANCE_API_SECRET="tu_api_secret"

En Windows (PowerShell):
    $env:BINANCE_API_KEY="tu_api_key"
    $env:BINANCE_API_SECRET="tu_api_secret"
"""

import os
import math
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from errors import ConfigurationError

# --- Credenciales ---
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# --- Modo de operación ---
# v0.2 startup rejects False. No real-money support.
USE_TESTNET = True

# --- Parámetros de mercado ---
FIXED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "ADAUSDT")
SYMBOL = FIXED_SYMBOLS[0]   # Temporary legacy alias; no watchlist execution yet.
TIMEFRAME = "15m"           # Temporalidad de las velas (1m, 5m, 15m, 1h, 4h, 1d...)
LEVERAGE = 3                # Apalancamiento (cuidado: a mayor leverage, mayor riesgo)

# --- Parámetros de estrategia (cruce de medias móviles) ---
EMA_FAST = 9
EMA_SLOW = 21

# --- Gestión de riesgo ---
RISK_PER_TRADE_PCT = 1.0    # % del capital disponible que se arriesga por operación
STOP_LOSS_PCT = 1.5         # % de stop-loss respecto al precio de entrada
TAKE_PROFIT_PCT = 3.0       # % de take-profit respecto al precio de entrada
MAX_OPEN_POSITIONS = 1      # Máximo de posiciones simultáneas

# --- Intervalo de chequeo del bot (segundos) ---
POLL_INTERVAL_SECONDS = 60


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    """Startup snapshot. Credentials are excluded; future limits are declarative.

    Baseline defaults above stay compatible with unchanged strategy/risk code.
    New defaults below are exported as module constants after this class.
    """

    USE_TESTNET: bool = USE_TESTNET
    TRADING_ENABLED: bool = False
    SYMBOLS: tuple[str, ...] = FIXED_SYMBOLS
    POSITION_MODE: str = "ONE_WAY"
    MARKET_TYPE: str = "USDT_M"
    TIMEFRAME: str = TIMEFRAME
    EMA_FAST: int = EMA_FAST
    EMA_SLOW: int = EMA_SLOW
    LEVERAGE: int = LEVERAGE
    RISK_PER_TRADE_PCT: float = RISK_PER_TRADE_PCT
    STOP_LOSS_PCT: float = STOP_LOSS_PCT
    TAKE_PROFIT_PCT: float = TAKE_PROFIT_PCT
    MAX_OPEN_POSITIONS: int = MAX_OPEN_POSITIONS
    POLL_INTERVAL_SECONDS: int = POLL_INTERVAL_SECONDS
    MAX_LEVERAGE: int = 3
    MAX_POSITION_NOTIONAL_USDT: Decimal = Decimal("1000")
    MAX_POSITION_QTY: Mapping[str, Decimal] = field(default_factory=lambda: MappingProxyType({
        "BTCUSDT": Decimal("0.01"), "ETHUSDT": Decimal("0.1"), "ADAUSDT": Decimal("1000"),
    }))
    MAX_DAILY_LOSS_PCT: float = 3.0
    MAX_CONSECUTIVE_LOSSES: int = 3
    MAX_DATA_AGE_SECONDS: int = 120
    MANAGEMENT_POLL_SECONDS: int = 5
    API_TIMEOUT_SECONDS: int = 10
    MAX_READ_RETRIES: int = 3
    ENTRY_CONFIRM_TIMEOUT_SECONDS: int = 15
    PROTECTION_TIMEOUT_SECONDS: int = 10
    DATABASE_PATH: Path = PROJECT_ROOT / "data" / "bot.sqlite3"
    LOG_DIRECTORY: Path = PROJECT_ROOT / "logs"

    def validate(self) -> "Settings":
        def require(condition, message):
            if not condition:
                raise ConfigurationError(message)  # Never include rejected values.

        def positive(name, value, allow_zero=False):
            valid_type = type(value) in (int, float, Decimal)
            try:
                finite = valid_type and (value.is_finite() if isinstance(value, Decimal) else math.isfinite(value))
            except OverflowError:
                finite = False
            require(finite, name + " must be a finite number")
            require(value >= 0 if allow_zero else value > 0, name + " is below its allowed range")

        require(self.USE_TESTNET is True, "USE_TESTNET must be True in v0.2")
        require(type(self.TRADING_ENABLED) is bool, "TRADING_ENABLED must be a boolean")
        require(type(self.SYMBOLS) is tuple and self.SYMBOLS == FIXED_SYMBOLS,
                "SYMBOLS must be the fixed BTCUSDT/ETHUSDT/ADAUSDT tuple in that order")
        require(self.POSITION_MODE == "ONE_WAY", "POSITION_MODE must be ONE_WAY")
        require(self.MARKET_TYPE == "USDT_M", "MARKET_TYPE must be USDT_M")
        require(self.TIMEFRAME == "15m", "TIMEFRAME must retain the 15m baseline")
        for name, expected in (("EMA_FAST", 9), ("EMA_SLOW", 21), ("MAX_OPEN_POSITIONS", 1)):
            require(type(getattr(self, name)) is int and getattr(self, name) == expected,
                    name + " must retain the v0.2 baseline")
        for name in ("LEVERAGE", "MAX_LEVERAGE", "MAX_CONSECUTIVE_LOSSES", "POLL_INTERVAL_SECONDS",
                     "MAX_DATA_AGE_SECONDS", "MANAGEMENT_POLL_SECONDS", "API_TIMEOUT_SECONDS",
                     "ENTRY_CONFIRM_TIMEOUT_SECONDS", "PROTECTION_TIMEOUT_SECONDS", "MAX_READ_RETRIES"):
            value = getattr(self, name)
            require(type(value) is int, name + " must be an integer")
            positive(name, value, allow_zero=name == "MAX_READ_RETRIES")
        require(self.LEVERAGE <= self.MAX_LEVERAGE, "LEVERAGE must not exceed MAX_LEVERAGE")
        for name in ("RISK_PER_TRADE_PCT", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT", "MAX_DAILY_LOSS_PCT"):
            if name != "MAX_DAILY_LOSS_PCT":
                require(type(getattr(self, name)) in (int, float),
                        name + " must use int/float for legacy risk arithmetic")
            positive(name, getattr(self, name))
            require(getattr(self, name) < 100, name + " must be less than 100")
        require(self.RISK_PER_TRADE_PCT <= self.MAX_DAILY_LOSS_PCT,
                "RISK_PER_TRADE_PCT must not exceed MAX_DAILY_LOSS_PCT")
        positive("MAX_POSITION_NOTIONAL_USDT", self.MAX_POSITION_NOTIONAL_USDT)
        require(isinstance(self.MAX_POSITION_QTY, Mapping), "MAX_POSITION_QTY must be a symbol mapping")
        require(set(self.MAX_POSITION_QTY) == set(FIXED_SYMBOLS),
                "MAX_POSITION_QTY must contain exactly the fixed watchlist")
        for symbol in FIXED_SYMBOLS:
            positive("MAX_POSITION_QTY", self.MAX_POSITION_QTY[symbol])
        for name in ("ENTRY_CONFIRM_TIMEOUT_SECONDS", "PROTECTION_TIMEOUT_SECONDS", "MAX_DATA_AGE_SECONDS"):
            require(getattr(self, name) >= self.API_TIMEOUT_SECONDS, name + " must cover API_TIMEOUT_SECONDS")
        require(self.MANAGEMENT_POLL_SECONDS <= self.MAX_DATA_AGE_SECONDS,
                "MANAGEMENT_POLL_SECONDS must not exceed MAX_DATA_AGE_SECONDS")
        for name in ("DATABASE_PATH", "LOG_DIRECTORY"):
            value = getattr(self, name)
            require(isinstance(value, Path) and value.is_absolute() and ".." not in value.parts,
                    name + " must be an absolute path without parent traversal")
        require(self.DATABASE_PATH.suffix in (".db", ".sqlite", ".sqlite3"),
                "DATABASE_PATH must name a SQLite file")
        require(self.DATABASE_PATH != self.LOG_DIRECTORY and self.DATABASE_PATH not in self.LOG_DIRECTORY.parents,
                "DATABASE_PATH must not conflict with LOG_DIRECTORY")
        return self


_defaults = Settings()
for _setting in fields(Settings):
    globals()[_setting.name] = getattr(_defaults, _setting.name)


def load_settings() -> Settings:
    """Snapshot exported values and validate before startup; no I/O or keys needed."""
    values = {item.name: globals()[item.name] for item in fields(Settings)}
    settings = Settings(**values).validate()
    if SYMBOL != settings.SYMBOLS[0]:
        raise ConfigurationError("legacy SYMBOL must remain the first watchlist symbol")
    return Settings(**{**values, "MAX_POSITION_QTY": MappingProxyType(dict(settings.MAX_POSITION_QTY))})
