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

# --- Credenciales ---
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# --- Modo de operación ---
# True = Testnet (simulado, SIN riesgo de plata real). False = cuenta real.
USE_TESTNET = True

# --- Parámetros de mercado ---
SYMBOL = "BTCUSDT"          # Par a operar
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
