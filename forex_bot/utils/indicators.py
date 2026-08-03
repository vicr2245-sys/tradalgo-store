"""
Technical Indicator Library
Pure Python/numpy implementations. No TA-Lib dependency required.
"""

import numpy as np


def closes(candles: list) -> np.ndarray:
    return np.array([c["close"] for c in candles], dtype=float)

def highs(candles: list) -> np.ndarray:
    return np.array([c["high"] for c in candles], dtype=float)

def lows(candles: list) -> np.ndarray:
    return np.array([c["low"] for c in candles], dtype=float)


def ema(series: np.ndarray, period: int) -> np.ndarray:
    result = np.full_like(series, np.nan)
    k = 2 / (period + 1)
    # seed with SMA
    result[period - 1] = series[:period].mean()
    for i in range(period, len(series)):
        result[i] = series[i] * k + result[i - 1] * (1 - k)
    return result


def sma(series: np.ndarray, period: int) -> np.ndarray:
    result = np.full_like(series, np.nan)
    for i in range(period - 1, len(series)):
        result[i] = series[i - period + 1 : i + 1].mean()
    return result


def rsi(series: np.ndarray, period: int = 14) -> np.ndarray:
    deltas = np.diff(series)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.full(len(series), np.nan)
    avg_loss = np.full(len(series), np.nan)

    avg_gain[period] = gains[:period].mean()
    avg_loss[period] = losses[:period].mean()

    for i in range(period + 1, len(series)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period

    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
    rsi_vals = np.where(avg_loss == 0, 100, 100 - (100 / (1 + rs)))
    return rsi_vals


def macd(series: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast   = ema(series, fast)
    ema_slow   = ema(series, slow)
    macd_line  = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series: np.ndarray, period: int = 20, num_std: float = 2.0):
    mid  = sma(series, period)
    std  = np.full_like(series, np.nan)
    for i in range(period - 1, len(series)):
        std[i] = series[i - period + 1 : i + 1].std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(candles: list, period: int = 14) -> np.ndarray:
    h = highs(candles)
    l = lows(candles)
    c = closes(candles)
    tr = np.zeros(len(candles))
    for i in range(1, len(candles)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    result = np.full(len(candles), np.nan)
    result[period] = tr[1 : period + 1].mean()
    for i in range(period + 1, len(candles)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


def is_nan(val) -> bool:
    return val is None or (isinstance(val, float) and np.isnan(val))
