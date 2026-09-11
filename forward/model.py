"""Completed-only observation kernels; no execution, data acquisition or state."""
import math
from decimal import Decimal, Context, localcontext

import pandas as pd

from backtest.data import Candle, SYMBOLS, STEP_MS
from research.data import aggregate_bucket, exact_sum
from research.features import _atr
from research.spec import fingerprint, canonical

LABEL = 'FORWARD_PAPER / NON_EXECUTING'
FOUR_HOURS = 16 * STEP_MS
HISTORY = 600 * FOUR_HOURS
CONTRACT = 'b9db00e5e2ec6fcf6cc34cde7c2144a3bdba73556c8d2bb05c7c7450cf68c27d'
SELECTION = '92ee399eccbb27b0b6cfa32efbcfea1e9e837569bf61befd6d777eff10014d84'


def completed(candles, symbol, start, end):
    """Validate the exact immutable, contiguous half-open input window."""
    cs = tuple(candles)
    if symbol not in SYMBOLS or start % STEP_MS or end % STEP_MS or end <= start:
        raise ValueError('Invalid completed interval')
    if len(cs) != (end-start)//STEP_MS:
        raise ValueError('Missing completed candles')
    for i, c in enumerate(cs):
        if (type(c) is not Candle or c.symbol != symbol or c.timeframe != '15m'
                or c.open_time_ms != start+i*STEP_MS or c.close_time_ms >= end):
            raise ValueError('Duplicate, gap, symbol mismatch or future candle')
    return cs


def signal_snapshot(candles, symbol, boundary):
    """99 completed EMA rows, 100 ATR rows, no current opening price required.

    EWM is causal: indices -2/-1 of 99 completed rows equal -3/-2 of the
    frozen 100-row frame for ANY appended forming price. No EMA caching.
    The separate forward adapter avoids weakening historical access guards.
    """
    if type(boundary) is not int or boundary % FOUR_HOURS:
        raise ValueError('Only completed UTC 4h boundaries')
    cs = completed(candles, symbol, boundary-HISTORY, boundary)
    bars = tuple(aggregate_bucket(cs[i:i+16], '4h') for i in range(0, len(cs), 16))
    values = [float(c.close) for c in bars[-99:]]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('Unrepresentable EMA price')
    series = pd.Series(values, dtype='float64')
    fast = series.ewm(span=9, adjust=False).mean()
    slow = series.ewm(span=21, adjust=False).mean()
    signal = ('LONG' if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
              else 'SHORT' if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
              else 'NONE')
    body = canonical(dict(label=LABEL, candidate='phase6d-c-atr-v1', contract=CONTRACT,
        symbol=symbol, boundary_ms=boundary, signal_timeframe='4h', signal=signal,
        completed_through_ms=boundary-1, ema_completed_rows=99,
        ema_completed_opens=[c.open_time_ms for c in bars[-99:]],
        atr_input_rows=100, atr14=_atr(bars[-100:]),
        visible_fingerprint=fingerprint([c.record() for c in bars]),
        ema_previous=[Decimal(str(fast.iloc[-2])), Decimal(str(slow.iloc[-2]))],
        ema_latest=[Decimal(str(fast.iloc[-1])), Decimal(str(slow.iloc[-1]))]))
    return dict(body, signal_fingerprint=fingerprint(body))


def context_snapshot(candles, symbol, boundary):
    """Latest FULLY completed bucket per context TF; descriptive OHLCV only.

    No fitted thresholds or strategy indicators. In particular the current day
    is excluded until UTC midnight. Context never enters signal_snapshot().
    """
    cs = completed(candles, symbol, boundary-HISTORY, boundary)
    result = {}
    for name, step in (('1D', 96*STEP_MS), ('1H', 4*STEP_MS), ('15M', STEP_MS)):
        end = boundary//step*step
        offset = (end-step-cs[0].open_time_ms)//STEP_MS
        parts = cs[offset:offset+step//STEP_MS]
        completed(parts, symbol, end-step, end)
        with localcontext(Context(prec=50)):
            result[name] = canonical(dict(role='DESCRIPTIVE_ONLY', open_time_ms=end-step,
                close_time_ms=end-1, open=parts[0].open, high=max(c.high for c in parts),
                low=min(c.low for c in parts), close=parts[-1].close,
                volume=exact_sum(c.volume for c in parts),
                return_fraction=(parts[-1].close-parts[0].open)/parts[0].open,
                range_fraction=(max(c.high for c in parts)-min(c.low for c in parts))/parts[0].open,
                components_fingerprint=fingerprint([c.record() for c in parts])))
    return dict(values=result, context_fingerprint=fingerprint(result))
