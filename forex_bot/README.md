# Tradalgo — Forex Trading Bot

## Quick Start

```bash
pip install -r requirements.txt

# 1. Fill in your credentials (see Setup below)
# 2. Run the bot
python bot.py

# 3. Open the live dashboard (separate terminal)
python dashboard.py
# → http://localhost:5000

# 4. Run a backtest first (no real trades)
python backtest.py
```

---

## Setup

### Step 1 — OANDA Practice Account (free, risk-free)
1. Go to **https://www.oanda.com** → Open a Free Demo Account
2. After login: **My Account → Manage API Access → Generate**
3. Copy your **API Token** and **Account ID** (format: `101-004-XXXXXXXX-001`)

### Step 2 — Gmail App Password
1. Google Account → **Security → 2-Step Verification** (enable if not already)
2. Search "App Passwords" → create one named "Tradalgo"
3. Copy the 16-character password shown

### Step 3 — Fill in `config.py`
```python
OANDA_ACCOUNT_ID = "101-004-XXXXXXXX-001"
OANDA_API_KEY    = "your-api-token-here"

EMAIL_SENDER    = "you@gmail.com"
EMAIL_PASSWORD  = "xxxx xxxx xxxx xxxx"   # App Password, not login password
EMAIL_RECIPIENT = "you@gmail.com"
```

---

## Live Dashboard (http://localhost:5000)

Run `python dashboard.py` and open your browser.

**What you see:**
- **Candlestick chart** with live price updates (no page refresh needed)
- **EMA 9 / 21 / 50** overlay lines colour-coded blue / amber / purple
- **Bollinger Bands** shown as dashed lines
- **Trade entry arrows** — green arrow up for BUY, red arrow down for SELL
- **Timeframe switcher** — M5 / M15 / H1 / H4 / D
- **10 instrument pairs** in the left sidebar with live price + signal badge
- **Open trades panel** showing entry, SL, TP, live unrealised P&L
- **Account summary** — balance, NAV, margin used, open trade count
- **Session status** — shows London / New York / Overlap / Off-hours

---

## Email Alerts

You receive an email for every:

| Event | Subject example |
|---|---|
| Trade opened | `🟢 Trade Opened: BUY EUR_USD` |
| Trade closed (loss) | `❌ Trade Closed: EUR_USD \| LOSS -$18.50` |
| Trade closed (win) | `✅ Trade Closed: EUR_USD \| WIN +$37.20` |
| **Win (extra alert)** | `🏆 WIN: EUR_USD +$37.20 (+1.24%)` |
| Session started | `🕐 London Session Started` |
| Bot error | `⚠️ ForexBot Error` |

**Win trades send TWO emails** — a standard close summary and a dedicated
🏆 win email so wins always stand out in your inbox.

**Emails survive restarts** — the trade ledger is saved to `logs/trade_ledger.json`
so if you restart the bot while trades are open, close/win emails still fire correctly.

---

## The 5 Strategies

| Strategy | Signal Logic |
|---|---|
| **EMA Cross** | EMA(9) crosses EMA(21), above/below EMA(50) trend filter |
| **RSI Reversal** | RSI recovers through 30 (oversold) or 70 (overbought) |
| **Bollinger Break** | Price closes outside Bollinger Bands (20, 2σ) |
| **MACD Momentum** | MACD line crosses signal, histogram confirms direction |
| **Session Breakout** | Price breaks the session's opening range |

Trades only fire when weighted consensus score ≥ 35% (at least 2 strategies agreeing).

---

## Risk Management
- **1% of balance risked per trade** (change `RISK_PER_TRADE_PCT` in config)
- **Stop loss + take profit set server-side on OANDA** — protected even if bot goes offline
- **Max 5 simultaneous trades** (`MAX_OPEN_TRADES` in config)
- **London + New York sessions only** (07:00–21:00 UTC)

---

## Latency Optimisations
- Persistent HTTP session with TCP keep-alive (saves ~50–100ms per request)
- All 10 candle feeds fetched in **parallel** via ThreadPoolExecutor
- All prices fetched in a **single batch request** per cycle
- Account balance fetched **once** per cycle, reused for all position sizing
- **55-minute candle cache** — no redundant API calls within the same bar

---

## File Structure
```
forex_bot/
├── config.py                ← edit this first
├── bot.py                   ← live trading loop
├── dashboard.py             ← web dashboard (http://localhost:5000)
├── backtest.py              ← historical backtesting
├── requirements.txt
├── strategies/
│   └── signals.py           ← all 5 strategies + consensus
├── utils/
│   ├── oanda_client.py      ← OANDA REST API (persistent session, parallel fetch)
│   ├── email_alerts.py      ← Gmail alerts (open/close/win/loss/session/error)
│   ├── trade_tracker.py     ← persistent trade ledger (survives restarts)
│   ├── indicators.py        ← EMA, RSI, MACD, Bollinger, ATR
│   └── sessions.py          ← London/NY session detection
├── logs/
│   ├── bot.log              ← auto-created
│   └── trade_ledger.json    ← auto-created (persists across restarts)
└── backtest_results/        ← auto-created
```

---

## Going Live
1. In `config.py`: set `OANDA_ENV = "live"`
2. Replace API key + account ID with your live credentials
3. Lower `RISK_PER_TRADE_PCT = 0.5` for your first week
4. Watch the dashboard closely for the first few sessions
