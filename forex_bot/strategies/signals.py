"""
5 Trading Strategies
Each strategy returns a signal dict: {signal: "BUY"|"SELL"|None, sl_pips, tp_pips, reason}
"""

import numpy as np
from utils.indicators import closes, ema, rsi, macd, bollinger_bands, atr, is_nan
from config import DEFAULT_SL_PIPS, DEFAULT_TP_PIPS, GOLD_SL_PIPS, GOLD_TP_PIPS


def _sl_tp(instrument, sl_pips=None, tp_pips=None):
    if "XAU" in instrument:
        return sl_pips or GOLD_SL_PIPS, tp_pips or GOLD_TP_PIPS
    return sl_pips or DEFAULT_SL_PIPS, tp_pips or DEFAULT_TP_PIPS


# ── Strategy 1: EMA Cross ─────────────────────────────────────────────────────

def ema_cross(candles: list, instrument: str) -> dict:
    """
    Fast EMA (9) crosses above/below Slow EMA (21).
    Trend filter: price must be above/below 50 EMA.
    """
    c = closes(candles)
    fast   = ema(c, 9)
    slow   = ema(c, 21)
    trend  = ema(c, 50)

    i = -1  # latest complete candle
    if any(is_nan(v) for v in [fast[i], fast[i-1], slow[i], slow[i-1], trend[i]]):
        return {"signal": None, "reason": "insufficient data"}

    cross_up   = fast[i-1] < slow[i-1] and fast[i] > slow[i]
    cross_down = fast[i-1] > slow[i-1] and fast[i] < slow[i]

    sl, tp = _sl_tp(instrument)

    if cross_up and c[i] > trend[i]:
        return {"signal": "BUY",  "sl_pips": sl, "tp_pips": tp * 1.5, "reason": "EMA9 crossed above EMA21 (bullish trend)"}
    if cross_down and c[i] < trend[i]:
        return {"signal": "SELL", "sl_pips": sl, "tp_pips": tp * 1.5, "reason": "EMA9 crossed below EMA21 (bearish trend)"}
    return {"signal": None, "reason": "no EMA cross"}


# ── Strategy 2: RSI Reversal ──────────────────────────────────────────────────

def rsi_reversal(candles: list, instrument: str) -> dict:
    """
    RSI oversold (<30) → BUY, overbought (>70) → SELL.
    Requires RSI to recover back across 30/70 threshold (confirmation).
    """
    c   = closes(candles)
    rsi_vals = rsi(c, 14)
    i = -1

    if is_nan(rsi_vals[i]) or is_nan(rsi_vals[i-1]):
        return {"signal": None, "reason": "insufficient data"}

    sl, tp = _sl_tp(instrument)

    # Crossed back up through 30 (oversold recovery)
    if rsi_vals[i-1] < 30 and rsi_vals[i] > 30:
        return {"signal": "BUY",  "sl_pips": sl, "tp_pips": tp, "reason": f"RSI recovered from oversold ({rsi_vals[i]:.1f})"}
    # Crossed back down through 70 (overbought reversal)
    if rsi_vals[i-1] > 70 and rsi_vals[i] < 70:
        return {"signal": "SELL", "sl_pips": sl, "tp_pips": tp, "reason": f"RSI turned from overbought ({rsi_vals[i]:.1f})"}
    return {"signal": None, "reason": f"RSI neutral ({rsi_vals[i]:.1f})"}


# ── Strategy 3: Bollinger Band Breakout ───────────────────────────────────────

def bollinger_break(candles: list, instrument: str) -> dict:
    """
    Price closes outside Bollinger Band (20, 2σ) → momentum entry.
    Inside band afterward = exit signal (mean reversion).
    """
    c = closes(candles)
    upper, mid, lower = bollinger_bands(c, 20, 2.0)
    i = -1

    if any(is_nan(v) for v in [upper[i], lower[i], mid[i]]):
        return {"signal": None, "reason": "insufficient data"}

    band_width = upper[i] - lower[i]
    sl_pips = max(_sl_tp(instrument)[0], int(band_width * 5000))  # ATR-adjusted SL
    _, tp_pips = _sl_tp(instrument)

    # Breakout above upper band (strong bullish momentum)
    if c[i] > upper[i] and c[i-1] <= upper[i-1]:
        return {"signal": "BUY",  "sl_pips": sl_pips, "tp_pips": tp_pips * 2, "reason": f"BB upper breakout (price={c[i]:.5f}, upper={upper[i]:.5f})"}
    # Breakdown below lower band (strong bearish momentum)
    if c[i] < lower[i] and c[i-1] >= lower[i-1]:
        return {"signal": "SELL", "sl_pips": sl_pips, "tp_pips": tp_pips * 2, "reason": f"BB lower breakdown (price={c[i]:.5f}, lower={lower[i]:.5f})"}
    return {"signal": None, "reason": "price within Bollinger Bands"}


# ── Strategy 4: MACD Momentum ─────────────────────────────────────────────────

def macd_momentum(candles: list, instrument: str) -> dict:
    """
    MACD line crosses signal line.
    Histogram must confirm (growing in signal direction).
    """
    c = closes(candles)
    macd_line, signal_line, histogram = macd(c, 12, 26, 9)
    i = -1

    if any(is_nan(v) for v in [macd_line[i], signal_line[i], histogram[i], macd_line[i-1]]):
        return {"signal": None, "reason": "insufficient data"}

    sl, tp = _sl_tp(instrument)

    bullish_cross = macd_line[i-1] < signal_line[i-1] and macd_line[i] > signal_line[i]
    bearish_cross = macd_line[i-1] > signal_line[i-1] and macd_line[i] < signal_line[i]

    if bullish_cross and histogram[i] > 0:
        return {"signal": "BUY",  "sl_pips": sl, "tp_pips": tp, "reason": f"MACD bullish cross (hist={histogram[i]:.6f})"}
    if bearish_cross and histogram[i] < 0:
        return {"signal": "SELL", "sl_pips": sl, "tp_pips": tp, "reason": f"MACD bearish cross (hist={histogram[i]:.6f})"}
    return {"signal": None, "reason": f"MACD: {macd_line[i]:.6f} | signal: {signal_line[i]:.6f}"}


# ── Strategy 5: Session Breakout ──────────────────────────────────────────────

def session_break(candles: list, instrument: str) -> dict:
    """
    Uses the first 4 candles of the session to define a range.
    Breakout above/below range = signal.
    Designed for H1 candles.
    """
    if len(candles) < 10:
        return {"signal": None, "reason": "not enough candles"}

    c = closes(candles)
    # Define range from 4 candles back (session open range)
    from utils.indicators import highs, lows
    h = highs(candles)
    l = lows(candles)

    range_high = max(h[-6:-2])
    range_low  = min(l[-6:-2])
    current    = c[-1]

    sl, tp = _sl_tp(instrument)

    if current > range_high * 1.0005:  # 0.05% clearance
        return {"signal": "BUY",  "sl_pips": sl, "tp_pips": tp, "reason": f"Session range breakout UP ({current:.5f} > {range_high:.5f})"}
    if current < range_low * 0.9995:
        return {"signal": "SELL", "sl_pips": sl, "tp_pips": tp, "reason": f"Session range breakdown ({current:.5f} < {range_low:.5f})"}
    return {"signal": None, "reason": f"Inside session range [{range_low:.5f} – {range_high:.5f}]"}


# ── Registry ──────────────────────────────────────────────────────────────────

STRATEGIES = {
    "EMA_Cross":       ema_cross,
    "RSI_Reversal":    rsi_reversal,
    "Bollinger_Break": bollinger_break,
    "MACD_Momentum":   macd_momentum,
    "Session_Break":   session_break,
}


def run_all_strategies(candles: list, instrument: str) -> dict:
    """Run all strategies and return their individual results."""
    results = {}
    for name, fn in STRATEGIES.items():
        try:
            results[name] = fn(candles, instrument)
        except Exception as e:
            results[name] = {"signal": None, "reason": f"error: {e}"}
    return results


def consensus_signal(results: dict, weights: dict, threshold: float = 0.4) -> dict:
    """
    Weighted consensus across strategy signals.
    Returns strongest signal if weighted score > threshold.
    """
    buy_score  = 0.0
    sell_score = 0.0
    reasons    = []

    for name, res in results.items():
        w = weights.get(name, 0.2)
        sig = res.get("signal")
        if sig == "BUY":
            buy_score  += w
            reasons.append(f"{name}: BUY")
        elif sig == "SELL":
            sell_score += w
            reasons.append(f"{name}: SELL")

    best_signal = None
    best_score  = 0.0
    best_sl     = DEFAULT_SL_PIPS
    best_tp     = DEFAULT_TP_PIPS

    if buy_score >= threshold and buy_score > sell_score:
        best_signal = "BUY"
        best_score  = buy_score
        # use SL/TP from highest-weighted BUY strategy
        buy_results = {n: r for n, r in results.items() if r.get("signal") == "BUY"}
        if buy_results:
            top = max(buy_results, key=lambda n: weights.get(n, 0))
            best_sl = buy_results[top].get("sl_pips", DEFAULT_SL_PIPS)
            best_tp = buy_results[top].get("tp_pips", DEFAULT_TP_PIPS)

    elif sell_score >= threshold and sell_score > buy_score:
        best_signal = "SELL"
        best_score  = sell_score
        sell_results = {n: r for n, r in results.items() if r.get("signal") == "SELL"}
        if sell_results:
            top = max(sell_results, key=lambda n: weights.get(n, 0))
            best_sl = sell_results[top].get("sl_pips", DEFAULT_SL_PIPS)
            best_tp = sell_results[top].get("tp_pips", DEFAULT_TP_PIPS)

    return {
        "signal":    best_signal,
        "score":     round(best_score, 3),
        "sl_pips":   best_sl,
        "tp_pips":   best_tp,
        "reasons":   reasons,
    }
