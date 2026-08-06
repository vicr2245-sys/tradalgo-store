"""
Tradalgo — Main Trading Bot
Latency optimisations:
  - Candles for all 10 pairs fetched in parallel at the top of each bar
  - All prices fetched in a single batch request
  - Account balance fetched once and reused for all position-sizing
  - Persistent HTTP session in OandaClient (TCP keep-alive)
  - Candle cache prevents redundant API calls within the same bar
"""

import time
import logging
import signal
import sys
import os
from concurrent.futures import ThreadPoolExecutor

from utils.oanda_client import OandaClient
from utils.email_alerts  import alert_trade_opened, alert_trade_closed, alert_win, alert_error, alert_session_start, alert_daily_summary
from utils.sessions      import is_trading_session, current_session, session_info, minutes_until_next_session
from utils.trade_tracker  import record_open, get_open, remove, all_open_ids, seed_from_oanda
from utils.performance    import record_close, get_stats, get_today, get_unemailed_days, mark_daily_emailed, seed_from_oanda
from strategies.signals  import run_all_strategies, consensus_signal
from config import (
    INSTRUMENTS, STRATEGY_WEIGHTS, RISK_PER_TRADE_PCT,
    MAX_OPEN_TRADES, LOG_FILE, LOG_LEVEL,
    DEFAULT_SL_PIPS, DEFAULT_TP_PIPS,
)

# ── logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("bot")

# ── shared state ──────────────────────────────────────────────────────────────
client        = OandaClient()
_last_session = None
# Trade IDs are now tracked in utils/trade_tracker.py ledger


def pip_size(instrument):
    if "JPY" in instrument: return 0.01
    if "XAU" in instrument: return 0.1
    return 0.0001


def price_from_pips(instrument, price, pips, direction):
    return round(price + direction * pips * pip_size(instrument), 5)


# ── trade state sync ─────────────────────────────────────────────────────────

def sync_trade_state():
    """
    Detect OANDA-closed trades and send win/loss email alerts.
    Polls every 30s. Uses two detection paths:
      A) Ledger diff  — trades we opened this session that disappeared from OANDA
      B) Recent txns  — catch ANY close even if bot was restarted mid-trade
    """
    try:
        open_trades    = client.get_open_trades()
        oanda_open_ids = {t["id"] for t in open_trades}
        our_ids        = all_open_ids()
        closed_ids     = our_ids - oanda_open_ids

        log.debug(f"sync: oanda_open={len(oanda_open_ids)} ledger={len(our_ids)} closed={len(closed_ids)}")

        # ── Path A: ledger diff ──────────────────────────────────────────────
        for trade_id in closed_ids:
            ledger_entry = get_open(trade_id)
            if not ledger_entry:
                remove(trade_id)
                continue

            instrument = ledger_entry["instrument"]
            direction  = ledger_entry["direction"]
            entry      = float(ledger_entry.get("entry") or 0)
            units      = int(ledger_entry.get("units") or 1)

            try:
                closed   = client.get_trade(trade_id)
                pl       = float(closed.get("realizedPL") or 0)
                close_px = float(closed.get("averageClosePrice") or entry)

                # Determine reason from OANDA state
                if closed.get("takeProfitOrder") and pl > 0:
                    close_reason = "Take Profit ✅"
                elif closed.get("stopLossOrder") and pl < 0:
                    close_reason = "Stop Loss"
                else:
                    close_reason = "Manual Close"

                units_abs = abs(units)
                pos_val_usd = (units_abs if instrument.startswith("USD_") else units_abs * 1.08) if "JPY" in instrument else (entry * units_abs)
                pl_pct = round((pl / max(pos_val_usd, 1.0)) * 100, 4)

                log.info(f"{'🏆 WIN' if pl>0 else '❌ LOSS'}  {instrument} "
                         f"| P&L={pl:+.2f} | {close_reason} | trade_id={trade_id}")

                # Record in performance tracker
                record_close(
                    trade_id   = trade_id,
                    instrument = instrument,
                    direction  = direction,
                    entry      = entry,
                    exit_price = close_px,
                    pl         = pl,
                    pl_pct     = pl_pct,
                    reason     = close_reason,
                    strategy   = ledger_entry.get("strategy", ""),
                    opened_at  = ledger_entry.get("opened_at", ""),
                )

                alert_trade_closed(
                    instrument=instrument, direction=direction,
                    entry=f"{entry:.5f}", exit_price=f"{close_px:.5f}",
                    pl=pl, pl_pct=pl_pct, reason=close_reason,
                )

                if pl > 0:
                    alert_win(
                        instrument=instrument, direction=direction,
                        entry=f"{entry:.5f}", exit_price=f"{close_px:.5f}",
                        pl=pl, pl_pct=pl_pct,
                        strategy=ledger_entry.get("strategy", ""),
                    )

            except Exception as e:
                log.error(f"sync path-A error for trade {trade_id}: {e}")
                # Fire fallback email so nothing is ever silent
                try:
                    alert_trade_closed(
                        instrument=instrument, direction=direction,
                        entry=f"{entry:.5f}", exit_price="?",
                        pl=0, pl_pct=0, reason="Closed (could not fetch details)",
                    )
                except Exception:
                    pass
            finally:
                remove(trade_id)

        # ── Path B: recent transactions sweep ────────────────────────────────
        # Catches closes that happened before the bot started or ledger was empty.
        # We track which transaction IDs we've already processed in a small set.
        _check_recent_transactions(oanda_open_ids)

    except Exception as e:
        log.error(f"sync_trade_state error: {e}")


# Tracks txn IDs already emailed so we don't double-fire
_seen_txn_ids: set = set()

def _check_recent_transactions(current_open_ids: set):
    """
    Scans the last 30 OANDA transactions for ORDER_FILL closes
    that we haven't processed yet. Fires emails for any found.
    """
    global _seen_txn_ids
    try:
        txns = client.get_transactions(count=30)
        for txn in txns:
            txn_id = str(txn.get("id", ""))
            if not txn_id or txn_id in _seen_txn_ids:
                continue

            txn_type = txn.get("type", "")
            # We want ORDER_FILL events that closed a trade
            # (tradesClosed or tradeReduced present, and not in our open set)
            trades_closed = txn.get("tradesClosed", [])
            trade_reduced = txn.get("tradeReduced")

            closed_items = trades_closed[:]
            if trade_reduced:
                closed_items.append(trade_reduced)

            for item in closed_items:
                item_tid = str(item.get("tradeID", ""))
                if item_tid in current_open_ids:
                    continue  # still open, skip
                if item_tid in _seen_txn_ids:
                    continue

                pl       = float(item.get("realizedPL") or txn.get("pl") or 0)
                inst     = txn.get("instrument", "?")
                close_px = float(txn.get("price") or 0)

                # Get entry from our ledger if available, else from txn
                ledger_entry = get_open(item_tid)
                entry     = float(ledger_entry.get("entry") or txn.get("price") or close_px)
                direction = ledger_entry.get("direction") or ("BUY" if pl > 0 else "SELL")
                units     = int(ledger_entry.get("units") or abs(int(item.get("units") or 1)))
                units_abs = abs(units)
                pos_val_usd = (units_abs if inst.startswith("USD_") else units_abs * 1.08) if "JPY" in inst else (entry * units_abs)
                pl_pct    = round((pl / max(pos_val_usd, 1.0)) * 100, 4)

                if close_px and item_tid not in _seen_txn_ids:
                    log.info(f"Path-B close detected: {inst} P&L={pl:+.2f} txn={txn_id}")

                    alert_trade_closed(
                        instrument=inst, direction=direction,
                        entry=f"{entry:.5f}", exit_price=f"{close_px:.5f}",
                        pl=pl, pl_pct=pl_pct, reason="Closed (transaction sweep)",
                    )
                    if pl > 0:
                        alert_win(
                            instrument=inst, direction=direction,
                            entry=f"{entry:.5f}", exit_price=f"{close_px:.5f}",
                            pl=pl, pl_pct=pl_pct,
                            strategy=ledger_entry.get("strategy", "auto-detected"),
                        )

                    # Record in performance tracker
                    record_close(
                        trade_id   = item_tid,
                        instrument = inst,
                        direction  = direction,
                        entry      = entry,
                        exit_price = close_px,
                        pl         = pl,
                        pl_pct     = pl_pct,
                        reason     = "Closed (transaction sweep)",
                        strategy   = ledger_entry.get("strategy", "auto-detected"),
                        opened_at  = ledger_entry.get("opened_at", ""),
                    )

                    _seen_txn_ids.add(item_tid)
                    if ledger_entry:
                        remove(item_tid)

            _seen_txn_ids.add(txn_id)

        # Keep set bounded
        if len(_seen_txn_ids) > 500:
            _seen_txn_ids = set(list(_seen_txn_ids)[-250:])

    except Exception as e:
        log.debug(f"_check_recent_transactions error: {e}")


# ── main trading cycle ────────────────────────────────────────────────────────

def run_trading_cycle():
    global _last_session

    info    = session_info()
    session = info["session"]

    if session != _last_session:
        if info["trading_active"]:
            alert_session_start(session, len(INSTRUMENTS))
            log.info(f"▶  Session started: {session}")
        _last_session = session

    if not info["trading_active"]:
        wait = minutes_until_next_session()
        log.info(f"⏸  Off-hours ({info['utc_time']} UTC) — London opens in ~{wait} min")
        return

    # ── Pre-fetch everything in bulk before the strategy loop ─────────────────
    t0 = time.time()

    try:
        open_trades = client.get_open_trades()
    except Exception as e:
        log.error(f"Could not fetch open trades: {e}")
        return

    if len(open_trades) >= MAX_OPEN_TRADES:
        log.info(f"Max open trades ({MAX_OPEN_TRADES}) reached — holding.")
        return

    already_open = {t["instrument"] for t in open_trades}
    candidates   = [i for i in INSTRUMENTS if i not in already_open]

    if not candidates:
        return

    # 1. Fetch candles for all candidates in parallel (biggest latency saving)
    client.invalidate_cache()   # new bar → clear stale cache
    all_candles = client.get_all_candles_parallel(candidates, granularity="H1", count=100)

    # 2. Fetch all prices in a single request
    all_prices = client.get_prices(candidates)

    # 3. Fetch balance once — reuse for all position sizing
    balance = client.get_balance()

    log.info(f"Pre-fetch done in {time.time()-t0:.2f}s — analysing {len(candidates)} pairs…")

    # ── Strategy loop ─────────────────────────────────────────────────────────
    slots   = MAX_OPEN_TRADES - len(open_trades)
    traded  = 0

    for instrument in candidates:
        if traded >= slots:
            break

        candles = all_candles.get(instrument, [])
        if len(candles) < 60:
            continue

        try:
            strat_results = run_all_strategies(candles, instrument)
            consensus     = consensus_signal(strat_results, STRATEGY_WEIGHTS, threshold=0.35)
            signal        = consensus["signal"]

            if not signal:
                continue

            sl_pips = consensus.get("sl_pips", DEFAULT_SL_PIPS)
            tp_pips = consensus.get("tp_pips", DEFAULT_TP_PIPS)
            units   = client.calculate_units(instrument, sl_pips, RISK_PER_TRADE_PCT, balance=balance)
            if signal == "SELL":
                units = -units

            price_data  = all_prices.get(instrument, {})
            entry_price = price_data.get("ask" if signal == "BUY" else "bid", 0)
            if not entry_price:
                continue

            direction = +1 if signal == "BUY" else -1
            sl_price  = price_from_pips(instrument, entry_price, sl_pips, -direction)
            tp_price  = price_from_pips(instrument, entry_price, tp_pips,  direction)

            reason_summary = " | ".join(consensus["reasons"][:2]) or "multi-strategy consensus"

            order_result = client.place_market_order(
                instrument=instrument, units=units,
                stop_loss_price=sl_price, take_profit_price=tp_price,
                client_comment=reason_summary,
            )

            trade_id = (
                order_result.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
                or order_result.get("orderFillTransaction", {}).get("id")
            )
            if not trade_id:
                log.warning(f"Order placed but could not extract trade_id: {order_result}")
                continue

            # 1. Record in ledger FIRST (before email — so sync never misses it)
            record_open(
                trade_id   = str(trade_id),
                instrument = instrument,
                direction  = signal,
                entry      = entry_price,
                units      = abs(units),
                sl         = sl_price,
                tp         = tp_price,
                strategy   = reason_summary,
            )

            def _fmt(price: float, inst: str) -> str:
                if "JPY" in inst: return f"{price:.3f}"
                if "XAU" in inst: return f"{price:.2f}"
                return f"{price:.5f}"

            # 2. Send opened email
            alert_trade_opened(
                instrument=instrument, direction=signal, units=abs(units),
                entry=_fmt(entry_price, instrument), sl=_fmt(sl_price, instrument), tp=_fmt(tp_price, instrument),
                strategy=reason_summary,
            )

            log.info(
                f"✅ {signal} {instrument} | {abs(units):,} units | "
                f"entry={_fmt(entry_price, instrument)} SL={_fmt(sl_price, instrument)} TP={_fmt(tp_price, instrument)} | "
                f"score={consensus['score']}"
            )
            traded += 1

        except Exception as e:
            log.error(f"Error on {instrument}: {e}")
            alert_error(f"{instrument}: {e}")


# ── graceful shutdown ─────────────────────────────────────────────────────────

def _shutdown(sig, frame):
    log.info("Shutdown signal — stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── startup banner ────────────────────────────────────────────────────────────

def _print_banner(acct):
    bal = float(acct["balance"])
    print()
    print("  ████████╗██████╗  █████╗ ██████╗  █████╗ ██╗      ██████╗  ██████╗ ")
    print("     ██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗██║     ██╔════╝ ██╔═══██╗")
    print("     ██║   ██████╔╝███████║██║  ██║███████║██║     ██║  ███╗██║   ██║")
    print("     ██║   ██╔══██╗██╔══██║██║  ██║██╔══██║██║     ██║   ██║██║   ██║")
    print("     ██║   ██║  ██║██║  ██║██████╔╝██║  ██║███████╗╚██████╔╝╚██████╔╝")
    print("     ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝ ")
    print()
    print(f"  Account  : {acct['id']}")
    print(f"  Balance  : ${bal:,.2f}  ({acct.get('currency','USD')})")
    print(f"  Pairs    : {', '.join(INSTRUMENTS)}")
    print(f"  Risk/trade: {RISK_PER_TRADE_PCT}%  |  Max trades: {MAX_OPEN_TRADES}")
    print(f"  Sessions : London 07:00–16:00 UTC  |  New York 12:00–21:00 UTC")
    print(f"  Dashboard: http://localhost:5000")
    print()


# ── main loop ─────────────────────────────────────────────────────────────────

def main():
    try:
        acct = client.get_account()
        _print_banner(acct)
    except Exception as e:
        print(f"\n  ❌  Cannot connect to OANDA: {e}")
        print("  Check OANDA_API_KEY and OANDA_ACCOUNT_ID in config.py\n")
        sys.exit(1)

    log.info("Bot started. Waiting for trading session…")

    BAR_INTERVAL  = 60 * 60   # 1 hour
    POLL_INTERVAL = 30        # check for closed trades every 30s

    now_init          = time.time()
    last_bar          = now_init - (now_init % BAR_INTERVAL)
    last_poll         = 0
    last_daily_check  = 0

    while True:
        now = time.time()

        if now - last_poll >= POLL_INTERVAL:
            sync_trade_state()
            last_poll = now

        if now - last_bar >= BAR_INTERVAL:
            run_trading_cycle()
            last_bar = now

        # Daily summary email — check once per hour if yesterday needs emailing
        if now - last_daily_check >= 3600:
            _send_pending_daily_summaries()
            last_daily_check = now

        time.sleep(10)


if __name__ == "__main__":
    main()


def _send_pending_daily_summaries():
    """
    Checks if any completed trading days haven't had a summary email sent yet.
    Fires the summary for each pending day. Called hourly from the main loop.
    """
    try:
        pending = get_unemailed_days()
        for date_str in pending:
            log.info(f"Sending daily summary for {date_str}…")
            stats = get_stats()          # all-time stats for context
            try:
                balance = client.get_balance()
            except Exception:
                balance = 0.0
            # Override today bucket with the specific date
            day_data = get_stats(days=1)
            stats["today"] = day_data.get("today", {})
            alert_daily_summary(date_str, stats, balance)
            mark_daily_emailed(date_str)
            log.info(f"Daily summary sent for {date_str}")
    except Exception as e:
        log.error(f"Daily summary error: {e}")
