# Bot de Trading Automático — Binance Futures

Bot en Python que opera automáticamente en el mercado de futuros de Binance,
usando una estrategia de cruce de medias móviles (EMA) como punto de partida.

## ⚠️ Advertencias importantes

- El trading con futuros usa **apalancamiento**: las pérdidas pueden ser
  rápidas y superar el capital invertido si no se gestiona bien el riesgo.
- Este código es un **punto de partida educativo**, no un producto
  financiero terminado ni una recomendación de inversión.
- **Probá siempre en Testnet primero**, durante un tiempo largo, antes de
  arriesgar dinero real.
- Nadie puede garantizar rentabilidad. Rendimientos pasados (backtests)
  no garantizan resultados futuros.

## Estructura del proyecto

```
binance_bot/
├── config.py          # Parámetros del bot (símbolo, riesgo, EMAs, etc.)
├── binance_client.py   # Wrapper de la API de Binance Futures
├── strategy.py          # Lógica de la estrategia (cruce de EMAs)
├── risk_manager.py       # Cálculo de tamaño de posición, SL y TP
├── bot.py               # Loop principal que ejecuta todo
└── requirements.txt
```

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración de las API Keys (Testnet)

1. Entrá a https://testnet.binancefuture.com
2. Logueate con tu cuenta de GitHub (no hace falta cuenta real de Binance)
3. Generá tu API Key y Secret Key
4. Seteá las variables de entorno:

```bash
# Linux / Mac
export BINANCE_API_KEY="tu_api_key"
export BINANCE_API_SECRET="tu_api_secret"

# Windows PowerShell
$env:BINANCE_API_KEY="tu_api_key"
$env:BINANCE_API_SECRET="tu_api_secret"
```

**Nunca subas tus claves a GitHub ni las escribas directamente en el código.**

## Uso

Revisá y ajustá los parámetros en `config.py`:
- `SYMBOL`: par a operar (ej: "BTCUSDT")
- `TIMEFRAME`: temporalidad de las velas
- `LEVERAGE`: apalancamiento
- `RISK_PER_TRADE_PCT`, `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`: gestión de riesgo
- `USE_TESTNET`: dejalo en `True` hasta estar seguro del comportamiento del bot

Ejecutar:

```bash
python bot.py
```

El bot va a:
1. Revisar la señal de la estrategia cada `POLL_INTERVAL_SECONDS`
2. Abrir una posición cuando detecta un cruce de EMAs, con SL y TP automáticos
3. Esperar a que la posición se cierre (por SL o TP) antes de buscar la próxima señal

Detenerlo con `Ctrl+C`.

## Cómo pasar a cuenta real

Cuando estés conforme con los resultados en testnet:
1. Generá API keys en tu cuenta real de Binance (con permisos de Futures, sin retiro)
2. Actualizá las variables de entorno con esas nuevas claves
3. Cambiá `USE_TESTNET = False` en `config.py`
4. Empezá con montos chicos

## Cómo personalizar la estrategia

Toda la lógica de señales vive en `strategy.py`, en la función `get_signal(df)`.
Podés reemplazarla por tu propia lógica (RSI, MACD, bandas de Bollinger,
price action, un modelo de ML, etc.) siempre que la función siga devolviendo
`"LONG"`, `"SHORT"` o `"NONE"`.

## Limitaciones conocidas (a mejorar antes de producción)

- No maneja reconexión avanzada ante caídas prolongadas de la API
- No persiste estado en disco (si el proceso se reinicia, pierde el historial)
- No incluye backtesting automatizado (recomendado hacerlo antes de operar real)
- No contempla comisiones ni slippage en el cálculo de riesgo
