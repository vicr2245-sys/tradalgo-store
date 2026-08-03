"""
Email Diagnostic Tool
Run this standalone to test every part of the email + trade alert pipeline.
Usage: python test_email.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

print("\n" + "="*55)
print("  Tradalgo — Email Diagnostic")
print("="*55 + "\n")

# ── Step 1: Check config ───────────────────────────────────────────────────
print("Step 1: Checking config.py...")
try:
    from config import (
        EMAIL_ENABLED, EMAIL_SENDER, EMAIL_PASSWORD,
        EMAIL_RECIPIENT, SMTP_HOST, SMTP_PORT,
        OANDA_API_KEY, OANDA_ACCOUNT_ID
    )
    print(f"  EMAIL_ENABLED   : {EMAIL_ENABLED}")
    print(f"  EMAIL_SENDER    : {EMAIL_SENDER}")
    print(f"  EMAIL_PASSWORD  : {'*' * len(EMAIL_PASSWORD) if EMAIL_PASSWORD else '❌ EMPTY'}")
    print(f"  EMAIL_RECIPIENT : {EMAIL_RECIPIENT}")
    print(f"  SMTP_HOST       : {SMTP_HOST}:{SMTP_PORT}")

    if not EMAIL_ENABLED:
        print("\n  ❌ EMAIL_ENABLED is False in config.py — set it to True\n")
        sys.exit(1)
    if "your" in EMAIL_SENDER.lower() or "@" not in EMAIL_SENDER:
        print("\n  ❌ EMAIL_SENDER looks unconfigured — set your Gmail address\n")
        sys.exit(1)
    if not EMAIL_PASSWORD or "your" in EMAIL_PASSWORD.lower():
        print("\n  ❌ EMAIL_PASSWORD looks unconfigured — set your Gmail App Password\n")
        sys.exit(1)
    print("  ✅ Config looks filled in\n")
except Exception as e:
    print(f"  ❌ Could not load config: {e}\n")
    sys.exit(1)

# ── Step 2: SMTP connection test ───────────────────────────────────────────
print("Step 2: Testing SMTP connection to Gmail...")
import smtplib
try:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
        s.ehlo()
        s.starttls()
        print("  ✅ Connected and TLS started")
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        print("  ✅ Gmail login successful\n")
except smtplib.SMTPAuthenticationError:
    print("""
  ❌ Gmail authentication failed.

  Most likely causes:
  1. You used your normal Gmail password instead of an App Password
     → Go to: myaccount.google.com → Security → App Passwords
     → Create one named 'Tradalgo', paste the 16-char code into config.py

  2. Your App Password has spaces — keep them:  'xxxx xxxx xxxx xxxx'

  3. 2-Step Verification is not enabled on your Google account
     → Required before App Passwords work
""")
    sys.exit(1)
except smtplib.SMTPConnectError as e:
    print(f"  ❌ Could not connect to {SMTP_HOST}:{SMTP_PORT} — {e}")
    print("  Check your internet connection / firewall.\n")
    sys.exit(1)
except Exception as e:
    print(f"  ❌ SMTP error: {e}\n")
    sys.exit(1)

# ── Step 3: Send a real test email ─────────────────────────────────────────
print("Step 3: Sending test emails to", EMAIL_RECIPIENT, "...")
from utils.email_alerts import alert_trade_opened, alert_trade_closed, alert_win, alert_session_start

try:
    alert_session_start("London", 10)
    print("  ✅ Session start email sent")

    alert_trade_opened(
        instrument="EUR_USD", direction="BUY", units=10000,
        entry="1.08450", sl="1.08250", tp="1.08850",
        strategy="EMA Cross | RSI Reversal (TEST)"
    )
    print("  ✅ Trade opened email sent")

    alert_trade_closed(
        instrument="EUR_USD", direction="BUY",
        entry="1.08450", exit_price="1.08250",
        pl=-20.00, pl_pct=-0.20, reason="Stop Loss"
    )
    print("  ✅ Trade closed (loss) email sent")

    alert_trade_closed(
        instrument="EUR_USD", direction="BUY",
        entry="1.08450", exit_price="1.08850",
        pl=40.00, pl_pct=0.40, reason="Take Profit"
    )
    print("  ✅ Trade closed (win) email sent")

    alert_win(
        instrument="EUR_USD", direction="BUY",
        entry="1.08450", exit_price="1.08850",
        pl=40.00, pl_pct=0.40, strategy="EMA Cross | RSI Reversal (TEST)"
    )
    print("  ✅ WIN alert email sent\n")

except Exception as e:
    print(f"  ❌ Email send failed: {e}\n")
    sys.exit(1)

# ── Step 4: OANDA connection + trade close simulation ─────────────────────
print("Step 4: Testing OANDA connection + trade close detection...")
try:
    from utils.oanda_client import OandaClient
    c = OandaClient()
    acct = c.get_account()
    print(f"  ✅ OANDA connected — balance: ${float(acct['balance']):,.2f}")

    open_trades = c.get_open_trades()
    print(f"  ✅ Open trades: {len(open_trades)}")

    if open_trades:
        print("\n  Simulating close detection on first open trade...")
        from utils.trade_tracker import record_open, all_open_ids, get_open, remove
        t = open_trades[0]
        tid  = str(t["id"])
        inst = t["instrument"]
        entry_px = float(t["price"])
        units    = abs(int(t["currentUnits"]))
        dirn     = "BUY" if int(t["currentUnits"]) > 0 else "SELL"

        record_open(tid, inst, dirn, entry_px, units, 0, 0, "diagnostic test")
        print(f"  ✅ Recorded trade {tid} ({dirn} {inst} @ {entry_px}) in ledger")

        # Simulate what sync_trade_state does
        prices = c.get_prices([inst])
        current_px = prices[inst]["mid"]
        pl = (current_px - entry_px) * units if dirn == "BUY" else (entry_px - current_px) * units
        pl_pct = round((pl / (entry_px * units)) * 100, 4)

        alert_trade_closed(
            instrument=inst, direction=dirn,
            entry=f"{entry_px:.5f}", exit_price=f"{current_px:.5f}",
            pl=round(pl, 2), pl_pct=pl_pct, reason="Diagnostic Simulation"
        )
        print(f"  ✅ Simulated close email sent (P&L={pl:+.2f})")

        if pl > 0:
            alert_win(inst, dirn, f"{entry_px:.5f}", f"{current_px:.5f}", round(pl,2), pl_pct, "diagnostic")
            print("  ✅ Simulated WIN email sent")

        remove(tid)
        print("  ✅ Ledger cleaned up")
    else:
        print("  ℹ️  No open trades to simulate — that's fine")

except Exception as e:
    print(f"  ❌ OANDA error: {e}")
    print("     Check OANDA_API_KEY and OANDA_ACCOUNT_ID in config.py\n")
    sys.exit(1)

# ── Step 5: Ledger file check ─────────────────────────────────────────────
print("\nStep 5: Checking trade ledger persistence...")
from utils.trade_tracker import record_open, remove, all_open_ids
import os

record_open("DIAG_001", "GBP_USD", "SELL", 1.27000, 5000, 1.27200, 1.26600, "diagnostic")
ledger_path = "logs/trade_ledger.json"
if os.path.exists(ledger_path):
    print(f"  ✅ Ledger file exists at {ledger_path}")
else:
    print(f"  ❌ Ledger file NOT created at {ledger_path} — check logs/ folder permissions")
remove("DIAG_001")

# ── Done ──────────────────────────────────────────────────────────────────
print()
print("="*55)
print("  ✅ All checks passed!")
print(f"  Check {EMAIL_RECIPIENT} — you should have 4-5 test emails.")
print("  If emails are missing, check your spam/junk folder.")
print("="*55 + "\n")
