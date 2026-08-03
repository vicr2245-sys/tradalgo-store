"""
Backtesting Engine
Simulates the bot's strategies on historical candle data.
Outputs trade log, equity curve, and performance metrics.
"""

import json
import logging
from datetime import datetime
from pathlib  import Path

from utils.oanda_client import OandaClient
from strategies.signals import run_all_strategies, consensus_signal
from config import (
    INSTRUMENTS, BACKTEST_GRANULARITY, BACKTEST_CANDLES,
    STRATEGY_WEIGHTS, RISK_PER_TRADE_PCT, DEFAULT_SL_PIPS, DEFAULT_TP_PIPS
)

log = logging.getLogger(__name__)


def pip_size(instrument: str) -> float:
    if "JPY" in instrument: return 0.01
    if "XAU" in instrument: return 0.1
    return 0.0001


def run_backtest(
    instruments: list = None,
    granularity: str  = None,
    candle_count: int = None,
    initial_balance: float = 10_000.0,
) -> dict:

    instruments  = instruments  or INSTRUMENTS
    granularity  = granularity  or BACKTEST_GRANULARITY
    candle_count = candle_count or BACKTEST_CANDLES

    client  = OandaClient()
    results = {}
    all_trades = []

    for instrument in instruments:
        log.info(f"Backtesting {instrument} on {candle_count} × {granularity} candles…")
        try:
            candles = client.get_candles(instrument, granularity, candle_count)
        except Exception as e:
            log.error(f"Failed to fetch candles for {instrument}: {e}")
            continue

        if len(candles) < 60:
            log.warning(f"Not enough candles for {instrument}")
            continue

        trades      = []
        balance     = initial_balance
        equity_curve = [balance]
        pip         = pip_size(instrument)

        # Walk forward from candle 60 onward (enough for indicators)
        for i in range(60, len(candles)):
            window  = candles[:i]
            current = candles[i]

            strategy_results = run_all_strategies(window, instrument)
            consensus        = consensus_signal(strategy_results, STRATEGY_WEIGHTS, threshold=0.35)
            signal           = consensus["signal"]

            if not signal:
                equity_curve.append(balance)
                continue

            sl_pips = consensus.get("sl_pips", DEFAULT_SL_PIPS)
            tp_pips = consensus.get("tp_pips", DEFAULT_TP_PIPS)
            entry   = current["open"]

            if signal == "BUY":
                sl_price = entry - sl_pips * pip
                tp_price = entry + tp_pips * pip
                # simulate: did candle hit TP or SL first?
                if current["low"] <= sl_price:
                    pl = -sl_pips * pip
                    outcome = "SL"
                elif current["high"] >= tp_price:
                    pl = tp_pips * pip
                    outcome = "TP"
                else:
                    # held open; close at candle close
                    pl = current["close"] - entry
                    outcome = "close"
            else:  # SELL
                sl_price = entry + sl_pips * pip
                tp_price = entry - tp_pips * pip
                if current["high"] >= sl_price:
                    pl = -sl_pips * pip
                    outcome = "SL"
                elif current["low"] <= tp_price:
                    pl = tp_pips * pip
                    outcome = "TP"
                else:
                    pl = entry - current["close"]
                    outcome = "close"

            # Convert P&L to money (simplified: 1 pip = $1 per micro lot)
            units    = int((balance * RISK_PER_TRADE_PCT / 100) / (sl_pips * pip * 1000))
            pl_money = pl * units * 1000
            balance  = max(0, balance + pl_money)

            trade = {
                "instrument": instrument,
                "time":       current["time"],
                "signal":     signal,
                "entry":      round(entry, 5),
                "sl":         round(sl_price, 5),
                "tp":         round(tp_price, 5),
                "outcome":    outcome,
                "pl_pips":    round(pl / pip, 1),
                "pl_money":   round(pl_money, 2),
                "balance":    round(balance, 2),
                "strategies": [r for r in consensus["reasons"]],
            }
            trades.append(trade)
            all_trades.append(trade)
            equity_curve.append(balance)

        results[instrument] = _summarise(instrument, trades, equity_curve, initial_balance)
        log.info(f"  {instrument}: {len(trades)} trades | "
                 f"win%={results[instrument]['win_rate']:.1f}% | "
                 f"net={results[instrument]['net_pl']:+.2f}")

    summary = _overall_summary(all_trades, initial_balance)
    output  = {"summary": summary, "by_instrument": results, "trades": all_trades}

    # Save to disk
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(f"backtest_results/backtest_{ts}.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(output, indent=2))
    log.info(f"Backtest saved to {path}")

    return output


def _summarise(instrument: str, trades: list, equity_curve: list, initial: float) -> dict:
    if not trades:
        return {"instrument": instrument, "trades": 0}

    wins   = [t for t in trades if t["pl_pips"] > 0]
    losses = [t for t in trades if t["pl_pips"] <= 0]
    net_pl = sum(t["pl_money"] for t in trades)

    pls        = [t["pl_pips"] for t in trades]
    avg_win    = sum(t["pl_pips"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss   = sum(t["pl_pips"] for t in losses) / len(losses) if losses else 0
    profit_factor = (abs(avg_win * len(wins)) / abs(avg_loss * len(losses))) if losses else float("inf")

    # max drawdown on equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v) / peak
        if dd > max_dd: max_dd = dd

    return {
        "instrument":     instrument,
        "trades":         len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / len(trades) * 100, 1),
        "net_pl":         round(net_pl, 2),
        "net_pl_pct":     round((net_pl / initial) * 100, 2),
        "avg_win_pips":   round(avg_win,  1),
        "avg_loss_pips":  round(avg_loss, 1),
        "profit_factor":  round(profit_factor, 2),
        "max_drawdown":   round(max_dd * 100, 2),
        "final_balance":  round(equity_curve[-1], 2),
    }


def _overall_summary(trades: list, initial: float) -> dict:
    if not trades:
        return {}
    wins     = [t for t in trades if t["pl_pips"] > 0]
    net_pl   = sum(t["pl_money"] for t in trades)
    return {
        "total_trades": len(trades),
        "win_rate":     round(len(wins) / len(trades) * 100, 1),
        "net_pl":       round(net_pl, 2),
        "net_pl_pct":   round((net_pl / initial) * 100, 2),
        "initial_balance": initial,
        "final_balance":   round(initial + net_pl, 2),
    }
