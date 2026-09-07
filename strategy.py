"""
Estrategia de ejemplo: cruce de medias móviles exponenciales (EMA).

Señal LONG:  la EMA rápida cruza hacia arriba a la EMA lenta.
Señal SHORT: la EMA rápida cruza hacia abajo a la EMA lenta.
Sin señal:   no hay cruce reciente.

Esto es solo un punto de partida. Podés reemplazar get_signal() por
tu propia lógica (RSI, MACD, price action, machine learning, etc.)
manteniendo la misma firma de función para no romper el resto del bot.
"""

import pandas as pd
import config


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=config.EMA_SLOW, adjust=False).mean()
    return df


def get_signal(df: pd.DataFrame) -> str:
    """
    Devuelve "LONG", "SHORT" o "NONE" según el cruce de EMAs en la última vela cerrada.
    Se usa la penúltima vela como "actual cerrada" y la anterior como referencia,
    para evitar operar sobre una vela que todavía está formándose.
    """
    df = add_indicators(df)

    if len(df) < max(config.EMA_FAST, config.EMA_SLOW) + 2:
        return "NONE"

    prev = df.iloc[-3]
    curr = df.iloc[-2]  # última vela cerrada

    cruzo_arriba = prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
    cruzo_abajo = prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]

    if cruzo_arriba:
        return "LONG"
    elif cruzo_abajo:
        return "SHORT"
    return "NONE"
