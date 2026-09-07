"""
Wrapper sobre python-binance para simplificar las operaciones de Futures
que usa el bot: obtener velas, precio actual, mandar órdenes, cerrar posiciones, etc.
"""

import logging
import pandas as pd
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET
)

import config
from logging_config import LOGGER_NAME


class BinanceFuturesClient:
    def __init__(self):
        if not config.API_KEY or not config.API_SECRET:
            raise ValueError(
                "Faltan las API keys. Seteá las variables de entorno "
                "BINANCE_API_KEY y BINANCE_API_SECRET antes de correr el bot."
            )

        self.client = Client(config.API_KEY, config.API_SECRET, testnet=config.USE_TESTNET)

        # Aseguramos el apalancamiento configurado para el símbolo
        try:
            self.client.futures_change_leverage(symbol=config.SYMBOL, leverage=config.LEVERAGE)
        except Exception:
            logging.getLogger(LOGGER_NAME).warning(
                "No se pudo fijar el apalancamiento", exc_info=True,
                extra={"event_type": "leverage_error", "symbol": config.SYMBOL},
            )

    def get_klines(self, limit: int = 100) -> pd.DataFrame:
        """Trae las últimas velas del símbolo/timeframe configurado."""
        raw = self.client.futures_klines(
            symbol=config.SYMBOL, interval=config.TIMEFRAME, limit=limit
        )
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df

    def get_mark_price(self) -> float:
        data = self.client.futures_mark_price(symbol=config.SYMBOL)
        return float(data["markPrice"])

    def get_account_balance_usdt(self) -> float:
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    def get_open_position(self):
        """Devuelve la posición abierta actual (o None si no hay)."""
        positions = self.client.futures_position_information(symbol=config.SYMBOL)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                return p
        return None

    def get_symbol_precision(self):
        """Obtiene la precisión de cantidad y precio requerida por Binance para el símbolo."""
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == config.SYMBOL:
                qty_precision = s["quantityPrecision"]
                price_precision = s["pricePrecision"]
                return qty_precision, price_precision
        raise ValueError(f"No se encontró info del símbolo {config.SYMBOL}")

    def open_market_position(self, side: str, quantity: float):
        """
        Abre una posición a mercado.
        side: SIDE_BUY (long) o SIDE_SELL (short)
        """
        order = self.client.futures_create_order(
            symbol=config.SYMBOL,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
        )
        return order

    def set_stop_loss_take_profit(self, side: str, quantity: float,
                                   stop_price: float, take_profit_price: float):
        """
        Coloca las órdenes de stop-loss y take-profit para una posición ya abierta.
        Si side fue BUY (long), las órdenes de cierre son SELL, y viceversa.
        """
        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY

        sl_order = self.client.futures_create_order(
            symbol=config.SYMBOL,
            side=close_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=stop_price,
            closePosition=True,
        )

        tp_order = self.client.futures_create_order(
            symbol=config.SYMBOL,
            side=close_side,
            type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=take_profit_price,
            closePosition=True,
        )
        return sl_order, tp_order

    def close_position_market(self):
        """Cierra cualquier posición abierta a mercado."""
        position = self.get_open_position()
        if position is None:
            return None
        amt = float(position["positionAmt"])
        side = SIDE_SELL if amt > 0 else SIDE_BUY
        return self.client.futures_create_order(
            symbol=config.SYMBOL,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=abs(amt),
        )

    def has_protective_orders(self) -> bool:
        """
        Chequea si existen órdenes de Stop-Loss o Take-Profit abiertas
        para el símbolo configurado. Útil para detectar una posición
        que quedó abierta sin protección (ej: por un corte de conexión).
        """
        open_orders = self.client.futures_get_open_orders(symbol=config.SYMBOL)
        protective_types = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"}
        return any(o["type"] in protective_types for o in open_orders)

    def cancel_all_open_orders(self):
        """Cancela todas las órdenes abiertas (ej: SL/TP viejas) del símbolo."""
        return self.client.futures_cancel_all_open_orders(symbol=config.SYMBOL)
