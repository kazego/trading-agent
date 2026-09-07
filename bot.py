"""
Bot de trading automático para Binance Futures.

FLUJO:
1. Cada POLL_INTERVAL_SECONDS, trae las últimas velas del símbolo.
2. Calcula la señal de la estrategia (LONG / SHORT / NONE).
3. Si hay señal y no hay posición abierta, calcula tamaño de posición
   y niveles de SL/TP según la gestión de riesgo, y abre la operación.
4. Si ya hay una posición abierta, no abre otra (según MAX_OPEN_POSITIONS).
5. Loguea todo lo que hace.

IMPORTANTE:
- Correr primero en TESTNET (config.USE_TESTNET = True) hasta estar
  seguro de que el bot se comporta como esperás.
- Este es un punto de partida educativo, no un producto terminado.
  Antes de usar plata real: backtesteá la estrategia, probá en testnet
  por un tiempo largo, y considerá casos borde (caídas de conexión,
  errores de la API, slippage, comisiones, etc.)
"""

import time
import logging
from uuid import uuid4

from binance.enums import SIDE_BUY, SIDE_SELL

import config
import strategy
import risk_manager
from binance_client import BinanceFuturesClient
from logging_config import LOGGER_NAME, configure_logging
from errors import ApplicationError


logger = logging.getLogger(LOGGER_NAME)


def log(msg: str):
    severity = logging.INFO
    if "[ERROR CRÍTICO]" in msg or "[ERROR GRAVE]" in msg:
        severity = logging.CRITICAL
    elif "[ERROR]" in msg:
        severity = logging.ERROR
    elif "[WARN]" in msg or "[ALERTA]" in msg:
        severity = logging.WARNING
    logger.log(severity, msg, extra={"event_type": "legacy_bot", "symbol": config.SYMBOL})


def run_bot():
    settings = config.load_settings()  # Must precede any Binance construction.
    configure_logging(settings.LOG_DIRECTORY, secrets=(config.API_KEY, config.API_SECRET), run_id=str(uuid4()))
    logger.info("Configuration validated; Phase 2 limits are declarative",
                extra={"event_type": "startup", "symbol": config.SYMBOL})
    logger.warning("TRADING_ENABLED is declarative and is not enforced by the legacy loop",
                   extra={"event_type": "phase2_scope"})
    try:
        client = BinanceFuturesClient()
        qty_precision, price_precision = client.get_symbol_precision()
    except Exception:
        logger.exception("Client initialization failed", extra={"event_type": "startup_error"})
        raise ApplicationError("Client initialization failed; see sanitized logs") from None
    log(f"Bot iniciado | Símbolo: {config.SYMBOL} | Timeframe: {config.TIMEFRAME} "
        f"| Testnet: {config.USE_TESTNET}")

    while True:
        try:
            position = client.get_open_position()

            if position is not None:
                amt = float(position["positionAmt"])
                if client.has_protective_orders():
                    log(f"Posición abierta detectada: {'LONG' if amt > 0 else 'SHORT'} "
                        f"cantidad={amt}. Esperando cierre por SL/TP...")
                else:
                    # Posición sin SL/TP (ej: por un corte de conexión previo).
                    # No la dejamos así, la cerramos por seguridad.
                    log(f"[ALERTA] Posición {'LONG' if amt > 0 else 'SHORT'} cantidad={amt} "
                        f"SIN órdenes de protección. Cerrando por seguridad...")
                    try:
                        client.close_position_market()
                        log("Posición desprotegida cerrada exitosamente.")
                    except Exception as e:
                        log(f"[ERROR GRAVE] No se pudo cerrar la posición desprotegida: {e}. "
                            f"¡¡REVISAR MANUALMENTE EN BINANCE AHORA!!")
            else:
                df = client.get_klines(limit=100)
                signal = strategy.get_signal(df)
                log(f"Señal actual: {signal}")

                if signal in ("LONG", "SHORT"):
                    entry_price = client.get_mark_price()
                    balance = client.get_account_balance_usdt()

                    if balance <= 0:
                        log("[WARN] Balance USDT en 0, no se puede operar.")
                    else:
                        quantity = risk_manager.calculate_position_size(
                            balance, entry_price, qty_precision
                        )
                        if quantity <= 0:
                            log("[WARN] Cantidad calculada es 0, se omite la operación.")
                        else:
                            side = SIDE_BUY if signal == "LONG" else SIDE_SELL
                            sl_price = risk_manager.calculate_stop_loss(
                                entry_price, signal, price_precision
                            )
                            tp_price = risk_manager.calculate_take_profit(
                                entry_price, signal, price_precision
                            )

                            log(f"Abriendo {signal} | qty={quantity} | "
                                f"entry~={entry_price} | SL={sl_price} | TP={tp_price}")

                            client.open_market_position(side, quantity)
                            log("Posición abierta. Colocando órdenes SL/TP...")

                            try:
                                client.set_stop_loss_take_profit(
                                    side, quantity, sl_price, tp_price
                                )
                                log("Posición y órdenes SL/TP colocadas correctamente.")
                            except Exception as sltp_error:
                                # CRÍTICO: si no se pudo proteger la posición, la cerramos
                                # de inmediato en vez de dejarla abierta sin SL/TP.
                                log(f"[ERROR CRÍTICO] No se pudo colocar SL/TP: {sltp_error}")
                                log("Cerrando posición de emergencia por seguridad...")
                                try:
                                    client.close_position_market()
                                    log("Posición cerrada de emergencia exitosamente.")
                                except Exception as close_error:
                                    log(f"[ERROR GRAVE] No se pudo cerrar la posición "
                                        f"de emergencia: {close_error}. "
                                        f"¡¡REVISAR MANUALMENTE EN BINANCE AHORA!!")

            time.sleep(config.POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Bot detenido manualmente por el usuario.", extra={"event_type": "shutdown"})
            break
        except Exception as e:
            log(f"[ERROR] {e}")
            logger.exception("Main loop error", extra={"event_type": "api_error", "symbol": config.SYMBOL})
            time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_bot()
