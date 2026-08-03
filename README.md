# Tradalgo

Automated forex trading bot for OANDA. 10 pairs, 5 strategies, live dashboard.

## First Time

1. Double-click **build.bat** → produces `tradalgo.exe` (takes ~90 seconds)
2. Run `tradalgo.exe --setup` → enter your OANDA + Gmail credentials
3. Run `tradalgo.exe` → starts the bot and opens the dashboard

## Commands

| Command | What it does |
|---|---|
| `tradalgo.exe` | Bot + live dashboard (normal use) |
| `tradalgo.exe --setup` | Configure OANDA API key, Gmail, risk settings |
| `tradalgo.exe --email` | Send 5 test emails to verify alerts work |
| `tradalgo.exe --backtest` | Run historical backtest on all 10 pairs |
| `tradalgo.exe --bot` | Bot only, no dashboard |
| `tradalgo.exe --dash` | Dashboard only (http://localhost:5000) |

## Files Created at Runtime

| File | Purpose |
|---|---|
| `tradalgo_config.json` | Your credentials and settings |
| `tradalgo_data/bot.log` | Full activity log |
| `tradalgo_data/trade_ledger.json` | Open trade tracking (survives restarts) |
| `tradalgo_data/performance.json` | Trade history and stats |
| `tradalgo_data/backtest_results/` | Backtest output files |

## Running the Tests (Developers)

`test_tradalgo.py` covers the indicator math, position sizing, strategy
consensus logic, and the news-event/DST parsing. It's a dev-time tool only —
not bundled into `tradalgo.exe` and has no effect on the shipped app.

```
pip install pytest numpy requests flask
python -m pytest test_tradalgo.py -v
```

## Keeping Your Credentials Secure (Recommended)

By default, `tradalgo.exe --setup` saves your OANDA key, Gmail app password,
and AI API key into `tradalgo_config.json` next to the exe. That file is
plaintext — treat it like a password and **never share it, upload it, or
commit it to version control**.

For better security, you can set these as environment variables instead.
Any variable set here always overrides the config file, and — once set —
that field is never written back to disk in plaintext:

| Environment Variable | Replaces config field |
|---|---|
| `TRADALGO_OANDA_ACCOUNT_ID` | `OANDA_ACCOUNT_ID` |
| `TRADALGO_OANDA_API_KEY` | `OANDA_API_KEY` |
| `TRADALGO_EMAIL_SENDER` | `EMAIL_SENDER` |
| `TRADALGO_EMAIL_PASSWORD` | `EMAIL_PASSWORD` |
| `TRADALGO_EMAIL_RECIPIENT` | `EMAIL_RECIPIENT` |
| `TRADALGO_AI_BIAS_API_KEY` (or `ANTHROPIC_API_KEY`) | `AI_BIAS_API_KEY` |

**Windows (persist across reboots):**
```
setx TRADALGO_OANDA_API_KEY "your-key-here"
setx TRADALGO_OANDA_ACCOUNT_ID "101-004-XXXXXXXX-001"
setx TRADALGO_EMAIL_SENDER "you@gmail.com"
setx TRADALGO_EMAIL_PASSWORD "your 16-char app password"
```
Close and reopen your terminal (or restart) after `setx` for it to take effect.

**Windows (current session only):**
```
set TRADALGO_OANDA_API_KEY=your-key-here
```

If you've already run `--setup` and have real secrets sitting in
`tradalgo_config.json`, rotate them (generate new ones in OANDA / Google /
Anthropic and revoke the old ones) once you switch to environment variables,
since the old values already touched disk in plaintext.

## Getting Your OANDA Credentials

1. Go to **oanda.com** → Open a Free Practice Account
2. Log in → **My Account → Manage API Access → Generate**
3. Copy the **API Token** and your **Account ID** (format: `101-004-XXXXXXXX-001`)

## Getting a Gmail App Password

1. Google Account → **Security → 2-Step Verification** (enable it)
2. Search **"App Passwords"** → create one named "Tradalgo"
3. Copy the 16-character password (keep the spaces)

## What It Trades

10 currency pairs: EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD,
USD/CAD, NZD/USD, EUR/GBP, EUR/JPY, and Gold (XAU/USD).

London session (07:00–16:00 UTC) and New York session (12:00–21:00 UTC) only.

## Emails You Receive

- 🟢 Trade opened (entry, SL, TP, strategy)
- ✅ Trade closed — win or loss with P&L
- 🏆 Extra WIN email when take profit hits
- 🕐 Session started (London / New York / Overlap)
- 📈 Daily summary every morning
- ⚠️ Error alerts

## Going Live

In `tradalgo_config.json`, change:
```json
"OANDA_ENV": "live"
```
Then update your account ID and API key to your live credentials.
Lower `RISK_PER_TRADE_PCT` to `0.5` for your first live week.
