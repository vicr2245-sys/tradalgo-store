"""
Forex Trading Bot Configuration
Edit this file to configure your OANDA credentials, email, and trading preferences.
"""

# ─── OANDA API ───────────────────────────────────────────────────────────────
OANDA_ACCOUNT_ID = "YOUR_ACCOUNT_ID"          # e.g. "101-001-12345678-001"
OANDA_API_KEY    = "YOUR_API_KEY"             # from OANDA developer portal
OANDA_ENV        = "practice"                 # "practice" or "live"

OANDA_API_URL    = (
    "https://api-fxpractice.oanda.com"
    if OANDA_ENV == "practice"
    else "https://api-fxtrade.oanda.com"
)
OANDA_STREAM_URL = (
    "https://stream-fxpractice.oanda.com"
    if OANDA_ENV == "practice"
    else "https://stream-fxtrade.oanda.com"
)

# ─── EMAIL ALERTS ─────────────────────────────────────────────────────────────
EMAIL_ENABLED   = True
EMAIL_SENDER    = "your.email@gmail.com"
EMAIL_PASSWORD  = "your-app-password"         # Gmail App Password (not login password)
EMAIL_RECIPIENT = "your.email@gmail.com"
SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 587

# ─── TRADING PAIRS ───────────────────────────────────────────────────────────
INSTRUMENTS = [
    "EUR_USD",
    "GBP_USD",
    "USD_JPY",
    "USD_CHF",
    "AUD_USD",
    "USD_CAD",
    "NZD_USD",
    "EUR_GBP",
    "EUR_JPY",
    "XAU_USD",   # Gold
]

# ─── SESSIONS (UTC) ──────────────────────────────────────────────────────────
LONDON_OPEN  = (7, 0)    # 07:00 UTC
LONDON_CLOSE = (16, 0)   # 16:00 UTC
NY_OPEN      = (12, 0)   # 12:00 UTC
NY_CLOSE     = (21, 0)   # 21:00 UTC

# ─── RISK MANAGEMENT ─────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT    = 1.0    # % of account balance to risk per trade
MAX_OPEN_TRADES       = 5      # maximum simultaneous open trades
DEFAULT_SL_PIPS       = 20     # stop-loss in pips (used if strategy doesn't override)
DEFAULT_TP_PIPS       = 40     # take-profit in pips (2:1 R:R)
GOLD_SL_PIPS          = 200    # wider SL for gold (different pip value)
GOLD_TP_PIPS          = 400

# ─── STRATEGY WEIGHTS (must sum to 1.0) ──────────────────────────────────────
STRATEGY_WEIGHTS = {
    "EMA_Cross":       0.25,
    "RSI_Reversal":    0.20,
    "Bollinger_Break": 0.20,
    "MACD_Momentum":   0.20,
    "Session_Break":   0.15,
}

# ─── BACKTESTING ─────────────────────────────────────────────────────────────
BACKTEST_GRANULARITY = "H1"     # M1, M5, M15, H1, H4, D
BACKTEST_CANDLES     = 5000     # number of historical candles to fetch

# ─── LOGGING ─────────────────────────────────────────────────────────────────
LOG_FILE  = "logs/bot.log"
LOG_LEVEL = "INFO"
