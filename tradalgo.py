#!/usr/bin/env python3
"""
████████╗██████╗  █████╗ ██████╗  █████╗ ██╗      ██████╗  ██████╗
   ██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗██║     ██╔════╝ ██╔═══██╗
   ██║   ██████╔╝███████║██║  ██║███████║██║     ██║  ███╗██║   ██║
   ██║   ██╔══██╗██╔══██║██║  ██║██╔══██║██║     ██║   ██║██║   ██║
   ██║   ██║  ██║██║  ██║██████╔╝██║  ██║███████╗╚██████╔╝╚██████╔╝
   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝

Single-file forex trading bot.
Usage:
  tradalgo.exe            → launches bot + dashboard together
  tradalgo.exe --bot      → bot only (no dashboard)
  tradalgo.exe --dash     → dashboard only
  tradalgo.exe --backtest → run backtest and exit
  tradalgo.exe --email    → test email config and exit
  tradalgo.exe --setup    → first-time setup wizard

All config is stored in tradalgo_config.json next to the exe.
All data  is stored in tradalgo_data/  next to the exe.
"""

# ══════════════════════════════════════════════════════════════════════════════
# STDLIB
# ══════════════════════════════════════════════════════════════════════════════
import argparse, copy, json, logging, logging.handlers, os, queue, secrets, signal, smtplib, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime           import datetime, timezone, timedelta, date as _date
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from pathlib              import Path

# ══════════════════════════════════════════════════════════════════════════════
# THIRD-PARTY  (pip install requests flask numpy)
# ══════════════════════════════════════════════════════════════════════════════
try:
    import numpy as np
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util import Retry
    from flask import Flask, Response, jsonify, request as freq, stream_with_context
except ImportError as _e:
    print(f"\n  Missing dependency: {_e}")
    print("  Run:  pip install requests flask numpy\n")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 1: CONFIG ────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# Config and data live next to the exe (or script when running from source)
_BASE_DIR   = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_FILE = _BASE_DIR / "tradalgo_config.json"
DATA_DIR    = _BASE_DIR / "tradalgo_data"
LOG_FILE    = DATA_DIR  / "bot.log"
LEDGER_FILE = DATA_DIR  / "trade_ledger.json"
PERF_FILE   = DATA_DIR  / "performance.json"

DATA_DIR.mkdir(exist_ok=True)

def _atomic_write_json(path: Path, data) -> None:
    """
    Write JSON to `path` without ever leaving it half-written.

    A plain `path.write_text(...)` truncates the file first, then streams
    the new content in — if the process dies (crash, power loss, forced
    kill) between those two steps, the file is left as invalid/truncated
    JSON and the next load silently falls back to empty defaults, losing
    the trade ledger / performance history / config.

    Instead we write the new content to a temp file in the same directory,
    then use os.replace() to swap it into place. os.replace() is atomic on
    both POSIX and Windows — the target path always resolves to either the
    fully-old or fully-new content, never a partial file.
    """
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try: tmp_path.unlink()
            except Exception: pass

_cfg_lock = threading.RLock()

_DEFAULT_CONFIG = {
    "OANDA_ACCOUNT_ID": "",
    "OANDA_API_KEY":    "",
    "OANDA_ENV":        "practice",
    "EMAIL_ENABLED":    True,
    "EMAIL_SENDER":     "",
    "EMAIL_PASSWORD":   "",
    "EMAIL_RECIPIENT":  "",
    "SMTP_HOST":        "smtp.gmail.com",
    "SMTP_PORT":        587,
    "INSTRUMENTS": [
        "EUR_USD","GBP_USD","USD_JPY","USD_CHF","AUD_USD",
        "USD_CAD","NZD_USD","EUR_GBP","EUR_JPY","XAU_USD"
    ],
    "LONDON_OPEN":  [7,  0],
    "LONDON_CLOSE": [16, 0],
    "NY_OPEN":      [12, 0],
    "NY_CLOSE":     [21, 0],
    "RISK_PER_TRADE_PCT":  1.0,
    "MAX_OPEN_TRADES":     5,
    "DEFAULT_SL_PIPS":     20,
    "DEFAULT_TP_PIPS":     50,
    "TRAILING_STOP_PIPS":  0,
    "GOLD_SL_PIPS":        200,
    "GOLD_TP_PIPS":        400,
    "STRATEGY_WEIGHTS": {
        "EMA_Cross":       0.25,
        "RSI_Reversal":    0.20,
        "Bollinger_Break": 0.20,
        "MACD_Momentum":   0.20,
        "Session_Break":   0.15
    },
    "BACKTEST_GRANULARITY": "H1",
    "BACKTEST_CANDLES":     5000,
    "BACKTEST_SPREAD_ENABLED": True,
    "LOG_LEVEL":            "INFO",
    "DASHBOARD_PORT":       5000,
    "LIVE_TRADING_ENABLED": True,
    "MAX_SLIPPAGE_PIPS":    2.0,
    "RISK_GUARD_ENABLED":   True,
    "RISK_GUARD_MAX_DD_PCT": 10.0,
    "ONBOARDING_DONE":      False,
    "LICENCE_KEY":          "UNRESTRICTED",
    "LICENCE_STATUS":       "active",
    "LICENCE_EMAIL":        "",
    "FIRST_RUN_SHOWN":      False,
    "NEWS_FILTER_ENABLED":  True,
    "NEWS_BLOCK_MINUTES":   45,
    "AI_BIAS_ENABLED":      False,
    "AI_BIAS_API_KEY":      "",
    "AI_BIAS_LAST_RUN":     "",
    "AI_BIAS_DATA":         {},
}

# ── Secrets ───────────────────────────────────────────────────────────────
_SECRET_ENV_VARS = {
    "OANDA_ACCOUNT_ID": "TRADALGO_OANDA_ACCOUNT_ID",
    "OANDA_API_KEY":    "TRADALGO_OANDA_API_KEY",
    "EMAIL_SENDER":     "TRADALGO_EMAIL_SENDER",
    "EMAIL_PASSWORD":   "TRADALGO_EMAIL_PASSWORD",
    "EMAIL_RECIPIENT":  "TRADALGO_EMAIL_RECIPIENT",
    "AI_BIAS_API_KEY":  "TRADALGO_AI_BIAS_API_KEY",
}
_env_sourced_keys: set = set()

def _apply_env_overrides(cfg: dict) -> dict:
    global _env_sourced_keys
    _env_sourced_keys = set()
    for cfg_key, env_name in _SECRET_ENV_VARS.items():
        val = os.environ.get(env_name)
        if val and not cfg.get(cfg_key):
            cfg[cfg_key] = val
            _env_sourced_keys.add(cfg_key)
    if "AI_BIAS_API_KEY" not in _env_sourced_keys and not cfg.get("AI_BIAS_API_KEY") and os.environ.get("ANTHROPIC_API_KEY"):
        cfg["AI_BIAS_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        _env_sourced_keys.add("AI_BIAS_API_KEY")
    return cfg

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = {**_DEFAULT_CONFIG, **saved}
            return _apply_env_overrides(cfg)
        except Exception as e:
            log.error(f"Failed to parse {CONFIG_FILE}: {e}")
            try:
                bak = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.bak")
                CONFIG_FILE.rename(bak)
                log.warning(f"Backed up corrupted config to {bak}")
            except Exception: pass
    return _apply_env_overrides(dict(_DEFAULT_CONFIG))

def _save_config(cfg: dict):
    with _cfg_lock:
        cleaned = dict(cfg)
        for k in ("OANDA_API_KEY", "OANDA_ACCOUNT_ID", "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECIPIENT", "AI_BIAS_API_KEY"):
            if k in cleaned and isinstance(cleaned[k], str):
                cleaned[k] = cleaned[k].strip().strip("\"'").strip()
        if "OANDA_ENV" in cleaned and isinstance(cleaned.get("OANDA_ENV"), str):
            cleaned["OANDA_ENV"] = cleaned["OANDA_ENV"].strip().lower()

        full = {**_DEFAULT_CONFIG, **CFG, **cleaned}
        for key in cleaned:
            _env_sourced_keys.discard(key)
        _atomic_write_json(CONFIG_FILE, full)
        CFG.update(full)

CFG = _load_config()

def _oanda_api_url():
    env = str(CFG.get("OANDA_ENV", "practice")).strip().lower()
    return ("https://api-fxpractice.oanda.com" if ("practice" in env or "demo" in env)
            else "https://api-fxtrade.oanda.com")

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 1b: LICENCE SYSTEM ───────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
"""
Licence validation flow:
1. On startup, check CFG["LICENCE_KEY"]
2. If empty or status == "unlicensed", show activation screen instead of bot
3. Validate key against licence server (simple HTTPS endpoint you control)
4. On success, store status="active" + email in config
5. Re-validate once per day silently in background

Licence server endpoint (you host this — a simple Flask app or Gumroad webhook):
  POST https://your-server.com/api/validate
  Body: {"key": "XXXX-XXXX-XXXX-XXXX", "machine_id": "hash"}
  Response: {"valid": true, "email": "buyer@email.com", "plan": "lifetime"}

For development/testing, LICENCE_SERVER can be set to "bypass" to skip validation.
"""

LICENCE_SERVER = "https://licence.tradalgo.com/api/validate"
_LICENCE_CACHE_TTL = 86400  # re-validate once per 24h


def _machine_id() -> str:
    """Stable unique identifier for this machine — used to bind licence to device."""
    import hashlib, platform, uuid
    raw = platform.node() + str(uuid.getnode()) + platform.system()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def validate_licence(key: str, force: bool = False) -> dict:
    return {"valid": True, "email": "unrestricted@tradalgo.com", "plan": "lifetime"}


def is_licenced() -> bool:
    return True


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 2: LOGGING ───────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, CFG.get("LOG_LEVEL", "INFO")),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        # Rotates at 5MB, keeps 5 old copies (bot.log.1 .. bot.log.5) — a bot
        # left running for months would otherwise grow this file forever.
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("tradalgo")

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 3: INDICATORS ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _closes(candles):  return np.array([c["close"] for c in candles], dtype=float)
def _highs(candles):   return np.array([c["high"]  for c in candles], dtype=float)
def _lows(candles):    return np.array([c["low"]   for c in candles], dtype=float)

def _ema(series, period):
    result = np.full_like(series, np.nan)
    k = 2 / (period + 1)
    valid_mask = ~np.isnan(series)
    if not np.any(valid_mask):
        return result
    first_valid = int(np.argmax(valid_mask))
    seed_end = first_valid + period
    if seed_end > len(series):
        return result
    result[seed_end - 1] = np.mean(series[first_valid:seed_end])
    for i in range(seed_end, len(series)):
        if np.isnan(series[i]):
            result[i] = result[i - 1]
        else:
            result[i] = series[i] * k + result[i - 1] * (1 - k)
    return result

def _sma(series, period):
    result = np.full_like(series, np.nan)
    for i in range(period-1, len(series)):
        result[i] = series[i-period+1:i+1].mean()
    return result

def _rsi(series, period=14):
    deltas = np.diff(series)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.full(len(series), np.nan)
    avg_l  = np.full(len(series), np.nan)
    avg_g[period] = gains[:period].mean()
    avg_l[period] = losses[:period].mean()
    for i in range(period+1, len(series)):
        avg_g[i] = (avg_g[i-1] * (period-1) + gains[i-1])  / period
        avg_l[i] = (avg_l[i-1] * (period-1) + losses[i-1]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_l == 0, np.inf, avg_g / avg_l)
    return np.where(avg_l == 0, 100, 100 - (100 / (1+rs)))

def _macd(series, fast=12, slow=26, signal=9):
    ml = _ema(series, fast) - _ema(series, slow)
    sl = _ema(ml, signal)
    return ml, sl, ml - sl

def _bollinger(series, period=20, std=2.0):
    mid = _sma(series, period)
    s   = np.full_like(series, np.nan)
    for i in range(period-1, len(series)):
        s[i] = series[i-period+1:i+1].std()
    return mid + std*s, mid, mid - std*s

def _atr(candles, period=14):
    h, l, c = _highs(candles), _lows(candles), _closes(candles)
    tr = np.zeros(len(candles))
    for i in range(1, len(candles)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    r = np.full(len(candles), np.nan)
    r[period] = tr[1:period+1].mean()
    for i in range(period+1, len(candles)):
        r[i] = (r[i-1]*(period-1) + tr[i]) / period
    return r

def _nan(v): return v is None or (isinstance(v, float) and np.isnan(v))

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 4: SESSIONS ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _utc_now(): return datetime.now(timezone.utc)

def current_session():
    now  = _utc_now()
    h, m = now.hour, now.minute
    lo, lc = tuple(CFG["LONDON_OPEN"]), tuple(CFG["LONDON_CLOSE"])
    no, nc = tuple(CFG["NY_OPEN"]),     tuple(CFG["NY_CLOSE"])
    in_l = lo <= (h,m) < lc
    in_n = no <= (h,m) < nc
    if in_l and in_n: return "London/NY Overlap"
    if in_l: return "London"
    if in_n: return "New York"
    return None

def is_trading_session(): return current_session() is not None

def session_info():
    now = _utc_now()
    s   = current_session()
    return {"utc_time": now.strftime("%H:%M:%S"), "session": s or "Off-hours",
            "trading_active": s is not None}

def minutes_until_next_session():
    now  = _utc_now()
    h, m = now.hour, now.minute
    if is_trading_session(): return 0
    lo_h, lo_m = CFG["LONDON_OPEN"]
    cur = h*60+m; lon = lo_h*60+lo_m
    return (lon-cur) if cur < lon else (1440-cur+lon)

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 5: TRADE TRACKER ─────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_ledger: dict = {}
# The bot loop thread mutates _ledger (record_open/remove_ledger/seed) while
# Flask request threads read it concurrently (route_candles, /api/why-no-trades,
# etc, since app.run(threaded=True)). Verified by testing: the old _ledger_save()
# called json.dumps(_ledger) directly on the live dict, and json.dumps()
# genuinely raises "RuntimeError: dictionary changed size during iteration"
# under concurrent mutation — reproduced reliably under load. The lock now
# guards a dict(_ledger) snapshot so each save serializes a frozen copy
# instead of a dict that can change mid-serialize, and also makes each
# mutate-then-save sequence atomic with respect to other mutations.
_ledger_lock = threading.RLock()

def _ledger_save():
    try:
        with _ledger_lock:
            snapshot = dict(_ledger)
        _atomic_write_json(LEDGER_FILE, snapshot)
    except Exception as e: log.error(f"Ledger save: {e}")

def _ledger_load():
    global _ledger
    if LEDGER_FILE.exists():
        try:
            loaded = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _ledger = loaded
        except Exception as e:
            log.warning(f"Ledger load failed: {e}")
            _ledger = {}

_ledger_load()

def record_open(trade_id, instrument, direction, entry, units, sl, tp, strategy):
    with _ledger_lock:
        _ledger[str(trade_id)] = {
            "instrument": instrument, "direction": direction,
            "entry": entry, "units": abs(units), "sl": sl, "tp": tp,
            "strategy": strategy, "opened_at": _utc_now().isoformat(),
        }
    _ledger_save()

def seed_from_oanda(open_trades):
    added = 0
    with _ledger_lock:
        for t in open_trades:
            tid = str(t["id"])
            if tid not in _ledger:
                _ledger[tid] = {
                    "instrument": t.get("instrument","?"),
                    "direction":  "BUY" if int(t.get("currentUnits",1)) > 0 else "SELL",
                    "entry":  float(t.get("price",0)), "units": abs(int(t.get("currentUnits",0))),
                    "sl": 0, "tp": 0, "strategy": "pre-existing", "opened_at": t.get("openTime",""),
                }
                added += 1
    if added: _ledger_save()

def get_ledger(trade_id):
    with _ledger_lock:
        return _ledger.get(str(trade_id), {})

def remove_ledger(trade_id):
    with _ledger_lock:
        _ledger.pop(str(trade_id), None)
    _ledger_save()

def all_ledger_ids():
    with _ledger_lock:
        return set(_ledger.keys())

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 6: PERFORMANCE TRACKER ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_perf: dict = {"trades": [], "daily": {}, "weekly": {}, "starting_balance": None, "start_date": None}
# Unlike _ledger, every _perf mutation today runs sequentially on the single
# bot thread (sync_trades / send_pending_summaries / run_bot) — there's no
# live cross-thread race to fix. This lock is precautionary: it protects the
# compound read-modify-write in record_close() (check-then-increment on
# _perf["daily"][today]) in case a future feature (e.g. a manual "close
# trade" API endpoint) ever adds a second writer.
_perf_lock = threading.RLock()

def _perf_save():
    try:
        with _perf_lock:
            snapshot = copy.deepcopy(_perf)
        _atomic_write_json(PERF_FILE, snapshot)
    except Exception as e: log.error(f"Perf save: {e}")


# ── Activity feed ─────────────────────────────────────────────────────────────
_feed: list = []   # in-memory ring buffer, max 50 events
_FEED_MAX   = 50
_feed_lock  = threading.RLock()

_FRIENDLY_NAMES = {
    "EUR_USD": "Euro / Dollar",   "GBP_USD": "Pound / Dollar",
    "USD_JPY": "Dollar / Yen",    "USD_CHF": "Dollar / Franc",
    "AUD_USD": "Aussie / Dollar", "USD_CAD": "Dollar / CAD",
    "NZD_USD": "Kiwi / Dollar",   "EUR_GBP": "Euro / Pound",
    "EUR_JPY": "Euro / Yen",      "XAU_USD": "Gold",
}

def _friendly(instrument: str) -> str:
    return _FRIENDLY_NAMES.get(instrument, instrument.replace("_", "/"))

def feed_push(event_type: str, data: dict):
    """Push an event to the activity feed. Types: open, close_win, close_loss, session, info"""
    with _feed_lock:
        _feed.insert(0, {
            "type":  event_type,
            "data":  data,
            "ts":    _utc_now().isoformat(),
            "ts_ms": int(_utc_now().timestamp() * 1000),
        })
        if len(_feed) > _FEED_MAX:
            _feed.pop()

def feed_open(instrument, direction, entry, sl, tp, strategy):
    dir_word = "Bought" if direction == "BUY" else "Sold"
    feed_push("open", {
        "title":    f"{dir_word} {_friendly(instrument)}",
        "body":     f"The bot opened a {'buy' if direction=='BUY' else 'sell'} trade. "
                    f"It will close automatically if the safety price (${sl}) is hit, "
                    f"or take profit at ${tp}.",
        "instrument": instrument,
        "direction":  direction,
        "entry":      entry,
    })

def feed_close(instrument, direction, pl, reason):
    won      = pl > 0
    dir_word = "Bought" if direction == "BUY" else "Sold"
    if won:
        body = f"{dir_word} and the target price was hit. The bot made a profit of ${abs(pl):.2f} on this trade."
    else:
        body = f"{dir_word} and the safety price was hit. The bot cut the loss at ${abs(pl):.2f} to protect your account."
    feed_push("close_win" if won else "close_loss", {
        "title":      f"{_friendly(instrument)} trade closed · {'Won' if won else 'Lost'} ${abs(pl):.2f}",
        "body":       body,
        "instrument": instrument,
        "pl":         pl,
    })

def feed_session(session_name: str, active: bool):
    if active:
        feed_push("session", {
            "title": f"{session_name} session open",
            "body":  "The bot is now watching the markets and will trade when it spots opportunities.",
        })
    else:
        feed_push("info", {
            "title": "Markets closed for now",
            "body":  "No trading outside London and New York hours. The bot will resume when the next session opens.",
        })

def _perf_load():
    global _perf
    if PERF_FILE.exists():
        try:
            loaded = json.loads(PERF_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _perf.update(loaded)
        except Exception as e:
            log.warning(f"Perf load failed ({e}), keeping default perf dict")

_perf_load()

def record_close(trade_id, instrument, direction, entry, exit_price,
                 pl, pl_pct, reason, strategy, opened_at=""):
    now   = _utc_now()
    today = now.strftime("%Y-%m-%d")
    with _perf_lock:
        _perf["trades"].append({
            "id": trade_id, "instrument": instrument, "direction": direction,
            "entry": round(float(entry),5), "exit": round(float(exit_price),5),
            "pl": round(float(pl),2), "pl_pct": round(float(pl_pct),4),
            "reason": reason, "strategy": strategy,
            "opened_at": opened_at, "closed_at": now.isoformat(),
        })
        if today not in _perf["daily"]:
            _perf["daily"][today] = {"trades":0,"wins":0,"losses":0,"pl":0.0,"emailed":False}
        _perf["daily"][today]["trades"] += 1
        _perf["daily"][today]["pl"]     = round(_perf["daily"][today]["pl"] + float(pl), 2)
        if float(pl) > 0: _perf["daily"][today]["wins"]   += 1
        else:             _perf["daily"][today]["losses"]  += 1
    _perf_save()

def _compute_risk_ratios(days: int = None) -> dict:
    """
    Sharpe and Sortino ratios computed from daily P&L as a percentage of
    the account's starting balance — a standard simplification for a
    retail dashboard. A fully rigorous version would use daily
    mark-to-market equity as the return denominator rather than the fixed
    starting balance, but we don't currently snapshot equity daily.
    Risk-free rate is treated as 0 (also a common simplification).
    Annualized using 252 trading days/year, matching typical forex/equity
    convention. Non-trading days correctly count as 0% return days —
    _perf["daily"] only has entries for days a trade closed, so this
    reconstructs the full date range and fills the gaps with zero.
    """
    start_date       = _perf.get("start_date")
    starting_balance = _perf.get("starting_balance")
    if not start_date or not starting_balance or starting_balance <= 0:
        return {"sharpe": None, "sortino": None}

    try:
        y, m, d = [int(x) for x in start_date.split("-")]
        start = _date(y, m, d)
    except Exception:
        return {"sharpe": None, "sortino": None}

    end = _utc_now().date()
    if days is not None:
        start = max(start, end - timedelta(days=days))

    n_days = (end - start).days + 1
    if n_days < 2:
        return {"sharpe": None, "sortino": None}

    daily_returns = []
    for i in range(n_days):
        d_key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        pl = _perf["daily"].get(d_key, {}).get("pl", 0.0)
        daily_returns.append(pl / starting_balance)

    arr    = np.array(daily_returns)
    mean_r = arr.mean()
    std_r  = arr.std(ddof=1) if len(arr) > 1 else 0.0

    sharpe = round(float(mean_r / std_r * np.sqrt(252)), 2) if std_r > 0 else None

    downside     = arr[arr < 0]
    downside_dev = float(np.sqrt(np.mean(downside ** 2))) if len(downside) > 0 else 0.0
    sortino = round(float(mean_r / downside_dev * np.sqrt(252)), 2) if downside_dev > 0 else None

    return {"sharpe": sharpe, "sortino": sortino}


def get_stats(days=None):
    trades = _perf["trades"]
    if days is not None:
        cutoff = (_utc_now() - timedelta(days=days)).isoformat()
        trades = [t for t in trades if t.get("closed_at","") >= cutoff]
    if not trades:
        return {"period": f"Last {days} days" if days else "All time",
                "total_trades":0,"wins":0,"losses":0,"win_rate":0,"net_pl":0,
                "avg_win":0,"avg_loss":0,"profit_factor":0,"max_drawdown":0,
                "gross_profit":0,"gross_loss":0,"current_streak":0,
                "best_streak":0,"worst_streak":0,"best_trade":None,
                "worst_trade":None,"by_instrument":{},"by_strategy":{},
                "today":{},"daily_history":_daily_history(14),"trades":[],
                "sharpe":None,"sortino":None}
    wins   = [t for t in trades if t["pl"] > 0]
    losses = [t for t in trades if t["pl"] <= 0]
    pls    = [t["pl"] for t in trades]
    gp     = sum(t["pl"] for t in wins)
    gl     = abs(sum(t["pl"] for t in losses))
    # max drawdown
    run=pk=mdd=0.0
    for p in pls:
        run+=p; pk=max(pk,run); mdd=max(mdd,pk-run)
    # streaks
    streak=best=0; worst_s=0
    for t in sorted(trades, key=lambda x: x.get("closed_at","")):
        streak = max(streak+1,1) if t["pl"]>0 else min(streak-1,-1)
        best=max(best,streak); worst_s=min(worst_s,streak)
    # by instrument
    by_inst={}
    for t in trades:
        i=t["instrument"]
        if i not in by_inst: by_inst[i]={"trades":0,"wins":0,"losses":0,"pl":0.0,"win_rate":0}
        by_inst[i]["trades"]+=1; by_inst[i]["pl"]=round(by_inst[i]["pl"]+t["pl"],2)
        if t["pl"]>0: by_inst[i]["wins"]+=1
        else:         by_inst[i]["losses"]+=1
    for i in by_inst:
        n=by_inst[i]["trades"]
        by_inst[i]["win_rate"]=round(by_inst[i]["wins"]/n*100,1) if n else 0
    by_inst=dict(sorted(by_inst.items(),key=lambda x:x[1]["pl"],reverse=True))
    # by strategy
    by_strat={}
    for t in trades:
        s=(t.get("strategy","Unknown") or "Unknown").split("|")[0].strip()
        if s not in by_strat: by_strat[s]={"trades":0,"wins":0,"pl":0.0,"win_rate":0}
        by_strat[s]["trades"]+=1; by_strat[s]["pl"]=round(by_strat[s]["pl"]+t["pl"],2)
        if t["pl"]>0: by_strat[s]["wins"]+=1
    for s in by_strat:
        n=by_strat[s]["trades"]
        by_strat[s]["win_rate"]=round(by_strat[s]["wins"]/n*100,1) if n else 0
    today_key = _utc_now().strftime("%Y-%m-%d")
    n=len(trades)
    ratios = _compute_risk_ratios(days)
    return {
        "period":         f"Last {days} days" if days else "All time",
        "total_trades":   n,
        "wins":           len(wins), "losses": len(losses),
        "win_rate":       round(len(wins)/n*100,1),
        "net_pl":         round(sum(pls),2),
        "avg_win":        round(gp/len(wins),2)   if wins   else 0,
        "avg_loss":       round(-gl/len(losses),2) if losses else 0,
        "profit_factor":  round(gp/gl,2)           if gl     else 999,
        "max_drawdown":   round(mdd,2),
        "gross_profit":   round(gp,2), "gross_loss": round(gl,2),
        "current_streak": streak, "best_streak": best, "worst_streak": abs(worst_s),
        "best_trade":     max(trades,key=lambda t:t["pl"]),
        "worst_trade":    min(trades,key=lambda t:t["pl"]),
        "by_instrument":  by_inst, "by_strategy": by_strat,
        "today":          _perf["daily"].get(today_key,{}),
        "daily_history":  _daily_history(14),
        "trades":         trades[-50:],
        "sharpe":         ratios["sharpe"],
        "sortino":        ratios["sortino"],
    }

def get_today():
    return _perf["daily"].get(_utc_now().strftime("%Y-%m-%d"),
                              {"trades":0,"wins":0,"losses":0,"pl":0.0})

def get_unemailed_days():
    yesterday = (_utc_now()-timedelta(days=1)).strftime("%Y-%m-%d")
    return sorted(d for d,v in _perf["daily"].items()
                  if d<=yesterday and not v.get("emailed") and v.get("trades",0)>0)

def mark_daily_emailed(date_str):
    if date_str in _perf["daily"]:
        _perf["daily"][date_str]["emailed"] = True
        _perf_save()


def _iso_week_key(date_obj):
    """Returns 'YYYY-Www' e.g. '2026-W24' for a given date."""
    y, w, _ = date_obj.isocalendar()
    return f"{y}-W{w:02d}"


def set_starting_balance(balance: float):
    """Called once on first bot run to record the baseline for growth %."""
    if _perf.get("starting_balance") is None:
        _perf["starting_balance"] = round(float(balance), 2)
        _perf["start_date"] = _utc_now().strftime("%Y-%m-%d")
        _perf_save()
        log.info(f"Starting balance recorded: ${balance:,.2f}")


def check_risk_guard(current_balance: float) -> bool:
    """
    Checks if account has dropped too far from starting balance.
    If so, auto-pauses live trading and sends an alert email.
    Returns True if guard triggered (trading should stop), False if all ok.
    """
    if not CFG.get("RISK_GUARD_ENABLED", True):
        return False

    starting_bal = _perf.get("starting_balance")
    if not starting_bal:
        return False

    max_dd_pct  = CFG.get("RISK_GUARD_MAX_DD_PCT", 10.0)
    drawdown_pct = (starting_bal - current_balance) / starting_bal * 100

    if drawdown_pct >= max_dd_pct:
        # Only trigger once — don't spam if already paused
        if not CFG.get("LIVE_TRADING_ENABLED", True):
            return True  # already paused, nothing to do

        log.warning(f"RISK GUARD TRIGGERED: drawdown {drawdown_pct:.1f}% >= limit {max_dd_pct}%")
        CFG["LIVE_TRADING_ENABLED"] = False
        _save_config(CFG)

        # Push to activity feed
        feed_push("info", {
            "title": "Bot paused — safety limit reached",
            "body":  (f"Your account has dropped {drawdown_pct:.1f}% from its starting balance of "
                      f"${starting_bal:,.2f}. The bot has paused automatically to protect your funds. "
                      f"Review the Performance page and press Resume when you're ready to continue."),
        })

        # Send email alert
        _send_risk_guard_email(starting_bal, current_balance, drawdown_pct, max_dd_pct)
        return True

    return False


def _send_risk_guard_email(starting_bal, current_balance, drawdown_pct, limit_pct):
    loss     = starting_bal - current_balance
    body = f"""<tr><td style='background:#7f1d1d;padding:20px 24px'>
  <h2 style='margin:0;color:#fff;font-size:20px;line-height:1.3'>&#9888; Bot Paused &mdash; Safety Limit Reached</h2>
  <p style='margin:6px 0 0;color:rgba(255,255,255,.7);font-size:13px'>Tradalgo has automatically stopped trading</p>
</td></tr>
<tr><td style='padding:18px 24px;font-size:14px;line-height:1.7;color:#e0e0e0'>
  <p style='margin:0 0 12px'>Your account has dropped <b style='color:#f87171'>{drawdown_pct:.1f}%</b> from
  its starting balance, which reached your safety limit of <b>{limit_pct}%</b>.</p>
  <p style='margin:0;color:#aaa;font-size:13px'>The bot has paused automatically. No new trades will open until you resume.</p>
</td></tr>
<tr><td>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    <tr class="stat-row"><td style='padding:8px 14px;color:#888;font-size:13px'>Starting Balance</td>
        <td style='padding:8px 14px;font-weight:600;font-size:13px;text-align:right'>${starting_bal:,.2f}</td></tr>
    <tr class="stat-row"><td style='padding:8px 14px;color:#888;font-size:13px'>Current Balance</td>
        <td style='padding:8px 14px;font-weight:600;font-size:13px;color:#f87171;text-align:right'>${current_balance:,.2f}</td></tr>
    <tr class="stat-row"><td style='padding:8px 14px;color:#888;font-size:13px'>Loss</td>
        <td style='padding:8px 14px;font-weight:600;font-size:13px;color:#f87171;text-align:right'>-${loss:,.2f} ({drawdown_pct:.1f}%)</td></tr>
    <tr class="stat-row"><td style='padding:8px 14px;color:#888;font-size:13px'>Your Safety Limit</td>
        <td style='padding:8px 14px;font-weight:600;font-size:13px;text-align:right'>{limit_pct}% drawdown</td></tr>
  </table>
</td></tr>
<tr><td style='padding:16px 24px;background:#12122a'>
  <p style='margin:0 0 8px;font-size:13px;color:#aaa'>What to do next:</p>
  <ol style='margin:0;padding-left:18px;font-size:13px;color:#aaa;line-height:1.8'>
    <li>Open the Tradalgo dashboard and review the Performance page</li>
    <li>Check which pairs or strategies caused the losses</li>
    <li>When you are ready, press the <b style='color:#e0e0e0'>Resume</b> button to restart trading</li>
  </ol>
</td></tr>
<tr><td style='padding:12px 24px;background:#0d0d1f;font-size:11px;color:#444;text-align:center'>
  Tradalgo &middot; {CFG["OANDA_ENV"].title()} Account &middot; Risk Guard activated</td></tr>"""

    html = _EMAIL_WRAP_OPEN + body + _EMAIL_WRAP_CLOSE
    _send_email("&#9888; Tradalgo paused — safety limit reached", html)


def get_unemailed_weeks():
    """
    Returns ISO week keys for completed weeks (strictly before the current week)
    that have trades and haven't been emailed yet.
    """
    today_key = _iso_week_key(_utc_now().date())
    weeks = {}
    for t in _perf["trades"]:
        closed = t.get("closed_at", "")[:10]
        if not closed:
            continue
        try:
            y, m, d = [int(x) for x in closed.split("-")]
            wk = _iso_week_key(_date(y, m, d))
        except Exception:
            continue
        if wk == today_key:
            continue  # current week isn't complete yet
        weeks.setdefault(wk, []).append(t)

    out = []
    for wk, trades in weeks.items():
        if not _perf["weekly"].get(wk, {}).get("emailed"):
            out.append(wk)
    return sorted(out)


def mark_week_emailed(week_key: str):
    if week_key not in _perf["weekly"]:
        _perf["weekly"][week_key] = {}
    _perf["weekly"][week_key]["emailed"] = True
    _perf_save()


def get_week_stats(week_key: str):
    """Aggregate stats for a single ISO week (e.g. '2026-W24')."""
    trades = []
    for t in _perf["trades"]:
        closed = t.get("closed_at", "")[:10]
        if not closed:
            continue
        try:
            y, m, d = [int(x) for x in closed.split("-")]
            if _iso_week_key(_date(y, m, d)) == week_key:
                trades.append(t)
        except Exception:
            continue

    if not trades:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "pl": 0.0,
                "best_trade": None, "worst_trade": None, "by_instrument": {}}

    wins   = [t for t in trades if t["pl"] > 0]
    losses = [t for t in trades if t["pl"] <= 0]
    pl     = sum(t["pl"] for t in trades)

    by_inst = {}
    for t in trades:
        i = t["instrument"]
        if i not in by_inst:
            by_inst[i] = {"trades": 0, "wins": 0, "pl": 0.0}
        by_inst[i]["trades"] += 1
        by_inst[i]["pl"] = round(by_inst[i]["pl"] + t["pl"], 2)
        if t["pl"] > 0: by_inst[i]["wins"] += 1
    for i in by_inst:
        n = by_inst[i]["trades"]
        by_inst[i]["win_rate"] = round(by_inst[i]["wins"]/n*100, 1) if n else 0
    by_inst = dict(sorted(by_inst.items(), key=lambda x: x[1]["pl"], reverse=True))

    return {
        "trades":    len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins)/len(trades)*100, 1),
        "pl":        round(pl, 2),
        "best_trade":  max(trades, key=lambda t: t["pl"]),
        "worst_trade": min(trades, key=lambda t: t["pl"]),
        "by_instrument": by_inst,
    }

def _daily_history(days):
    today = _utc_now().date()
    out   = []
    for i in range(days-1,-1,-1):
        d = (today-timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date":d,**_perf["daily"].get(d,{"trades":0,"wins":0,"losses":0,"pl":0.0})})
    return out

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 7: EMAIL ALERTS ──────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _send_email(subject, html_body):
    if not CFG.get("EMAIL_ENABLED"): return
    try:
        sender    = str(CFG.get("EMAIL_SENDER", "")).strip()
        recipient = str(CFG.get("EMAIL_RECIPIENT", "")).strip() or sender
        password  = str(CFG.get("EMAIL_PASSWORD", "")).replace(" ", "").strip()
        if not sender or not password:
            log.warning("Email send skipped: EMAIL_SENDER or EMAIL_PASSWORD not configured.")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = recipient
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(CFG.get("SMTP_HOST", "smtp.gmail.com"), int(CFG.get("SMTP_PORT", 587)), timeout=15) as s:
            s.ehlo(); s.starttls()
            s.login(sender, password)
            s.sendmail(sender, recipient, msg.as_string())
        log.info(f"Email sent: {subject}")
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"Email failed (Gmail Authentication Error 535 - Bad Credentials for {CFG.get('EMAIL_SENDER')}): {e}")
    except (OSError, Exception) as e:
        if "getaddrinfo" in str(e) or "11004" in str(e) or "11001" in str(e):
            log.warning(f"Email notification skipped due to temporary network/DNS drop: {e}")
        else:
            log.error(f"Email failed: {e}")

# Responsive email wrapper — uses a table-based layout (the only thing
# that reliably constrains width across Gmail/Outlook/Apple Mail mobile apps).
# A <div style="max-width:..."> is silently ignored by many mobile mail
# clients, causing the desktop-width email to render full-size and crop.
_EMAIL_WRAP_OPEN = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body,table,td {{ font-family:-apple-system,Segoe UI,system-ui,sans-serif !important; }}
  body {{ margin:0; padding:0; background:#0b0e1a; }}
  .container {{ max-width:560px; width:100% !important; margin:0 auto; }}
  @media only screen and (max-width:480px) {{
    .container {{ width:100% !important; }}
    .stack-cell {{ display:block !important; width:100% !important; box-sizing:border-box; }}
    .stat-row td {{ padding:10px 6px !important; }}
  }}
</style></head>
<body>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0b0e1a">
<tr><td align="center" style="padding:16px 8px">
<table role="presentation" class="container" cellpadding="0" cellspacing="0"
  style="width:100%;max-width:560px;background:#1a1a2e;border-radius:12px;overflow:hidden;color:#e0e0e0">
"""

_EMAIL_WRAP_CLOSE = """
</table>
</td></tr></table>
</body></html>"""


def _base_email(title, color, rows):
    rows_html = "".join(
        f"<tr class='stat-row'><td style='padding:6px 12px;color:#888;font-size:13px'>{k}</td>"
        f"<td style='padding:6px 12px;font-weight:600;font-size:13px;text-align:right'>{v}</td></tr>"
        for k,v in rows)
    body = f"""<tr><td style='background:{color};padding:18px 24px'>
  <h2 style='margin:0;color:#fff;font-size:18px;line-height:1.3'>{title}</h2>
  <p style='margin:4px 0 0;color:rgba(255,255,255,.75);font-size:13px'>
    {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
</td></tr>
<tr><td>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    {rows_html}
  </table>
</td></tr>
<tr><td style='padding:12px 24px;background:#12122a;font-size:11px;color:#555;text-align:center'>
  Tradalgo &middot; {CFG["OANDA_ENV"].title()} Account &middot; OANDA</td></tr>"""
    return _EMAIL_WRAP_OPEN + body + _EMAIL_WRAP_CLOSE

def email_opened(instrument, direction, units, entry, sl, tp, strategy):
    color = "#22a65e" if direction=="BUY" else "#e05252"
    _send_email(f"🟢 Trade Opened: {direction} {instrument}",
        _base_email(f"{'📈' if direction=='BUY' else '📉'} Trade Opened — {direction} {instrument}", color,
            [("Instrument",instrument),("Direction",f"<span style='color:{color}'>{direction}</span>"),
             ("Units",f"{units:,}"),("Entry",entry),("Stop Loss",sl),("Take Profit",tp),("Strategy",strategy)]))

def email_closed(instrument, direction, entry, exit_price, pl, pl_pct, reason):
    won=pl>=0; color="#22a65e" if won else "#e05252"; icon="✅" if won else "❌"
    _send_email(f"{icon} Trade Closed: {instrument} | {'WIN' if won else 'LOSS'} {pl:+.2f}",
        _base_email(f"{icon} Trade Closed — {instrument}", color,
            [("Instrument",instrument),("Direction",direction),("Entry",entry),("Exit",exit_price),
             ("P&L",f"<span style='color:{color}'>{pl:+.2f} ({pl_pct:+.2f}%)</span>"),("Closed by",reason)]))

def email_win(instrument, direction, entry, exit_price, pl, pl_pct, strategy):
    _send_email(f"🏆 WIN: {instrument} +${pl:.2f} ({pl_pct:+.2f}%)",
        _base_email(f"🏆 Winning Trade — {instrument}", "#16803c",
            [("Instrument",instrument),("Direction",direction),("Entry",entry),("Exit",exit_price),
             ("Profit",f"<span style='color:#4ade80;font-size:16px'>+${pl:.2f} ({pl_pct:+.2f}%)</span>"),
             ("Strategy",strategy),("Closed by","Take Profit ✅")]))

def email_error(message):
    _send_email("⚠️ Tradalgo Error",
        _base_email("⚠️ Bot Error","#c0392b",[("Error",message),("Time",datetime.utcnow().isoformat())]))

def email_session(session, pairs):
    _send_email(f"🕐 {session} Session Started",
        _base_email(f"🕐 {session} Session Started","#2980b9",
            [("Session",session),("Active Pairs",pairs),("Status","Bot is trading")]))

def email_daily_summary(date_str, stats, balance):
    td=stats.get("today",{}); trades=td.get("trades",stats.get("total_trades",0))
    wins=td.get("wins",stats.get("wins",0)); pl=td.get("pl",stats.get("net_pl",0))
    wr=round(wins/trades*100,1) if trades else 0
    pc=("#166534" if pl>=0 else "#7f1d1d"); icon="📈" if pl>=0 else "📉"
    best=stats.get("best_trade"); worst=stats.get("worst_trade")
    b_row=f"{best['instrument']} +${best['pl']:.2f}" if best else "—"
    w_row=f"{worst['instrument']} ${worst['pl']:.2f}" if worst else "—"
    by_inst=stats.get("by_instrument",{})
    inst_rows="".join(f"<tr style='font-size:12px'><td style='padding:5px 12px'>{i.replace('_','/')}</td>"
        f"<td style='padding:5px 12px;text-align:center'>{d['trades']}</td>"
        f"<td style='padding:5px 12px;text-align:center'>{d['win_rate']}%</td>"
        f"<td style='padding:5px 12px;font-weight:600;color:{'#4ade80' if d['pl']>=0 else '#f87171'};text-align:right'>"
        f"{'+'if d['pl']>=0 else''}${d['pl']:.2f}</td></tr>"
        for i,d in list(by_inst.items())[:5])
    body = f"""<tr><td style='background:{pc};padding:20px 24px'>
  <h2 style='margin:0;color:#fff;font-size:20px;line-height:1.3'>{icon} Daily Summary &mdash; {date_str}</h2>
  <p style='margin:6px 0 0;color:rgba(255,255,255,.7);font-size:13px'>Balance: <b>${balance:,.2f}</b></p>
</td></tr>
<tr><td style='background:#12122a;border-bottom:1px solid #2a2a4a'>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    <tr>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center;border-right:1px solid #2a2a4a;border-bottom:1px solid #2a2a4a'>
        <div style='font-size:22px;font-weight:700;color:{"#4ade80" if pl>=0 else "#f87171"}'>{"+" if pl>=0 else ""}${pl:.2f}</div>
        <div style='font-size:11px;color:#666'>Day P&amp;L</div></td>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center;border-bottom:1px solid #2a2a4a'>
        <div style='font-size:22px;font-weight:700'>{trades}</div>
        <div style='font-size:11px;color:#666'>Trades</div></td>
    </tr>
    <tr>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center;border-right:1px solid #2a2a4a'>
        <div style='font-size:22px;font-weight:700;color:#4ade80'>{wins}W</div>
        <div style='font-size:11px;color:#666'>Wins</div></td>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center'>
        <div style='font-size:22px;font-weight:700;color:{"#4ade80" if wr>=50 else "#f87171"}'>{wr}%</div>
        <div style='font-size:11px;color:#666'>Win Rate</div></td>
    </tr>
  </table>
</td></tr>
<tr><td>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    <tr class="stat-row"><td style='padding:7px 12px;color:#888;font-size:13px'>Best Trade</td>
        <td style='padding:7px 12px;font-weight:600;color:#4ade80;font-size:13px;text-align:right'>{b_row}</td></tr>
    <tr class="stat-row"><td style='padding:7px 12px;color:#888;font-size:13px'>Worst Trade</td>
        <td style='padding:7px 12px;font-weight:600;color:#f87171;font-size:13px;text-align:right'>{w_row}</td></tr>
    <tr class="stat-row"><td style='padding:7px 12px;color:#888;font-size:13px'>All-time Win Rate</td>
        <td style='padding:7px 12px;font-weight:600;font-size:13px;text-align:right'>{stats.get("win_rate",0)}%</td></tr>
    <tr class="stat-row"><td style='padding:7px 12px;color:#888;font-size:13px'>All-time P&amp;L</td>
        <td style='padding:7px 12px;font-weight:600;font-size:13px;text-align:right;color:{"#4ade80" if stats.get("net_pl",0)>=0 else "#f87171"}'>
          {"+"if stats.get("net_pl",0)>=0 else""}${stats.get("net_pl",0):.2f}</td></tr>
    <tr class="stat-row"><td style='padding:7px 12px;color:#888;font-size:13px'>Profit Factor</td>
        <td style='padding:7px 12px;font-weight:600;font-size:13px;text-align:right'>{stats.get("profit_factor",0)}</td></tr>
  </table>
</td></tr>
{f"<tr><td style='padding:10px 12px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#555;background:#12122a'>Top Pairs</td></tr><tr><td style='background:#12122a'><table role=presentation width=100% cellpadding=0 cellspacing=0 style=border-collapse:collapse><tr style=font-size:10px;color:#555;text-transform:uppercase><td style=padding:5px 12px>Pair</td><td style=padding:5px 12px;text-align:center>Trades</td><td style=padding:5px 12px;text-align:center>Win%</td><td style=padding:5px 12px;text-align:right>P&amp;L</td></tr>{inst_rows}</table></td></tr>" if inst_rows else ""}
<tr><td style='padding:12px 24px;background:#0d0d1f;font-size:11px;color:#444;text-align:center'>
  Tradalgo &middot; {CFG["OANDA_ENV"].title()} Account &middot; OANDA</td></tr>"""
    html = _EMAIL_WRAP_OPEN + body + _EMAIL_WRAP_CLOSE
    _send_email(f"{icon} Daily Summary {date_str} | {'+'if pl>=0 else''}${pl:.2f} | {wr}% win rate", html)


def email_weekly_summary(week_key, week_stats, balance):
    """
    Plain-English weekly report sent every Monday.
    week_key: "2026-W24"  week_stats: from get_week_stats()
    """
    trades = week_stats.get("trades", 0)
    wins   = week_stats.get("wins", 0)
    pl     = week_stats.get("pl", 0.0)
    wr     = week_stats.get("win_rate", 0)

    starting_bal = _perf.get("starting_balance") or balance
    growth_pct   = round((balance - starting_bal) / starting_bal * 100, 2) if starting_bal else 0
    start_date   = _perf.get("start_date", "")

    pc   = "#166534" if pl >= 0 else "#7f1d1d"
    icon = "&#128200;" if pl >= 0 else "&#128201;"  # 📈 📉

    best  = week_stats.get("best_trade")
    worst = week_stats.get("worst_trade")
    b_row = f"{_friendly(best['instrument'])} +${best['pl']:.2f}" if best else "&mdash;"
    w_row = f"{_friendly(worst['instrument'])} ${worst['pl']:.2f}" if worst else "&mdash;"

    by_inst    = week_stats.get("by_instrument", {})
    inst_rows  = "".join(
        f"<tr style='font-size:12px'><td style='padding:5px 12px'>{_friendly(i)}</td>"
        f"<td style='padding:5px 12px;text-align:center'>{d['trades']}</td>"
        f"<td style='padding:5px 12px;text-align:center'>{d['win_rate']}%</td>"
        f"<td style='padding:5px 12px;font-weight:600;color:{'#4ade80' if d['pl']>=0 else '#f87171'};text-align:right'>"
        f"{'+'if d['pl']>=0 else''}${d['pl']:.2f}</td></tr>"
        for i, d in list(by_inst.items())[:5]
    )

    # Plain-English summary line — the headline of the email
    if trades == 0:
        summary_line = "No trades were made this week. The bot waits for the right opportunities and won't force a trade."
    elif pl > 0:
        summary_line = (f"Your bot made <b>{trades}</b> trade{'s' if trades!=1 else ''} this week, "
                        f"winning <b>{wins}</b> of them, and made <b style='color:#4ade80'>+${pl:.2f}</b>.")
    else:
        summary_line = (f"Your bot made <b>{trades}</b> trade{'s' if trades!=1 else ''} this week, "
                        f"winning <b>{wins}</b> of them, and lost <b style='color:#f87171'>${abs(pl):.2f}</b>.")

    growth_line = ""
    if start_date:
        growth_color = "#4ade80" if growth_pct >= 0 else "#f87171"
        growth_line = (f"Your account is <b style='color:{growth_color}'>"
                       f"{'+' if growth_pct>=0 else ''}{growth_pct}%</b> "
                       f"since you started on {start_date}.")

    body = f"""<tr><td style='background:{pc};padding:22px 24px'>
  <h2 style='margin:0;color:#fff;font-size:20px;line-height:1.3'>{icon} Your Weekly Report</h2>
  <p style='margin:6px 0 0;color:rgba(255,255,255,.7);font-size:13px'>Week of {week_key.split("-")[1]} &middot; Balance: <b>${balance:,.2f}</b></p>
</td></tr>
<tr><td style='padding:18px 24px;font-size:14px;line-height:1.7'>
  <p style='margin:0 0 8px'>{summary_line}</p>
  {f"<p style='margin:0;color:#aaa;font-size:13px'>{growth_line}</p>" if growth_line else ""}
</td></tr>
<tr><td style='background:#12122a;border-bottom:1px solid #2a2a4a;border-top:1px solid #2a2a4a'>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
    <tr>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center;border-right:1px solid #2a2a4a;border-bottom:1px solid #2a2a4a'>
        <div style='font-size:22px;font-weight:700;color:{"#4ade80" if pl>=0 else "#f87171"}'>{"+" if pl>=0 else ""}${pl:.2f}</div>
        <div style='font-size:11px;color:#666'>This Week</div></td>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center;border-bottom:1px solid #2a2a4a'>
        <div style='font-size:22px;font-weight:700'>{trades}</div>
        <div style='font-size:11px;color:#666'>Trades</div></td>
    </tr>
    <tr>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center;border-right:1px solid #2a2a4a'>
        <div style='font-size:22px;font-weight:700;color:#4ade80'>{wins}W</div>
        <div style='font-size:11px;color:#666'>Wins</div></td>
      <td class="stack-cell" width="50%" style='padding:14px;text-align:center'>
        <div style='font-size:22px;font-weight:700;color:{"#4ade80" if wr>=50 else "#f87171"}'>{wr}%</div>
        <div style='font-size:11px;color:#666'>Win Rate</div></td>
    </tr>
  </table>
</td></tr>
{f"<tr><td><table role=presentation width=100% cellpadding=0 cellspacing=0 style=border-collapse:collapse><tr class=stat-row><td style=padding:7px 12px;color:#888;font-size:13px>Best Trade</td><td style=padding:7px 12px;font-weight:600;color:#4ade80;font-size:13px;text-align:right>{b_row}</td></tr><tr class=stat-row><td style=padding:7px 12px;color:#888;font-size:13px>Worst Trade</td><td style=padding:7px 12px;font-weight:600;color:#f87171;font-size:13px;text-align:right>{w_row}</td></tr></table></td></tr>" if trades else ""}
{f"<tr><td style='padding:10px 12px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#555;background:#12122a'>Pairs Traded This Week</td></tr><tr><td style='background:#12122a'><table role=presentation width=100% cellpadding=0 cellspacing=0 style=border-collapse:collapse><tr style=font-size:10px;color:#555;text-transform:uppercase><td style=padding:5px 12px>Pair</td><td style=padding:5px 12px;text-align:center>Trades</td><td style=padding:5px 12px;text-align:center>Win%</td><td style=padding:5px 12px;text-align:right>P&amp;L</td></tr>{inst_rows}</table></td></tr>" if inst_rows else ""}
<tr><td style='padding:14px 24px;background:#0d0d1f;font-size:11px;color:#444;text-align:center'>
  Tradalgo &middot; {CFG["OANDA_ENV"].title()} Account &middot; OANDA<br>
  <span style='color:#333'>This is an automated weekly summary. The bot keeps running &mdash; no action needed.</span>
</td></tr>"""

    html = _EMAIL_WRAP_OPEN + body + _EMAIL_WRAP_CLOSE
    subject = f"{icon} Your Weekly Report | {'+' if pl>=0 else ''}${pl:.2f} | {trades} trades"
    _send_email(subject.replace("&#128200;","\U0001F4C8").replace("&#128201;","\U0001F4C9"), html)

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 7b: POSITION SIZING (shared by live trading + backtester) ───────
# ══════════════════════════════════════════════════════════════════════════════
"""
Previously, live trading (OandaClient.calculate_units) and the backtester
(run_backtest) used two completely different sizing formulas that didn't
even share a pip-size convention. Depending on the instrument they could
diverge by anywhere from roughly correct-by-coincidence (EUR_USD-style
pairs, quote currency = USD) to a ~100,000x discrepancy (JPY pairs) —
meaning backtest P&L figures for JPY pairs bore no relation to what live
trading would actually risk, and live trading's own JPY sizing was itself
built on a hardcoded constant that doesn't reflect the real exchange rate.

This section provides one formula both paths call. It correctly handles:
  1. Quote currency is USD (EUR_USD, GBP_USD, AUD_USD, NZD_USD, XAU_USD)
     → pip value in USD is just the pip size — no conversion needed.
  2. Base currency is USD (USD_JPY, USD_CHF, USD_CAD)
     → divide the quote-currency pip size by the pair's own current price.
  3. Cross pairs where neither leg is USD (EUR_GBP, EUR_JPY)
     → needs a second reference rate to convert the quote currency's pip
       value into USD. `price_lookup(pair)` fetches it live when available
       (live trading always has this, since all instrument prices are
       already fetched every cycle). The backtester currently only has
       candle data for the single instrument it's testing, so it falls
       back to a fixed approximate rate for the 2 cross pairs specifically
       — clearly less precise than the live path, but still self-consistent
       with it, unlike before, and it's an honest, documented approximation
       rather than a silently-wrong one.
"""

def _pip_size(instrument: str) -> float:
    if "JPY" in instrument: return 0.01
    if "XAU" in instrument: return 0.1
    return 0.0001

# Only used as a last resort when a live reference rate isn't available —
# i.e. always during backtesting (which has no price_lookup), and live only
# if a price fetch genuinely fails. Update periodically; being off here
# affects position sizing accuracy for these currencies specifically, never
# the stop-loss/take-profit levels themselves.
_CROSS_RATE_FALLBACK = {
    "GBP": 1.27,   # approx GBP/USD, used for EUR_GBP's quote leg
    "JPY": 155.0,  # approx USD/JPY, used for EUR_JPY's quote leg and USD_JPY
    "CHF": 0.88,   # approx USD/CHF, used for USD_CHF
    "CAD": 1.36,   # approx USD/CAD, used for USD_CAD
}

def pip_value_usd_per_unit(instrument: str, price_lookup=None):
    """Returns the USD value of a 1-pip move for 1 unit of `instrument`.
    Returns None (not a float) if a required cross-rate is unavailable,
    so callers can skip the trade instead of sizing with a fallback."""
    pip = _pip_size(instrument)
    if "_" not in instrument:
        return pip
    base, quote = instrument.split("_", 1)

    if quote == "USD":
        return pip

    if base == "USD":
        px = price_lookup(instrument) if price_lookup else None
        if px: return pip / px
        fallback = _CROSS_RATE_FALLBACK.get(quote)
        if fallback: return pip / fallback
        return None  # No rate available — caller must skip trade

    # Cross pair — convert the quote currency's pip value into USD via a
    # second reference rate.
    if price_lookup:
        px_direct = price_lookup(f"{quote}_USD")   # e.g. GBP_USD for EUR_GBP
        if px_direct: return pip * px_direct
        px_inverse = price_lookup(f"USD_{quote}")  # e.g. USD_JPY for EUR_JPY
        if px_inverse: return pip / px_inverse
        # Live price lookup available but rates missing — refuse fallback
        return None
    rate = _CROSS_RATE_FALLBACK.get(quote)
    if rate:
        return pip * rate if quote == "GBP" else pip / rate
    return None  # Last resort: no fallback available, skip rather than oversize

def calculate_position_units(instrument: str, sl_pips: float, risk_pct: float,
                              balance: float, price_lookup=None):
    """
    Sizes a position so that hitting the stop loss risks approximately
    risk_pct% of `balance`, in USD-equivalent terms. Shared by live trading
    and the backtester so their P&L figures are directly comparable.
    Returns None if a required conversion rate is unavailable (caller should skip the trade).
    """
    if balance <= 0 or sl_pips <= 0:
        return 1
    risk = balance * (risk_pct / 100)
    ppv  = pip_value_usd_per_unit(instrument, price_lookup)
    if ppv is None:
        log.warning(f"calculate_position_units: no pip rate for {instrument} — skipping trade")
        return None  # Signal to caller: skip this trade
    if ppv <= 0:
        return 1
    return max(1, min(int(risk / (sl_pips * ppv)), 1_000_000))

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 8: OANDA CLIENT ──────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_candle_cache: dict = {}
CACHE_TTL = 55 * 60

class OandaClient:
    def __init__(self):
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=50, max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "Content-Type":  "application/json",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
        })

    def _auth_headers(self):
        key = str(CFG.get("OANDA_API_KEY", "")).strip().strip("\"'").strip()
        return {"Authorization": f"Bearer {key}"}

    def _base(self): return _oanda_api_url()
    def _aid(self):
        aid = str(CFG.get("OANDA_ACCOUNT_ID", "")).strip().strip("\"'").strip()
        digits = "".join(c for c in aid if c.isdigit())
        if len(digits) in (16, 17) and "-" not in aid:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:-3]}-{digits[-3:]}"
        return aid

    def test_connection(self):
        """Verifies OANDA API key & account ID against OANDA REST API."""
        aid = self._aid()
        key = str(CFG.get("OANDA_API_KEY", "")).strip().strip("\"'").strip()
        env = str(CFG.get("OANDA_ENV", "practice")).strip().lower()
        if not aid or not key:
            return False, "OANDA API Key or Account ID is missing in Settings."
        try:
            r = self.session.get(f"{self._base()}/v3/accounts/{aid}/summary", headers=self._auth_headers(), timeout=8)
            if r.status_code == 200:
                return True, f"Connected to OANDA successfully ({env.title()} mode)."
            elif r.status_code == 401:
                return False, f"OANDA 401 Unauthorized: Invalid API Key or environment mismatch (currently set to '{env.title()}' mode)."
            elif r.status_code == 404:
                return False, f"OANDA 404 Not Found: Account ID '{aid}' not found in '{env.title()}' mode."
            else:
                return False, f"OANDA returned HTTP {r.status_code}."
        except Exception as e:
            return False, f"OANDA connection error: {e}"

    def _get(self, path, params=None):
        r = self.session.get(f"{self._base()}{path}", headers=self._auth_headers(), params=params, timeout=10)
        r.raise_for_status(); return r.json()

    def _post(self, path, body):
        r = self.session.post(f"{self._base()}{path}", headers=self._auth_headers(), json=body, timeout=10)
        r.raise_for_status(); return r.json()

    def _put(self, path, body):
        r = self.session.put(f"{self._base()}{path}", headers=self._auth_headers(), json=body, timeout=10)
        r.raise_for_status(); return r.json()

    def get_account(self):
        return self._get(f"/v3/accounts/{self._aid()}")["account"]

    def get_balance(self):
        return float(self._get(f"/v3/accounts/{self._aid()}/summary")["account"]["balance"])

    def get_open_trades(self):
        return self._get(f"/v3/accounts/{self._aid()}/openTrades")["trades"]

    def get_trade(self, tid):
        return self._get(f"/v3/accounts/{self._aid()}/trades/{tid}")["trade"]

    def get_transactions(self, count=30):
        return self._get(f"/v3/accounts/{self._aid()}/transactions",
                         params={"count":count}).get("transactions",[])

    def get_price(self, instrument):
        data = self._get(f"/v3/accounts/{self._aid()}/pricing",
                         params={"instruments":instrument})
        p=data["prices"][0]; bid=float(p["bids"][0]["price"]); ask=float(p["asks"][0]["price"])
        return {"bid":bid,"ask":ask,"mid":(bid+ask)/2,"time":p["time"]}

    def get_prices(self, instruments=None):
        instruments = instruments or CFG["INSTRUMENTS"]
        data = self._get(f"/v3/accounts/{self._aid()}/pricing",
                         params={"instruments":",".join(instruments)})
        result={}
        for p in data["prices"]:
            bid=float(p["bids"][0]["price"]); ask=float(p["asks"][0]["price"])
            result[p["instrument"]]={"bid":bid,"ask":ask,"mid":(bid+ask)/2}
        return result

    def get_candles(self, instrument, granularity="H1", count=100):
        key=f"{instrument}:{granularity}"; now=time.time()
        cached=_candle_cache.get(key)
        if cached and (now-cached["cached_at"])<CACHE_TTL:
            return cached["candles"]
        candles=self._fetch_candles(instrument, granularity, count)
        _candle_cache[key]={"candles":candles,"cached_at":now}
        return candles

    def _fetch_candles(self, instrument, granularity, count):
        data=self._get(f"/v3/instruments/{instrument}/candles",
                       params={"granularity":granularity,"count":count,"price":"M"})
        out=[]
        for c in data["candles"]:
            if not c["complete"]: continue
            m=c["mid"]
            out.append({"time":c["time"],"open":float(m["o"]),"high":float(m["h"]),
                        "low":float(m["l"]),"close":float(m["c"]),"volume":int(c["volume"])})
        return out

    def get_all_candles_parallel(self, instruments, granularity="H1", count=100, workers=6):
        results={}; to_fetch=[]
        for inst in instruments:
            key=f"{inst}:{granularity}"; now=time.time(); entry=_candle_cache.get(key)
            if entry and (now-entry["cached_at"])<CACHE_TTL: results[inst]=entry["candles"]
            else: to_fetch.append(inst)
        if to_fetch:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures={pool.submit(self._fetch_candles,i,granularity,count):i for i in to_fetch}
                for f in as_completed(futures):
                    inst=futures[f]
                    try:
                        c=f.result()
                        _candle_cache[f"{inst}:{granularity}"]={"candles":c,"cached_at":time.time()}
                        results[inst]=c
                    except Exception as e: log.error(f"Candle fetch {inst}: {e}"); results[inst]=[]
        return results

    def invalidate_cache(self): _candle_cache.clear()

    def place_market_order(self, instrument, units, stop_loss_price=None,
                           take_profit_price=None, trailing_stop_pips=None, client_comment="",
                           price_bound=None):
        def _fmt(p, inst):
            if "JPY" in inst: return f"{p:.3f}"
            if "XAU" in inst: return f"{p:.2f}"
            return f"{p:.5f}"
        order={"type":"MARKET","instrument":instrument,"units":str(units)}
        if price_bound:       order["priceBound"]      =_fmt(price_bound, instrument)
        if stop_loss_price:   order["stopLossOnFill"]  ={"price":_fmt(stop_loss_price, instrument)}
        if take_profit_price: order["takeProfitOnFill"]={"price":_fmt(take_profit_price, instrument)}
        tsl = float(trailing_stop_pips or 0)
        if tsl > 0:
            def _pip_tsl(inst):
                if "JPY" in inst: return 0.01
                if "XAU" in inst: return 0.10
                return 0.0001
            order["trailingStopLossOnFill"]={"distance":_fmt(tsl * _pip_tsl(instrument), instrument)}
        if client_comment:    order["clientExtensions"]={"comment":client_comment[:128]}
        return self._post(f"/v3/accounts/{self._aid()}/orders",{"order":order})

    def close_trade(self, tid):
        return self._put(f"/v3/accounts/{self._aid()}/trades/{tid}/close",{})

    def calculate_units(self, instrument, sl_pips, risk_pct, balance=None, prices_hint=None):
        """
        prices_hint: optional dict of {instrument: {"mid": ...}} already
        fetched this cycle (trading_cycle() passes its all_prices dict here).
        Avoids an extra API call for the common case, cross pairs (EUR_GBP,
        EUR_JPY) fall back to a direct price lookup for the reference rate
        only when it isn't already in the hint.
        """
        if balance is None: balance = self.get_balance()
        def _lookup(pair):
            if prices_hint and pair in prices_hint:
                return prices_hint[pair].get("mid")
            try:    return self.get_price(pair)["mid"]
            except Exception: return None
        return calculate_position_units(instrument, sl_pips, risk_pct, balance, price_lookup=_lookup)


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 9: STRATEGIES ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _sl_tp(instrument, sl=None, tp=None):
    if "XAU" in instrument:
        return sl or CFG["GOLD_SL_PIPS"], tp or CFG["GOLD_TP_PIPS"]
    return sl or CFG["DEFAULT_SL_PIPS"], tp or CFG["DEFAULT_TP_PIPS"]

def _strat_ema_cross(candles, instrument):
    c=_closes(candles); f=_ema(c,9); s=_ema(c,21); t=_ema(c,50); i=-1
    if any(_nan(v) for v in [f[i],f[i-1],s[i],s[i-1],t[i]]): return {"signal":None}
    sl,tp=_sl_tp(instrument)
    if f[i-1]<s[i-1] and f[i]>s[i] and c[i]>t[i]: return {"signal":"BUY","sl_pips":sl,"tp_pips":tp*1.5,"reason":"EMA_Cross: BUY"}
    if f[i-1]>s[i-1] and f[i]<s[i] and c[i]<t[i]: return {"signal":"SELL","sl_pips":sl,"tp_pips":tp*1.5,"reason":"EMA_Cross: SELL"}
    return {"signal":None}

def _strat_rsi(candles, instrument):
    r=_rsi(_closes(candles),14); i=-1
    if _nan(r[i]) or _nan(r[i-1]): return {"signal":None}
    sl,tp=_sl_tp(instrument)
    if r[i-1]<30 and r[i]>30: return {"signal":"BUY","sl_pips":sl,"tp_pips":tp,"reason":f"RSI_Reversal: BUY ({r[i]:.1f})"}
    if r[i-1]>70 and r[i]<70: return {"signal":"SELL","sl_pips":sl,"tp_pips":tp,"reason":f"RSI_Reversal: SELL ({r[i]:.1f})"}
    return {"signal":None}

def _strat_bb(candles, instrument):
    c=_closes(candles); u,_m,l=_bollinger(c,20,2.0); i=-1
    if any(_nan(v) for v in [u[i],l[i]]): return {"signal":None}
    sl,tp=_sl_tp(instrument)
    if c[i]>u[i] and c[i-1]<=u[i-1]: return {"signal":"BUY","sl_pips":sl,"tp_pips":tp*2,"reason":"Bollinger_Break: BUY"}
    if c[i]<l[i] and c[i-1]>=l[i-1]: return {"signal":"SELL","sl_pips":sl,"tp_pips":tp*2,"reason":"Bollinger_Break: SELL"}
    return {"signal":None}

def _strat_macd(candles, instrument):
    c=_closes(candles); ml,sl_,h=_macd(c); i=-1
    if any(_nan(v) for v in [ml[i],sl_[i],h[i],ml[i-1]]): return {"signal":None}
    sl,tp=_sl_tp(instrument)
    if ml[i-1]<sl_[i-1] and ml[i]>sl_[i] and h[i]>0: return {"signal":"BUY","sl_pips":sl,"tp_pips":tp,"reason":"MACD_Momentum: BUY"}
    if ml[i-1]>sl_[i-1] and ml[i]<sl_[i] and h[i]<0: return {"signal":"SELL","sl_pips":sl,"tp_pips":tp,"reason":"MACD_Momentum: SELL"}
    return {"signal":None}

def _strat_session(candles, instrument):
    """Session breakout: uses the 4-candle range ending 2 candles ago,
    anchored by candle timestamps so it always covers a real session window
    rather than a fixed offset that may drift on lower time-frames."""
    if len(candles) < 10: return {"signal": None}
    c = _closes(candles); h = _highs(candles); l = _lows(candles)
    # Use candles[-8:-2] (6 candles back to 2 back) for a wider session window
    # that remains meaningful on H1 charts (6h = full London session)
    window_h = h[-8:-2]; window_l = l[-8:-2]
    if len(window_h) == 0: return {"signal": None}
    rh = max(window_h); rl = min(window_l); cur = c[-1]
    sl, tp = _sl_tp(instrument)
    if cur > rh * 1.0005: return {"signal": "BUY",  "sl_pips": sl, "tp_pips": tp, "reason": "Session_Break: BUY"}
    if cur < rl * 0.9995: return {"signal": "SELL", "sl_pips": sl, "tp_pips": tp, "reason": "Session_Break: SELL"}
    return {"signal": None}

STRATEGIES = {
    "EMA_Cross":       _strat_ema_cross,
    "RSI_Reversal":    _strat_rsi,
    "Bollinger_Break": _strat_bb,
    "MACD_Momentum":   _strat_macd,
    "Session_Break":   _strat_session,
}

def run_all_strategies(candles, instrument):
    results={}
    for name,fn in STRATEGIES.items():
        try:    results[name]=fn(candles,instrument)
        except Exception as e: results[name]={"signal":None,"reason":f"error:{e}"}
    return results

def consensus_signal(results, weights, threshold=0.45):  # Raised from 0.35 — requires ≥2 strategies to agree
    buy=sell=0.0; reasons=[]
    for name,res in results.items():
        w=weights.get(name,0.2); sig=res.get("signal")
        if sig=="BUY":  buy+=w;  reasons.append(res.get("reason",f"{name}: BUY"))
        elif sig=="SELL": sell+=w; reasons.append(res.get("reason",f"{name}: SELL"))
    sl=CFG["DEFAULT_SL_PIPS"]; tp=CFG["DEFAULT_TP_PIPS"]
    if buy>=threshold and buy>sell:
        top=max((n for n,r in results.items() if r.get("signal")=="BUY"),key=lambda n:weights.get(n,0),default=None)
        if top: sl=results[top].get("sl_pips",sl); tp=results[top].get("tp_pips",tp)
        return {"signal":"BUY","score":round(buy,3),"sl_pips":sl,"tp_pips":tp,"reasons":reasons}
    if sell>=threshold and sell>buy:
        top=max((n for n,r in results.items() if r.get("signal")=="SELL"),key=lambda n:weights.get(n,0),default=None)
        if top: sl=results[top].get("sl_pips",sl); tp=results[top].get("tp_pips",tp)
        return {"signal":"SELL","score":round(sell,3),"sl_pips":sl,"tp_pips":tp,"reasons":reasons}
    return {"signal":None,"score":0,"sl_pips":sl,"tp_pips":tp,"reasons":reasons}

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 8d: NEWS FILTER ──────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
"""
Blocks trading in the 45 minutes before and after high-impact economic events.
Uses the free ForexFactory calendar API (no key required).
High-impact events: NFP, CPI, rate decisions, GDP, PMI.
"""

_news_cache: dict = {"events": [], "fetched_at": 0.0}
_NEWS_CACHE_TTL = 3600  # refresh hourly


def _fetch_news_events() -> list:
    """Fetch today's high-impact forex events from ForexFactory."""
    now = time.time()
    if now - _news_cache["fetched_at"] < _NEWS_CACHE_TTL and _news_cache["events"]:
        return _news_cache["events"]

    try:
        # ForexFactory public calendar JSON
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=8, headers={"User-Agent": "Tradalgo/1.0"}
        )
        r.raise_for_status()
        all_events = r.json()

        high_impact = []
        skipped = 0
        for ev in all_events:
            if ev.get("impact", "").lower() != "high":
                continue
            # Current API format: a single ISO-8601 timestamp with the
            # correct UTC offset already applied per event, e.g.
            # "2026-07-30T07:00:00-04:00" (EDT) or "...-05:00" (EST).
            # This means DST is handled correctly automatically — we just
            # convert to UTC, no manual EST/EDT arithmetic needed.
            raw = ev.get("date", "")
            if not raw:
                skipped += 1
                continue
            try:
                ev_dt = datetime.fromisoformat(raw)
                if ev_dt.tzinfo is None:
                    # Defensive fallback in case a future API response ever
                    # omits the offset — assume US Eastern, DST-aware.
                    try:
                        from zoneinfo import ZoneInfo
                        ev_dt = ev_dt.replace(tzinfo=ZoneInfo("America/New_York"))
                    except Exception:
                        skipped += 1
                        continue
                ev_dt = ev_dt.astimezone(timezone.utc)
                high_impact.append({
                    "title":    ev.get("title", "Unknown"),
                    "currency": ev.get("country", ""),
                    "time":     ev_dt,
                    "impact":   "high",
                })
            except Exception:
                skipped += 1
                continue

        if skipped:
            log.debug(f"News filter: skipped {skipped} events with unparseable timestamps")

        _news_cache["events"]     = high_impact
        _news_cache["fetched_at"] = now
        log.info(f"News filter: fetched {len(high_impact)} high-impact events this week")
        return high_impact

    except Exception as e:
        log.warning(f"News filter: could not fetch calendar ({e}) — trading allowed")
        return []


def is_news_blackout(instrument: str) -> tuple:
    """
    Returns (blocked: bool, reason: str).
    Blocks trading if within NEWS_BLOCK_MINUTES of a high-impact event
    affecting the currencies in the instrument.
    """
    if not CFG.get("NEWS_FILTER_ENABLED", True):
        return False, ""

    block_minutes = CFG.get("NEWS_BLOCK_MINUTES", 45)
    now           = _utc_now()

    # Extract currencies from instrument (EUR_USD → EUR, USD)
    currencies = set(instrument.replace("XAU", "USD").split("_"))

    events = _fetch_news_events()
    for ev in events:
        ev_time = ev["time"]
        delta   = abs((ev_time - now).total_seconds() / 60)
        # Check if event affects our instrument's currencies
        ev_currency = ev.get("currency", "").upper()
        affects     = not ev_currency or ev_currency in currencies or ev_currency == "USD"
        if affects and delta <= block_minutes:
            direction = "in" if ev_time > now else "ago"
            mins      = int(delta)
            return True, (f"News blackout: '{ev['title']}' {mins} min {direction} "
                          f"— no trading {block_minutes} min before/after high-impact events")

    return False, ""


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 8e: AI DAILY BIAS ────────────────────────────────────════════════
# ══════════════════════════════════════════════════════════════════════════════
"""
Once per day, calls Claude (claude-haiku-4-5-20251001 — cheap) with web search
enabled to research current forex market conditions and produce a structured
trading bias that overrides default signal behaviour for 24 hours.

The bias contains:
  - overall_sentiment: "risk_on" | "risk_off" | "neutral"
  - usd_bias: "strong" | "weak" | "neutral"
  - preferred_pairs: list of instruments to favour
  - avoid_pairs: list of instruments to skip today
  - avoid_direction: {"EUR_USD": "BUY"} — directional blocks per pair
  - confidence: 0.0-1.0
  - summary: plain-English explanation

Stored in CFG["AI_BIAS_DATA"] and refreshed once per calendar day.
Requires ANTHROPIC_API_KEY in config (or ANTHROPIC_API_KEY env var).
"""

_AI_BIAS_PROMPT = """You are a professional forex market analyst. Research the current forex market 
conditions using web search and provide a structured trading bias for today.

Analyse:
1. Major central bank stances (Fed, ECB, BOE, BOJ, SNB, RBA, BOC, RBNZ)
2. Recent high-impact economic data releases
3. Current risk sentiment (risk-on vs risk-off)
4. Any major geopolitical or macro events affecting forex
5. USD strength/weakness drivers

The bot trades these pairs: EUR_USD, GBP_USD, USD_JPY, USD_CHF, AUD_USD, USD_CAD, NZD_USD, EUR_GBP, EUR_JPY, XAU_USD

Respond ONLY with a JSON object, no other text:
{
  "overall_sentiment": "risk_on|risk_off|neutral",
  "usd_bias": "strong|weak|neutral",
  "preferred_pairs": ["EUR_USD", ...],
  "avoid_pairs": ["USD_JPY", ...],
  "avoid_direction": {"USD_JPY": "BUY"},
  "confidence": 0.75,
  "summary": "One sentence plain-English summary of today's conditions"
}"""


def run_ai_bias() -> dict:
    """
    Call Claude API with web search to get today's market bias.
    Returns the bias dict, or empty dict on failure.
    """
    api_key = CFG.get("AI_BIAS_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("AI Bias: no API key configured (set AI_BIAS_API_KEY in config)")
        return {}

    try:
        log.info("AI Bias: requesting daily market analysis from Claude...")
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": _AI_BIAS_PROMPT}],
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        # Extract text response (may come after tool use blocks)
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        # Parse JSON from response
        import json as _json
        # Strip markdown fences if present
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        bias  = _json.loads(clean)

        # Validate required fields
        required = ["overall_sentiment", "usd_bias", "confidence", "summary"]
        for f in required:
            if f not in bias:
                raise ValueError(f"Missing field: {f}")

        bias["generated_at"] = _utc_now().isoformat()
        log.info(f"AI Bias: {bias['summary']} (confidence: {bias['confidence']})")
        return bias

    except Exception as e:
        log.error(f"AI Bias failed: {e}")
        return {}


def refresh_ai_bias_if_needed():
    """Called from bot loop once per hour — refreshes bias once per calendar day."""
    if not CFG.get("AI_BIAS_ENABLED", False):
        return

    last_run  = CFG.get("AI_BIAS_LAST_RUN", "")
    today_str = _utc_now().strftime("%Y-%m-%d")

    if last_run == today_str and CFG.get("AI_BIAS_DATA"):
        return  # already have today's bias

    bias = run_ai_bias()
    if bias:
        CFG["AI_BIAS_DATA"]    = bias
        CFG["AI_BIAS_LAST_RUN"] = today_str
        _save_config(CFG)
        feed_push("info", {
            "title": "AI market analysis updated",
            "body":  bias.get("summary", "Daily market bias refreshed."),
        })
        log.info(f"AI Bias refreshed for {today_str}")


def apply_ai_bias(signal: str, instrument: str) -> tuple:
    """
    Check if AI bias blocks or modifies the proposed signal.
    Returns (allowed: bool, reason: str)
    """
    if not CFG.get("AI_BIAS_ENABLED", False):
        return True, ""

    bias = CFG.get("AI_BIAS_DATA", {})
    if not bias or bias.get("confidence", 0) < 0.5:
        return True, ""  # low confidence — don't override

    # Check avoid_pairs
    if instrument in bias.get("avoid_pairs", []):
        return False, f"AI Bias: avoiding {instrument} today — {bias.get('summary','')}"

    # Check avoid_direction (e.g. {"USD_JPY": "BUY"} blocks BUY on USD_JPY)
    avoid_dir = bias.get("avoid_direction", {})
    if avoid_dir.get(instrument) == signal:
        return False, (f"AI Bias: blocking {signal} on {instrument} — "
                       f"directional bias against this trade")

    # Check preferred_pairs — if list exists and instrument not in it, reduce but don't block
    preferred = bias.get("preferred_pairs", [])
    if preferred and instrument not in preferred:
        # Not blocked, just noted
        log.debug(f"AI Bias: {instrument} not in preferred pairs today but not blocked")

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 9b: TRADE FILTERS ────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════
"""
Two silent background filters applied after strategy consensus.
Neither requires user configuration — they run automatically on every signal.

Filter 1 — 200 EMA Trend Filter
  Only take BUY signals when price is above the 200-period EMA.
  Only take SELL signals when price is below the 200-period EMA.
  Rationale: cuts counter-trend trades, which account for the majority of
  losses in momentum-based strategies like the five we use.

Filter 2 — ATR Volatility Filter
  Only trade when current ATR (14-period) is above the 20-period SMA of ATR.
  Rationale: low-volatility environments produce more false signals and
  smaller moves that don't justify the spread cost. This filter avoids
  trading in dead/ranging markets.

Both filters are logged but never shown to the user — they just quietly
reduce bad trades. The filters are also applied in the backtester so
backtest results reflect live behaviour accurately.
"""

_FILTER_ATR_PERIOD  = 14   # ATR lookback
_FILTER_ATR_SMA     = 20   # SMA of ATR to compare against
_FILTER_TREND_EMA   = 200  # trend EMA period


def apply_trade_filters(signal: str, candles: list, instrument: str) -> tuple:
    """
    Apply both filters to a proposed signal.
    Returns (passed: bool, reasons: list[str])

    Called after consensus_signal() has produced a BUY or SELL.
    If either filter rejects the signal, the trade is skipped.
    """
    if not signal or len(candles) < _FILTER_TREND_EMA + 5:
        return True, []   # not enough data to filter — pass through

    c      = _closes(candles)
    passed = True
    reasons = []

    # ── Filter 1: 200 EMA trend alignment ────────────────────────────────────
    ema200 = _ema(c, _FILTER_TREND_EMA)
    price  = c[-1]
    ema_val = ema200[-1]

    if not _nan(ema_val):
        if signal == "BUY" and price < ema_val:
            passed = False
            reasons.append(
                f"Trend filter: price ({price:.5f}) below 200 EMA ({ema_val:.5f}) "
                f"— skipping BUY in downtrend"
            )
        elif signal == "SELL" and price > ema_val:
            passed = False
            reasons.append(
                f"Trend filter: price ({price:.5f}) above 200 EMA ({ema_val:.5f}) "
                f"— skipping SELL in uptrend"
            )
        else:
            reasons.append(
                f"Trend OK: price {'above' if signal=='BUY' else 'below'} 200 EMA"
            )

    # ── Filter 2: ATR volatility — only run if trend filter passed ────────────
    if passed:
        atr_vals = _atr(candles, _FILTER_ATR_PERIOD)
        atr_now  = atr_vals[-1]

        # SMA of the last _FILTER_ATR_SMA ATR values
        valid_atr = [v for v in atr_vals[-_FILTER_ATR_SMA:] if not _nan(v)]
        if len(valid_atr) >= _FILTER_ATR_SMA // 2 and not _nan(atr_now):
            atr_sma = sum(valid_atr) / len(valid_atr)
            if atr_sma < 1e-8: pass  # Guard: skip filter if ATR SMA is near-zero (extremely low-vol pair)
            elif atr_now < atr_sma * 0.85:   # at least 85% of average volatility
                passed = False
                reasons.append(
                    f"Volatility filter: ATR ({atr_now:.5f}) below average "
                    f"({atr_sma:.5f}) — market too quiet, skipping"
                )
            else:
                reasons.append(
                    f"Volatility OK: ATR ({atr_now:.5f}) >= threshold ({atr_sma:.5f})"
                )

    return passed, reasons


def filtered_signal(consensus: dict, candles: list, instrument: str) -> dict:
    """
    Wraps consensus_signal output with the two filters.
    Returns the same dict shape as consensus_signal() — callers need no changes
    except to call this instead of using the consensus result directly.
    """
    sig = consensus.get("signal")
    if not sig:
        return consensus   # nothing to filter

    passed, filter_reasons = apply_trade_filters(sig, candles, instrument)

    if not passed:
        for r in filter_reasons:
            log.debug(f"[{instrument}] {r}")
        # Return a copy with signal nulled — preserves score/sl/tp for logging
        return {**consensus, "signal": None,
                "reasons": consensus.get("reasons", []) + filter_reasons,
                "filtered": True}

    for r in filter_reasons:
        log.debug(f"[{instrument}] {r}")

    return {**consensus, "filtered": False}


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 10: BACKTESTER ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _estimated_spread_pips(instrument: str) -> float:
    """
    Rough typical retail spread estimate, in pips, used only by the
    backtester so results aren't unrealistically optimistic (live trading
    obviously pays the real spread automatically via OANDA's bid/ask
    prices — see place_market_order). These are ballpark figures for
    normal market conditions, not a precise cost model; real spreads widen
    around news and in low-liquidity sessions. Toggle with
    BACKTEST_SPREAD_ENABLED in the config if you want a pure signal-only
    backtest with no cost assumptions.
    """
    if "XAU" in instrument:
        return 3.0
    base, _, quote = instrument.partition("_")
    if base == "USD" or quote == "USD":
        return 1.2  # majors
    return 1.8      # cross pairs (EUR_GBP, EUR_JPY) — typically wider

def run_backtest(instruments=None, granularity=None, candle_count=None, initial_balance=10_000.0):
    instruments  = instruments  or CFG["INSTRUMENTS"]
    granularity  = granularity  or CFG["BACKTEST_GRANULARITY"]
    candle_count = candle_count or CFG["BACKTEST_CANDLES"]
    spread_on    = CFG.get("BACKTEST_SPREAD_ENABLED", True)
    client       = OandaClient()
    results={}; all_trades=[]
    for instrument in instruments:
        log.info(f"Backtesting {instrument}…")
        try: candles=client.get_candles(instrument,granularity,candle_count)
        except Exception as e: log.error(f"{instrument}: {e}"); continue
        if len(candles)<60: continue
        trades=[]; balance=initial_balance; equity=[balance]; pip=_pip_size(instrument)
        spread_pips = _estimated_spread_pips(instrument) if spread_on else 0.0
        for i in range(60,len(candles)):
            window=candles[:i]; current=candles[i]
            sr=run_all_strategies(window,instrument)
            con=consensus_signal(sr,CFG["STRATEGY_WEIGHTS"],threshold=CFG.get("CONSENSUS_THRESHOLD",0.45))
            con=filtered_signal(con,window,instrument)
            if not con["signal"]: equity.append(balance); continue
            sl_p=con["sl_pips"]; tp_p=con["tp_pips"]; entry=current["open"]
            if con["signal"]=="BUY":
                sl_pr=entry-sl_p*pip; tp_pr=entry+tp_p*pip
                if current["low"]<=sl_pr:   pl=-sl_p*pip; outcome="SL"
                elif current["high"]>=tp_pr: pl=tp_p*pip;  outcome="TP"
                else:                         pl=current["close"]-entry; outcome="close"
            else:
                sl_pr=entry+sl_p*pip; tp_pr=entry-tp_p*pip
                if current["high"]>=sl_pr:  pl=-sl_p*pip; outcome="SL"
                elif current["low"]<=tp_pr: pl=tp_p*pip;  outcome="TP"
                else:                        pl=entry-current["close"]; outcome="close"
            pl -= spread_pips * pip  # round-trip cost of crossing the spread
            # Same sizing formula live trading uses — see Section 7b. Cross
            # pairs (EUR_GBP, EUR_JPY) use the documented fallback rate here
            # since backtesting doesn't have a second instrument's aligned
            # live price to convert with.
            units=calculate_position_units(instrument, sl_p, CFG["RISK_PER_TRADE_PCT"], balance)
            if not units: equity.append(balance); continue
            pl_m=pl*units; balance=max(0,balance+pl_m)
            trades.append({"instrument":instrument,"time":current["time"],
                "signal":con["signal"],"entry":round(entry,5),"outcome":outcome,
                "pl_pips":round(pl/pip,1),"pl_money":round(pl_m,2),"balance":round(balance,2)})
            all_trades.append(trades[-1]); equity.append(balance)
        if not trades: continue
        wins=[t for t in trades if t["pl_pips"]>0]; losses=[t for t in trades if t["pl_pips"]<=0]
        net=sum(t["pl_money"] for t in trades)
        aw=sum(t["pl_pips"] for t in wins)/len(wins) if wins else 0
        al=sum(t["pl_pips"] for t in losses)/len(losses) if losses else 0
        pf=(abs(aw*len(wins))/abs(al*len(losses))) if losses and al else 999
        pk=mx=0.0
        for v in equity:
            pk=max(pk,v); mx=max(mx,(pk-v)/pk) if pk else 0
        results[instrument]={
            "instrument":instrument,"trades":len(trades),"wins":len(wins),"losses":len(losses),
            "win_rate":round(len(wins)/len(trades)*100,1),"net_pl":round(net,2),
            "net_pl_pct":round(net/initial_balance*100,2),"avg_win_pips":round(aw,1),
            "avg_loss_pips":round(al,1),"profit_factor":round(pf,2),
            "max_drawdown":round(mx*100,2),"final_balance":round(equity[-1],2)}
        log.info(f"  {instrument}: {len(trades)} trades | WR={results[instrument]['win_rate']}% | net={net:+.2f}")
    wins_all=[t for t in all_trades if t["pl_pips"]>0]
    summary={"total_trades":len(all_trades),"win_rate":round(len(wins_all)/len(all_trades)*100,1) if all_trades else 0,
             "net_pl":round(sum(t["pl_money"] for t in all_trades),2),"initial_balance":initial_balance,
             "final_balance":round(initial_balance+sum(t["pl_money"] for t in all_trades),2)}
    output={"summary":summary,"by_instrument":results,"trades":all_trades}
    ts=datetime.now().strftime("%Y%m%d_%H%M%S")
    bt_dir=DATA_DIR/"backtest_results"; bt_dir.mkdir(exist_ok=True)
    _atomic_write_json(bt_dir/f"backtest_{ts}.json", output)
    return output

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 11: DASHBOARD ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# The dashboard is bound to 127.0.0.1 only, so it's not reachable from the
# network — but any webpage open in the same browser (a different tab, a
# malicious ad, etc.) CAN still silently send POST requests to it, since
# browsers don't block cross-origin requests, only cross-origin *reads* of
# the response. A page with no relation to Tradalgo could, for example,
# POST to /api/pause or /api/licence/deactivate without you knowing.
#
# This token closes that gap: it's generated fresh per process (never
# written to disk), injected into the dashboard's own HTML as a JS
# variable, and required as a header on every mutating (POST) /api/ call.
# A third-party page has no way to read it (browsers enforce same-origin
# on page content), so it can't forge a valid request even though it can
# still technically reach the server.
_LOCAL_API_TOKEN = secrets.token_hex(16)

@app.before_request
def _require_local_api_token():
    if freq.method == "POST" and freq.path.startswith("/api/"):
        if freq.headers.get("X-Tradalgo-Token") != _LOCAL_API_TOKEN:
            return jsonify({"error": "missing or invalid local API token"}), 403

_price_cache: dict = {}
_price_lock        = threading.Lock()
_sse_listeners     = []
_sse_lock          = threading.Lock()

def _price_broadcast_loop():
    while True:
        try:
            prices = OandaClient().get_prices(CFG["INSTRUMENTS"])
            with _price_lock: _price_cache.update(prices)
            msg=f"data: {json.dumps(prices)}\n\n"
            with _sse_lock:
                dead=[]
                for q in _sse_listeners:
                    try: q.put_nowait(msg)
                    except (queue.Full, Exception): dead.append(q)
                for q in dead: _sse_listeners.remove(q)
        except Exception as e: log.debug(f"Price broadcast: {e}")
        time.sleep(5)

# ── Dashboard HTML (main) ─────────────────────────────────────────────────────
_MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradalgo</title>
<style>
:root{
  --bg:#0b0f19;--bg2:#111726;--bg3:#182033;--bg4:#202b42;
  --border:#1e293d;--border-light:rgba(255,255,255,.08);
  --text:#f8fafc;--text-sub:#cbd5e1;--muted:#8493a8;
  --green:#10b981;--red:#ef4444;--blue:#3b82f6;--blue-btn:#2563eb;
  --gold:#f59e0b;--purple:#8b5cf6;--radius:8px;
  --font-mono:ui-monospace,'SF Mono','Cascadia Mono','JetBrains Mono',Consolas,monospace;
  --ease:cubic-bezier(.4,0,.2,1);
}
*{box-sizing:border-box;margin:0;padding:0}
button{font-family:inherit}
html,body{height:100%;background:var(--bg);color:var(--text);font:13px/1.5 system-ui,sans-serif;overflow:hidden}

/* ── Transitions on everything interactive ── */
a,button,.pair-btn,.tf,.badge{transition:background .18s var(--ease),color .18s var(--ease),border-color .18s var(--ease),opacity .18s var(--ease);}

/* ── Header ── */
header{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 16px;height:44px;display:flex;align-items:center;gap:6px;
  position:sticky;top:0;z-index:100;
}
.logo{font-size:16px;font-weight:700;letter-spacing:.3px;flex-shrink:0}
.logo span{color:var(--gold)}
.badge{
  background:var(--bg3);border:1px solid var(--border);
  border-radius:20px;padding:2px 9px;font-size:11px;color:var(--muted);
  white-space:nowrap;flex-shrink:0;
}
.badge.active-sess{border-color:#2fbf7155;color:var(--green)}
.dot{
  width:8px;height:8px;border-radius:50%;background:var(--green);
  flex-shrink:0;box-shadow:0 0 6px var(--green);
  animation:pulse 2.5s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.85)}}
nav{display:flex;gap:2px;flex-shrink:0}
nav a{
  color:var(--muted);text-decoration:none;padding:5px 12px;
  border-radius:6px;font-size:12px;font-weight:500;white-space:nowrap;
  position:relative;
  transition:color .2s var(--ease),background .2s var(--ease),transform .15s var(--ease);
}
nav a::after{
  content:'';position:absolute;bottom:-2px;left:50%;right:50%;
  height:2px;background:var(--blue);border-radius:1px;
  transition:left .25s var(--ease),right .25s var(--ease);
}
nav a:hover{background:var(--bg3);color:var(--text)}
nav a:hover::after,nav a.active::after{left:10px;right:10px}
nav a.active{background:var(--bg4);color:var(--text)}
.hspace{flex:1;min-width:4px}
#bal{font-size:13px;font-weight:600;font-family:var(--font-mono);font-variant-numeric:tabular-nums;flex-shrink:0}

/* Stat row hover */
.stat{transition:padding-left .15s var(--ease),background .15s var(--ease)}
.stat:hover{background:var(--bg3);border-radius:6px;padding-left:8px}

/* ── Layout ── */
.layout{
  display:grid;grid-template-columns:210px 1fr 265px;
  height:calc(100vh - 44px);
}
.col{overflow-y:auto;padding:10px 8px}
.left{background:var(--bg2);border-right:1px solid var(--border)}
.mid{display:flex;flex-direction:column;background:var(--bg);overflow:hidden}
.right{background:var(--bg2);border-left:1px solid var(--border)}
.right-panel{display:flex;flex-direction:column;overflow:hidden}

/* ── Section labels ── */
.sec{
  font-size:9px;text-transform:uppercase;letter-spacing:1px;
  color:var(--muted);margin:12px 4px 5px;font-weight:600;
}

/* ── Pair buttons ── */
.pair-btn{
  width:100%;display:flex;justify-content:space-between;align-items:center;
  background:none;border:none;color:var(--text);padding:6px 8px;
  border-radius:6px;cursor:pointer;font-size:12px;text-align:left;
  border-left:2px solid transparent;
  animation:fadeSlideIn .25s var(--ease) both;
}
.pair-btn:hover{background:var(--bg3)}
.pair-btn.active{
  background:var(--bg4);border-left-color:var(--blue);
  color:var(--text);
}
@keyframes fadeSlideIn{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}

.pair-px{font-size:10px;color:var(--muted);font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.sig-buy{font-size:9px;padding:1px 5px;border-radius:3px;background:#26995a22;color:#2fbf71;font-weight:700}
.sig-sell{font-size:9px;padding:1px 5px;border-radius:3px;background:#c33d4122;color:#e5484d;font-weight:700}
.sig-none{font-size:9px;color:var(--muted)}

/* ── Chart bar ── */
.chart-bar{
  padding:9px 14px;background:var(--bg2);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;flex-shrink:0;
}
#chart-title{font-size:15px;font-weight:600;letter-spacing:.3px}
#chart-price{
  font-size:14px;font-family:var(--font-mono);font-variant-numeric:tabular-nums;
  transition:color .3s var(--ease);
}
#chart-chg{
  font-size:11px;padding:2px 8px;border-radius:4px;
  font-variant-numeric:tabular-nums;
  transition:background .3s var(--ease),color .3s var(--ease);
}

/* ── Timeframe buttons ── */
.tf{
  padding:3px 9px;border-radius:5px;border:1px solid var(--border);
  background:none;color:var(--muted);cursor:pointer;font-size:11px;font-weight:500;
  font-family:inherit;line-height:1.4;-webkit-appearance:none;appearance:none;
}
.tf.active,.tf:hover{background:var(--blue);color:#fff;border-color:var(--blue)}

/* ── Chart wrap ── */
#chart-wrap{
  flex:1;position:relative;min-height:0;
  transition:opacity .3s var(--ease);
}
#chart-wrap.loading{opacity:.4}

/* ── Status bar ── */
#statusbar{
  padding:4px 14px;background:#0a0d17;font-size:10px;color:var(--muted);
  border-top:1px solid var(--border);flex-shrink:0;
  display:flex;align-items:center;gap:6px;
  transition:color .3s var(--ease);
}
#statusbar.ok{color:#2fbf7144}
#statusbar.err{color:#e5484d88}
.status-dot{width:5px;height:5px;border-radius:50%;background:currentColor;flex-shrink:0}

/* ── Stat rows ── */
.stat{
  display:flex;justify-content:space-between;align-items:center;
  padding:6px 4px;border-bottom:1px solid var(--border);font-size:12px;
}
.stat:last-child{border:none}
.sv{
  font-weight:600;font-family:var(--font-mono);font-variant-numeric:tabular-nums;
  transition:color .4s var(--ease);
}
.green{color:var(--green)}.red{color:var(--red)}.blue{color:var(--blue)}

/* ── Trade cards ── */
.tc{
  background:var(--bg3);border-radius:var(--radius);padding:10px;margin-bottom:8px;
  border-left:3px solid var(--border);
  animation:cardIn .3s var(--ease) both;
  transition:transform .18s var(--ease),box-shadow .18s var(--ease);
}
.tc:hover{transform:translateX(2px);box-shadow:-2px 0 12px rgba(59,130,246,.15)}
.tc.buy{border-left-color:var(--green)}
.tc.sell{border-left-color:var(--red)}
@keyframes cardIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.tc-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.tc-pair{font-weight:600;font-size:13px}
.tc-dir{font-size:10px;padding:2px 6px;border-radius:4px;font-weight:700}
.dir-buy{background:#26995a22;color:#2fbf71}
.dir-sell{background:#c33d4122;color:#e5484d}
.tc-row{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);padding:1px 0}
.tc-row span:last-child{font-family:var(--font-mono)}
.tc-pl{font-size:13px;font-weight:700;font-family:var(--font-mono);font-variant-numeric:tabular-nums}

/* ── Page fade ── */
@keyframes pageFade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.layout{animation:pageFade .35s var(--ease)}

/* ── Tooltip ── */
[data-tip]{position:relative;cursor:default}
[data-tip]:hover::after{
  content:attr(data-tip);
  position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);
  background:#1a2035;border:1px solid var(--border);color:var(--text);
  padding:4px 8px;border-radius:5px;font-size:10px;white-space:nowrap;z-index:200;
  pointer-events:none;animation:ttFade .15s var(--ease);
}
@keyframes ttFade{from{opacity:0;transform:translateX(-50%) translateY(3px)}to{opacity:1;transform:translateX(-50%)}}
/* Tooltips in the chart toolbar have no room above them (flush against
   the clipped top edge of .mid), so open them downward instead. */
.chart-bar [data-tip]:hover::after{bottom:auto;top:calc(100% + 6px)}

/* ── Number flash animations ── */
@keyframes flashGreen{0%,100%{color:inherit}50%{color:var(--green)}}
@keyframes flashRed{0%,100%{color:inherit}50%{color:var(--red)}}
.flash-green{animation:flashGreen .6s var(--ease)}
.flash-red{animation:flashRed .6s var(--ease)}

/* ── Scrollbar ── */
/* ── Right panel tabs ── */
.right-panel{display:flex;flex-direction:column;background:var(--bg2);
  border-left:1px solid var(--border);overflow:hidden;height:100%}
.tab-bar{display:flex;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--bg2)}
.tab-btn{flex:1;padding:10px 0;background:none;border:none;color:var(--muted);
  font-size:12px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;
  transition:color .2s var(--ease),border-color .2s var(--ease)}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--text);border-bottom-color:var(--blue)}
.tab-content{display:none;flex:1;overflow-y:auto;padding:10px 10px;
  animation:tabIn .22s var(--ease)}
.tab-content.active{display:block}
@keyframes tabIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* ── Activity feed ── */
.feed-card{background:var(--bg3);border-radius:var(--radius);padding:11px 12px;margin-bottom:8px;
  border-left:3px solid var(--border);
  animation:feedIn .3s var(--ease) both;
  transition:transform .15s var(--ease)}
.feed-card:hover{transform:translateX(2px)}
.feed-card.open{border-left-color:var(--green)}
.feed-card.close_win{border-left-color:var(--green)}
.feed-card.close_loss{border-left-color:var(--red)}
.feed-card.session{border-left-color:var(--blue)}
.feed-card.info{border-left-color:var(--muted)}
@keyframes feedIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}
.feed-icon{font-size:16px;float:left;margin-right:8px;margin-top:1px;line-height:1}
.feed-title{font-size:12px;font-weight:600;margin-bottom:3px;line-height:1.3}
.feed-body{font-size:11px;color:var(--muted);line-height:1.5;clear:both}
.feed-time{font-size:10px;color:var(--muted);margin-top:5px;opacity:.7}
.feed-empty{color:var(--muted);font-size:11px;padding:20px 4px;text-align:center;
  border:1px dashed var(--border);border-radius:var(--radius);margin-top:4px;line-height:1.6}

/* ── Page transition (nav) ── */
.layout{animation:pageIn .3s var(--ease)}
@keyframes pageIn{from{opacity:0}to{opacity:1}}

/* ── Pause button ── */
#pause-btn{
  padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;
  letter-spacing:.2px;cursor:pointer;border:none;display:inline-flex;align-items:center;
  gap:7px;transition:all .2s cubic-bezier(.4,0,.2,1);flex-shrink:0;
  backdrop-filter:blur(8px);user-select:none;
}
#pause-btn.running{
  background:linear-gradient(135deg, rgba(16,185,129,.14), rgba(5,150,105,.22));
  color:#10b981;border:1px solid rgba(16,185,129,.35);
  box-shadow:0 2px 8px rgba(16,185,129,.12);
}
#pause-btn.running:hover{
  background:linear-gradient(135deg, rgba(16,185,129,.24), rgba(5,150,105,.32));
  border-color:rgba(16,185,129,.55);box-shadow:0 4px 14px rgba(16,185,129,.28);
  transform:translateY(-1px);
}
#pause-btn.paused{
  background:linear-gradient(135deg, rgba(239,68,68,.14), rgba(220,38,38,.22));
  color:#ef4444;border:1px solid rgba(239,68,68,.35);
  box-shadow:0 2px 8px rgba(239,68,68,.12);
  animation:pausePulse 2s cubic-bezier(.4,0,.6,1) infinite;
}
@keyframes pausePulse{
  0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.35)}
  50%{box-shadow:0 0 0 6px rgba(239,68,68,0)}
}
#pause-btn.paused:hover{
  background:linear-gradient(135deg, rgba(239,68,68,.24), rgba(220,38,38,.32));
  border-color:rgba(239,68,68,.55);box-shadow:0 4px 14px rgba(239,68,68,.28);
  animation:none;transform:translateY(-1px);
}

/* ── Onboarding overlay ── */
/* Default state: completely invisible and non-interactive.
   Nothing renders or blocks clicks until #ob-overlay has .active. */
#ob-overlay{position:fixed;inset:0;z-index:9999;pointer-events:none;
  opacity:0;visibility:hidden;transition:opacity .3s var(--ease),visibility 0s linear .3s}
#ob-overlay.active{pointer-events:all;opacity:1;visibility:visible;
  transition:opacity .3s var(--ease),visibility 0s linear 0s}
#ob-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.7)}
/* Spotlight box-shadow is what paints the dark dim — it must NEVER
   render unless the overlay is active, otherwise it darkens the
   whole screen the instant width/height/top/left are set by JS. */
#ob-spotlight{position:absolute;border-radius:var(--radius);
  box-shadow:none;transition:all .4s var(--ease);pointer-events:none;
  border:2px solid transparent;z-index:10000}
#ob-overlay.active #ob-spotlight{
  box-shadow:0 0 0 9999px rgba(0,0,0,.72);
  border-color:var(--blue)}
#ob-card{position:absolute;background:var(--bg2);border:1px solid rgba(255,255,255,.12);
  border-radius:12px;padding:20px 22px;width:320px;
  box-shadow:0 20px 50px rgba(0,0,0,.75);z-index:10001;
  transition:all .35s var(--ease);opacity:0;transform:translateY(8px)}
#ob-overlay.active #ob-card{opacity:1;transform:none}
.ob-step{font-size:10px;text-transform:uppercase;letter-spacing:.8px;
  color:var(--blue);font-weight:600;margin-bottom:6px}
.ob-title{font-size:15px;font-weight:700;margin-bottom:8px;color:var(--text)}
.ob-body{font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:16px}
.ob-actions{display:flex;align-items:center;justify-content:space-between}
.ob-skip{background:none;border:none;color:var(--muted);font-size:11px;
  cursor:pointer;padding:0;text-decoration:underline}
.ob-skip:hover{color:var(--text)}
.ob-next{padding:8px 20px;background:var(--blue-btn);color:#fff;border:1px solid rgba(59,130,246,.4);
  border-radius:var(--radius);font-size:12px;font-weight:600;cursor:pointer;
  box-shadow:0 4px 14px rgba(37,99,235,.3);
  transition:all .18s var(--ease)}
.ob-next:hover{background:#1d4ed8;box-shadow:0 6px 18px rgba(37,99,235,.45);transform:translateY(-1px)}
.ob-dots{display:flex;gap:5px;align-items:center}
.ob-dot{width:6px;height:6px;border-radius:50%;background:var(--border);
  transition:background .2s var(--ease)}
.ob-dot.active{background:var(--blue)}

::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}

/* ── No-trades placeholder ── */
.no-trades{
  color:var(--muted);font-size:11px;padding:16px 4px;
  text-align:center;border:1px dashed var(--border);
  border-radius:var(--radius);margin-top:4px;
}

/* ── Micro-interactions: Button depress on click ── */
button:active, .btn:active, .pair-btn:active, .tf:active, nav a:active, .tab-btn:active, .btn-primary:active, .btn-secondary:active{
  transform:scale(0.95) translateY(1px) !important;
  transition:transform 0.08s ease !important;
}

/* ── Vector SVG Icon for Pause / Play ── */
#pause-icon{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;position:relative}
#pause-icon svg{display:block;transition:transform .2s ease,opacity .2s ease}
#pause-btn.running .icon-pause{display:block;opacity:1}
#pause-btn.running .icon-play{display:none;opacity:0}
#pause-btn.paused .icon-pause{display:none;opacity:0}
#pause-btn.paused .icon-play{display:block;opacity:1}

/* ── Micro-interactions: Spring physics toggles ── */
.toggle-switch input:checked + .slider::before,
.switch input:checked + .slider::before {
  transform:translateX(18px) scale(1.1);
  transition:transform 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.slider::before {
  transition:transform 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
}

/* ── Animated Sidebar & Pair Buttons (Left-border slide) ── */
.pair-btn{
  width:100%;display:flex;justify-content:space-between;align-items:center;
  background:none;border:none;color:var(--text);padding:6px 8px 6px 10px;
  border-radius:6px;cursor:pointer;font-size:12px;text-align:left;
  position:relative;overflow:hidden;
  transition:background .2s ease, transform .15s ease, padding-left .2s ease;
}
.pair-btn::before{
  content:'';position:absolute;top:0;left:0;bottom:0;width:3px;
  background:var(--blue);transform:scaleY(0);transform-origin:center;
  transition:transform 0.25s cubic-bezier(0.4, 0, 0.2, 1), background-color 0.25s ease;
}
.pair-btn:hover::before{
  transform:scaleY(1);
}
.pair-btn:hover{
  background:var(--bg3);
  padding-left:14px;
}
.pair-btn.active{
  background:var(--bg4);
  color:var(--text);
  padding-left:14px;
}
.pair-btn.active::before{
  transform:scaleY(1);
  background:var(--gold);
}
@keyframes sidebarSlideIn{
  from{opacity:0;transform:translateX(-16px)}
  to{opacity:1;transform:translateX(0)}
}

/* ── Animated Price Ticks ── */
@keyframes tickUpFlash{
  0%{background:rgba(34, 197, 94, 0.35);color:#2fbf71;text-shadow:0 0 8px rgba(34, 197, 94, 0.6);}
  100%{background:transparent;color:inherit;}
}
@keyframes tickDownFlash{
  0%{background:rgba(239, 68, 68, 0.35);color:#e5484d;text-shadow:0 0 8px rgba(239, 68, 68, 0.6);}
  100%{background:transparent;color:inherit;}
}
.tick-up{animation:tickUpFlash 0.75s cubic-bezier(0.16, 1, 0.3, 1);border-radius:4px;}
.tick-down{animation:tickDownFlash 0.75s cubic-bezier(0.16, 1, 0.3, 1);border-radius:4px;}

/* ── Chart Loading Shimmer ── */
.chart-skeleton{
  position:absolute;inset:0;z-index:10;background:var(--bg);
  display:flex;flex-direction:column;padding:20px;gap:16px;
  pointer-events:none;transition:opacity 0.35s ease;
}
.chart-skeleton.hidden{opacity:0;pointer-events:none;}
.skeleton-header{display:flex;gap:12px;}
.skeleton-candles{display:flex;align-items:flex-end;justify-content:space-around;flex:1;padding-bottom:10px;}
.skeleton-line, .skeleton-bar{
  background:linear-gradient(90deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.09) 50%, rgba(255,255,255,0.03) 100%);
  background-size:200% 100%;
  animation:shimmer 1.5s infinite linear;border-radius:4px;
}
@keyframes shimmer{0%{background-position:-200% 0;}100%{background-position:200% 0;}}

/* ── Trade Card Entrance Animation ── */
.tc.trade-card-enter{
  animation:tradeSlideInRight 0.38s cubic-bezier(0.16, 1, 0.3, 1) both;
}
@keyframes tradeSlideInRight{
  from{opacity:0;transform:translateX(36px) scale(0.96);}
  to{opacity:1;transform:translateX(0) scale(1);}
}

/* ── Smooth Page Transitions (Crossfade + Slight Upward Slide) ── */
.page-container, .layout{
  animation:pageCrossfadeSlide 0.38s cubic-bezier(0.16, 1, 0.3, 1) both;
}
.page-exit{
  animation:pageExitSlide 0.18s cubic-bezier(0.4, 0, 1, 1) both !important;
}
@keyframes pageCrossfadeSlide{
  from{opacity:0;transform:translateY(12px);}
  to{opacity:1;transform:translateY(0);}
}
@keyframes pageExitSlide{
  from{opacity:1;transform:translateY(0);}
  to{opacity:0;transform:translateY(-8px);}
}

</style>
</head>
<body>

<header>
  <div class="dot" id="conn-dot"></div>
  <div class="logo">Trad<span>algo</span></div>
  <div class="hspace"></div>
  <nav>
    <a href="/" class="active">Live</a>
    <a href="/backtest">Backtest</a>
    <a href="/performance">Performance</a>
    <a href="/settings">Settings</a>
  </nav>
  <div class="hspace"></div>
  <div class="badge" id="sess-badge">—</div>
  <div class="badge" id="env-badge">{{ env }}</div>
  <button id="env-toggle-btn" onclick="showEnvSwitchModal()" title="Switch between Practice and Live trading" style="margin-left:8px;padding:5px 12px;border-radius:20px;border:1.5px solid;font-size:11px;font-weight:800;cursor:pointer;transition:all .2s;letter-spacing:0.5px;"></button>
  <button id="pause-btn" class="running" onclick="togglePause()" title="Click to pause — no new trades will open">
    <span id="pause-icon">
      <svg class="icon-pause" viewBox="0 0 14 14" width="12" height="12" fill="currentColor">
        <rect x="2" y="1.5" width="3.5" height="11" rx="1.2"/>
        <rect x="8.5" y="1.5" width="3.5" height="11" rx="1.2"/>
      </svg>
      <svg class="icon-play" viewBox="0 0 14 14" width="12" height="12" fill="currentColor">
        <path d="M3.5 1.8 C3.5 1.1 4.3 0.6 4.9 1.0 L12.3 5.7 C12.9 6.1 12.9 7.0 12.3 7.4 L4.9 12.1 C4.3 12.5 3.5 12.0 3.5 11.3 Z"/>
      </svg>
    </span>
    <span id="pause-label">Running</span>
  </button>
  <div id="bal">—</div>
</header>

<!-- ═══ RISK DISCLAIMER MODAL ═══ -->
<div id="risk-modal-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:99999;align-items:center;justify-content:center;backdrop-filter:blur(4px);">
  <div style="background:#111827;border:1px solid #1e2d45;border-radius:16px;max-width:520px;width:90%;padding:36px;box-shadow:0 25px 60px rgba(0,0,0,0.7);">
    <div style="text-align:center;margin-bottom:20px;">
      <div style="font-size:2.5rem;margin-bottom:10px;">&#9888;</div>
      <h2 style="font-size:1.3rem;font-weight:800;color:#fff;margin-bottom:8px;">Risk Disclosure</h2>
      <p style="color:#94A3B8;font-size:0.9rem;line-height:1.6;">Automated trading involves <strong style="color:#ef4444;">significant financial risk</strong>. You may lose some or all of your invested capital. Tradalgo is a software tool, not financial advice. Past performance does not guarantee future results. Only trade with money you can afford to lose entirely. Never risk funds needed for living expenses.</p>
    </div>
    <div style="background:#0b0e1a;border:1px solid #1e2d45;border-radius:8px;padding:14px;margin-bottom:20px;font-size:0.82rem;color:#64748B;line-height:1.7;">
      • Forex and CFD trading carries a high level of risk<br>
      • Leverage can work against you as well as for you<br>
      • This software does not guarantee profitable trades<br>
      • You are solely responsible for your trading decisions
    </div>
    <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:20px;color:#e2e8f0;font-size:0.88rem;">
      <input type="checkbox" id="risk-accept-cb" style="width:16px;height:16px;accent-color:#00f2fe;cursor:pointer;">
      I understand that automated trading involves significant financial risk and I accept full responsibility for my trading decisions.
    </label>
    <button id="risk-accept-btn" onclick="acceptRiskDisclaimer()" disabled style="width:100%;padding:14px;background:linear-gradient(135deg,#00f2fe,#00a8ff);color:#050b14;border:none;border-radius:8px;font-weight:800;font-size:14px;cursor:not-allowed;opacity:0.5;transition:all .2s;">I Accept &mdash; Continue to Dashboard</button>
  </div>
</div>
<script>
(function(){
  // Show risk disclaimer once per browser
  if(!localStorage.getItem('tradalgo_risk_accepted')){
    var overlay = document.getElementById('risk-modal-overlay');
    if(overlay){ overlay.style.display='flex'; document.body.style.overflow='hidden'; }
  }
  var cb = document.getElementById('risk-accept-cb');
  var btn = document.getElementById('risk-accept-btn');
  if(cb && btn){
    cb.addEventListener('change', function(){
      btn.disabled = !cb.checked;
      btn.style.opacity = cb.checked ? '1' : '0.5';
      btn.style.cursor = cb.checked ? 'pointer' : 'not-allowed';
    });
  }
})();
function acceptRiskDisclaimer(){
  localStorage.setItem('tradalgo_risk_accepted','1');
  var overlay = document.getElementById('risk-modal-overlay');
  if(overlay){ overlay.style.display='none'; document.body.style.overflow=''; }
}
</script>

<!-- ═══ ENV SWITCH MODAL ═══ -->
<div id="env-switch-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:99998;align-items:center;justify-content:center;backdrop-filter:blur(4px);">
  <div style="background:#111827;border:1px solid #ef4444;border-radius:16px;max-width:460px;width:90%;padding:32px;box-shadow:0 25px 60px rgba(239,68,68,0.3);">
    <div style="text-align:center;margin-bottom:20px;">
      <div style="font-size:2.5rem;margin-bottom:8px;">&#128308;</div>
      <h2 style="font-size:1.2rem;font-weight:800;color:#fff;margin-bottom:10px;" id="env-modal-title">Switch to LIVE Trading?</h2>
      <p style="color:#94A3B8;font-size:0.88rem;line-height:1.6;" id="env-modal-body">You are about to switch to a <strong style="color:#ef4444;">REAL MONEY</strong> account. All trades will use real funds from your live OANDA account. This cannot be undone without restarting the bot.</p>
    </div>
    <div style="display:flex;gap:12px;">
      <button onclick="closeEnvModal()" style="flex:1;padding:12px;background:#1a2035;border:1px solid #1e2d45;color:#94A3B8;border-radius:8px;font-weight:700;cursor:pointer;">Cancel</button>
      <button id="env-confirm-btn" onclick="confirmEnvSwitch()" style="flex:1;padding:12px;background:#ef4444;border:none;color:#fff;border-radius:8px;font-weight:800;cursor:pointer;">Confirm Switch</button>
    </div>
  </div>
</div>
<script>
var _pendingEnv = null;
function _updateEnvToggle(env){
  var btn = document.getElementById('env-toggle-btn');
  if(!btn) return;
  if(env==='live'){
    btn.textContent='\u25CF LIVE';
    btn.style.color='#ef4444'; btn.style.borderColor='#ef4444';
    btn.style.background='rgba(239,68,68,0.12)';
  } else {
    btn.textContent='\u25CF PRACTICE';
    btn.style.color='#10b981'; btn.style.borderColor='#10b981';
    btn.style.background='rgba(16,185,129,0.12)';
  }
}
function showEnvSwitchModal(){
  var curEnv = document.getElementById('env-badge') ? document.getElementById('env-badge').textContent.toLowerCase() : 'practice';
  _pendingEnv = curEnv==='live' ? 'practice' : 'live';
  var title = document.getElementById('env-modal-title');
  var body = document.getElementById('env-modal-body');
  var confirmBtn = document.getElementById('env-confirm-btn');
  if(_pendingEnv==='live'){
    title.textContent='Switch to LIVE Trading?';
    body.innerHTML='You are about to switch to a <strong style="color:#ef4444;">REAL MONEY</strong> account. All trades will use real funds from your live OANDA account.';
    confirmBtn.style.background='#ef4444';
    confirmBtn.textContent='Yes, Switch to LIVE';
  } else {
    title.textContent='Switch to PRACTICE Mode?';
    body.innerHTML='You are about to switch back to a <strong style="color:#10b981;">demo account</strong>. No real funds will be used.';
    confirmBtn.style.background='#10b981';
    confirmBtn.textContent='Switch to Practice';
  }
  var modal = document.getElementById('env-switch-modal');
  if(modal){ modal.style.display='flex'; }
}
function closeEnvModal(){
  var modal = document.getElementById('env-switch-modal');
  if(modal){ modal.style.display='none'; }
  _pendingEnv = null;
}
async function confirmEnvSwitch(){
  if(!_pendingEnv) return;
  closeEnvModal();
  try{
    const r = await fetch('/api/env-switch',{method:'POST',headers:{'Content-Type':'application/json','X-Tradalgo-Token':API_TOKEN},body:JSON.stringify({env:_pendingEnv})});
    const d = await r.json();
    if(d.status==='ok'){
      _updateEnvToggle(d.env);
      var badge = document.getElementById('env-badge');
      if(badge) badge.textContent = d.env.charAt(0).toUpperCase()+d.env.slice(1);
      alert('\u2705 Switched to ' + d.env.toUpperCase() + '. Please restart Tradalgo to reconnect to the new API endpoint.');
    }
  } catch(e){ alert('Failed to switch environment: '+e); }
}
document.addEventListener('DOMContentLoaded',function(){
  var badge = document.getElementById('env-badge');
  if(badge){ _updateEnvToggle(badge.textContent.toLowerCase()); }
});
</script>
<div id="oanda-warning-banner" style="display:none;margin:12px 16px 0 16px;background:rgba(239,68,68,0.15);border:1px solid #ef4444;border-radius:8px;padding:12px 16px;color:#f8fafc;font-size:12px;align-items:center;justify-content:space-between;z-index:10;">
  <div style="display:flex;align-items:center;gap:10px;">
    <span style="font-size:16px;">⚠️</span>
    <span><strong>OANDA Connection Alert:</strong> <span id="oanda-warning-text">Check your OANDA API Key & Account ID in Settings.</span></span>
  </div>
  <a href="/settings" style="background:#ef4444;color:#fff;padding:6px 14px;border-radius:6px;font-weight:700;text-decoration:none;font-size:11px;">Go to Settings &rarr;</a>
</div>

<div class="layout">

  <!-- LEFT: instruments -->
  <div class="col left">
    <div class="sec">Instruments</div>
    <div id="pair-list"></div>
  </div>

  <!-- MID: chart -->
  <div class="mid">
    <div class="chart-bar">
      <div id="chart-title">EUR/USD</div>
      <div id="chart-price">—</div>
      <div id="chart-chg"></div>
      <div id="chart-spread" style="display:flex;gap:4px;align-items:center;margin-left:4px" data-tip="Bid / Ask spread"></div>
      <div style="flex:1"></div>
      <button class="tf" data-tf="M5"  data-tip="5 minute">M5</button>
      <button class="tf" data-tf="M15" data-tip="15 minute">M15</button>
      <button class="tf active" data-tf="H1" data-tip="1 hour">H1</button>
      <button class="tf" data-tf="H4"  data-tip="4 hour">H4</button>
      <button class="tf" data-tf="D"   data-tip="Daily">D</button>
    </div>
    <div id="chart-wrap">
    <div id="chart-skeleton" class="chart-skeleton">
      <div class="skeleton-header">
        <div class="skeleton-line" style="width: 120px; height: 16px;"></div>
        <div class="skeleton-line" style="width: 80px; height: 16px;"></div>
      </div>
      <div class="skeleton-candles">
        <div class="skeleton-bar" style="height: 60%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 80%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 45%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 90%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 70%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 55%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 85%; width: 14px;"></div>
        <div class="skeleton-bar" style="height: 65%; width: 14px;"></div>
      </div>
    </div>
</div>
    <div id="statusbar"><div class="status-dot"></div><span id="status-text">Connecting…</span></div>
  </div>

  <!-- RIGHT: tabbed panel -->
  <div class="right-panel">
    <!-- Tab bar -->
    <div class="tab-bar">
      <button class="tab-btn active" id="tab-account" onclick="switchTab('account')">Account</button>
      <button class="tab-btn"        id="tab-activity" onclick="switchTab('activity')">Activity</button>
    </div>

    <!-- Account tab -->
    <div class="tab-content active" id="panel-account">
      <div class="sec">Overview</div>
      <div class="stat"><span>Balance</span><span class="sv" id="a-bal">—</span></div>
      <div class="stat"><span>Unrealised P&amp;L</span><span class="sv" id="a-upl">—</span></div>
      <div class="stat"><span>Open Trades</span><span class="sv" id="a-ot">—</span></div>
      <div class="stat"><span>Session</span><span class="sv" id="a-sess">—</span></div>

      <!-- News status panel -->
      <div id="news-panel" style="margin-top:10px;display:none">
        <div style="background:var(--bg3);border-radius:var(--radius);padding:9px 11px;
          border-left:3px solid var(--border)" id="news-card">
        </div>
      </div>

      <div class="sec" style="margin-top:14px">Open Trades</div>
      <div id="trades-panel">
        <div class="no-trades">No open trades</div>
      </div>

      <div class="sec" style="margin-top:14px">Today</div>
      <div class="stat"><span>Trades</span><span class="sv" id="t-trades">0</span></div>
      <div class="stat"><span>Wins</span><span class="sv green" id="t-wins">0</span></div>
      <div class="stat"><span>P&amp;L</span><span class="sv" id="t-pl">$0.00</span></div>

      <div id="risk-guard-panel" style="margin-top:14px;display:none">
        <div class="sec">Safety Limit</div>
        <div style="font-size:11px;color:var(--muted);margin-bottom:5px">
          Account drawdown: <span id="rg-pct" style="font-weight:600">0%</span>
          of <span id="rg-limit">10%</span> limit
        </div>
        <div style="height:5px;background:var(--border);border-radius:3px;overflow:hidden">
          <div id="rg-bar" style="height:100%;width:0%;background:var(--green);
            border-radius:3px;transition:width .6s var(--ease),background .4s var(--ease)"></div>
        </div>
        <div id="rg-status" style="font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:8px"></div>
        <button id="reset-baseline-btn" class="btn" style="width:100%;padding:6px 0;font-size:12px;background:rgba(255,255,255,0.05);color:var(--text);border:1px solid var(--border);" onclick="resetDrawdownBaseline()">Reset Baseline</button>
      </div>
    </div>

    <!-- Activity tab -->
    <div class="tab-content" id="panel-activity">
      <div id="bias-panel" style="display:none;margin-bottom:10px">
        <div class="sec">Today's AI Market View</div>
        <div id="bias-card" style="background:var(--bg3);border-radius:var(--radius);padding:10px 12px;
          border-left:3px solid var(--purple);font-size:11px;line-height:1.6;color:var(--muted)">
        </div>
      </div>
      <div class="sec">What is the bot doing?</div>
      <div id="feed-list">
        <div class="feed-empty">No activity yet — the bot will log trades here as they happen.</div>
      </div>
    </div>
  </div>

</div>

<script>
var INSTRUMENTS = {{ instruments }};
var API_TOKEN   = "{{ api_token }}";
var activeInst  = INSTRUMENTS.length ? INSTRUMENTS[0] : 'EUR_USD';
var activeTf    = 'H1';
var chart       = null;
var candleSeries = null;
var overlays    = [];
var lastClose   = {};
var prevPrices  = {};

// ── Status bar ────────────────────────────────────────────────────────────
function sb(msg, type) {
  var el = document.getElementById('status-text');
  var bar = document.getElementById('statusbar');
  if (el)  el.textContent = msg;
  if (bar) bar.className = 'statusbar ' + (type || '');
}

// ── Fetch helper ──────────────────────────────────────────────────────────
function apiFetch(url) {
  return fetch(url).then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

// ── Number flash on change ────────────────────────────────────────────────

function animatePriceTick(el, newPx, oldPx) {
  if (!el || newPx === undefined || oldPx === undefined || newPx === oldPx) return;
  el.classList.remove('tick-up', 'tick-down');
  void el.offsetWidth;
  if (newPx > oldPx) {
    el.classList.add('tick-up');
  } else if (newPx < oldPx) {
    el.classList.add('tick-down');
  }
  setTimeout(function() { el.classList.remove('tick-up', 'tick-down'); }, 750);
}

function flashEl(el, positive) {
  el.classList.remove('flash-green', 'flash-red');
  void el.offsetWidth; // reflow
  el.classList.add(positive ? 'flash-green' : 'flash-red');
  setTimeout(function() { el.classList.remove('flash-green', 'flash-red'); }, 650);
}

// ── Build pair list with staggered animation ──────────────────────────────
function buildPairList() {
  var el = document.getElementById('pair-list');
  el.innerHTML = '';
  INSTRUMENTS.forEach(function(inst, i) {
    var b = document.createElement('button');
    b.className = 'pair-btn' + (inst === activeInst ? ' active' : '');
    b.dataset.inst = inst;
    b.style.animationDelay = (i * 30) + 'ms';
    b.innerHTML =
      '<span>' + inst.replace('_', '/') + '</span>' +
      '<span style="display:flex;align-items:center;gap:5px">' +
        '<span class="rsi-badge" id="rsi-' + inst + '" data-tip="RSI (14, H1)" style="font-size:9px;color:var(--muted);font-family:var(--font-mono);min-width:20px;text-align:right"></span>' +
        '<span class="sig-none" id="sig-' + inst + '">—</span>' +
        '<span class="pair-px" id="px-' + inst + '">—</span>' +
      '</span>';
    b.onclick = function() { selectInst(inst); };
    el.appendChild(b);
  });
}

function selectInst(inst) {
  activeInst = inst;
  document.querySelectorAll('.pair-btn').forEach(function(x) {
    x.classList.toggle('active', x.dataset.inst === inst);
  });
  document.getElementById('chart-title').textContent = inst.replace('_', '/');
  var wrap = document.getElementById('chart-wrap');
  wrap.classList.add('loading');
  loadChart(inst, activeTf).then(function() {
    wrap.classList.remove('loading');
  });
}

function showOandaBanner(msg) {
  var b = document.getElementById('oanda-warning-banner');
  var t = document.getElementById('oanda-warning-text');
  if (b && t) {
    t.textContent = msg || 'Check your OANDA API Key & Account ID in Settings.';
    b.style.display = 'flex';
  }
}
function hideOandaBanner() {
  var b = document.getElementById('oanda-warning-banner');
  if (b) b.style.display = 'none';
}

// ── Account ───────────────────────────────────────────────────────────────
function refreshAccount() {
  apiFetch('/api/account').then(function(a) {
    if (a.error) { sb('OANDA: ' + a.error, 'err'); showOandaBanner(a.error); return; }
    hideOandaBanner();
    var bal = parseFloat(a.balance || 0);
    var upl = parseFloat(a.unrealizedPL || 0);
    var balEl = document.getElementById('a-bal');
    var newBal = '$' + bal.toLocaleString('en', {minimumFractionDigits:2});
    if (balEl.textContent && balEl.textContent !== '—' && balEl.textContent !== newBal) {
      flashEl(balEl, bal > parseFloat(balEl.textContent.replace(/[$,]/g,'')));
    }
    balEl.textContent = newBal;
    document.getElementById('a-upl').textContent = (upl >= 0 ? '+' : '') + upl.toFixed(2);
    document.getElementById('a-upl').className = 'sv ' + (upl >= 0 ? 'green' : 'red');
    document.getElementById('a-ot').textContent = a.openTradeCount || 0;
    document.getElementById('bal').textContent = newBal;
    sb('Updated ' + new Date().toLocaleTimeString(), 'ok');
  }).catch(function(e) {
    sb('Account error: ' + e.message, 'err');
    showOandaBanner('OANDA Unauthorized — check API Key and Account ID in Settings');
  });
}

// ── Session ───────────────────────────────────────────────────────────────
function refreshSession() {
  apiFetch('/api/session').then(function(s) {
    var badge = document.getElementById('sess-badge');
    badge.textContent = s.session || '—';
    badge.className = 'badge' + (s.trading_active ? ' active-sess' : '');
    document.getElementById('a-sess').textContent = s.session || '—';
    document.getElementById('a-sess').className = 'sv' + (s.trading_active ? ' green' : '');
  }).catch(function(){});
}

// ── Trades ────────────────────────────────────────────────────────────────
function refreshTrades() {
  apiFetch('/api/trades').then(function(trades) {
    var panel = document.getElementById('trades-panel');
    if (!Array.isArray(trades) || !trades.length) {
      panel.innerHTML = '<div class="no-trades">No open trades</div>';
      return;
    }
    // Only re-render if count changed (avoids animation flicker on every poll)
    var newCount = trades.length;
    var curCount = panel.querySelectorAll('.tc').length;
    if (newCount !== curCount) {
      panel.innerHTML = trades.map(function(t, i) {
        var dir = parseInt(t.currentUnits) > 0 ? 'BUY' : 'SELL';
        var pl  = parseFloat(t.unrealizedPL || 0);
        return '<div class="tc ' + dir.toLowerCase() + '" style="animation-delay:' + (i*50) + 'ms">' +
          '<div class="tc-h">' +
            '<span class="tc-pair">' + t.instrument.replace('_','/') + '</span>' +
            '<span class="tc-dir dir-' + dir.toLowerCase() + '">' + dir + '</span>' +
          '</div>' +
          '<div class="tc-row"><span>Entry</span><span>' + parseFloat(t.price).toFixed(5) + '</span></div>' +
          '<div class="tc-row"><span>SL</span><span>' + (t.stopLossOrder ? parseFloat(t.stopLossOrder.price).toFixed(5) : '—') + '</span></div>' +
          '<div class="tc-row"><span>TP</span><span>' + (t.takeProfitOrder ? parseFloat(t.takeProfitOrder.price).toFixed(5) : '—') + '</span></div>' +
          '<div class="tc-row" style="margin-top:4px"><span>P&amp;L</span>' +
            '<span class="tc-pl ' + (pl >= 0 ? 'green' : 'red') + '">' + (pl >= 0 ? '+' : '') + '$' + pl.toFixed(2) + '</span>' +
          '</div>' +
        '</div>';
      }).join('');
    } else {
      // Just update P&L values in place without re-rendering
      var cards = panel.querySelectorAll('.tc');
      trades.forEach(function(t, i) {
        if (!cards[i]) return;
        var pl    = parseFloat(t.unrealizedPL || 0);
        var plEl  = cards[i].querySelector('.tc-pl');
        if (plEl) {
          var newVal = (pl >= 0 ? '+' : '') + '$' + pl.toFixed(2);
          if (plEl.textContent !== newVal) {
            plEl.textContent  = newVal;
            plEl.className    = 'tc-pl ' + (pl >= 0 ? 'green' : 'red');
            flashEl(plEl, pl >= 0);
          }
        }
      });
    }
  }).catch(function(){});
}

// ── Today ─────────────────────────────────────────────────────────────────
function refreshToday() {
  apiFetch('/api/performance/today').then(function(d) {
    var pl = parseFloat(d.pl || 0);
    document.getElementById('t-trades').textContent = d.trades || 0;
    document.getElementById('t-wins').textContent   = d.wins   || 0;
    var plEl = document.getElementById('t-pl');
    plEl.textContent = (pl >= 0 ? '+' : '') + '$' + Math.abs(pl).toFixed(2);
    plEl.className = 'sv ' + (pl >= 0 ? 'green' : 'red');
  }).catch(function(){});
}

// ── Prices ────────────────────────────────────────────────────────────────
function pipSizeFor(inst) {
  if (inst.indexOf('JPY') >= 0) return 0.01;
  if (inst.indexOf('XAU') >= 0) return 0.1;
  return 0.0001;
}

function refreshPrices() {
  apiFetch('/api/prices').then(function(prices) {
    Object.keys(prices).forEach(function(inst) {
      var p   = prices[inst];
      var el  = document.getElementById('px-' + inst);
      var dec = inst.indexOf('JPY') >= 0 ? 3 : inst.indexOf('XAU') >= 0 ? 2 : 5;
      if (el) {
        var newVal = p.mid.toFixed(dec);
        if (el.textContent && el.textContent !== '—' && el.textContent !== newVal) {
          flashEl(el, p.mid > (prevPrices[inst] || p.mid));
        }
        el.textContent = newVal;
      }
      prevPrices[inst] = p.mid;
    });
    var ap = prices[activeInst];
    if (ap) {
      var px  = ap.mid;
      var dec = activeInst.indexOf('JPY') >= 0 ? 3 : activeInst.indexOf('XAU') >= 0 ? 2 : 5;
      document.getElementById('chart-price').textContent = px.toFixed(dec);
      var base = lastClose[activeInst] || px;
      var chg  = ((px - base) / base * 100);
      var ce   = document.getElementById('chart-chg');
      ce.textContent      = (chg >= 0 ? '+' : '') + chg.toFixed(3) + '%';
      ce.style.background = chg >= 0 ? '#26995a33' : '#c33d4133';
      ce.style.color      = chg >= 0 ? '#2fbf71'   : '#e5484d';

      if (ap.bid !== undefined && ap.ask !== undefined) {
        var pip        = pipSizeFor(activeInst);
        var spreadPips = ((ap.ask - ap.bid) / pip).toFixed(1);
        var sp = document.getElementById('chart-spread');
        sp.setAttribute('data-tip', 'Bid ' + ap.bid.toFixed(dec) + ' / Ask ' + ap.ask.toFixed(dec));
        sp.innerHTML =
          '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:var(--red);color:var(--bg);font-family:var(--font-mono);opacity:.85">' + ap.bid.toFixed(dec) + '</span>' +
          '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:var(--green);color:var(--bg);font-family:var(--font-mono);opacity:.85">' + ap.ask.toFixed(dec) + '</span>' +
          '<span style="font-size:10px;color:var(--muted)">' + spreadPips + 'p spread</span>';
      }
    }
  }).catch(function(){});
}

// ── Signals ───────────────────────────────────────────────────────────────
function refreshSignals() {
  apiFetch('/api/signals/quick').then(function(sigs) {
    Object.keys(sigs).forEach(function(inst) {
      var el  = document.getElementById('sig-' + inst);
      var data = sigs[inst] || {};
      var sig = data.signal;
      if (el) {
        var old = el.textContent;
        el.textContent = sig || '—';
        el.className   = sig === 'BUY' ? 'sig-buy' : sig === 'SELL' ? 'sig-sell' : 'sig-none';
        if (sig && sig !== old && old !== '—') flashEl(el, sig === 'BUY');
      }
      var rsiEl = document.getElementById('rsi-' + inst);
      if (rsiEl) {
        if (data.rsi === null || data.rsi === undefined) {
          rsiEl.textContent = '';
        } else {
          rsiEl.textContent = data.rsi.toFixed(0);
          // Overbought (>70) / oversold (<30) get a subtle color cue, like
          // the RSI/MFI badges in professional charting platforms.
          rsiEl.style.color = data.rsi >= 70 ? '#e5484d' : data.rsi <= 30 ? '#2fbf71' : 'var(--muted)';
        }
      }
    });
  }).catch(function(){});
}

// ── Chart ─────────────────────────────────────────────────────────────────
function initChart() {
  var wrap = document.getElementById('chart-wrap');
  wrap.innerHTML = '';
  var div = document.createElement('div');
  div.style.cssText = 'width:100%;height:100%';
  wrap.appendChild(div);
  chart = LightweightCharts.createChart(div, {
    layout:    { background: { color: '#0b0e1a' }, textColor: '#9ca3af' },
    grid:      { vertLines: { color: '#1a2035' }, horzLines: { color: '#1a2035' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#1e2d45' },
    timeScale: { borderColor:'#1e2d45', timeVisible:true, secondsVisible:false },
    width:  wrap.clientWidth,
    height: wrap.clientHeight,
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:'#2fbf71', downColor:'#e5484d',
    borderUpColor:'#2fbf71', borderDownColor:'#e5484d',
    wickUpColor:'#2fbf71',   wickDownColor:'#e5484d',
  });
  new ResizeObserver(function() {
    if (chart) chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });
  }).observe(wrap);
}

function loadChart(inst, tf) {
  document.getElementById('chart-title').textContent = inst.replace('_', '/');
  if (!chart) { sb('Chart library loading…'); return Promise.resolve(); }
  return apiFetch('/api/candles?instrument=' + inst + '&granularity=' + tf + '&count=200')
  .then(function(d) {
    if (d.error) { sb('Chart: ' + d.error, 'err'); return; }
    if (!d.candles || !d.candles.length) { sb('No candle data for ' + inst, 'err'); return; }
    var candles = d.candles.map(function(c) {
      return { time:Math.floor(new Date(c.time).getTime()/1000),
               open:c.open, high:c.high, low:c.low, close:c.close };
    });
    candleSeries.setData(candles);
    lastClose[inst] = candles[candles.length - 1].close;
    overlays.forEach(function(s) { chart.removeSeries(s); }); overlays = [];
    function addLine(data, color, style) {
      var s = chart.addLineSeries({ color:color, lineWidth:1,
        priceLineVisible:false, lastValueVisible:false, lineStyle:style||0 });
      s.setData(data.map(function(v,i) {
        return v ? { time:candles[i].time, value:v } : null;
      }).filter(Boolean));
      overlays.push(s);
    }
    addLine(d.ema9, '#4c8fd6'); addLine(d.ema21, '#d1a13c');
    addLine(d.ema50, '#a78bfa'); addLine(d.bb_upper, '#374151', 2);
    addLine(d.bb_lower, '#374151', 2);
    if (d.markers && d.markers.length) {
      candleSeries.setMarkers(d.markers.map(function(m) {
        if (m.kind === 'exit') {
          var won = (m.pl_pct || 0) >= 0;
          var pct = (m.pl_pct || 0).toFixed(2) + '%';
          return { time:Math.floor(new Date(m.time).getTime()/1000),
            position: m.direction==='BUY' ? 'aboveBar' : 'belowBar',
            color: won ? '#2fbf71' : '#e5484d',
            shape: 'circle',
            text: (won?'+':'') + pct };
        }
        return { time:Math.floor(new Date(m.time).getTime()/1000),
          position:m.direction==='BUY'?'belowBar':'aboveBar',
          color:m.direction==='BUY'?'#2fbf71':'#e5484d',
          shape:m.direction==='BUY'?'arrowUp':'arrowDown', text:m.direction };
      }));
    }
    chart.timeScale().fitContent();
  }).catch(function(e) { sb('Chart: ' + e.message, 'err'); });
}

// ── Timeframe buttons ─────────────────────────────────────────────────────
document.querySelectorAll('.tf').forEach(function(b) {
  b.onclick = function() {
    document.querySelectorAll('.tf').forEach(function(x) { x.classList.remove('active'); });
    b.classList.add('active');
    activeTf = b.dataset.tf;
    var wrap = document.getElementById('chart-wrap');
    wrap.classList.add('loading');
    loadChart(activeInst, activeTf).then(function() { wrap.classList.remove('loading'); });
  };
});

// ── Chart library loader ──────────────────────────────────────────────────
(function loadLib() {
  var s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js';
  s.onload = function() {
    initChart();
    loadChart(activeInst, activeTf);
    setInterval(function() { loadChart(activeInst, activeTf); }, 60000);
  };
  s.onerror = function() {
    var s2 = document.createElement('script');
    s2.src = 'https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js';
    s2.onload = function() {
      initChart();
      loadChart(activeInst, activeTf);
      setInterval(function() { loadChart(activeInst, activeTf); }, 60000);
    };
    s2.onerror = function() { sb('Chart library unavailable — check internet'); };
    document.head.appendChild(s2);
  };
  document.head.appendChild(s);
})();

// ── Tab switching ────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(function(b) {
    b.classList.toggle('active', b.id === 'tab-' + name);
  });
  document.querySelectorAll('.tab-content').forEach(function(p) {
    var willActive = p.id === 'panel-' + name;
    if (willActive && !p.classList.contains('active')) {
      p.classList.add('active');
      if (name === 'activity') { refreshFeed(); refreshBias(); }
    } else if (!willActive) {
      p.classList.remove('active');
    }
  });
}

// ── Activity feed ─────────────────────────────────────────────────────────
var FEED_ICONS = {
  open:'&#x1F7E2;', close_win:'&#x2705;',
  close_loss:'&#x1F534;', session:'&#x23F0;', info:'&#x23F8;'
};
function timeAgo(tsMs) {
  var s=Math.floor((Date.now()-tsMs)/1000);
  if(s<60) return 'just now';
  var m=Math.floor(s/60); if(m<60) return m+' minute'+(m===1?'':'s')+' ago';
  var h=Math.floor(m/60); if(h<24) return h+' hour'+(h===1?'':'s')+' ago';
  var d=Math.floor(h/24); return d+' day'+(d===1?'':'s')+' ago';
}
function refreshFeed() {
  apiFetch('/api/feed').then(function(events) {
    var el=document.getElementById('feed-list'); if(!el) return;
    if(!events||!events.length) {
      el.innerHTML='<div class="feed-empty">No activity yet.<br>Trades will appear here as they happen.</div>';
      return;
    }
    el.innerHTML=events.map(function(ev,i) {
      var d=ev.data||{}; var icon=FEED_ICONS[ev.type]||'&#x2139;';
      var ago=timeAgo(ev.ts_ms||0);
      return '<div class="feed-card '+ev.type+'" style="animation-delay:'+(i*40)+'ms">'
        +'<div class="feed-icon">'+icon+'</div>'
        +'<div class="feed-title">'+(d.title||'')+'</div>'
        +'<div class="feed-body">'+(d.body||'')+'</div>'
        +'<div class="feed-time">'+ago+'</div></div>';
    }).join('');
  }).catch(function(){});
}
setInterval(function(){
  var p=document.getElementById('panel-activity');
  if(p&&p.classList.contains('active')) refreshFeed();
},15000);

// ── Pause / Resume ───────────────────────────────────────────────────────
var _isRunning = true;

function togglePause() {
  var btn = document.getElementById('pause-btn');
  if (btn) btn.style.opacity = '0.6';

  fetch('/api/pause', {method:'POST', headers:{'X-Tradalgo-Token': API_TOKEN}})
  .then(function(r){ return r.json(); })
  .then(function(d){
    _isRunning = d.live_trading;
    updatePauseBtn(_isRunning);
    // Refresh feed so the paused/resumed event appears immediately
    refreshFeed();
  })
  .catch(function(e){ console.error('Pause toggle failed:', e); })
  .finally(function(){
    var b = document.getElementById('pause-btn');
    if (b) b.style.opacity = '1';
  });
}

function resetDrawdownBaseline() {
  if (!confirm("Are you sure you want to reset your starting balance baseline?\n\nThis will reset your drawdown calculation to 0% from your current balance, effectively bypassing the safety limit and allowing trading to safely resume.")) {
    return;
  }
  var btn = document.getElementById('reset-baseline-btn');
  if (btn) btn.style.opacity = '0.5';
  fetch('/api/reset-drawdown', {method:'POST', headers:{'X-Tradalgo-Token': API_TOKEN}})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (d.success) {
      refreshStatus(); // Immediately refresh stats
      refreshFeed(); // Show feed update
    } else {
      alert("Failed to reset baseline: " + (d.error || "Unknown error"));
    }
  })
  .catch(function(e){ alert("Error resetting baseline: " + e); })
  .finally(function(){ if (btn) btn.style.opacity = '1'; });
}

function updatePauseBtn(running) {
  var btn   = document.getElementById('pause-btn');
  var label = document.getElementById('pause-label');
  if (!btn) return;
  if (running) {
    btn.className   = 'running';
    label.textContent = 'Running';
    btn.title = 'Click to pause — no new trades will open';
  } else {
    btn.className   = 'paused';
    label.textContent = 'Paused';
    btn.title = 'Click to resume trading';
  }
}

function refreshStatus() {
  apiFetch('/api/status').then(function(d) {
    _isRunning = d.live_trading;
    updatePauseBtn(_isRunning);

    // Risk guard panel
    var panel = document.getElementById('risk-guard-panel');
    if (!panel) return;
    if (d.risk_guard_enabled) {
      panel.style.display = 'block';
      var pct   = d.drawdown_pct || 0;
      var limit = d.risk_guard_limit || 10;
      var fill  = Math.min(100, (pct / limit) * 100);
      var color = fill >= 100 ? 'var(--red)' : fill >= 75 ? 'var(--gold)' : 'var(--green)';
      document.getElementById('rg-pct').textContent   = pct.toFixed(1) + '%';
      document.getElementById('rg-limit').textContent = limit + '%';
      document.getElementById('rg-bar').style.width      = fill + '%';
      document.getElementById('rg-bar').style.background = color;
      var statusEl = document.getElementById('rg-status');
      if (pct <= 0) {
        statusEl.textContent = 'Account is up from starting balance';
        statusEl.style.color = 'var(--green)';
      } else if (fill >= 100) {
        statusEl.textContent = 'Safety limit reached — trading paused';
        statusEl.style.color = 'var(--red)';
      } else if (fill >= 75) {
        statusEl.textContent = 'Approaching safety limit — watch closely';
        statusEl.style.color = 'var(--gold)';
      } else {
        statusEl.textContent = 'Within safe range';
        statusEl.style.color = 'var(--muted)';
      }
    } else {
      panel.style.display = 'none';
    }
  }).catch(function(){});
}

// ── Onboarding ────────────────────────────────────────────────────────────
var OB_STEPS = [
  {
    target:  '.logo',
    title:   'Welcome to Tradalgo',
    body:    'Your automated forex trading bot is now running. This quick tour '
           + 'will show you where everything is. It only takes 30 seconds.',
    pos:     'bottom-right',
  },
  {
    target:  '#bal',
    title:   'Your Account Balance',
    body:    'This shows your live OANDA balance, updated every 15 seconds. '
           + 'Start in practice mode — it works exactly like a real account but with no real money at risk.',
    pos:     'bottom-left',
  },
  {
    target:  '#pause-btn',
    title:   'Pause Button',
    body:    'Click this any time to stop the bot opening new trades. '
           + 'Your existing trades stay open with their stop losses active. '
           + 'Press it again to resume.',
    pos:     'bottom-left',
  },
  {
    target:  '#pair-list',
    title:   'Instruments',
    body:    'Your bot watches these 10 currency pairs and Gold. '
           + 'Prices update every 5 seconds. Click any pair to see its chart.',
    pos:     'right',
  },
  {
    target:  '.tab-bar',
    title:   'Account & Activity',
    body:    'The Account tab shows your balance and open trades. '
           + 'Switch to Activity to see a plain-English log of everything '
           + 'your bot has done — no jargon, just plain facts.',
    pos:     'left',
  },
  {
    target:  'nav',
    title:   'Backtest & Performance',
    body:    'Use Backtest to see how the bot would have performed on historical data '
           + 'before risking real money. Performance tracks your real results over time. '
           + "When you're ready to go live, just update your credentials in Settings.",
    pos:     'bottom-right',
    last:    true,
  },
];

var obStep   = 0;
var obActive = false;

function obStart() {
  obActive = true;
  obStep   = 0;
  document.getElementById('ob-overlay').classList.add('active');
  obShow(0);
}

function obShow(idx) {
  try {
    var step   = OB_STEPS[idx];
    if (!step) { obDone(); return; }
    var target = document.querySelector(step.target);
    if (!target) {
      // Target not found — skip to next step instead of getting stuck
      if (idx < OB_STEPS.length - 1) { obStep = idx + 1; obShow(obStep); }
      else { obDone(); }
      return;
    }
    obShowInner(step, target, idx);
  } catch (e) {
    console.error('Onboarding error, closing tour:', e);
    obDone();
  }
}

function obShowInner(step, target, idx) {

  // Update text
  document.getElementById('ob-step-label').textContent = 'Step ' + (idx+1) + ' of ' + OB_STEPS.length;
  document.getElementById('ob-title').textContent       = step.title;
  document.getElementById('ob-body').textContent        = step.body;
  document.getElementById('ob-next-btn').textContent    = step.last ? 'Get started' : 'Next';

  // Dots
  document.getElementById('ob-dots').innerHTML = OB_STEPS.map(function(_,i) {
    return '<div class="ob-dot' + (i===idx?' active':'') + '"></div>';
  }).join('');

  // Spotlight
  var rect    = target.getBoundingClientRect();
  var pad     = 8;
  var spot    = document.getElementById('ob-spotlight');
  spot.style.left   = (rect.left   - pad) + 'px';
  spot.style.top    = (rect.top    - pad) + 'px';
  spot.style.width  = (rect.width  + pad*2) + 'px';
  spot.style.height = (rect.height + pad*2) + 'px';

  // Card position
  var card = document.getElementById('ob-card');
  var pos  = step.pos || 'bottom-right';
  var gap  = 16;
  var vw   = window.innerWidth;
  var vh   = window.innerHeight;
  var cw   = 300;

  // Reset
  card.style.left = card.style.right = card.style.top = card.style.bottom = 'auto';

  if (pos === 'bottom-right') {
    card.style.left = Math.min(rect.left, vw - cw - 16) + 'px';
    card.style.top  = (rect.bottom + gap) + 'px';
  } else if (pos === 'bottom-left') {
    card.style.left = Math.max(16, rect.right - cw) + 'px';
    card.style.top  = (rect.bottom + gap) + 'px';
  } else if (pos === 'right') {
    card.style.left = (rect.right + gap) + 'px';
    card.style.top  = Math.min(rect.top, vh - 220) + 'px';
  } else if (pos === 'left') {
    card.style.left = Math.max(16, rect.left - cw - gap) + 'px';
    card.style.top  = Math.min(rect.top, vh - 220) + 'px';
  }

  // Clamp within viewport
  var cardRect = card.getBoundingClientRect();
  if (cardRect.bottom > vh - 16) card.style.top = (vh - cardRect.height - 16) + 'px';
  if (cardRect.right  > vw - 16) card.style.left = (vw - cw - 16) + 'px';
}

function obNext() {
  if (obStep < OB_STEPS.length - 1) {
    obStep++;
    obShow(obStep);
  } else {
    obDone();
  }
}

function obSkip() { obDone(); }

function obDone() {
  obActive = false;
  var overlay = document.getElementById('ob-overlay');
  if (overlay) overlay.classList.remove('active');
  // Belt-and-suspenders: explicitly clear spotlight so no leftover
  // inline styles can keep painting a dark box-shadow anywhere
  var spot = document.getElementById('ob-spotlight');
  if (spot) {
    spot.style.width = '0px'; spot.style.height = '0px';
    spot.style.top = '-9999px'; spot.style.left = '-9999px';
  }
  fetch('/api/onboarding/done', {method:'POST', headers:{'X-Tradalgo-Token': API_TOKEN}}).catch(function(){});
}

function obCheckAndStart() {
  fetch('/api/onboarding/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (!d.done) {
      setTimeout(obStart, 1200);
      // Safety: force-close the tour after 60s no matter what,
      // in case something prevents the user from interacting with it
      setTimeout(function() {
        if (obActive) { console.warn('Onboarding timeout — auto-closing'); obDone(); }
      }, 60000);
    }
  }).catch(function(){});
}

// Escape key always closes the tour
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && obActive) obDone();
});

// ── AI Bias display ──────────────────────────────────────────────────────
function refreshBias() {
  apiFetch('/api/bias').then(function(d) {
    var panel = document.getElementById('bias-panel');
    var card  = document.getElementById('bias-card');
    if (!panel || !card) return;

    if (!d.enabled || !d.bias || !d.bias.summary) {
      panel.style.display = 'none';
      return;
    }

    panel.style.display = 'block';
    var b       = d.bias;
    var conf    = Math.round((b.confidence || 0) * 100);
    var usd     = b.usd_bias    || 'neutral';
    var sent    = b.overall_sentiment || 'neutral';
    var prefer  = (b.preferred_pairs  || []).map(function(p){return p.replace('_','/');}).join(', ') || 'All';
    var avoid   = (b.avoid_pairs      || []).map(function(p){return p.replace('_','/');}).join(', ') || 'None';
    var sentColor = sent === 'risk_on' ? 'var(--green)' : sent === 'risk_off' ? 'var(--red)' : 'var(--gold)';
    var usdColor  = usd  === 'strong'  ? 'var(--green)' : usd  === 'weak'    ? 'var(--red)' : 'var(--gold)';

    card.innerHTML =
      '<div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:6px">'
        + b.summary + '</div>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:6px">'
        + '<span>Sentiment: <b style="color:' + sentColor + '">' + sent.replace('_',' ') + '</b></span>'
        + '<span>USD: <b style="color:' + usdColor + '">' + usd + '</b></span>'
        + '<span>Prefer: <b style="color:var(--text)">' + prefer + '</b></span>'
        + '<span>Avoid: <b style="color:var(--red)">' + avoid + '</b></span>'
      + '</div>'
      + '<div style="font-size:10px;opacity:.6">Confidence: ' + conf + '% &middot; Updated: '
        + (d.last_run || 'today') + '</div>';
  }).catch(function(){});
}

// ── News filter display ──────────────────────────────────────────────────
function refreshNews() {
  apiFetch('/api/news').then(function(d) {
    var panel = document.getElementById('news-panel');
    var card  = document.getElementById('news-card');
    if (!panel || !card) return;

    if (!d.news_filter_enabled) {
      panel.style.display = 'none';
      return;
    }

    panel.style.display = 'block';

    var blocked = d.any_blocked_now;
    var color   = blocked ? 'var(--red)' : 'var(--green)';
    var label   = blocked ? '&#128683; Trading paused — news event' : '&#9989; Clear to trade';
    card.style.borderLeftColor = color;

    var eventsHtml = '';
    if (d.events && d.events.length) {
      eventsHtml = '<div style="margin-top:7px;display:flex;flex-direction:column;gap:3px">';
      d.events.slice(0,4).forEach(function(ev) {
        var evColor = ev.status === 'blocked'  ? 'var(--red)'   :
                      ev.status === 'upcoming' ? 'var(--gold)'  : 'var(--muted)';
        var mins    = ev.mins_away;
        var timeStr = mins > 0 ? 'in ' + mins + ' min' :
                      mins < 0 ? Math.abs(mins) + ' min ago' : 'now';
        eventsHtml +=
          '<div style="display:flex;justify-content:space-between;font-size:10px">'
          + '<span style="color:var(--muted)">' + ev.currency + ' ' + ev.title + '</span>'
          + '<span style="color:' + evColor + ';font-weight:600">' + timeStr + '</span>'
          + '</div>';
      });
      eventsHtml += '</div>';
    }

    card.innerHTML =
      '<div style="font-size:11px;font-weight:600;color:' + color + ';margin-bottom:2px">'
        + label + '</div>'
      + '<div style="font-size:10px;color:var(--muted)">News filter &middot; '
        + d.block_minutes + ' min blackout window</div>'
      + eventsHtml;

  }).catch(function(){});
}

// ── Boot ──────────────────────────────────────────────────────────────────
sb('Connecting…');
buildPairList();
refreshAccount();
refreshSession();
refreshTrades();
refreshToday();
refreshPrices();
refreshStatus();
refreshNews();
licCheckOnLoad();
frCheckOnLoad();
setInterval(refreshAccount,  15000);
setInterval(refreshSession,  30000);
setInterval(refreshTrades,   10000);
setInterval(refreshToday,    30000);
setInterval(refreshPrices,    5000);
setInterval(refreshStatus,   10000);
setInterval(refreshNews,     60000);
setTimeout(refreshSignals,   4000);
setInterval(refreshSignals,  90000);

// ── Smooth Page Transition Handler ──
document.addEventListener('DOMContentLoaded', function() {
  var mainContainer = document.querySelector('.layout') || document.querySelector('.container') || document.querySelector('body > div');
  if (mainContainer) mainContainer.classList.add('page-container');

  var navLinks = document.querySelectorAll('header nav a');
  navLinks.forEach(function(link) {
    link.addEventListener('click', function(e) {
      var href = link.getAttribute('href');
      if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
      if (window.location.pathname === href) return;
      e.preventDefault();
      
      var targetWrap = document.querySelector('.layout') || document.querySelector('.container') || mainContainer;
      if (targetWrap) {
        targetWrap.classList.remove('page-container');
        targetWrap.classList.add('page-exit');
      }
      setTimeout(function() {
        window.location.href = href;
      }, 160);
    });
  });
});

</script>
<!-- First-run welcome screen -->
<div id="fr-overlay" style="display:none;position:fixed;inset:0;z-index:99997;
  background:rgba(11,14,26,.96);align-items:center;justify-content:center">
  <div style="background:#111827;border:1px solid #1e2d45;border-radius:16px;
    padding:40px 44px;max-width:500px;width:90%;animation:frIn .4s cubic-bezier(.4,0,.2,1)">
    <style>@keyframes frIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}</style>

    <div style="font-size:28px;font-weight:800;margin-bottom:4px">
      Trad<span style="color:#d1a13c">algo</span>
    </div>
    <div style="font-size:13px;color:#4b5563;margin-bottom:28px">Your bot is now running</div>

    <div style="display:flex;flex-direction:column;gap:16px;margin-bottom:28px">
      <div style="display:flex;gap:14px;align-items:flex-start">
        <div style="width:32px;height:32px;background:#16803c22;border:1px solid #16803c55;
          border-radius:var(--radius);display:flex;align-items:center;justify-content:center;
          font-size:16px;flex-shrink:0">&#128200;</div>
        <div>
          <div style="font-weight:600;font-size:13px;margin-bottom:3px">Running in practice mode</div>
          <div style="font-size:12px;color:#4b5563;line-height:1.6">
            Your bot is trading with virtual money on a real OANDA practice account.
            No real money is at risk until you switch to a live account.
          </div>
        </div>
      </div>
      <div style="display:flex;gap:14px;align-items:flex-start">
        <div style="width:32px;height:32px;background:#1d4ed822;border:1px solid #1d4ed855;
          border-radius:var(--radius);display:flex;align-items:center;justify-content:center;
          font-size:16px;flex-shrink:0">&#128336;</div>
        <div>
          <div style="font-weight:600;font-size:13px;margin-bottom:3px">Trades happen automatically</div>
          <div style="font-size:12px;color:#4b5563;line-height:1.6">
            The bot checks the market every hour during London (07:00&ndash;16:00 UTC)
            and New York (12:00&ndash;21:00 UTC) sessions. Outside those hours it waits.
          </div>
        </div>
      </div>
      <div style="display:flex;gap:14px;align-items:flex-start">
        <div style="width:32px;height:32px;background:#92400e22;border:1px solid #92400e55;
          border-radius:var(--radius);display:flex;align-items:center;justify-content:center;
          font-size:16px;flex-shrink:0">&#128140;</div>
        <div>
          <div style="font-weight:600;font-size:13px;margin-bottom:3px">You'll get an email for every trade</div>
          <div style="font-size:12px;color:#4b5563;line-height:1.6">
            Every time a trade opens or closes you'll receive an email.
            Check your inbox &mdash; the first trade could come within hours.
          </div>
        </div>
      </div>
      <div style="display:flex;gap:14px;align-items:flex-start">
        <div style="width:32px;height:32px;background:#5b21b622;border:1px solid #5b21b655;
          border-radius:var(--radius);display:flex;align-items:center;justify-content:center;
          font-size:16px;flex-shrink:0">&#9989;</div>
        <div>
          <div style="font-weight:600;font-size:13px;margin-bottom:3px">You can pause any time</div>
          <div style="font-size:12px;color:#4b5563;line-height:1.6">
            The <b style="color:#e2e8f0">Running</b> button at the top of the dashboard
            pauses and resumes trading instantly. Your funds are always safe.
          </div>
        </div>
      </div>
    </div>

    <button onclick="frDone()"
      style="width:100%;padding:13px;background:#4c8fd6;color:#fff;border:none;
      border-radius:var(--radius);font-size:14px;font-weight:600;cursor:pointer;
      transition:background .2s,transform .15s"
      onmouseover="this.style.background='#417ab6';this.style.transform='translateY(-1px)'"
      onmouseout="this.style.background='#4c8fd6';this.style.transform='none'">
      Got it &mdash; show me the dashboard
    </button>
    <div style="margin-top:12px;font-size:11px;color:#374151;text-align:center">
      This screen only appears once. You can always check the Activity tab for updates.
    </div>
  </div>
</div>

<script>
function frCheckOnLoad() {
  fetch('/api/first-run/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (!d.shown) {
      // Small delay so dashboard renders first
      setTimeout(function(){
        var el = document.getElementById('fr-overlay');
        if (el) { el.style.display = 'flex'; }
      }, 1800);
    }
  }).catch(function(){});
}

function frDone() {
  var el = document.getElementById('fr-overlay');
  if (el) {
    el.style.opacity = '0';
    el.style.transition = 'opacity .3s';
    setTimeout(function(){ el.style.display = 'none'; }, 300);
  }
  fetch('/api/first-run/done', {method:'POST', headers:{'X-Tradalgo-Token': API_TOKEN}}).catch(function(){});
  // Start onboarding tour after first-run screen closes
  setTimeout(function(){
    if (typeof obCheckAndStart === 'function') obCheckAndStart();
  }, 400);
}

// ── Smooth Page Transition Handler ──
document.addEventListener('DOMContentLoaded', function() {
  var mainContainer = document.querySelector('.layout') || document.querySelector('.container') || document.querySelector('body > div');
  if (mainContainer) mainContainer.classList.add('page-container');

  var navLinks = document.querySelectorAll('header nav a');
  navLinks.forEach(function(link) {
    link.addEventListener('click', function(e) {
      var href = link.getAttribute('href');
      if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
      if (window.location.pathname === href) return;
      e.preventDefault();
      
      var targetWrap = document.querySelector('.layout') || document.querySelector('.container') || mainContainer;
      if (targetWrap) {
        targetWrap.classList.remove('page-container');
        targetWrap.classList.add('page-exit');
      }
      setTimeout(function() {
        window.location.href = href;
      }, 160);
    });
  });
});

</script>

<!-- Licence activation overlay -->
<div id="lic-overlay" style="display:none !important;position:fixed;inset:0;z-index:99998;
  background:rgba(11,14,26,.97);align-items:center;justify-content:center">
  <div style="background:#111827;border:1px solid #1e2d45;border-radius:16px;
    padding:40px;max-width:440px;width:90%;text-align:center">
    <div style="font-size:28px;font-weight:800;margin-bottom:4px">
      Trad<span style="color:#d1a13c">algo</span>
    </div>
    <div style="font-size:13px;color:#4b5563;margin-bottom:28px">Automated Forex Trading</div>
    <div style="font-size:16px;font-weight:600;margin-bottom:8px;color:#e2e8f0">
      Activate your licence
    </div>
    <div style="font-size:13px;color:#4b5563;margin-bottom:20px;line-height:1.6">
      Enter the licence key from your purchase email to get started.
    </div>
    <input id="lic-input" type="text" placeholder="XXXX-XXXX-XXXX-XXXX"
      style="width:100%;background:#1a2035;border:1px solid #1e2d45;color:#e2e8f0;
      padding:12px 14px;border-radius:var(--radius);font-size:14px;outline:none;
      letter-spacing:2px;text-align:center;margin-bottom:10px;
      font-family:monospace;transition:border-color .2s"
      oninput="this.value=this.value.toUpperCase()"
      onfocus="this.style.borderColor='#4c8fd6'"
      onblur="this.style.borderColor='#1e2d45'"
      onkeydown="if(event.key==='Enter')licActivate()">
    <div id="lic-error" style="color:#e5484d;font-size:12px;min-height:18px;margin-bottom:10px"></div>
    <button onclick="licActivate()" id="lic-btn"
      style="width:100%;padding:12px;background:#4c8fd6;color:#fff;border:none;
      border-radius:var(--radius);font-size:14px;font-weight:600;cursor:pointer;
      transition:background .2s,transform .15s"
      onmouseover="this.style.background='#417ab6';this.style.transform='translateY(-1px)'"
      onmouseout="this.style.background='#4c8fd6';this.style.transform='none'">
      Activate
    </button>
    <div style="margin-top:16px;font-size:12px;color:#374151">
      Don't have a key?
      <a href="https://tradalgo.com" target="_blank"
         style="color:#4c8fd6;text-decoration:none">Purchase at tradalgo.com</a>
    </div>
    <div id="lic-active-info" style="display:none;margin-top:16px;padding:12px;
      background:#16803c22;border:1px solid #16803c55;border-radius:var(--radius);
      font-size:12px;color:#2fbf71"></div>
  </div>
</div>

<script>
function licCheckOnLoad() {
  fetch('/api/licence/status')
  .then(function(r){ return r.json(); })
  .then(function(d){
    var overlay = document.getElementById('lic-overlay');
    if (false) {
      overlay.style.display = 'flex';
    } else {
      overlay.style.display = 'none';
      // Show licence info subtly in corner if active
      var info = document.getElementById('lic-active-info');
      if (info) info.style.display = 'none';
    }
  }).catch(function(){});
}

function licActivate() {
  var key = document.getElementById('lic-input').value.trim();
  var btn = document.getElementById('lic-btn');
  var err = document.getElementById('lic-error');
  if (!key) { err.textContent = 'Please enter your licence key'; return; }
  btn.textContent = 'Activating...';
  btn.disabled    = true;
  err.textContent = '';

  fetch('/api/licence/activate', {
    method:  'POST',
    headers: {'Content-Type':'application/json', 'X-Tradalgo-Token': API_TOKEN},
    body:    JSON.stringify({key: key})
  })
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (d.valid) {
      document.getElementById('lic-overlay').style.display = 'none';
      document.getElementById('lic-active-info').style.display = 'block';
      document.getElementById('lic-active-info').textContent =
        '&#10003; Licenced to ' + (d.email || 'you') + ' (' + (d.plan||'lifetime') + ')';
    } else {
      err.textContent = d.error || 'Invalid key — check your purchase email';
      btn.textContent = 'Activate';
      btn.disabled    = false;
    }
  })
  .catch(function(e){
    err.textContent = 'Connection error — try again';
    btn.textContent = 'Activate';
    btn.disabled    = false;
  });
}

// ── Smooth Page Transition Handler ──
document.addEventListener('DOMContentLoaded', function() {
  var mainContainer = document.querySelector('.layout') || document.querySelector('.container') || document.querySelector('body > div');
  if (mainContainer) mainContainer.classList.add('page-container');

  var navLinks = document.querySelectorAll('header nav a');
  navLinks.forEach(function(link) {
    link.addEventListener('click', function(e) {
      var href = link.getAttribute('href');
      if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
      if (window.location.pathname === href) return;
      e.preventDefault();
      
      var targetWrap = document.querySelector('.layout') || document.querySelector('.container') || mainContainer;
      if (targetWrap) {
        targetWrap.classList.remove('page-container');
        targetWrap.classList.add('page-exit');
      }
      setTimeout(function() {
        window.location.href = href;
      }, 160);
    });
  });
});

</script>

<!-- Onboarding overlay -->
<div id="ob-overlay">
  <div id="ob-backdrop" onclick="obSkip()"></div>
  <div id="ob-spotlight"></div>
  <div id="ob-card">
    <div class="ob-step" id="ob-step-label">Step 1 of 6</div>
    <div class="ob-title" id="ob-title"></div>
    <div class="ob-body"  id="ob-body"></div>
    <div class="ob-actions">
      <div class="ob-dots" id="ob-dots"></div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="ob-skip" onclick="obSkip()">Skip tour</button>
        <button class="ob-next" id="ob-next-btn" onclick="obNext()">Next</button>
      </div>
    </div>
  </div>
</div>
</body>
</html>
"""



# ── Go Live Readiness page ────────────────────────────────────────────────────
_BACKTEST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradalgo - Backtest</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0b0f19;--bg2:#111726;--bg3:#182033;--bg4:#202b42;--border:#1e293d;
  --text:#f8fafc;--muted:#8493a8;--green:#10b981;--red:#ef4444;--blue:#3b82f6;--blue-btn:#2563eb;
  --gold:#f59e0b;--radius:8px;--font-mono:ui-monospace,'SF Mono','Cascadia Mono','JetBrains Mono',Consolas,monospace;
  --ease:cubic-bezier(.4,0,.2,1)}
*{box-sizing:border-box;margin:0;padding:0}
button{font-family:inherit}
body{background:var(--bg);color:var(--text);font:13px/1.5 system-ui,sans-serif;min-height:100vh}
a,button{transition:background .18s var(--ease),color .18s var(--ease)}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 24px;height:48px;
  display:flex;align-items:center;gap:10px;box-shadow:0 1px 12px rgba(0,0,0,.4)}
.logo{font-size:16px;font-weight:700}.logo span{color:var(--gold)}
nav{margin-left:8px;display:flex;gap:2px}
nav a{color:var(--muted);text-decoration:none;padding:5px 12px;border-radius:6px;font-size:12px;font-weight:500;position:relative;transition:color .2s,background .2s}
nav a::after{content:'';position:absolute;bottom:-2px;left:50%;right:50%;height:2px;background:var(--blue);border-radius:1px;transition:left .25s,right .25s}
nav a:hover{background:var(--bg3);color:var(--text)}
nav a:hover::after,nav a.active::after{left:10px;right:10px}
nav a.active{background:var(--bg4);color:var(--text)}
.hspace{flex:1}
main{max-width:1100px;margin:0 auto;padding:28px 20px}

/* Controls */
.controls{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:18px 20px;margin-bottom:22px;display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap}
.ctrl-group{display:flex;flex-direction:column;gap:5px}
.ctrl-group label{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:600}
select,input[type=number]{background:#0d1424;border:1px solid var(--border);color:var(--text);
  padding:8px 12px;border-radius:var(--radius);font-size:12px;outline:none;transition:all .2s var(--ease)}
select:focus,input:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.18)}
select option{background:#0d1424}
.run-btn{padding:9px 26px;background:var(--blue-btn);color:#fff;border:1px solid rgba(59,130,246,.4);border-radius:var(--radius);
  font-size:13px;font-weight:600;letter-spacing:.2px;cursor:pointer;white-space:nowrap;align-self:flex-end;
  box-shadow:0 4px 14px rgba(37,99,235,.3);transition:all .18s var(--ease)}
.run-btn:hover:not(:disabled){background:#1d4ed8;box-shadow:0 6px 18px rgba(37,99,235,.45);transform:translateY(-1px)}
.run-btn:disabled{background:var(--bg4);border-color:var(--border);color:var(--muted);cursor:wait;box-shadow:none}

/* Progress */
#progress{display:none;background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:20px;margin-bottom:22px;text-align:center}
.prog-bar-wrap{background:var(--bg3);border-radius:4px;height:6px;margin:12px 0}
.prog-bar{height:100%;border-radius:4px;background:var(--blue);
  transition:width .3s var(--ease);width:0%}
.prog-text{font-size:12px;color:var(--muted)}

/* KPI grid */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}
.kpi{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px 16px;
  animation:fadeUp .3s var(--ease) both}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.kv{font-size:22px;font-weight:700;font-family:var(--font-mono);font-variant-numeric:tabular-nums;line-height:1.2;margin-bottom:2px}
.kl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.green{color:var(--green)}.red{color:var(--red)}.blue{color:var(--blue)}.gold{color:var(--gold)}

/* Charts row */
.charts-row{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:18px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;
  animation:fadeUp .35s var(--ease) both}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:.6px;
  color:var(--muted);font-weight:600;margin-bottom:12px}
.chart-wrap{position:relative;height:200px}

/* Pair table */
.pair-table-wrap{margin-bottom:18px;animation:fadeUp .4s var(--ease)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--muted);padding:7px 10px;border-bottom:1px solid var(--border);
  font-size:10px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #161c2e}
tr:last-child td{border:none}
tr:hover td{background:var(--bg3)}
.pos{color:var(--green);font-weight:600;font-family:var(--font-mono)}.neg{color:var(--red);font-weight:600;font-family:var(--font-mono)}
.pill{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700}

/* Go live section */
.golive{background:var(--bg2);border:1px solid var(--border);border-radius:12px;
  padding:24px;text-align:center;margin-bottom:18px;animation:fadeUp .45s var(--ease)}
.golive-title{font-size:16px;font-weight:600;margin-bottom:8px}
.golive-sub{font-size:12px;color:var(--muted);margin-bottom:18px;line-height:1.7}
.golive-btn{display:inline-block;padding:12px 32px;border-radius:var(--radius);font-size:14px;
  font-weight:700;border:none;cursor:pointer;background:linear-gradient(135deg,#1d4ed8,#4c8fd6);
  color:#fff;transition:transform .15s var(--ease),box-shadow .15s var(--ease)}
.golive-btn:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(59,130,246,.3)}
.golive-guide{text-align:left;margin-top:18px;display:none}
.golive-guide ol{padding-left:20px;display:flex;flex-direction:column;gap:8px;
  font-size:13px;color:var(--muted)}
.golive-guide li b{color:var(--text)}
.oanda-link{display:inline-block;margin-top:14px;padding:9px 20px;background:var(--blue);
  color:#fff;border-radius:6px;text-decoration:none;font-size:12px;font-weight:600}

/* Empty state */
#empty{text-align:center;padding:60px 20px;color:var(--muted)}
#empty svg{opacity:.25;margin-bottom:16px}
#results{display:none}

/* ── Pause button ── */
#pause-btn{
  padding:6px 14px;border-radius:8px;font-size:12px;font-weight:600;
  letter-spacing:.2px;cursor:pointer;border:none;display:inline-flex;align-items:center;
  gap:7px;transition:all .2s cubic-bezier(.4,0,.2,1);flex-shrink:0;
  backdrop-filter:blur(8px);user-select:none;
}
#pause-btn.running{
  background:linear-gradient(135deg, rgba(16,185,129,.14), rgba(5,150,105,.22));
  color:#10b981;border:1px solid rgba(16,185,129,.35);
  box-shadow:0 2px 8px rgba(16,185,129,.12);
}
#pause-btn.running:hover{
  background:linear-gradient(135deg, rgba(16,185,129,.24), rgba(5,150,105,.32));
  border-color:rgba(16,185,129,.55);box-shadow:0 4px 14px rgba(16,185,129,.28);
  transform:translateY(-1px);
}
#pause-btn.paused{
  background:linear-gradient(135deg, rgba(239,68,68,.14), rgba(220,38,38,.22));
  color:#ef4444;border:1px solid rgba(239,68,68,.35);
  box-shadow:0 2px 8px rgba(239,68,68,.12);
  animation:pausePulse 2s cubic-bezier(.4,0,.6,1) infinite;
}
@keyframes pausePulse{
  0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.35)}
  50%{box-shadow:0 0 0 6px rgba(239,68,68,0)}
}
#pause-btn.paused:hover{
  background:linear-gradient(135deg, rgba(239,68,68,.24), rgba(220,38,38,.32));
  border-color:rgba(239,68,68,.55);box-shadow:0 4px 14px rgba(239,68,68,.28);
  animation:none;transform:translateY(-1px);
}

::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
@media(max-width:800px){.charts-row{grid-template-columns:1fr}}
/* Page animations */
main{animation:pageIn .3s cubic-bezier(.4,0,.2,1)}
@keyframes pageIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.kpi{transition:transform .15s,box-shadow .15s}
.kpi:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.3)}
.card{transition:box-shadow .2s}
.run-btn{transition:background .2s,transform .15s,box-shadow .15s}
.run-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(59,130,246,.4)}
tr:hover td{transition:background .15s}
</style>
</head>
<body>
<header>
  <div class="logo">Trad<span>algo</span></div>
  <nav>
    <a href="/">Live</a>
    <a href="/backtest" class="active">Backtest</a>
    <a href="/performance">Performance</a>
    <a href="/settings">Settings</a>
  </nav>
  <div class="hspace"></div>
  <button id="env-toggle-btn" onclick="showEnvSwitchModal()" title="Switch between Practice and Live trading" style="padding:5px 12px;border-radius:20px;border:1.5px solid;font-size:11px;font-weight:800;cursor:pointer;transition:all .2s;letter-spacing:0.5px;"></button>
  <span id="last-run" style="font-size:11px;color:var(--muted);margin-left:10px;"></span>
</header>
<script>
(function(){
  function _updateEnvToggle(env){
    var btn=document.getElementById('env-toggle-btn'); if(!btn)return;
    if(env==='live'){btn.textContent='\u25CF LIVE';btn.style.color='#ef4444';btn.style.borderColor='#ef4444';btn.style.background='rgba(239,68,68,0.12)';}
    else{btn.textContent='\u25CF PRACTICE';btn.style.color='#10b981';btn.style.borderColor='#10b981';btn.style.background='rgba(16,185,129,0.12)';}
  }
  fetch('/api/config').then(r=>r.json()).then(d=>_updateEnvToggle((d.OANDA_ENV||'practice').toLowerCase())).catch(()=>{});
  window.showEnvSwitchModal=function(){ window.location.href='/'; };
})();
</script>

<main>
  <!-- Controls -->
  <div class="controls">
    <div class="ctrl-group">
      <label>Timeframe</label>
      <select id="tf">
        <option value="M15">M15 — 15 min</option>
        <option value="H1" selected>H1 — 1 hour</option>
        <option value="H4">H4 — 4 hour</option>
        <option value="D">D — Daily</option>
      </select>
    </div>
    <div class="ctrl-group">
      <label>Candles per pair</label>
      <select id="candles">
        <option value="200">200 — fast preview</option>
        <option value="500" selected>500 — balanced</option>
        <option value="1000">1000 — deeper test</option>
        <option value="2000">2000 — thorough</option>
      </select>
    </div>
    <div class="ctrl-group">
      <label>Starting balance</label>
      <input type="number" id="balance" value="10000" min="100" step="100" style="width:120px">
    </div>
    <button class="run-btn" id="run-btn" onclick="runBacktest()">Run Backtest</button>
  </div>

  <!-- Progress bar -->
  <div id="progress">
    <div style="font-size:14px;font-weight:600;margin-bottom:4px">Running backtest...</div>
    <div class="prog-text" id="prog-text">Fetching historical data...</div>
    <div class="prog-bar-wrap"><div class="prog-bar" id="prog-bar"></div></div>
  </div>

  <!-- Empty state -->
  <div id="empty">
    <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2">
      <path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>
    </svg>
    <div style="font-size:15px;font-weight:600;color:var(--text);margin-bottom:6px">No backtest run yet</div>
    <div style="font-size:12px">Configure the settings above and click <b>Run Backtest</b> to test the bot on historical data.</div>
  </div>

  <!-- Results -->
  <div id="results">
    <!-- KPIs -->
    <div class="kpi-grid" id="kpi-row"></div>

    <!-- Charts -->
    <div class="charts-row">
      <div class="card"><div class="card-title">Equity Curve</div>
        <div class="chart-wrap"><canvas id="equity-chart"></canvas></div></div>
      <div class="card"><div class="card-title">Win / Loss Split</div>
        <div class="chart-wrap"><canvas id="wl-chart"></canvas></div></div>
    </div>

    <!-- Per-pair breakdown -->
    <div class="card pair-table-wrap">
      <div class="card-title">Performance by Pair</div>
      <table>
        <thead><tr>
          <th>Pair</th><th>Trades</th><th>Win Rate</th>
          <th>Avg Win</th><th>Avg Loss</th><th>Profit Factor</th>
          <th>Max DD</th><th>Net P&amp;L</th>
        </tr></thead>
        <tbody id="pair-tbody"></tbody>
      </table>
    </div>

    <!-- Go live section -->
    <div class="golive">
      <div class="golive-title" id="gl-title">How did it do?</div>
      <div class="golive-sub" id="gl-sub"></div>
      <button class="golive-btn" onclick="toggleGuide()">Switch to Live Account</button>
      <div class="golive-guide" id="gl-guide">
        <ol>
          <li>Open a <b>live fxTrade account</b> at oanda.com (separate from practice)</li>
          <li>Start small — <b>$500-$1,000</b> is enough to begin</li>
          <li>Run <b>tradalgo.exe --setup</b> and enter your live account ID and API key</li>
          <li>Set risk to <b>0.5%</b> per trade for your first week</li>
          <li>Watch the Live dashboard and give it at least 2 weeks before judging</li>
        </ol>
        <a class="oanda-link" href="https://www.oanda.com" target="_blank">Open OANDA Account &rarr;</a>
      </div>
    </div>
  </div>
</main>

<script>
var equityChart = null;
var wlChart     = null;

function runBacktest() {
  var tf      = document.getElementById("tf").value;
  var candles = document.getElementById("candles").value;
  var balance = document.getElementById("balance").value || 10000;
  var btn     = document.getElementById("run-btn");

  btn.disabled    = true;
  btn.textContent = "Running...";
  document.getElementById("empty").style.display    = "none";
  document.getElementById("results").style.display  = "none";
  document.getElementById("progress").style.display = "block";

  animateProgress(candles);

  var xhr = new XMLHttpRequest();
  xhr.open("GET", "/api/backtest/run?granularity=" + tf + "&candles=" + candles + "&balance=" + balance, true);
  xhr.onreadystatechange = function() {
    if (xhr.readyState !== 4) return;
    stopProgress();
    btn.disabled    = false;
    btn.textContent = "Run Backtest";
    document.getElementById("progress").style.display = "none";

    if (xhr.status !== 200) {
      document.getElementById("empty").style.display = "block";
      document.getElementById("empty").innerHTML =
        '<div style="color:var(--red);font-size:13px">Error: ' + xhr.status
        + '<br><small>' + xhr.responseText.slice(0,200) + '</small></div>';
      return;
    }
    try {
      var d = JSON.parse(xhr.responseText);
      if (d.error) {
        document.getElementById("empty").style.display = "block";
        document.getElementById("empty").innerHTML =
          '<div style="color:var(--red);font-size:13px">' + d.error + '</div>';
        return;
      }
      renderResults(d);
      document.getElementById("last-run").textContent = "Last run: " + new Date().toLocaleTimeString();
    } catch(e) {
      document.getElementById("empty").style.display = "block";
      document.getElementById("empty").innerHTML =
        '<div style="color:var(--red)">Parse error: ' + e.message + '</div>';
    }
  };
  xhr.onerror = function() {
    stopProgress();
    btn.disabled = false; btn.textContent = "Run Backtest";
    document.getElementById("progress").style.display = "none";
    document.getElementById("empty").style.display    = "block";
    document.getElementById("empty").innerHTML = '<div style="color:var(--red)">Network error</div>';
  };
  xhr.send();
}

var progTimer = null;
function animateProgress(candles) {
  var bar  = document.getElementById("prog-bar");
  var text = document.getElementById("prog-text");
  var msgs = ["Fetching historical data...", "Running strategies...",
              "Calculating results...", "Building equity curve..."];
  var pct  = 0; var msgIdx = 0;
  bar.style.width = "0%";
  progTimer = setInterval(function() {
    pct = Math.min(pct + (100 / (candles / 5)), 92);
    bar.style.width = pct + "%";
    if (msgIdx < msgs.length - 1 && pct > (msgIdx + 1) * 25) msgIdx++;
    text.textContent = msgs[msgIdx];
  }, 400);
}
function stopProgress() {
  clearInterval(progTimer);
  var bar = document.getElementById("prog-bar");
  if (bar) bar.style.width = "100%";
}

function renderResults(d) {
  document.getElementById("results").style.display = "block";
  var s = d.summary || {};

  // KPIs
  var netPl = s.net_pl || 0;
  var wr    = s.win_rate || 0;
  var kpis  = [
    {v: (netPl>=0?"+":"") + "$" + Math.abs(netPl).toFixed(2),  l:"Net P&L",       c: netPl>=0?"green":"red"},
    {v: wr + "%",                                                l:"Win Rate",      c: wr>=50?"green":"red"},
    {v: s.total_trades || 0,                                     l:"Total Trades",  c:""},
    {v: "$" + (s.final_balance||initBal).toFixed(2),             l:"Final Balance", c: (s.final_balance||initBal)>=(s.initial_balance||initBal)?"green":"red"},
  ];
  document.getElementById("kpi-row").innerHTML = kpis.map(function(k,i) {
    return '<div class="kpi" style="animation-delay:'+(i*60)+'ms">'
      + '<div class="kv '+(k.c||"")+'">' + k.v + '</div>'
      + '<div class="kl">' + k.l + '</div></div>';
  }).join("");

  // Equity curve from trade log
  var trades = d.trades || [];
  if (equityChart) equityChart.destroy();
  var initBal = parseFloat((d.summary && d.summary.initial_balance) || 10000);
  var eqData  = [initBal];
  var eqLabels= ["Start"];
  var balance = initBal;
  trades.forEach(function(t, i) {
    balance += (t.pl_money || 0);
    if (i % Math.max(1, Math.floor(trades.length/60)) === 0) {
      eqLabels.push(t.time ? t.time.slice(5,10) : "");
      eqData.push(parseFloat(balance.toFixed(2)));
    }
  });
  eqLabels.push("Now"); eqData.push(parseFloat(balance.toFixed(2)));

  equityChart = new Chart(document.getElementById("equity-chart"), {
    type: "line",
    data: {
      labels: eqLabels,
      datasets: [{
        data: eqData,
        borderColor: netPl >= 0 ? "#2fbf71" : "#e5484d",
        backgroundColor: netPl >= 0 ? "#2fbf7118" : "#e5484d18",
        borderWidth: 2, fill: true, tension: 0.3, pointRadius: 0,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:"#4b5563", font:{size:9}, maxTicksLimit:8 }, grid:{color:"#1a2035"} },
        y: { ticks: { color:"#4b5563", font:{size:9}, callback:function(v){return "$"+v;} }, grid:{color:"#1a2035"} }
      }
    }
  });

  // Win/loss donut
  if (wlChart) wlChart.destroy();
  var wins   = s.wins || (d.trades||[]).filter(function(t){return(t.pl_pips||0)>0;}).length;
  var losses = (s.total_trades||0) - wins;
  wlChart = new Chart(document.getElementById("wl-chart"), {
    type: "doughnut",
    data: {
      labels: ["Wins","Losses"],
      datasets:[{data:[wins,losses],
        backgroundColor:["#2fbf7188","#e5484d88"],
        borderColor:["#2fbf71","#e5484d"],borderWidth:2}]
    },
    options:{
      responsive:true, maintainAspectRatio:false, cutout:"65%",
      plugins:{legend:{position:"bottom",labels:{color:"#9ca3af",font:{size:11},boxWidth:10}}}
    }
  });

  // Pair table
  var byInst = d.by_instrument || {};
  var rows   = Object.keys(byInst).map(function(inst) {
    var r  = byInst[inst];
    var pl = r.net_pl || 0;
    var wr = r.win_rate || 0;
    return '<tr>'
      + '<td><b>' + inst.replace("_","/") + '</b></td>'
      + '<td>' + (r.trades||0) + '</td>'
      + '<td><span class="'+(wr>=50?"pos":"neg")+'">' + wr + '%</span></td>'
      + '<td class="pos">+' + (r.avg_win_pips||0) + ' pips</td>'
      + '<td class="neg">' + (r.avg_loss_pips||0) + ' pips</td>'
      + '<td class="'+(r.profit_factor>=1?"pos":"neg")+'">' + (r.profit_factor||0) + '</td>'
      + '<td class="neg">' + (r.max_drawdown||0) + '%</td>'
      + '<td class="'+(pl>=0?"pos":"neg")+'">' + (pl>=0?"+":"") + '$' + Math.abs(pl).toFixed(2) + '</td>'
      + '</tr>';
  }).join("");
  document.getElementById("pair-tbody").innerHTML = rows ||
    '<tr><td colspan="8" style="color:var(--muted);text-align:center;padding:16px">No data</td></tr>';

  // Go live section
  var glTitle = document.getElementById("gl-title");
  var glSub   = document.getElementById("gl-sub");
  if (glTitle && glSub) {
    if (wr >= 55 && netPl > 0) {
      glTitle.textContent = "The bot is profitable on historical data";
      glSub.innerHTML = "It won <b>" + wr + "%</b> of trades and made <b>$" + netPl.toFixed(2)
        + "</b> on a $10,000 practice balance. Past performance doesn't guarantee future results, "
        + "but this is a promising starting point for a live account.";
    } else if (netPl > 0) {
      glTitle.textContent = "The bot made money, but barely";
      glSub.innerHTML = "Win rate is <b>" + wr + "%</b> and net profit is <b>$" + netPl.toFixed(2)
        + "</b>. It's technically profitable but the margin is thin. "
        + "Consider running more candles for a more reliable picture before going live.";
    } else {
      glTitle.textContent = "The bot lost money on this test";
      glSub.innerHTML = "Net result is <b style='color:var(--red)'>-$" + Math.abs(netPl).toFixed(2)
        + "</b> on this historical period. This doesn't mean it won't work on live markets, "
        + "but it's worth running more tests across different timeframes before switching to real money.";
    }
  }
}

function toggleGuide() {
  var g = document.getElementById("gl-guide");
  g.style.display = g.style.display === "block" ? "none" : "block";
}
</script>
</body>
</html>
"""


_SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradalgo — Settings & Credentials</title>
<style>
:root {
  --bg:#0b0e1a; --bg2:#111827; --bg3:#1a2035; --bg4:#1f2a40;
  --border:#1e2d45; --text:#e2e8f0; --muted:#94A3B8; --dim:#64748B;
  --accent:#00f2fe; --green:#10b981; --red:#ef4444; --gold:#f59e0b;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding-bottom:60px; }
header { background:var(--bg2); border-bottom:1px solid var(--border); padding:0 24px; height:60px; display:flex; align-items:center; justify-content:space-between; }
header .logo { font-size:16px; font-weight:800; color:#fff; display:flex; align-items:center; gap:8px; }
header .logo span { color:var(--accent); }
nav a { color:var(--muted); text-decoration:none; margin-left:20px; font-size:13px; font-weight:600; padding:6px 12px; border-radius:6px; transition:all .2s; }
nav a:hover, nav a.active { color:#fff; background:var(--bg3); }
.container { max-width:900px; margin:30px auto; padding:0 20px; }
.card { background:var(--bg2); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:24px; }
.card-header { font-size:15px; font-weight:700; color:#fff; margin-bottom:16px; display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--border); padding-bottom:12px; }
.form-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.form-group { display:flex; flex-direction:column; gap:6px; }
.form-group.full { grid-column:span 2; }
label { font-size:12px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }
input[type="text"], input[type="password"], input[type="number"], select {
  background:var(--bg3); border:1px solid var(--border); color:#fff; padding:10px 14px; border-radius:8px; font-size:13px; font-family:inherit; outline:none; transition:border .2s;
}
input:focus, select:focus { border-color:var(--accent); }
.btn-save { background:linear-gradient(135deg, var(--accent), #00a8ff); color:#050b14; border:none; padding:14px 28px; border-radius:8px; font-weight:800; font-size:14px; cursor:pointer; box-shadow:0 4px 15px rgba(0,242,254,0.3); transition:transform .2s; }
.btn-save:hover { transform:translateY(-2px); }
.toast { position:fixed; bottom:20px; right:20px; background:var(--green); color:#fff; padding:12px 24px; border-radius:8px; font-weight:700; display:none; box-shadow:0 10px 30px rgba(0,0,0,0.5); z-index:10000; }
</style>
</head>
<body>
<header>
  <div class="logo">⚡ TRADALGO <span>SETTINGS</span></div>
  <nav>
    <a href="/">Live</a>
    <a href="/backtest">Backtest</a>
    <a href="/performance">Performance</a>
    <a href="/settings" class="active">⚙️ Settings</a>
  </nav>
  <button id="env-toggle-btn" onclick="window.location.href='/'" title="Switch environment via Live dashboard" style="margin-left:auto;padding:5px 12px;border-radius:20px;border:1.5px solid;font-size:11px;font-weight:800;cursor:pointer;transition:all .2s;letter-spacing:0.5px;"></button>
</header>
<script>
(function(){
  function _ut(env){var b=document.getElementById('env-toggle-btn');if(!b)return;if(env==='live'){b.textContent='\u25CF LIVE';b.style.color='#ef4444';b.style.borderColor='#ef4444';b.style.background='rgba(239,68,68,0.12)';}else{b.textContent='\u25CF PRACTICE';b.style.color='#10b981';b.style.borderColor='#10b981';b.style.background='rgba(16,185,129,0.12)';}}
  fetch('/api/config').then(r=>r.json()).then(d=>_ut((d.OANDA_ENV||'practice').toLowerCase())).catch(()=>{});
})();
</script>
<div class="container">
  <form id="settingsForm">
    <!-- OANDA Credentials -->
    <div class="card">
      <div class="card-header">🔑 OANDA API Credentials</div>
      <div class="form-grid">
        <div class="form-group">
          <label>OANDA Account ID</label>
          <input type="text" id="OANDA_ACCOUNT_ID" placeholder="101-004-XXXXXXXX-001">
        </div>
        <div class="form-group">
          <label>OANDA Environment</label>
          <select id="OANDA_ENV">
            <option value="practice">Practice (Demo Account)</option>
            <option value="live">Live (Real Account)</option>
          </select>
        </div>
        <div class="form-group full">
          <label>OANDA API Token</label>
          <input type="password" id="OANDA_API_KEY" placeholder="Paste your OANDA API Key here">
        </div>
      </div>
    </div>

    <!-- Email Notifications -->
    <div class="card">
      <div class="card-header">📧 Email Alerts & Notifications</div>
      <div class="form-grid">
        <div class="form-group">
          <label>Sender Gmail</label>
          <input type="text" id="EMAIL_SENDER" placeholder="your.email@gmail.com">
        </div>
        <div class="form-group">
          <label>Recipient Email</label>
          <input type="text" id="EMAIL_RECIPIENT" placeholder="your.email@gmail.com">
        </div>
        <div class="form-group full">
          <label>Gmail App Password</label>
          <input type="password" id="EMAIL_PASSWORD" placeholder="16-character App Password">
        </div>
      </div>
    </div>

    <!-- Risk & Trading Parameters -->
    <div class="card">
      <div class="card-header">⚡ Risk Management & Trading Limits</div>
      <div class="form-grid">
        <div class="form-group">
          <label>Risk Per Trade (%)</label>
          <input type="number" step="0.1" id="RISK_PER_TRADE_PCT" placeholder="1.0">
        </div>
        <div class="form-group">
          <label>Max Simultaneous Open Trades</label>
          <input type="number" id="MAX_OPEN_TRADES" placeholder="5">
        </div>
        <div class="form-group">
          <label>Default Stop Loss (Pips)</label>
          <input type="number" id="DEFAULT_SL_PIPS" placeholder="20">
        </div>
        <div class="form-group">
          <label>Default Take Profit (Pips)</label>
          <input type="number" id="DEFAULT_TP_PIPS" placeholder="40">
        </div>
        <div class="form-group">
          <label>Trailing Stop Loss (Pips, 0 to disable)</label>
          <input type="number" id="TRAILING_STOP_PIPS" placeholder="0">
        </div>
        <div class="form-group full">
          <label>AI Sentiment API Key (Anthropic / Gemini)</label>
          <input type="password" id="AI_BIAS_API_KEY" placeholder="Optional AI API Key">
        </div>
      </div>
    </div>

    <div style="text-align:right;">
      <button type="submit" class="btn-save">💾 Save & Apply Settings</button>
    </div>
  </form>
</div>

<div class="toast" id="toast">✓ Settings saved to tradalgo_config.json!</div>

<script>
const API_TOKEN = "{{ api_token }}";

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    for (const [k, v] of Object.entries(cfg)) {
      const el = document.getElementById(k);
      if (el) el.value = (v === null || v === undefined) ? '' : v;
    }
  } catch (err) {
    console.error('Failed to load config:', err);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', loadConfig);
} else {
  loadConfig();
}

document.getElementById('settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fields = ['OANDA_ACCOUNT_ID', 'OANDA_API_KEY', 'OANDA_ENV', 'EMAIL_SENDER', 'EMAIL_PASSWORD', 'EMAIL_RECIPIENT', 'RISK_PER_TRADE_PCT', 'MAX_OPEN_TRADES', 'DEFAULT_SL_PIPS', 'DEFAULT_TP_PIPS', 'TRAILING_STOP_PIPS', 'AI_BIAS_API_KEY'];
  const payload = {};
  fields.forEach(f => {
    const el = document.getElementById(f);
    if (el) {
      payload[f] = el.type === 'number' ? (parseFloat(el.value) || 0) : el.value;
    }
  });

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tradalgo-Token': API_TOKEN
      },
      body: JSON.stringify(payload)
    });

    const d = await res.json();
    if (res.ok) {
      const toast = document.getElementById('toast');
      toast.style.background = 'var(--green)';
      toast.textContent = '✓ ' + (d.message || 'Settings saved & OANDA connection verified!');
      toast.style.display = 'block';
      setTimeout(() => { toast.style.display = 'none'; }, 4500);
      await loadConfig();
    } else {
      alert('Settings Saved, BUT OANDA Connection Warning:\n\n' + (d.message || 'Check your credentials and environment.'));
      await loadConfig();
    }
  } catch (err) {
    alert('Failed to save settings: ' + err.message);
  }
});
</script>
</body>
</html>
"""


# ── Performance page HTML ─────────────────────────────────────────────────────
_PERF_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradalgo — Performance</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0b0f19;--bg2:#111726;--bg3:#182033;--bg4:#202b42;--border:#1e293d;--text:#f8fafc;
     --muted:#8493a8;--green:#10b981;--red:#ef4444;--blue:#3b82f6;--blue-btn:#2563eb;--gold:#f59e0b;--purple:#8b5cf6;--radius:8px;
     --font-mono:ui-monospace,'SF Mono','Cascadia Mono','JetBrains Mono',Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
button{font-family:inherit}
body{background:var(--bg);color:var(--text);font:13px/1.5 system-ui,sans-serif;min-height:100vh}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:0 24px;height:48px;
       display:flex;align-items:center;gap:12px}
.logo{font-size:16px;font-weight:700}.logo span{color:var(--gold)}
nav a{color:var(--muted);text-decoration:none;padding:5px 12px;border-radius:6px;font-size:12px;font-weight:500;position:relative;transition:color .2s,background .2s}
nav a::after{content:'';position:absolute;bottom:-2px;left:50%;right:50%;height:2px;background:var(--blue);border-radius:1px;transition:left .25s,right .25s}
nav a:hover{background:var(--bg3);color:var(--text)}
nav a:hover::after,nav a.active::after{left:10px;right:10px}
nav a.active{background:var(--bg4);color:var(--text)}
nav a:hover,nav a.active{background:var(--bg3);color:var(--text)}
.hspacer{flex:1}
main{max-width:1200px;margin:0 auto;padding:20px 16px}
.ptabs{display:flex;gap:8px;margin-bottom:18px}
.pb{padding:6px 16px;border-radius:var(--radius);border:1px solid var(--border);background:var(--bg3);
    color:var(--muted);cursor:pointer;font-size:12px;font-weight:500;transition:all .18s var(--ease)}
.pb.active,.pb:hover{background:var(--blue-btn);color:#fff;border-color:var(--blue);box-shadow:0 2px 10px rgba(37,99,235,.3)}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:18px}
.kpi{background:var(--bg2);border:1px solid var(--border);border-radius:9px;padding:12px 14px}
.kv{font-size:20px;font-weight:700;font-family:var(--font-mono);font-variant-numeric:tabular-nums;line-height:1.2;margin-bottom:2px}
.kl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.charts-row{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:16px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:9px;padding:14px}
.ct{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:12px}
.cw{position:relative;height:190px}
.bot-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:11px}
th{text-align:left;color:var(--muted);padding:4px 7px;border-bottom:1px solid var(--border);
   font-size:10px;text-transform:uppercase;letter-spacing:.4px}
td{padding:6px 7px;border-bottom:1px solid #161c2e}
tr:last-child td{border:none}
.pos{color:var(--green);font-weight:600;font-family:var(--font-mono)}.neg{color:var(--red);font-weight:600;font-family:var(--font-mono)}
.pill{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700}
.buy{background:#26995a22;color:#2fbf71}.sell{background:#c33d4122;color:#e5484d}
.green{color:var(--green)}.red{color:var(--red)}
@media(max-width:900px){.charts-row,.bot-row{grid-template-columns:1fr}}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
main{animation:pageIn .3s cubic-bezier(.4,0,.2,1)}
@keyframes pageIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.kpi{transition:transform .15s,box-shadow .15s}
.kpi:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.3)}
.card{transition:box-shadow .2s}
.pb{transition:background .15s,color .15s,border-color .15s,transform .1s}
.pb:hover:not(.active){transform:translateY(-1px)}
tr:hover td{background:var(--bg3);transition:background .15s}
</style>
</head>
<body>
<header>
  <div class="logo">Trad<span>algo</span></div>
  <nav><a href="/">Live</a><a href="/backtest">Backtest</a><a href="/performance" class="active">Performance</a><a href="/settings">Settings</a></nav>
  <div class="hspacer"></div>
  <button id="env-toggle-btn" onclick="window.location.href='/'" title="Switch environment via Live dashboard" style="padding:5px 12px;border-radius:20px;border:1.5px solid;font-size:11px;font-weight:800;cursor:pointer;transition:all .2s;letter-spacing:0.5px;"></button>
  <span id="lu" style="font-size:11px;color:var(--muted);margin-left:10px;"></span>
  <a href="/api/performance/export-csv" style="padding:6px 14px;background:var(--bg3);border:1px solid var(--border);color:var(--muted);border-radius:6px;font-size:11px;font-weight:500;text-decoration:none;margin-left:8px;transition:all .2s" onmouseover="this.style.color='var(--text)';this.style.background='var(--bg4)'" onmouseout="this.style.color='var(--muted)';this.style.background='var(--bg3)'"> &#8595; Export CSV</a>
</header>
<script>
(function(){
  function _ut(env){var b=document.getElementById('env-toggle-btn');if(!b)return;if(env==='live'){b.textContent='\u25CF LIVE';b.style.color='#ef4444';b.style.borderColor='#ef4444';b.style.background='rgba(239,68,68,0.12)';}else{b.textContent='\u25CF PRACTICE';b.style.color='#10b981';b.style.borderColor='#10b981';b.style.background='rgba(16,185,129,0.12)';}}
  fetch('/api/config').then(r=>r.json()).then(d=>_ut((d.OANDA_ENV||'practice').toLowerCase())).catch(()=>{});
})();
</script>
<main>
  <div class="ptabs">
    <button class="pb" data-p="1">Today</button>
    <button class="pb" data-p="7">7 Days</button>
    <button class="pb" data-p="30">30 Days</button>
    <button class="pb active" data-p="all">All Time</button>
  </div>
  <div class="card" id="balance-card" style="margin-bottom:16px;display:none">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px">
      <div class="ct" style="margin-bottom:0">Account Balance &amp; Drawdown</div>
      <span id="bal-growth" style="font-size:12px;font-weight:600"></span>
    </div>
    <div class="cw" style="height:220px"><canvas id="bal-chart"></canvas></div>
    <div style="display:flex;gap:14px;margin-top:8px;font-size:10px;color:var(--muted)">
      <span><span style="display:inline-block;width:8px;height:8px;background:#2fbf71;border-radius:2px;margin-right:4px;vertical-align:middle"></span>Balance</span>
      <span><span style="display:inline-block;width:8px;height:8px;background:#a78bfa88;border-radius:2px;margin-right:4px;vertical-align:middle"></span>Drawdown from peak</span>
    </div>
  </div>
  <div class="kpi-grid">
    <div class="kpi"><div class="kv" id="k-pl">—</div><div class="kl">Net P&L</div></div>
    <div class="kpi"><div class="kv" id="k-wr">—</div><div class="kl">Win Rate</div></div>
    <div class="kpi"><div class="kv" id="k-t">—</div><div class="kl">Trades</div></div>
    <div class="kpi"><div class="kv blue" id="k-pf">—</div><div class="kl">Profit Factor</div></div>
    <div class="kpi"><div class="kv red" id="k-dd">—</div><div class="kl">Max Drawdown</div></div>
    <div class="kpi"><div class="kv" style="color:var(--gold)" id="k-aw">—</div><div class="kl">Avg Win</div></div>
    <div class="kpi"><div class="kv red" id="k-al">—</div><div class="kl">Avg Loss</div></div>
    <div class="kpi"><div class="kv" id="k-st">—</div><div class="kl">Streak</div></div>
    <div class="kpi"><div class="kv" style="color:var(--purple)" id="k-sharpe">—</div><div class="kl">Sharpe Ratio</div></div>
    <div class="kpi"><div class="kv" style="color:var(--purple)" id="k-sortino">—</div><div class="kl">Sortino Ratio</div></div>
  </div>
  <div class="charts-row">
    <div class="card"><div class="ct">Daily P&L — Last 14 Days</div><div class="cw"><canvas id="dc"></canvas></div></div>
    <div class="card"><div class="ct">Win / Loss Split</div><div class="cw"><canvas id="wlc"></canvas></div></div>
  </div>
  <div class="bot-row">
    <div class="card"><div class="ct">By Pair</div>
      <table><thead><tr><th>Pair</th><th>Trades</th><th>Win%</th><th>P&L</th></tr></thead>
      <tbody id="pair-b"></tbody></table></div>
    <div class="card"><div class="ct">By Strategy</div>
      <table><thead><tr><th>Strategy</th><th>Trades</th><th>Win%</th><th>P&L</th></tr></thead>
      <tbody id="strat-b"></tbody></table></div>
    <div class="card"><div class="ct">Best &amp; Worst</div><div id="bw"></div></div>
  </div>
  <div class="card" style="margin-bottom:20px"><div class="ct">Recent Trades</div>
    <div style="overflow-x:auto">
    <table><thead><tr><th>Time</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr></thead>
    <tbody id="hist-b"></tbody></table></div></div>
</main>
<script>
let ap='all',dc,wlc;
const fpl=n=>{const v=parseFloat(n||0);return`<span class="${v>=0?'pos':'neg'}">${v>=0?'+':''}$${Math.abs(v).toFixed(2)}</span>`};
const fd=iso=>iso?new Date(iso).toLocaleString('en-GB',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
var balChart = null;

async function loadBalanceChart() {
  try {
    var r = await fetch('/api/performance/balance-history');
    var d = await r.json();
    if (!d.points || d.points.length < 2) {
      document.getElementById('balance-card').style.display = 'none';
      return;
    }
    document.getElementById('balance-card').style.display = 'block';

    var growth    = d.total_growth_pct || 0;
    var growthEl  = document.getElementById('bal-growth');
    growthEl.textContent  = (growth >= 0 ? '+' : '') + growth.toFixed(2) + '% since start';
    growthEl.style.color  = growth >= 0 ? 'var(--green)' : 'var(--red)';

    var labels    = d.points.map(function(p){ return p.date; });
    var balances  = d.points.map(function(p){ return p.balance; });
    var drawdowns = d.points.map(function(p){ return -(p.drawdown_pct || 0); }); // negative = below the peak
    var positive  = growth >= 0;

    if (balChart) balChart.destroy();
    balChart = new Chart(document.getElementById('bal-chart'), {
      data: {
        labels: labels,
        datasets: [
          {
            type:  'bar',
            label: 'Drawdown',
            data:  drawdowns,
            backgroundColor: '#a78bfa33',
            borderWidth: 0,
            barPercentage: 1.0,
            categoryPercentage: 1.0,
            yAxisID: 'y1',
            order: 2,
          },
          {
            type:  'line',
            label: 'Balance',
            data:            balances,
            borderColor:     positive ? '#2fbf71' : '#e5484d',
            backgroundColor: positive ? '#2fbf7118' : '#e5484d18',
            borderWidth:     2,
            fill:            true,
            tension:         0.3,
            pointRadius:     0,
            pointHoverRadius:4,
            yAxisID: 'y',
            order: 1,
          },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function(ctx) {
                if (ctx.dataset.label === 'Drawdown') return 'Drawdown: ' + Math.abs(ctx.parsed.y).toFixed(2) + '%';
                return 'Balance: $' + ctx.parsed.y.toFixed(2);
              },
              title: function(ctx) {
                var pt = d.points[ctx[0].dataIndex];
                return pt.label ? pt.label + ' — ' + pt.date : pt.date;
              }
            }
          }
        },
        scales: {
          x: { display: false },
          y: {
            position: 'left',
            ticks: { color:'#4b5563', font:{size:10}, callback: function(v){ return '$'+v; } },
            grid:  { color:'#1a2035' }
          },
          y1: {
            position: 'right',
            suggestedMin: -Math.max(10, Math.max.apply(null, d.points.map(function(p){ return p.drawdown_pct || 0; })) * 1.8),
            max: 0,
            ticks: { color:'#4b5563', font:{size:9}, callback: function(v){ return Math.abs(v).toFixed(0)+'%'; } },
            grid:  { display: false }
          }
        }
      }
    });
  } catch(e) {
    document.getElementById('balance-card').style.display = 'none';
  }
}

async function load(p){
  const url=p==='all'?'/api/performance':`/api/performance?period=${p}`;
  const d=await(await fetch(url)).json();if(d.error)return;render(d);
  document.getElementById('lu').textContent='Updated '+new Date().toLocaleTimeString();
}
function render(d){
  const pl=parseFloat(d.net_pl||0),wr=parseFloat(d.win_rate||0);
  document.getElementById('k-pl').innerHTML=`<span class="${pl>=0?'green':'red'}">${pl>=0?'+':''}$${Math.abs(pl).toFixed(2)}</span>`;
  document.getElementById('k-wr').innerHTML=`<span class="${wr>=50?'green':'red'}">${wr}%</span>`;
  document.getElementById('k-t').textContent=d.total_trades||0;
  document.getElementById('k-pf').textContent=parseFloat(d.profit_factor||0).toFixed(2);
  document.getElementById('k-dd').textContent=`-$${parseFloat(d.max_drawdown||0).toFixed(2)}`;
  document.getElementById('k-aw').textContent=`+$${parseFloat(d.avg_win||0).toFixed(2)}`;
  document.getElementById('k-al').textContent=`-$${Math.abs(parseFloat(d.avg_loss||0)).toFixed(2)}`;
  const st=parseInt(d.current_streak||0);
  document.getElementById('k-st').innerHTML=`<span style="color:${st>=0?'var(--green)':'var(--red)'}">${st>=0?'🔥':'❄️'} ${Math.abs(st)} ${st>=0?'W':'L'}</span>`;
  const sharpeEl=document.getElementById('k-sharpe'), sortinoEl=document.getElementById('k-sortino');
  sharpeEl.textContent  = (d.sharpe===null||d.sharpe===undefined)  ? 'n/a' : parseFloat(d.sharpe).toFixed(2);
  sortinoEl.textContent = (d.sortino===null||d.sortino===undefined) ? 'n/a' : parseFloat(d.sortino).toFixed(2);
  const h=d.daily_history||[],ls=h.map(x=>x.date.slice(5)),ps=h.map(x=>parseFloat(x.pl||0));
  if(dc)dc.destroy();
  dc=new Chart(document.getElementById('dc'),{type:'bar',data:{labels:ls,datasets:[{data:ps,
    backgroundColor:ps.map(v=>v>=0?'#2fbf7188':'#e5484d88'),borderColor:ps.map(v=>v>=0?'#2fbf71':'#e5484d'),borderWidth:1,borderRadius:3}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{ticks:{color:'#4b5563',font:{size:10}},grid:{color:'#1a2035'}},
              y:{ticks:{color:'#4b5563',font:{size:10},callback:v=>`$${v}`},grid:{color:'#1a2035'}}}}});
  if(wlc)wlc.destroy();
  wlc=new Chart(document.getElementById('wlc'),{type:'doughnut',data:{labels:['Wins','Losses'],
    datasets:[{data:[d.wins||0,d.losses||0],backgroundColor:['#2fbf7188','#e5484d88'],borderColor:['#2fbf71','#e5484d'],borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom',labels:{color:'#9ca3af',font:{size:10},boxWidth:10}}}}});
  document.getElementById('pair-b').innerHTML=Object.entries(d.by_instrument||{}).map(([inst,r])=>
    `<tr><td><b>${inst.replace('_','/')}</b></td><td>${r.trades}</td><td><span class="${r.win_rate>=50?'pos':''}">${r.win_rate}%</span></td><td>${fpl(r.pl)}</td></tr>`).join('')||'<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:12px">No trades yet</td></tr>';
  document.getElementById('strat-b').innerHTML=Object.entries(d.by_strategy||{}).map(([s,r])=>
    `<tr><td><b>${s}</b></td><td>${r.trades}</td><td><span class="${r.win_rate>=50?'pos':''}">${r.win_rate}%</span></td><td>${fpl(r.pl)}</td></tr>`).join('')||'<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:12px">No trades yet</td></tr>';
  const best=d.best_trade,worst=d.worst_trade;
  document.getElementById('bw').innerHTML=(best?`<div style="margin-bottom:10px"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;margin-bottom:5px">Best</div>
    <div style="background:#26995a11;border:1px solid #26995a33;border-radius:7px;padding:9px">
      <div style="font-weight:600">${best.instrument.replace('_','/')}</div>
      <div style="color:#2fbf71;font-size:17px;font-weight:700">+$${parseFloat(best.pl).toFixed(2)}</div>
      <div style="font-size:10px;color:var(--muted)">${fd(best.closed_at)}</div></div></div>`:'')
    +(worst?`<div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;margin-bottom:5px">Worst</div>
    <div style="background:#c33d4111;border:1px solid #c33d4133;border-radius:7px;padding:9px">
      <div style="font-weight:600">${worst.instrument.replace('_','/')}</div>
      <div style="color:#e5484d;font-size:17px;font-weight:700">$${parseFloat(worst.pl).toFixed(2)}</div>
      <div style="font-size:10px;color:var(--muted)">${fd(worst.closed_at)}</div></div></div>`:'')
    ||'<div style="color:var(--muted);text-align:center;padding:20px">No closed trades yet</div>';
  const trades=(d.trades||[]).slice().reverse();
  document.getElementById('hist-b').innerHTML=trades.length?trades.map(t=>
    `<tr><td style="white-space:nowrap">${fd(t.closed_at)}</td><td><b>${(t.instrument||'').replace('_','/')}</b></td>
    <td><span class="pill ${(t.direction||'').toLowerCase()}">${t.direction||'?'}</span></td>
    <td style="font-family:var(--font-mono)">${parseFloat(t.entry||0).toFixed(5)}</td>
    <td style="font-family:var(--font-mono)">${parseFloat(t.exit||0).toFixed(5)}</td>
    <td>${fpl(t.pl)}</td><td style="color:var(--muted)">${t.reason||'—'}</td></tr>`).join('')
    :'<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">No closed trades yet</td></tr>';
}
document.querySelectorAll('.pb').forEach(b=>{b.onclick=()=>{
  document.querySelectorAll('.pb').forEach(x=>x.classList.remove('active'));b.classList.add('active');
  ap=b.dataset.p;load(ap);}});
load(ap);loadBalanceChart();setInterval(()=>load(ap),30000);setInterval(loadBalanceChart,60000);
</script>
</body>
</html>"""

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def route_index():
    # Don't use render_template_string — Jinja2 escapes quotes in JSON
    # and {{ }} conflicts with JS template literals. Use plain replace instead.
    html = _MAIN_HTML
    html = html.replace("{{ env }}", CFG["OANDA_ENV"].title())
    html = html.replace("{{ instruments }}", json.dumps(CFG["INSTRUMENTS"]))
    html = html.replace("{{ api_token }}", _LOCAL_API_TOKEN)
    return html

@app.route("/performance")
def route_performance():
    from flask import Response
    return Response(_PERF_HTML, mimetype="text/html; charset=utf-8")


@app.route("/backtest")
def route_backtest_page():
    from flask import Response
    return Response(_BACKTEST_HTML, mimetype="text/html; charset=utf-8")


@app.route("/settings")
def route_settings_page():
    from flask import Response
    html = _SETTINGS_HTML.replace("{{ api_token }}", _LOCAL_API_TOKEN)
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/account")
def route_account():
    try:
        return jsonify(OandaClient().get_account())
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 500
        msg = "OANDA Unauthorized — check API Key and Account ID in Settings" if code == 401 else str(e)
        return jsonify({"error": msg, "status_code": code}), code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/session")
def route_session():
    return jsonify(session_info())

@app.route("/api/trades")
def route_trades():
    try:
        return jsonify(OandaClient().get_open_trades())
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 500
        msg = "OANDA Unauthorized — check API Key and Account ID in Settings" if code == 401 else str(e)
        return jsonify({"error": msg, "status_code": code}), code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/candles")
def route_candles():
    instrument  = freq.args.get("instrument","EUR_USD")
    granularity = freq.args.get("granularity","H1")
    count       = int(freq.args.get("count",150))
    try:
        candles = OandaClient().get_candles(instrument, granularity, count)
        c = _closes(candles)
        def n2n(arr): return [None if (v is None or (isinstance(v,float) and np.isnan(v))) else round(float(v),6) for v in arr]
        bu,_,bl = _bollinger(c,20,2.0)
        entry_markers = [{"time":get_ledger(tid).get("opened_at",""),"direction":get_ledger(tid).get("direction",""),
                          "kind":"entry"}
                   for tid in all_ledger_ids() if get_ledger(tid).get("instrument")==instrument]
        exit_markers = [{"time":t.get("closed_at",""),"direction":t.get("direction",""),
                         "pl_pct":t.get("pl_pct",0),"reason":t.get("reason",""),"kind":"exit"}
                   for t in _perf["trades"] if t.get("instrument")==instrument][-30:]
        markers = entry_markers + exit_markers
        return jsonify({"candles":candles,"ema9":n2n(_ema(c,9)),"ema21":n2n(_ema(c,21)),
                        "ema50":n2n(_ema(c,50)),"bb_upper":n2n(bu),"bb_lower":n2n(bl),"markers":markers})
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 500
        msg = "OANDA Unauthorized — check API Key and Account ID in Settings" if code == 401 else str(e)
        return jsonify({"error": msg, "status_code": code}), code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/prices")
def route_prices():
    try:
        return jsonify(OandaClient().get_prices(CFG["INSTRUMENTS"]))
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 500
        msg = "OANDA Unauthorized — check API Key and Account ID in Settings" if code == 401 else str(e)
        return jsonify({"error": msg, "status_code": code}), code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/prices/stream")
def route_price_stream():
    q = queue.Queue(maxsize=10)
    with _sse_lock: _sse_listeners.append(q)
    def generate():
        with _price_lock:
            if _price_cache: yield f"data: {json.dumps(_price_cache)}\n\n"
        try:
            while True:
                try:    yield q.get(timeout=30)
                except queue.Empty: yield ": ping\n\n"
        except GeneratorExit:
            with _sse_lock:
                try: _sse_listeners.remove(q)
                except ValueError: pass
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/signals/quick")
def route_signals():
    result={}
    cl=OandaClient()
    for inst in CFG["INSTRUMENTS"]:
        try:
            candles=cl.get_candles(inst,"H1",100)
            con_r=consensus_signal(run_all_strategies(candles,inst),CFG["STRATEGY_WEIGHTS"])
            sig = filtered_signal(con_r,candles,inst)["signal"]
            rsi_val = _rsi(_closes(candles), 14)[-1]
            result[inst] = {"signal": sig, "rsi": None if _nan(rsi_val) else round(float(rsi_val), 1)}
        except Exception:
            result[inst] = {"signal": None, "rsi": None}
    return jsonify(result)

@app.route("/api/performance")
def route_performance_api():
    period=freq.args.get("period","all")
    days=None if period=="all" else int(period)
    try:    return jsonify(get_stats(days=days))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/performance/today")
def route_today():
    return jsonify(get_today())

@app.route("/api/backtest/latest")
def route_backtest_latest():
    bt_dir=DATA_DIR/"backtest_results"
    files=sorted(bt_dir.glob("backtest_*.json")) if bt_dir.exists() else []
    if not files: return jsonify({}),404
    return jsonify(json.loads(files[-1].read_text(encoding="utf-8")))

@app.route("/api/config", methods=["GET","POST"])
def route_config():
    if freq.method=="POST":
        updates = freq.get_json() or {}
        oanda_changed = any(k in updates for k in ("OANDA_API_KEY", "OANDA_ACCOUNT_ID", "OANDA_ENV"))
        with _cfg_lock:
            _save_config(updates)
            _candle_cache.clear()
            msg = "Saved successfully."
            test_ok = True
            if oanda_changed:
                global _client
                _client = OandaClient()
                log.info("OandaClient re-initialized after config change")
                test_ok, msg = _client.test_connection()
                if not test_ok:
                    return jsonify({"status": "error", "message": msg, "client_reinitialized": True}), 400
        return jsonify({"status": "ok", "message": msg, "client_reinitialized": oanda_changed})
    with _cfg_lock:
        return jsonify(dict(CFG))


@app.route("/api/env-switch", methods=["POST"])
def route_env_switch():
    """Switches OANDA_ENV between practice and live, reinitializes the client."""
    global _client
    data = freq.get_json() or {}
    new_env = data.get("env", "practice").lower()
    if new_env not in ("practice", "live"):
        return jsonify({"status": "error", "message": "env must be 'practice' or 'live'"}), 400

    with _cfg_lock:
        CFG["OANDA_ENV"] = new_env
        _save_config(CFG)
        _client = OandaClient()

    with _ledger_lock:
        _ledger.clear()
        _ledger_save()

    with _perf_lock:
        _perf["trades"] = []
        _perf["starting_balance"] = 0.0
        _perf_save()

    log.info(f"Environment switched to {new_env.upper()} — OandaClient re-initialized, ledger & perf reset")
    return jsonify({"status": "ok", "env": new_env, "restart_required": True})

@app.route("/api/backtest/run")
def route_backtest_run():
    """
    Runs the full backtest on all instruments and returns results.
    Accepts: ?instruments=EUR_USD,GBP_USD&granularity=H1&candles=500
    """
    inst_param  = freq.args.get("instruments", "")
    instruments = inst_param.split(",") if inst_param else CFG["INSTRUMENTS"]
    granularity = freq.args.get("granularity", CFG["BACKTEST_GRANULARITY"])
    candle_count    = int(freq.args.get("candles", 500))
    initial_balance = float(freq.args.get("balance", 10000.0))

    try:
        result = run_backtest(
            instruments    = instruments,
            granularity    = granularity,
            candle_count   = candle_count,
            initial_balance= initial_balance,
        )
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/feed")
def route_feed():
    with _feed_lock:
        snapshot = list(_feed[:30])
    return jsonify(snapshot)


@app.route("/api/pause", methods=["POST"])
def route_pause():
    """Toggle live trading on/off. State persists to config file."""
    current = CFG.get("LIVE_TRADING_ENABLED", True)
    CFG["LIVE_TRADING_ENABLED"] = not current
    _save_config(CFG)
    state = CFG["LIVE_TRADING_ENABLED"]
    log.info(f"Trading {'RESUMED' if state else 'PAUSED'} by user")
    if state:
        feed_push("session", {"title": "Trading resumed", "body": "The bot is active again and will trade when opportunities arise."})
    else:
        feed_push("info", {"title": "Trading paused", "body": "You have paused the bot. No new trades will be opened until you resume."})
    return jsonify({"live_trading": state})


@app.route("/api/reset-drawdown", methods=["POST"])
def route_reset_drawdown():
    """Resets the starting balance to the current broker balance to reset drawdown to 0%."""
    try:
        cur_bal = _client.get_balance() if _client else None
        if cur_bal is not None:
            _perf["starting_balance"] = round(float(cur_bal), 2)
            _perf["start_date"] = _utc_now().strftime("%Y-%m-%d")
            _perf_save()
            log.info(f"Drawdown baseline manually reset to ${cur_bal:,.2f}")
            feed_push("info", {"title": "Baseline Reset", "body": f"Safety limit baseline has been reset to your current balance (${cur_bal:,.2f}). Drawdown is now 0%."})
            return jsonify({"success": True, "new_baseline": _perf["starting_balance"]})
        else:
            return jsonify({"success": False, "error": "Could not fetch current balance from broker."}), 500
    except Exception as e:
        log.error(f"Error resetting drawdown baseline: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
def route_status():
    """Quick status endpoint — used by the pause button to show current state."""
    starting_bal = _perf.get("starting_balance")
    try:
        cur_bal = _client.get_balance() if _client else None
    except Exception:
        cur_bal = None
    drawdown_pct = 0.0
    if starting_bal and cur_bal:
        drawdown_pct = round((starting_bal - cur_bal) / starting_bal * 100, 1)
    return jsonify({
        "live_trading":       CFG.get("LIVE_TRADING_ENABLED", True),
        "session":            session_info(),
        "open_trades":        len(_ledger),
        "risk_guard_enabled": CFG.get("RISK_GUARD_ENABLED", True),
        "risk_guard_limit":   CFG.get("RISK_GUARD_MAX_DD_PCT", 10.0),
        "drawdown_pct":       drawdown_pct,
    })


@app.route("/api/first-run/status")
def route_first_run_status():
    return jsonify({"shown": CFG.get("FIRST_RUN_SHOWN", False)})


@app.route("/api/first-run/done", methods=["POST"])
def route_first_run_done():
    CFG["FIRST_RUN_SHOWN"] = True
    _save_config(CFG)
    return jsonify({"ok": True})


@app.route("/api/onboarding/done", methods=["POST"])
def route_onboarding_done():
    CFG["ONBOARDING_DONE"] = True
    _save_config(CFG)
    return jsonify({"ok": True})


@app.route("/api/onboarding/status")
def route_onboarding_status():
    return jsonify({"done": CFG.get("ONBOARDING_DONE", False)})


@app.route("/api/why-no-trades")
def route_why_no_trades():
    """
    Diagnostic endpoint — explains exactly why the bot isn't trading
    on each instrument right now. Open in browser to see full analysis.
    """
    import traceback
    result = {
        "session":        session_info(),
        "paused":         not CFG.get("LIVE_TRADING_ENABLED", True),
        "open_trades":    len(_ledger),
        "max_trades":     CFG["MAX_OPEN_TRADES"],
        "slots_available": CFG["MAX_OPEN_TRADES"] - len(_ledger),
        "instruments":    {},
    }

    # If paused or no slots, no need to go further
    if result["paused"]:
        result["verdict"] = "Bot is PAUSED — press Resume to start trading again"
        return jsonify(result)

    if not result["session"]["trading_active"]:
        result["verdict"] = f"Off-hours ({result['session']['utc_time']} UTC) — only trades during London 07:00-16:00 and NY 12:00-21:00"
        return jsonify(result)

    if result["slots_available"] <= 0:
        result["verdict"] = f"Max open trades reached ({CFG['MAX_OPEN_TRADES']}) — will trade again when a position closes"
        return jsonify(result)

    # Analyse each instrument
    client = OandaClient()
    already_open = {t["instrument"] for t in client.get_open_trades()}
    blocked_count = filtered_count = no_signal_count = 0

    for inst in CFG["INSTRUMENTS"]:
        inst_result = {"already_open": inst in already_open}
        if inst in already_open:
            inst_result["skip_reason"] = "Already have an open position on this pair"
            blocked_count += 1
            result["instruments"][inst] = inst_result
            continue

        try:
            candles = OandaClient().get_candles(inst, "H1", 220)
            if len(candles) < 60:
                inst_result["skip_reason"] = f"Not enough candle data ({len(candles)} candles)"
                result["instruments"][inst] = inst_result
                continue

            # Run strategies
            sr  = run_all_strategies(candles, inst)
            thresh = CFG.get("CONSENSUS_THRESHOLD", 0.45)
            con = consensus_signal(sr, CFG["STRATEGY_WEIGHTS"], threshold=thresh)

            inst_result["strategy_signals"] = {
                name: res.get("signal") for name, res in sr.items()
            }
            inst_result["consensus_signal"] = con["signal"]
            inst_result["consensus_score"]  = con["score"]

            if not con["signal"]:
                inst_result["skip_reason"] = f"No consensus signal (score {con['score']:.2f} < {thresh:.2f} threshold)"
                no_signal_count += 1
                result["instruments"][inst] = inst_result
                continue

            # Run filters
            filtered = filtered_signal(con, candles, inst)
            inst_result["filter_passed"]  = not filtered.get("filtered", False)
            inst_result["filter_reasons"] = filtered.get("reasons", [])

            if filtered.get("filtered"):
                inst_result["skip_reason"] = "Blocked by trade filter: " + (filtered.get("reasons", ["unknown"])[-1] if filtered.get("reasons") else "unknown")
                filtered_count += 1
            else:
                inst_result["skip_reason"] = None
                inst_result["would_trade"] = filtered["signal"]

        except Exception as e:
            inst_result["skip_reason"] = f"Error: {e}"
            inst_result["traceback"]   = traceback.format_exc()

        result["instruments"][inst] = inst_result

    # Summary verdict
    would_trade = [i for i,v in result["instruments"].items() if v.get("would_trade")]
    if would_trade:
        result["verdict"] = f"Bot WOULD trade {', '.join(would_trade)} right now if the cycle runs"
    elif filtered_count > 0 and no_signal_count == 0:
        result["verdict"] = f"All signals are being blocked by the trend/volatility filters — market conditions not ideal for trading"
    elif no_signal_count > 0 and filtered_count == 0:
        result["verdict"] = f"No strategy consensus on any pair — strategies disagree, no clear signal"
    else:
        result["verdict"] = f"Mix of no signals ({no_signal_count}) and filtered signals ({filtered_count}) — normal in quiet markets"

    return jsonify(result)


@app.route("/api/bias/refresh", methods=["POST"])
def route_bias_refresh():
    """Manually trigger AI bias refresh — no need to wait for hourly cycle."""
    try:
        bias = run_ai_bias()
        if bias:
            CFG["AI_BIAS_DATA"]     = bias
            CFG["AI_BIAS_LAST_RUN"] = _utc_now().strftime("%Y-%m-%d")
            _save_config(CFG)
            feed_push("info", {
                "title": "AI market analysis updated",
                "body":  bias.get("summary", "Daily market bias refreshed."),
            })
            return jsonify({"ok": True, "bias": bias})
        else:
            return jsonify({"ok": False, "error": "Claude returned empty response — check AI_BIAS_API_KEY"}), 500
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/bias")
def route_bias():
    """Returns current AI bias data for display in dashboard."""
    bias = CFG.get("AI_BIAS_DATA", {})
    return jsonify({
        "enabled":      CFG.get("AI_BIAS_ENABLED", False),
        "last_run":     CFG.get("AI_BIAS_LAST_RUN", ""),
        "news_filter":  CFG.get("NEWS_FILTER_ENABLED", True),
        "bias":         bias,
    })


@app.route("/api/news")
def route_news():
    """Returns today's high-impact news events and any current blackouts."""
    events = _fetch_news_events()
    now    = _utc_now()
    block_minutes = CFG.get("NEWS_BLOCK_MINUTES", 45)

    result = []
    for ev in events:
        delta_mins = (ev["time"] - now).total_seconds() / 60
        status = "upcoming" if delta_mins > block_minutes else                  "blocked"  if abs(delta_mins) <= block_minutes else "passed"
        result.append({
            "title":      ev["title"],
            "currency":   ev.get("currency", ""),
            "time_utc":   ev["time"].strftime("%H:%M UTC"),
            "mins_away":  int(delta_mins),
            "status":     status,
        })

    # Sort by time proximity
    result.sort(key=lambda x: abs(x["mins_away"]))

    any_blocked = any(e["status"] == "blocked" for e in result)
    return jsonify({
        "news_filter_enabled": CFG.get("NEWS_FILTER_ENABLED", True),
        "block_minutes":       block_minutes,
        "any_blocked_now":     any_blocked,
        "events":              result[:8],  # top 8 closest events
    })


@app.route("/api/performance/balance-history")
def route_balance_history():
    """
    Returns running account balance over time built from trade history,
    plus the running drawdown-from-peak at each point (not just the single
    max-drawdown figure shown elsewhere) — lets the UI show how deep *and*
    how long drawdowns actually run, not just their worst moment.
    """
    trades   = sorted(_perf.get("trades", []), key=lambda t: t.get("closed_at",""))
    start_bal = _perf.get("starting_balance") or 10000.0
    start_dt  = _perf.get("start_date", "")

    points = [{"date": start_dt or (trades[0]["closed_at"][:10] if trades else ""),
               "balance": round(start_bal, 2), "label": "Start", "drawdown_pct": 0.0}]
    running = start_bal
    peak    = start_bal
    for t in trades:
        running += t.get("pl", 0)
        peak = max(peak, running)
        drawdown_pct = round((peak - running) / peak * 100, 2) if peak > 0 else 0.0
        points.append({
            "date":    t.get("closed_at","")[:10],
            "balance": round(running, 2),
            "label":   t.get("instrument","").replace("_","/"),
            "pl":      round(t.get("pl",0), 2),
            "drawdown_pct": drawdown_pct,
        })
    return jsonify({
        "points":          points,
        "starting_balance": round(start_bal, 2),
        "current_balance":  round(running, 2),
        "total_growth_pct": round((running - start_bal) / start_bal * 100, 2) if start_bal else 0,
    })


@app.route("/api/performance/export-csv")
def route_export_csv():
    """Download all closed trades as a CSV file."""
    trades = sorted(_perf.get("trades", []), key=lambda t: t.get("closed_at",""))
    lines  = ["Date,Pair,Direction,Entry,Exit,P&L ($),P&L (%),Reason,Strategy"]
    for t in trades:
        lines.append(",".join([
            t.get("closed_at","")[:19].replace("T"," "),
            t.get("instrument","").replace("_","/"),
            t.get("direction",""),
            str(t.get("entry","")),
            str(t.get("exit","")),
            str(t.get("pl","")),
            str(t.get("pl_pct","")),
            t.get("reason","").replace(",",";"),
            t.get("strategy","").replace(",",";"),
        ]))
    csv_data = chr(10).join(lines)
    from flask import Response as _Resp
    return _Resp(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=tradalgo_trades_{_utc_now().strftime('%Y%m%d')}.csv"}
    )


@app.route("/api/licence/status")
def route_licence_status():
    return jsonify({
        "key":    "UNRESTRICTED",
        "status": "active",
        "email":  "unrestricted@tradalgo.com",
        "plan":   "lifetime",
    })


@app.route("/api/licence/activate", methods=["POST"])
def route_licence_activate():
    data = freq.get_json() or {}
    key  = data.get("key","").strip().upper()
    if not key:
        return jsonify({"valid": False, "error": "Please enter a licence key"}), 400
    result = validate_licence(key, force=True)
    return jsonify(result)


@app.route("/api/licence/deactivate", methods=["POST"])
def route_licence_deactivate():
    CFG["LICENCE_KEY"]    = ""
    CFG["LICENCE_STATUS"] = "unlicensed"
    CFG["LICENCE_EMAIL"]  = ""
    _save_config(CFG)
    return jsonify({"ok": True})


@app.route("/api/debug")
def route_debug():
    """Hit localhost:5000/api/debug in your browser to diagnose issues."""
    import traceback
    result = {
        "cfg_account_id":  CFG.get("OANDA_ACCOUNT_ID","")[:8] + "..." if CFG.get("OANDA_ACCOUNT_ID") else "EMPTY",
        "cfg_api_key":     "SET" if CFG.get("OANDA_API_KEY") else "EMPTY",
        "cfg_env":         CFG.get("OANDA_ENV","?"),
        "session_info":    session_info(),
    }
    try:
        acct = OandaClient().get_account()
        result["oanda_connection"] = "OK"
        result["balance"] = acct.get("balance","?")
        result["account_id"] = acct.get("id","?")
    except Exception as e:
        result["oanda_connection"] = "FAILED"
        result["oanda_error"] = str(e)
        result["oanda_traceback"] = traceback.format_exc()
    return jsonify(result)


def _run_flask(port):
    """Run Flask in a background thread — used by both browser and PyWebView modes."""
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)


def start_dashboard():
    """
    Start the dashboard. Uses PyWebView for a native desktop window if available,
    falls back to browser mode if PyWebView is not installed.
    """
    threading.Thread(target=_price_broadcast_loop, daemon=True).start()
    port = CFG.get("DASHBOARD_PORT", 5000)
    log.info(f"Dashboard at http://localhost:{port}")

    # Try PyWebView first — gives a proper native desktop window
    try:
        import webview

        # Start Flask in background thread
        flask_thread = threading.Thread(target=_run_flask, args=(port,), daemon=True)
        flask_thread.start()
        time.sleep(1.2)  # let Flask bind before opening window

        log.info("Starting PyWebView desktop window")
        window = webview.create_window(
            title       = "Tradalgo",
            url         = f"http://127.0.0.1:{port}",
            width       = 1280,
            height      = 820,
            min_size    = (900, 600),
            resizable   = True,
            on_top      = False,
        )
        webview.start(debug=False)
        # When window closes, shut down
        log.info("PyWebView window closed — shutting down")
        import os; os._exit(0)

    except ImportError:
        # PyWebView not installed — fall back to browser mode
        log.info("PyWebView not found — opening in browser (run: pip install pywebview)")
        flask_thread = threading.Thread(target=_run_flask, args=(port,), daemon=True)
        flask_thread.start()
        time.sleep(1.5)
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass
        # Block forever in fallback mode
        while True:
            time.sleep(60)

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 12: BOT LOOP ─────────────────────────────────────────════════════
# ══════════════════════════════════════════════════════════════════════════════

_client         = None
_last_session   = None
_seen_txn_ids   = set()

def _price_from_pips(instrument, price, pips, direction):
    return round(price + direction * pips * _pip_size(instrument), 5)

def sync_trades():
    global _seen_txn_ids
    try:
        open_trades    = _client.get_open_trades()
        oanda_open_ids = {t["id"] for t in open_trades}
        closed_ids     = all_ledger_ids() - oanda_open_ids
        # Path A: ledger diff
        for tid in closed_ids:
            entry=get_ledger(tid)
            if not entry: remove_ledger(tid); continue
            inst=entry["instrument"]; dirn=entry["direction"]
            ep=float(entry.get("entry") or 0); units=int(entry.get("units") or 1)
            try:
                closed=_client.get_trade(tid)
                pl=float(closed.get("realizedPL") or 0)
                cpx=float(closed.get("averageClosePrice") or ep)
                reason="Take Profit ✅" if (closed.get("takeProfitOrder") and pl>0) else ("Stop Loss" if pl<0 else "Manual Close")
                pl_pct=round((pl/max(ep*units,1))*100,4)
                log.info(f"{'🏆 WIN' if pl>0 else '❌ LOSS'} {inst} P&L={pl:+.2f} | {reason}")
                record_close(tid,inst,dirn,ep,cpx,pl,pl_pct,reason,entry.get("strategy",""),entry.get("opened_at",""))
                email_closed(inst,dirn,f"{ep:.5f}",f"{cpx:.5f}",pl,pl_pct,reason)
                feed_close(inst, dirn, pl, reason)
                if pl>0: email_win(inst,dirn,f"{ep:.5f}",f"{cpx:.5f}",pl,pl_pct,entry.get("strategy",""))
            except Exception as e:
                log.error(f"Path-A error {tid}: {e}")
                try: email_closed(inst,dirn,f"{ep:.5f}","?",0,0,"Closed (details unavailable)")
                except Exception as ex: log.debug(f"Path-A email fallback error: {ex}")
            finally: remove_ledger(tid)
        # Path B: transaction sweep
        try:
            for txn in _client.get_transactions(count=30):
                tid=str(txn.get("id",""))
                if not tid or tid in _seen_txn_ids: continue
                for item in txn.get("tradesClosed",[]) + ([txn.get("tradeReduced")] if txn.get("tradeReduced") else []):
                    item_tid=str(item.get("tradeID",""))
                    if item_tid in oanda_open_ids or item_tid in _seen_txn_ids: continue
                    pl=float(item.get("realizedPL") or txn.get("pl") or 0)
                    inst=txn.get("instrument","?"); cpx=float(txn.get("price") or 0)
                    le=get_ledger(item_tid)
                    ep=float(le.get("entry") or txn.get("price") or cpx)
                    dirn=le.get("direction") or ("BUY" if pl>0 else "SELL")
                    units=int(le.get("units") or abs(int(item.get("units") or 1)))
                    pl_pct=round((pl/max(ep*units,1))*100,4)
                    if cpx and item_tid not in _seen_txn_ids:
                        log.info(f"Path-B: {inst} P&L={pl:+.2f}")
                        record_close(item_tid,inst,dirn,ep,cpx,pl,pl_pct,"Closed",le.get("strategy","auto"),le.get("opened_at",""))
                        email_closed(inst,dirn,f"{ep:.5f}",f"{cpx:.5f}",pl,pl_pct,"Closed")
                        feed_close(inst, dirn, pl, "Closed")
                        if pl>0: email_win(inst,dirn,f"{ep:.5f}",f"{cpx:.5f}",pl,pl_pct,le.get("strategy","auto"))
                        _seen_txn_ids.add(item_tid)
                        if le: remove_ledger(item_tid)
                _seen_txn_ids.add(tid)
            if len(_seen_txn_ids)>500: _seen_txn_ids=set(list(_seen_txn_ids)[-250:])
        except Exception as e: log.debug(f"Path-B: {e}")
    except Exception as e: log.error(f"sync_trades: {e}")

def trading_cycle():
    global _last_session
    if not CFG.get("LIVE_TRADING_ENABLED", True):
        log.debug("Live trading disabled — skipping cycle")
        return
    info=session_info(); session=info["session"]
    if session!=_last_session:
        if info["trading_active"]: email_session(session,len(CFG["INSTRUMENTS"]))
        _last_session=session
    if not info["trading_active"]:
        log.info(f"⏸ Off-hours ({info['utc_time']} UTC) ~{minutes_until_next_session()} min to London"); return
    try: open_trades=_client.get_open_trades()
    except Exception as e: log.error(f"Fetch trades: {e}"); return
    if len(open_trades)>=CFG["MAX_OPEN_TRADES"]: log.info("Max trades reached"); return
    already_open={t["instrument"] for t in open_trades}
    candidates=[i for i in CFG["INSTRUMENTS"] if i not in already_open]
    if not candidates: return
    _client.invalidate_cache()
    t0=time.time()
    all_candles=_client.get_all_candles_parallel(candidates)
    all_prices=_client.get_prices(candidates)
    balance=_client.get_balance()
    log.info(f"Pre-fetch {time.time()-t0:.2f}s — analysing {len(candidates)} pairs…")
    slots=CFG["MAX_OPEN_TRADES"]-len(open_trades); traded=0
    for instrument in candidates:
        if traded>=slots: break
        candles=all_candles.get(instrument,[])
        if len(candles)<60: continue
        try:
            sr=run_all_strategies(candles,instrument)
            con=consensus_signal(sr,CFG["STRATEGY_WEIGHTS"],threshold=CFG.get("CONSENSUS_THRESHOLD",0.45))
            if not con["signal"]: continue
            # Apply trend + volatility filters silently
            con=filtered_signal(con,candles,instrument)
            if not con["signal"]: continue

            # News blackout check
            blocked, news_reason = is_news_blackout(instrument)
            if blocked:
                log.info(f"[{instrument}] {news_reason}")
                continue

            # AI daily bias check
            ai_allowed, ai_reason = apply_ai_bias(con["signal"], instrument)
            if not ai_allowed:
                log.info(f"[{instrument}] {ai_reason}")
                continue
            sl_pips=con["sl_pips"]; tp_pips=con["tp_pips"]
            units=_client.calculate_units(instrument,sl_pips,CFG["RISK_PER_TRADE_PCT"],balance,prices_hint=all_prices)
            if not units: continue
            if con["signal"]=="SELL": units=-units
            pd=all_prices.get(instrument,{}); ep=pd.get("ask" if con["signal"]=="BUY" else "bid",0)
            if not ep: continue
            d=+1 if con["signal"]=="BUY" else -1
            sl_p=_price_from_pips(instrument,ep,sl_pips,-d); tp_p=_price_from_pips(instrument,ep,tp_pips,d)
            max_slip=float(CFG.get("MAX_SLIPPAGE_PIPS", 2.0))
            pb_p=_price_from_pips(instrument,ep,max_slip,d) if max_slip > 0 else None
            rs=" | ".join(con["reasons"][:2]) or "multi-strategy"
            result=_client.place_market_order(
                instrument,
                units,
                stop_loss_price=sl_p,
                take_profit_price=tp_p,
                trailing_stop_pips=CFG.get("TRAILING_STOP_PIPS", 0),
                client_comment=rs,
                price_bound=pb_p
            )
            tid=(result.get("orderFillTransaction",{}).get("tradeOpened",{}).get("tradeID")
                 or result.get("orderFillTransaction",{}).get("id"))
            if not tid:
                # The order may still have filled even though we couldn't
                # parse a trade ID out of the response shape we expected.
                # Rather than silently abandoning a potentially-live,
                # untracked position, re-query OANDA's own open trades and
                # match by instrument — we know for certain there was no
                # prior open trade on this instrument (it was excluded from
                # `candidates` otherwise), so any trade found here is the
                # one that was just opened.
                log.warning(f"No trade_id parsed from order response for {instrument} — reconciling via open trades")
                try:
                    fresh_open = _client.get_open_trades()
                    match = next((t for t in fresh_open if t.get("instrument")==instrument), None)
                except Exception as e:
                    match = None
                    log.error(f"Reconciliation lookup failed for {instrument}: {e}")
                if match:
                    tid = match["id"]
                    log.info(f"Recovered trade_id {tid} for {instrument} via reconciliation")
                else:
                    log.error(f"Could not reconcile order fill for {instrument} — response: {result}")
                    email_error(f"{instrument}: an order may have filled but Tradalgo couldn't "
                                f"determine the trade ID and will not be tracking it automatically. "
                                f"Please check your OANDA account directly to confirm its status.")
                    continue
            record_open(str(tid),instrument,con["signal"],ep,abs(units),sl_p,tp_p,rs)
            email_opened(instrument,con["signal"],abs(units),f"{ep:.5f}",f"{sl_p:.5f}",f"{tp_p:.5f}",rs)
            feed_open(instrument,con["signal"],f"{ep:.5f}",f"{sl_p:.5f}",f"{tp_p:.5f}",rs)
            log.info(f"✅ {con['signal']} {instrument} | {abs(units):,}u | entry={ep:.5f} SL={sl_p:.5f} TP={tp_p:.5f}")
            traded+=1
        except Exception as e: log.error(f"{instrument}: {e}"); email_error(f"{instrument}: {e}")

def send_pending_summaries():
    # Daily summaries
    for date_str in get_unemailed_days():
        try:
            bal=_client.get_balance() if _client else 0.0
            email_daily_summary(date_str,get_stats(),bal)
            mark_daily_emailed(date_str)
            log.info(f"Daily summary sent for {date_str}")
        except Exception as e: log.error(f"Daily summary {date_str}: {e}")

    # Weekly summaries (sent the Monday after a completed week)
    for week_key in get_unemailed_weeks():
        try:
            bal = _client.get_balance() if _client else 0.0
            email_weekly_summary(week_key, get_week_stats(week_key), bal)
            mark_week_emailed(week_key)
            log.info(f"Weekly summary sent for {week_key}")
        except Exception as e: log.error(f"Weekly summary {week_key}: {e}")

def run_bot():
    global _client
    _client = OandaClient()

    # Retry OANDA connection — never kill the process (Flask must stay alive)
    connected = False
    for attempt in range(1, 6):
        try:
            acct = _client.get_account()
            print(f"\n  Account : {acct['id']} | Balance: ${float(acct['balance']):,.2f}")
            seed_from_oanda(_client.get_open_trades())
            set_starting_balance(float(acct['balance']))
            connected = True
            # Run AI bias immediately on startup if not already done today
            refresh_ai_bias_if_needed()
            _send_email("🚀 Tradalgo Started", f"Tradalgo has been successfully started and connected to OANDA account {acct['id']}.")
            break
        except Exception as e:
            print(f"  ⚠ OANDA connection attempt {attempt}/5 failed: {e}")
            if attempt < 5:
                print(f"  Retrying in 10 seconds...")
                time.sleep(10)

    if not connected:
        print("\n  ❌ Could not connect to OANDA after 5 attempts.")
        print("  Dashboard is still running at http://localhost:5000")
        print("  Check your credentials at http://localhost:5000/api/debug")
        print("  Bot will retry every 60 seconds...\n")
        # Don't exit — keep retrying in the background so dashboard stays up
        while True:
            time.sleep(60)
            try:
                acct = _client.get_account()
                print(f"  ✅ OANDA connected: {acct['id']}")
                seed_from_oanda(_client.get_open_trades())
                _send_email("🚀 Tradalgo Started", f"Tradalgo has been successfully started and connected to OANDA account {acct['id']}.")
                break
            except Exception as e:
                print(f"  ⚠ Still can't connect: {e}")

    last_bar=last_poll=last_daily=last_guard=0
    while True:
        now=time.time()
        if now-last_poll  >= 30:   sync_trades();             last_poll=now
        if now-last_bar   >= 3600: trading_cycle();           last_bar=now
        if now-last_daily >= 3600:
            send_pending_summaries()
            refresh_ai_bias_if_needed()
            last_daily=now
        if now-last_guard >= 300:
            try:
                bal = _client.get_balance()
                check_risk_guard(bal)
            except Exception as e:
                log.debug(f"Risk guard check: {e}")
            last_guard = now
        time.sleep(10)

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 13: SETUP WIZARD (GUI) ───────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _create_desktop_shortcut():
    if os.name != 'nt': return
    vbs_path = None
    try:
        import subprocess, sys
        user_profile = os.environ.get('USERPROFILE') or os.path.expanduser('~')
        desktop = os.path.join(user_profile, 'Desktop')
        if not os.path.exists(desktop): return
        path = os.path.join(desktop, 'Tradalgo.lnk').replace('"', '""')
        target = (sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)).replace('"', '""')
        work_dir = os.path.dirname(target).replace('"', '""')
        
        vbs_script = f"""
Set ws = CreateObject("WScript.Shell")
Set link = ws.CreateShortcut("{path}")
link.TargetPath = "{target}"
link.WorkingDirectory = "{work_dir}"
link.Save
"""
        vbs_path = os.path.join(os.environ.get('TEMP', work_dir), 'create_tradalgo_shortcut.vbs')
        with open(vbs_path, 'w', encoding='utf-8') as f:
            f.write(vbs_script)
        
        subprocess.run(['cscript', '//nologo', vbs_path], creationflags=0x08000000, timeout=10)
    except Exception as e:
        log.debug(f"Failed to create desktop shortcut: {e}")
    finally:
        if vbs_path and os.path.exists(vbs_path):
            try: os.remove(vbs_path)
            except Exception: pass


def run_setup(on_complete=None):
    """
    Full Tkinter GUI setup wizard.
    3 pages: OANDA → Email → Risk.
    on_complete: optional callback fired after saving (used for first-run flow).
    """
    import tkinter as tk

    cfg = _load_config()
    DARK_BG   = "#0f1623"
    CARD_BG   = "#1a2035"
    BORDER    = "#1e2d45"
    TEXT      = "#e2e8f0"
    MUTED     = "#64748b"
    GOLD      = "#d1a13c"
    GREEN     = "#2fbf71"
    RED       = "#e5484d"
    BLUE      = "#4c8fd6"
    FONT      = ("Segoe UI", 10)
    FONT_SM   = ("Segoe UI", 9)
    FONT_LG   = ("Segoe UI", 13, "bold")
    FONT_MONO = ("Consolas", 10)

    root = tk.Tk()
    root.title("Tradalgo — Setup")
    root.geometry("520x620")
    root.resizable(False, False)
    root.configure(bg=DARK_BG)
    # Centre on screen
    root.update_idletasks()
    x = (root.winfo_screenwidth()  - 520) // 2
    y = (root.winfo_screenheight() - 620) // 2
    root.geometry(f"520x620+{x}+{y}")

    # ── helpers ───────────────────────────────────────────────────────────────

    def styled_frame(parent, **kw):
        return tk.Frame(parent, bg=CARD_BG, **kw)

    def label(parent, text, color=TEXT, font=FONT, anchor="w", **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                        font=font, anchor=anchor, **kw)

    def entry(parent, textvariable, show="", width=38):
        e = tk.Entry(parent, textvariable=textvariable, show=show, width=width,
                     bg="#0b0e1a", fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=FONT_MONO,
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=BLUE)
        return e

    def btn(parent, text, command, color=BLUE, fg="#fff", width=14):
        b = tk.Button(parent, text=text, command=command, bg=color, fg=fg,
                      activebackground=color, activeforeground=fg,
                      relief="flat", font=FONT, cursor="hand2",
                      padx=12, pady=6, width=width, bd=0)
        def on_enter(e): b.config(bg=_lighten(color))
        def on_leave(e): b.config(bg=color)
        b.bind("<Enter>", on_enter); b.bind("<Leave>", on_leave)
        return b

    def _lighten(hex_color):
        h = hex_color.lstrip("#")
        r,g,b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
        r=min(255,r+30); g=min(255,g+30); b=min(255,b+30)
        return f"#{r:02x}{g:02x}{b:02x}"

    def separator(parent):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=8)

    def status(parent, text="", color=MUTED):
        lbl = tk.Label(parent, text=text, bg=parent["bg"], fg=color, font=FONT_SM, wraplength=440)
        lbl.pack(pady=(4,0))
        return lbl

    # ── variables ─────────────────────────────────────────────────────────────
    v_account_id  = tk.StringVar(value=cfg.get("OANDA_ACCOUNT_ID",""))
    v_api_key     = tk.StringVar(value=cfg.get("OANDA_API_KEY",""))
    v_env         = tk.StringVar(value=cfg.get("OANDA_ENV","practice"))
    v_email_from  = tk.StringVar(value=cfg.get("EMAIL_SENDER",""))
    v_email_pass  = tk.StringVar(value=cfg.get("EMAIL_PASSWORD",""))
    v_email_to    = tk.StringVar(value=cfg.get("EMAIL_RECIPIENT",""))
    v_risk        = tk.StringVar(value=str(cfg.get("RISK_PER_TRADE_PCT",1.0)))
    v_max_trades  = tk.StringVar(value=str(cfg.get("MAX_OPEN_TRADES",5)))

    # ── page container ────────────────────────────────────────────────────────
    pages = {}
    current_page = [0]
    page_names   = ["oanda", "email", "risk"]

    container = tk.Frame(root, bg=DARK_BG)
    container.pack(fill="both", expand=True)

    # ── header ────────────────────────────────────────────────────────────────
    hdr = tk.Frame(root, bg="#0b0e1a", pady=0)
    hdr.pack(fill="x", side="top")
    tk.Label(hdr, text="Trad", bg="#0b0e1a", fg=TEXT,
             font=("Segoe UI", 18, "bold")).pack(side="left", padx=(20,0), pady=12)
    tk.Label(hdr, text="algo", bg="#0b0e1a", fg=GOLD,
             font=("Segoe UI", 18, "bold")).pack(side="left", pady=12)
    tk.Label(hdr, text="Setup Wizard", bg="#0b0e1a", fg=MUTED,
             font=("Segoe UI", 11)).pack(side="left", padx=12, pady=12)

    # progress dots
    dot_frame = tk.Frame(hdr, bg="#0b0e1a")
    dot_frame.pack(side="right", padx=20)
    dots = []
    for i in range(3):
        d = tk.Label(dot_frame, text="●", bg="#0b0e1a",
                     fg=BLUE if i==0 else BORDER, font=("Segoe UI", 10))
        d.pack(side="left", padx=3)
        dots.append(d)

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    def update_dots(idx):
        for i,d in enumerate(dots):
            d.config(fg=BLUE if i==idx else (GREEN if i<idx else BORDER))

    # ── navigation ────────────────────────────────────────────────────────────
    nav = tk.Frame(root, bg=DARK_BG, pady=10)
    nav.pack(fill="x", side="bottom")
    tk.Frame(root, bg=BORDER, height=1).pack(fill="x", side="bottom")

    status_lbl = tk.Label(nav, text="", bg=DARK_BG, fg=MUTED, font=FONT_SM, wraplength=300)
    status_lbl.pack(side="left", padx=20)

    def set_status(msg, color=MUTED):
        status_lbl.config(text=msg, fg=color)
        root.update_idletasks()

    btn_next = btn(nav, "Next →", lambda: navigate(1), color=BLUE)
    btn_next.pack(side="right", padx=10)
    btn_back = btn(nav, "← Back", lambda: navigate(-1), color="#374151", width=10)
    btn_back.pack(side="right", padx=4)
    btn_back.config(state="disabled")

    def navigate(direction):
        idx = current_page[0] + direction
        if idx < 0 or idx >= len(page_names): return
        if direction > 0 and not validate_page(page_names[current_page[0]]):
            return
        pages[page_names[current_page[0]]].pack_forget()
        current_page[0] = idx
        pages[page_names[idx]].pack(fill="both", expand=True, padx=24, pady=16)
        update_dots(idx)
        btn_back.config(state="normal" if idx > 0 else "disabled")
        if idx == len(page_names)-1:
            btn_next.config(text="Save & Launch ✓", command=save_and_launch, width=18)
        else:
            btn_next.config(text="Next →", command=lambda: navigate(1), width=14)
        set_status("")

    def validate_page(name):
        if name == "oanda":
            if not v_account_id.get().strip():
                set_status("Account ID is required", RED); return False
            if not v_api_key.get().strip():
                set_status("API Key is required", RED); return False
        if name == "email":
            sender = v_email_from.get().strip()
            if sender and "@" not in sender:
                set_status("Enter a valid email address", RED); return False
        return True

    def save_and_launch():
        # Collect values from all wizard fields
        cfg["OANDA_ACCOUNT_ID"]  = v_account_id.get().strip()
        cfg["OANDA_API_KEY"]     = v_api_key.get().strip()
        cfg["OANDA_ENV"]         = v_env.get()
        cfg["EMAIL_SENDER"]      = v_email_from.get().strip()
        cfg["EMAIL_PASSWORD"]    = v_email_pass.get().strip()
        cfg["EMAIL_RECIPIENT"]   = v_email_to.get().strip() or v_email_from.get().strip()
        cfg["EMAIL_ENABLED"]     = bool(cfg["EMAIL_SENDER"])
        try:
            cfg["RISK_PER_TRADE_PCT"]    = float(v_risk.get() or 1.0)
            cfg["MAX_OPEN_TRADES"]    = int(v_max_trades.get() or 5)
            cfg["RISK_GUARD_ENABLED"]    = bool(v_risk_guard_enabled.get())
            cfg["RISK_GUARD_MAX_DD_PCT"] = float(v_risk_guard_pct.get() or 10.0)
            cfg["NEWS_FILTER_ENABLED"]   = bool(v_news_enabled.get())
            cfg["AI_BIAS_ENABLED"]       = bool(v_ai_enabled.get())
            cfg["AI_BIAS_API_KEY"]       = v_ai_key.get().strip()
        except ValueError:
            set_status("Risk % and Max Trades must be numbers", RED); return

        # Write to disk
        _save_config(cfg)
        
        # Automatically create desktop shortcut on first setup
        _create_desktop_shortcut()

        # Update the live module-level CFG dict so bot/dashboard pick it up
        # immediately without needing a restart
        CFG.update(cfg)

        set_status("✅ Saved! Starting…", GREEN)
        root.update()           # force UI repaint so user sees the message

        # Destroy AFTER the repaint — this exits mainloop and returns
        # control to whichever caller called run_setup()
        root.after(600, root.destroy)

    # ══ PAGE 1: OANDA ═════════════════════════════════════════════════════════
    p1 = tk.Frame(container, bg=DARK_BG)
    pages["oanda"] = p1

    label(p1, "OANDA Credentials", color=TEXT, font=FONT_LG).pack(anchor="w", pady=(0,4))
    label(p1, "Connect Tradalgo to your OANDA practice account.", color=MUTED, font=FONT_SM).pack(anchor="w")
    separator(p1)

    card1 = styled_frame(p1)
    card1.pack(fill="x", pady=(0,12))
    card1.configure(padx=14, pady=12, relief="flat",
                    highlightthickness=1, highlightbackground=BORDER)

    label(card1, "Account ID", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card1, "Format: 101-004-XXXXXXXX-001", color=BORDER, font=("Segoe UI",8)).pack(anchor="w")
    e_acct = entry(card1, v_account_id); e_acct.pack(fill="x", pady=(4,10))

    label(card1, "API Token", color=MUTED, font=FONT_SM).pack(anchor="w")
    e_key = entry(card1, v_api_key, show="•"); e_key.pack(fill="x", pady=(4,10))

    label(card1, "Environment", color=MUTED, font=FONT_SM).pack(anchor="w")
    env_frame = tk.Frame(card1, bg=CARD_BG); env_frame.pack(anchor="w", pady=(4,4))
    for val, txt in [("practice","Practice (risk-free demo)"),("live","Live (real money)")]:
        rb = tk.Radiobutton(env_frame, text=txt, variable=v_env, value=val,
                            bg=CARD_BG, fg=TEXT, selectcolor="#0b0e1a",
                            activebackground=CARD_BG, activeforeground=TEXT,
                            font=FONT, cursor="hand2")
        rb.pack(side="left", padx=(0,16))

    link_frame = tk.Frame(p1, bg=DARK_BG); link_frame.pack(anchor="w", pady=4)
    label(link_frame, "Don't have an account?  ", color=MUTED, font=FONT_SM).pack(side="left")
    lnk = label(link_frame, "oanda.com → Open Free Demo", color=BLUE, font=FONT_SM)
    lnk.pack(side="left")
    lnk.bind("<Button-1>", lambda e: __import__("webbrowser").open(
        "https://www.oanda.com/us-en/trading/fxtrader/practice/"))
    lnk.config(cursor="hand2")

    label(p1, "API token: My Account → Manage API Access → Generate", color=MUTED, font=FONT_SM).pack(anchor="w", pady=2)

    # ══ PAGE 2: EMAIL ═════════════════════════════════════════════════════════
    p2 = tk.Frame(container, bg=DARK_BG)
    pages["email"] = p2

    label(p2, "Email Alerts", color=TEXT, font=FONT_LG).pack(anchor="w", pady=(0,4))
    label(p2, "Get notified when trades open, close, win, or lose.", color=MUTED, font=FONT_SM).pack(anchor="w")
    separator(p2)

    card2 = styled_frame(p2)
    card2.pack(fill="x", pady=(0,10))
    card2.configure(padx=14, pady=12, highlightthickness=1, highlightbackground=BORDER)

    label(card2, "Gmail Address", color=MUTED, font=FONT_SM).pack(anchor="w")
    entry(card2, v_email_from).pack(fill="x", pady=(4,10))

    label(card2, "Gmail App Password", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card2, "Not your login password — generate one at myaccount.google.com → Security → App Passwords",
          color=BORDER, font=("Segoe UI",8), wraplength=420).pack(anchor="w")
    entry(card2, v_email_pass, show="•").pack(fill="x", pady=(4,10))

    label(card2, "Send Alerts To", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card2, "Leave blank to send to yourself", color=BORDER, font=("Segoe UI",8)).pack(anchor="w")
    entry(card2, v_email_to).pack(fill="x", pady=(4,4))

    lnk2_f = tk.Frame(p2, bg=DARK_BG); lnk2_f.pack(anchor="w", pady=4)
    label(lnk2_f, "How to create an App Password: ", color=MUTED, font=FONT_SM).pack(side="left")
    lnk2 = label(lnk2_f, "Step-by-step guide →", color=BLUE, font=FONT_SM); lnk2.pack(side="left")
    lnk2.bind("<Button-1>", lambda e: __import__("webbrowser").open(
        "https://support.google.com/accounts/answer/185833"))
    lnk2.config(cursor="hand2")

    label(p2, "You can skip email setup and configure it later by running  tradalgo.exe --setup  again.",
          color=MUTED, font=("Segoe UI",8), wraplength=460).pack(anchor="w", pady=(8,0))

    # ══ PAGE 3: RISK ══════════════════════════════════════════════════════════
    p3 = tk.Frame(container, bg=DARK_BG)
    pages["risk"] = p3

    label(p3, "Risk Management", color=TEXT, font=FONT_LG).pack(anchor="w", pady=(0,4))
    label(p3, "Conservative defaults recommended for first-time use.", color=MUTED, font=FONT_SM).pack(anchor="w")
    separator(p3)

    card3 = styled_frame(p3)
    card3.pack(fill="x", pady=(0,10))
    card3.configure(padx=14, pady=12, highlightthickness=1, highlightbackground=BORDER)

    label(card3, "Risk Per Trade (%)", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card3, "% of account balance to risk on each trade. Recommended: 1.0",
          color=BORDER, font=("Segoe UI",8)).pack(anchor="w")
    risk_e = entry(card3, v_risk, width=10); risk_e.pack(anchor="w", pady=(4,12))

    label(card3, "Maximum Simultaneous Trades", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card3, "Bot will pause opening new trades once this limit is reached. Recommended: 5",
          color=BORDER, font=("Segoe UI",8)).pack(anchor="w")
    entry(card3, v_max_trades, width=10).pack(anchor="w", pady=(4,4))

    # Risk guard section
    sep_frame = tk.Frame(card3, bg=BORDER, height=1)
    sep_frame.pack(fill="x", pady=(10,8))
    label(card3, "Safety Limit", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card3, "Auto-pause if account drops this % from starting balance. Recommended: 10%. Set to 0 to disable.",
          color=BORDER, font=("Segoe UI",8), wraplength=420).pack(anchor="w")
    v_risk_guard_enabled = tk.BooleanVar(value=cfg.get("RISK_GUARD_ENABLED", True))
    v_risk_guard_pct     = tk.StringVar(value=str(cfg.get("RISK_GUARD_MAX_DD_PCT", 10.0)))
    guard_row = tk.Frame(card3, bg=CARD_BG); guard_row.pack(anchor="w", pady=(4,4))
    tk.Checkbutton(guard_row, text="Enable (pause if account drops", variable=v_risk_guard_enabled,
                   bg=CARD_BG, fg=TEXT, selectcolor="#0b0e1a", activebackground=CARD_BG,
                   activeforeground=TEXT, font=FONT, cursor="hand2").pack(side="left")
    entry(guard_row, v_risk_guard_pct, width=5).pack(side="left", padx=(6,4))
    label(guard_row, "%)", color=MUTED, font=FONT_SM).pack(side="left")




    # News filter section
    card_news = styled_frame(p3)
    card_news.pack(fill="x", pady=(8,8))
    card_news.configure(padx=14, pady=12, highlightthickness=1, highlightbackground=BORDER)
    label(card_news, "News Filter", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card_news, "Blocks trading 45 minutes before and after high-impact economic events (NFP, CPI, rate decisions, etc).",
          color=BORDER, font=("Segoe UI",8), wraplength=420).pack(anchor="w")
    v_news_enabled = tk.BooleanVar(value=cfg.get("NEWS_FILTER_ENABLED", True))
    tk.Checkbutton(card_news, text="Enable news filter (recommended)", variable=v_news_enabled,
                   bg=CARD_BG, fg=TEXT, selectcolor="#0b0e1a", activebackground=CARD_BG,
                   activeforeground=TEXT, font=FONT, cursor="hand2").pack(anchor="w", pady=(4,4))

    # AI bias section
    card_ai = styled_frame(p3)
    card_ai.pack(fill="x", pady=(0,8))
    card_ai.configure(padx=14, pady=12, highlightthickness=1, highlightbackground=BORDER)
    label(card_ai, "AI Daily Market Bias (Optional)", color=MUTED, font=FONT_SM).pack(anchor="w")
    label(card_ai, "Uses Claude AI to analyse market conditions once per day and adjust which pairs the bot trades. Requires an Anthropic API key.",
          color=BORDER, font=("Segoe UI",8), wraplength=420).pack(anchor="w")
    v_ai_enabled  = tk.BooleanVar(value=cfg.get("AI_BIAS_ENABLED", False))
    v_ai_key      = tk.StringVar(value=cfg.get("AI_BIAS_API_KEY", ""))
    tk.Checkbutton(card_ai, text="Enable AI daily bias", variable=v_ai_enabled,
                   bg=CARD_BG, fg=TEXT, selectcolor="#0b0e1a", activebackground=CARD_BG,
                   activeforeground=TEXT, font=FONT, cursor="hand2").pack(anchor="w", pady=(4,6))
    label(card_ai, "Anthropic API Key", color=MUTED, font=FONT_SM).pack(anchor="w")
    entry(card_ai, v_ai_key, show="").pack(fill="x", pady=(4,4))
    lnk_ai = tk.Frame(p3, bg=DARK_BG); lnk_ai.pack(anchor="w", pady=(0,8))
    label(lnk_ai, "Get a free API key: ", color=MUTED, font=FONT_SM).pack(side="left")
    lnk_a = label(lnk_ai, "console.anthropic.com", color=BLUE, font=FONT_SM)
    lnk_a.pack(side="left"); lnk_a.config(cursor="hand2")
    lnk_a.bind("<Button-1>", lambda e: __import__("webbrowser").open("https://console.anthropic.com"))

    # Risk guide
    guide = styled_frame(p3)
    guide.pack(fill="x", pady=(0,4))
    guide.configure(padx=14, pady=10, highlightthickness=1, highlightbackground=BORDER)
    label(guide, "Risk Guide", color=MUTED, font=FONT_SM).pack(anchor="w", pady=(0,6))
    for pct, desc, color in [
        ("0.5%", "Conservative — recommended for live trading first week", MUTED),
        ("1.0%", "Moderate — standard setting for practice", GREEN),
        ("2.0%", "Aggressive — higher reward, higher risk", GOLD),
    ]:
        row = tk.Frame(guide, bg=CARD_BG); row.pack(fill="x", pady=1)
        label(row, pct, color=color, font=("Consolas",10,"bold")).pack(side="left", padx=(0,10))
        label(row, desc, color=MUTED, font=FONT_SM).pack(side="left")

    # ── show first page ────────────────────────────────────────────────────────
    pages["oanda"].pack(fill="both", expand=True, padx=24, pady=16)
    update_dots(0)
    root.mainloop()


def run_setup_if_needed():
    """
    Shows the GUI wizard synchronously on the main thread.
    root.after(600, root.destroy) in save_and_launch exits mainloop,
    returning here. We then reload config from disk.
    """
    run_setup(on_complete=None)
    # Reload from disk after wizard writes tradalgo_config.json
    CFG.update(_load_config())

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 14: EMAIL TEST ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def run_email_test():
    print("\n" + "="*52)
    print("  Tradalgo - Email Diagnostic")
    print("="*52 + "\n")
    sender_str = str(CFG.get("EMAIL_SENDER") or "").strip()
    if not sender_str or "your" in sender_str.lower():
        print("  [X] EMAIL_SENDER not configured. Run --setup first.\n"); return
    print(f"  Sender    : {sender_str}")
    print(f"  Recipient : {CFG['EMAIL_RECIPIENT']}")
    print(f"  SMTP      : {CFG['SMTP_HOST']}:{CFG['SMTP_PORT']}\n")
    print("  Testing SMTP connection...")
    try:
        sender   = str(CFG.get("EMAIL_SENDER", "")).strip()
        password = str(CFG.get("EMAIL_PASSWORD", "")).replace(" ", "").strip()
        with smtplib.SMTP(CFG.get("SMTP_HOST", "smtp.gmail.com"), int(CFG.get("SMTP_PORT", 587)), timeout=10) as s:
            s.ehlo(); s.starttls(); s.login(sender, password)
        print("  [OK] Gmail login OK\n")
    except smtplib.SMTPAuthenticationError as e:
        print(f"  [X] Authentication failed for {CFG.get('EMAIL_SENDER')} - check App Password in config ({e})\n"); return
    except Exception as e:
        print(f"  [X] SMTP error: {e}\n"); return
    print("  Sending test emails...")
    email_session("London", 10)
    email_opened("EUR_USD","BUY",10000,"1.08450","1.08250","1.08850","EMA Cross (TEST)")
    email_closed("EUR_USD","BUY","1.08450","1.08250",-20.0,-0.20,"Stop Loss")
    email_closed("EUR_USD","BUY","1.08450","1.08850",40.0,0.40,"Take Profit")
    email_win("EUR_USD","BUY","1.08450","1.08850",40.0,0.40,"EMA Cross (TEST)")

    # Weekly report test — uses sample data since real history may not exist yet
    test_week_stats = {
        "trades": 12, "wins": 8, "losses": 4, "win_rate": 66.7, "pl": 94.20,
        "best_trade":  {"instrument":"XAU_USD","pl":47.20},
        "worst_trade": {"instrument":"GBP_USD","pl":-12.50},
        "by_instrument": {
            "EUR_USD": {"trades":5,"wins":3,"win_rate":60.0,"pl":22.10},
            "XAU_USD": {"trades":4,"wins":3,"win_rate":75.0,"pl":58.30},
            "GBP_USD": {"trades":3,"wins":2,"win_rate":66.7,"pl":13.80},
        },
    }
    test_balance = _perf.get("starting_balance") or 10000.0
    if _perf.get("starting_balance") is None:
        set_starting_balance(test_balance)
    email_weekly_summary("2026-W24", test_week_stats, test_balance * 1.0094)

    print("  [OK] 6 emails sent - check your inbox (and spam folder)\n")

# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 15: MAIN ENTRY POINT ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _print_banner():
    print()
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        print("  ████████╗██████╗  █████╗ ██████╗  █████╗ ██╗      ██████╗  ██████╗")
        print("     ██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔══██╗██║     ██╔════╝ ██╔═══██╗")
        print("     ██║   ██████╔╝███████║██║  ██║███████║██║     ██║  ███╗██║   ██║")
        print("     ██║   ██╔══██╗██╔══██║██║  ██║██╔══██║██║     ██║   ██║██║   ██║")
        print("     ██║   ██║  ██║██║  ██║██████╔╝██║  ██║███████╗╚██████╔╝╚██████╔╝")
        print("     ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝")
    except Exception:
        print("  TRADALGO -- Automated Forex Trading Bot")
    print()
    print(f"  Config : {CONFIG_FILE}")
    print(f"  Data   : {DATA_DIR}")
    print(f"  Account: {CFG['OANDA_ACCOUNT_ID'] or '⚠ not set — run --setup'}")
    print(f"  Env    : {CFG['OANDA_ENV']}")
    print()

def main():
    parser = argparse.ArgumentParser(prog="tradalgo", add_help=True)
    parser.add_argument("--bot",      action="store_true", help="Run bot only (no dashboard)")
    parser.add_argument("--dash",     action="store_true", help="Run dashboard only")
    parser.add_argument("--backtest", action="store_true", help="Run backtest and exit")
    parser.add_argument("--email",    action="store_true", help="Test email config and exit")
    parser.add_argument("--setup",    action="store_true", help="First-time setup wizard")
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return
    if args.email:
        run_email_test()
        return

    _print_banner()

    # Licensing check disabled

    # ── First-run: show GUI wizard if credentials are missing ────────────────
    if not CFG["OANDA_ACCOUNT_ID"] or not CFG["OANDA_API_KEY"]:
        print("  No credentials found — opening setup wizard...\n")
        run_setup_if_needed()   # blocks until wizard closes; CFG updated inside
        if not CFG["OANDA_ACCOUNT_ID"] or not CFG["OANDA_API_KEY"]:
            print("  Setup incomplete. Run tradalgo.exe again when ready.\n")
            return

    if args.backtest:
        print("  Running backtest…\n")
        result = run_backtest()
        s = result["summary"]
        print(f"\n  Trades : {s['total_trades']}")
        print(f"  Win %  : {s['win_rate']}%")
        print(f"  Net P&L: ${s['net_pl']:+.2f}")
        print(f"  Saved to {DATA_DIR / 'backtest_results'}\n")
        return

    if args.dash:
        start_dashboard(); return

    if args.bot:
        run_bot(); return

    # Default: Flask on main thread, bot in background thread.
    # Flask MUST be on the main thread — if it runs as a daemon it dies
    # the moment run_bot() exits or crashes, giving ERR_CONNECTION_REFUSED.
    port = CFG.get("DASHBOARD_PORT", 5000)
    print(f"  Starting bot + dashboard…")
    print(f"  Dashboard → http://localhost:{port}\n")

    def _handle_exit(sig, frame):
        print("\n  Shutting down…"); sys.exit(0)
    signal.signal(signal.SIGINT,  _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    # Bot runs in a background thread — crashes here never kill Flask
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Flask / PyWebView blocks here on the main thread — keeps process alive
    start_dashboard()

if __name__ == "__main__":
    main()
