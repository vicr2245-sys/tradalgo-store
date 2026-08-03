"""
Performance Tracker
-------------------
Maintains a persistent history of every closed trade and computes
live statistics: win rate, P&L, profit factor, streaks, drawdown,
best/worst pair, strategy breakdown, and daily summaries.

Data is stored in logs/performance.json — survives restarts.
Called by bot.py every time a trade closes (win or loss).
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing   import Optional

log = logging.getLogger(__name__)

PERF_FILE = "logs/performance.json"

# ── schema ────────────────────────────────────────────────────────────────────
# {
#   "trades": [
#     { "id", "instrument", "direction", "entry", "exit", "pl",
#       "pl_pct", "reason", "strategy", "opened_at", "closed_at" }
#   ],
#   "daily": {
#     "2024-01-15": { "trades": 4, "wins": 3, "pl": 47.20, "emailed": false }
#   }
# }

_data: dict = {"trades": [], "daily": {}}


def _save():
    os.makedirs("logs", exist_ok=True)
    try:
        with open(PERF_FILE, "w") as f:
            json.dump(_data, f, indent=2)
    except Exception as e:
        log.error(f"Performance save failed: {e}")


def _load():
    global _data
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE) as f:
                _data = json.load(f)
            log.info(f"Performance loaded: {len(_data['trades'])} trades")
        except Exception as e:
            log.warning(f"Performance load failed (starting fresh): {e}")
            _data = {"trades": [], "daily": {}}


_load()


# ── public API ────────────────────────────────────────────────────────────────

def record_close(
    trade_id:   str,
    instrument: str,
    direction:  str,
    entry:      float,
    exit_price: float,
    pl:         float,
    pl_pct:     float,
    reason:     str,
    strategy:   str,
    opened_at:  str = "",
):
    """Call this every time a trade closes. Adds to history and updates daily bucket."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    trade = {
        "id":          trade_id,
        "instrument":  instrument,
        "direction":   direction,
        "entry":       round(float(entry),      5),
        "exit":        round(float(exit_price), 5),
        "pl":          round(float(pl),         2),
        "pl_pct":      round(float(pl_pct),     4),
        "reason":      reason,
        "strategy":    strategy,
        "opened_at":   opened_at,
        "closed_at":   now.isoformat(),
    }
    _data["trades"].append(trade)

    # Daily bucket
    if today not in _data["daily"]:
        _data["daily"][today] = {"trades": 0, "wins": 0, "losses": 0, "pl": 0.0, "emailed": False}
    _data["daily"][today]["trades"]  += 1
    _data["daily"][today]["pl"]      += round(float(pl), 2)
    if pl > 0:
        _data["daily"][today]["wins"]    += 1
    else:
        _data["daily"][today]["losses"]  += 1

    _save()
    log.info(f"Performance recorded: {instrument} P&L={pl:+.2f} | total_trades={len(_data['trades'])}")


def get_stats(days: int = None) -> dict:
    """
    Compute all performance metrics.
    days=None → all time. days=7 → last 7 days. days=1 → today only.
    """
    trades = _data["trades"]

    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        trades = [t for t in trades if t.get("closed_at", "") >= cutoff]

    if not trades:
        return _empty_stats(days)

    total   = len(trades)
    wins    = [t for t in trades if t["pl"] > 0]
    losses  = [t for t in trades if t["pl"] <= 0]
    pls     = [t["pl"] for t in trades]
    net_pl  = round(sum(pls), 2)

    win_rate     = round(len(wins) / total * 100, 1)
    avg_win      = round(sum(t["pl"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss     = round(sum(t["pl"] for t in losses) / len(losses), 2) if losses else 0
    gross_profit = sum(t["pl"] for t in wins)
    gross_loss   = abs(sum(t["pl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else float("inf")

    # Max drawdown on running P&L curve
    running  = 0.0
    peak     = 0.0
    max_dd   = 0.0
    for pl in pls:
        running += pl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    # Win/loss streaks
    current_streak, best_streak, worst_streak = _streaks(trades)

    # Per-instrument breakdown
    by_instrument = _by_instrument(trades)

    # Per-strategy breakdown
    by_strategy = _by_strategy(trades)

    # Best and worst single trade
    best_trade  = max(trades, key=lambda t: t["pl"])
    worst_trade = min(trades, key=lambda t: t["pl"])

    # Today's stats
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_data = _data["daily"].get(today, {})

    return {
        "period":         f"Last {days} days" if days else "All time",
        "total_trades":   total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       win_rate,
        "net_pl":         net_pl,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "profit_factor":  profit_factor,
        "max_drawdown":   max_dd,
        "gross_profit":   round(gross_profit, 2),
        "gross_loss":     round(gross_loss,   2),
        "current_streak": current_streak,
        "best_streak":    best_streak,
        "worst_streak":   worst_streak,
        "best_trade":     best_trade,
        "worst_trade":    worst_trade,
        "by_instrument":  by_instrument,
        "by_strategy":    by_strategy,
        "today":          today_data,
        "daily_history":  _daily_history(14),  # last 14 days for chart
        "trades":         trades[-50:],         # last 50 for history table
    }


def get_today() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _data["daily"].get(today, {"trades": 0, "wins": 0, "losses": 0, "pl": 0.0})


def mark_daily_emailed(date_str: str):
    if date_str in _data["daily"]:
        _data["daily"][date_str]["emailed"] = True
        _save()


def get_unemailed_days() -> list:
    """Returns list of date strings that have trades but haven't been emailed yet."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    result = []
    for date, d in _data["daily"].items():
        if date <= yesterday and not d.get("emailed") and d.get("trades", 0) > 0:
            result.append(date)
    return sorted(result)


# ── helpers ───────────────────────────────────────────────────────────────────

def _empty_stats(days):
    return {
        "period": f"Last {days} days" if days else "All time",
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
        "net_pl": 0, "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
        "max_drawdown": 0, "gross_profit": 0, "gross_loss": 0,
        "current_streak": 0, "best_streak": 0, "worst_streak": 0,
        "best_trade": None, "worst_trade": None,
        "by_instrument": {}, "by_strategy": {}, "today": {},
        "daily_history": [], "trades": [],
    }


def _streaks(trades: list) -> tuple:
    """Returns (current_streak, best_win_streak, worst_loss_streak)."""
    if not trades:
        return 0, 0, 0

    sorted_trades = sorted(trades, key=lambda t: t.get("closed_at", ""))
    current = 0
    best    = 0
    worst   = 0
    streak  = 0

    for t in sorted_trades:
        if t["pl"] > 0:
            streak = max(streak + 1, 1)
        else:
            streak = min(streak - 1, -1)
        best  = max(best,  streak)
        worst = min(worst, streak)

    current = streak
    return current, best, abs(worst)


def _by_instrument(trades: list) -> dict:
    result = {}
    for t in trades:
        inst = t["instrument"]
        if inst not in result:
            result[inst] = {"trades": 0, "wins": 0, "losses": 0, "pl": 0.0, "win_rate": 0}
        result[inst]["trades"] += 1
        result[inst]["pl"]     = round(result[inst]["pl"] + t["pl"], 2)
        if t["pl"] > 0:
            result[inst]["wins"]   += 1
        else:
            result[inst]["losses"] += 1
    for inst in result:
        n = result[inst]["trades"]
        result[inst]["win_rate"] = round(result[inst]["wins"] / n * 100, 1) if n else 0
    # Sort by net P&L descending
    return dict(sorted(result.items(), key=lambda x: x[1]["pl"], reverse=True))


def _by_strategy(trades: list) -> dict:
    result = {}
    for t in trades:
        strats = t.get("strategy", "Unknown")
        # Strategy field may contain "EMA Cross | RSI Reversal" — use first one
        strat = strats.split("|")[0].strip() if strats else "Unknown"
        if strat not in result:
            result[strat] = {"trades": 0, "wins": 0, "pl": 0.0, "win_rate": 0}
        result[strat]["trades"] += 1
        result[strat]["pl"]     = round(result[strat]["pl"] + t["pl"], 2)
        if t["pl"] > 0:
            result[strat]["wins"] += 1
    for s in result:
        n = result[s]["trades"]
        result[s]["win_rate"] = round(result[s]["wins"] / n * 100, 1) if n else 0
    return dict(sorted(result.items(), key=lambda x: x[1]["pl"], reverse=True))


def _daily_history(days: int) -> list:
    result = []
    today  = datetime.now(timezone.utc).date()
    for i in range(days - 1, -1, -1):
        d     = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        entry = _data["daily"].get(d, {"trades": 0, "wins": 0, "losses": 0, "pl": 0.0})
        result.append({"date": d, **entry})
    return result
