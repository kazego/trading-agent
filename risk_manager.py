"""
Gestión de riesgo: calcula el tamaño de posición y los niveles de
stop-loss / take-profit según los parámetros definidos en config.py.

La idea central: nunca arriesgar más de un % fijo del capital en una sola operación,
sin importar el apalancamiento usado.
"""

import config


def calculate_position_size(balance_usdt: float, entry_price: float,
                             qty_precision: int) -> float:
    """
    Calcula la cantidad (en el activo base, ej BTC) a comprar/vender.

    Lógica: arriesgamos RISK_PER_TRADE_PCT% del balance como pérdida máxima,
    dado el STOP_LOSS_PCT% definido. Esto desacopla el tamaño de la posición
    del apalancamiento: el riesgo real en USDT es siempre el mismo.
    """
    risk_amount_usdt = balance_usdt * (config.RISK_PER_TRADE_PCT / 100)
    stop_loss_fraction = config.STOP_LOSS_PCT / 100

    # Cuánto capital (nominal, con apalancamiento) corresponde a ese riesgo
    notional = risk_amount_usdt / stop_loss_fraction
    notional = min(notional, balance_usdt * config.LEVERAGE)  # no exceder el margen disponible

    quantity = notional / entry_price
    return round(quantity, qty_precision)


def calculate_stop_loss(entry_price: float, side: str, price_precision: int) -> float:
    fraction = config.STOP_LOSS_PCT / 100
    if side == "LONG":
        price = entry_price * (1 - fraction)
    else:
        price = entry_price * (1 + fraction)
    return round(price, price_precision)


def calculate_take_profit(entry_price: float, side: str, price_precision: int) -> float:
    fraction = config.TAKE_PROFIT_PCT / 100
    if side == "LONG":
        price = entry_price * (1 + fraction)
    else:
        price = entry_price * (1 - fraction)
    return round(price, price_precision)
