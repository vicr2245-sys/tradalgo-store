"""
Email Alerting System
Sends HTML-formatted trade alerts via Gmail SMTP.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import datetime
from config               import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT, SMTP_HOST, SMTP_PORT, EMAIL_ENABLED

log = logging.getLogger(__name__)


def _send(subject: str, html_body: str):
    if not EMAIL_ENABLED:
        return
    try:
        sender    = str(EMAIL_SENDER or "").strip()
        recipient = str(EMAIL_RECIPIENT or "").strip() or sender
        password  = str(EMAIL_PASSWORD or "").replace(" ", "").strip()
        if not sender or not password:
            log.warning("Email send skipped: EMAIL_SENDER or EMAIL_PASSWORD not configured.")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = recipient
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        log.info(f"Email sent: {subject}")
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"Email failed (Gmail Authentication Error 535 - Bad Credentials for {EMAIL_SENDER}): {e}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def _base_html(title, color, rows):
    rows_html = "".join(
        f"<tr><td style='padding:6px 12px;color:#888;font-size:13px'>{k}</td>"
        f"<td style='padding:6px 12px;font-weight:600;font-size:13px'>{v}</td></tr>"
        for k, v in rows
    )
    return f"""
    <div style='font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;
                background:#1a1a2e;border-radius:12px;overflow:hidden;color:#e0e0e0'>
      <div style='background:{color};padding:18px 24px'>
        <h2 style='margin:0;color:#fff;font-size:18px;letter-spacing:.5px'>{title}</h2>
        <p  style='margin:4px 0 0;color:rgba(255,255,255,.75);font-size:13px'>
          {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
        </p>
      </div>
      <table style='width:100%;border-collapse:collapse'>
        {rows_html}
      </table>
      <div style='padding:12px 24px;background:#12122a;font-size:11px;color:#555;text-align:center'>
        ForexBot • Practice Account • OANDA
      </div>
    </div>"""


def alert_trade_opened(instrument, direction, units, entry, sl, tp, strategy):
    color = "#22a65e" if direction == "BUY" else "#e05252"
    subject = f"🟢 Trade Opened: {direction} {instrument}"
    html = _base_html(
        f"{'📈' if direction == 'BUY' else '📉'}  Trade Opened — {direction} {instrument}",
        color,
        [
            ("Instrument", instrument),
            ("Direction",  f"<span style='color:{color}'>{direction}</span>"),
            ("Units",      f"{units:,}"),
            ("Entry Price",entry),
            ("Stop Loss",  sl),
            ("Take Profit",tp),
            ("Strategy",   strategy),
        ]
    )
    _send(subject, html)


def alert_trade_closed(instrument, direction, entry, exit_price, pl, pl_pct, reason):
    won  = pl >= 0
    color = "#22a65e" if won else "#e05252"
    icon  = "✅" if won else "❌"
    subject = f"{icon} Trade Closed: {instrument} | {'WIN' if won else 'LOSS'} {pl:+.2f}"
    html = _base_html(
        f"{icon}  Trade Closed — {instrument}",
        color,
        [
            ("Instrument",  instrument),
            ("Direction",   direction),
            ("Entry",       entry),
            ("Exit",        exit_price),
            ("P&L",         f"<span style='color:{color}'>{pl:+.2f} ({pl_pct:+.2f}%)</span>"),
            ("Closed by",   reason),
        ]
    )
    _send(subject, html)


def alert_error(message: str):
    subject = "⚠️ ForexBot Error"
    html = _base_html(
        "⚠️  Bot Error",
        "#c0392b",
        [("Error", message), ("Time", datetime.utcnow().isoformat())]
    )
    _send(subject, html)


def alert_session_start(session: str, active_pairs: int):
    subject = f"🕐 {session} Session Started"
    html = _base_html(
        f"🕐  {session} Session Started",
        "#2980b9",
        [("Session", session), ("Active Pairs", active_pairs), ("Status", "Bot is trading")]
    )
    _send(subject, html)


def alert_win(instrument, direction, entry, exit_price, pl, pl_pct, strategy):
    """
    Dedicated WIN email — separate from the standard close alert.
    Fired only when realizedPL > 0 so the user gets a clear celebration.
    """
    subject = f"🏆 WIN: {instrument} +${pl:.2f} ({pl_pct:+.2f}%)"
    html = _base_html(
        f"🏆  Winning Trade — {instrument}",
        "#16803c",   # deep green
        [
            ("Instrument",  instrument),
            ("Direction",   direction),
            ("Entry",       entry),
            ("Exit",        exit_price),
            ("Profit",      f"<span style='color:#4ade80;font-size:16px'>+${pl:.2f} ({pl_pct:+.2f}%)</span>"),
            ("Strategy",    strategy),
            ("Closed by",   "Take Profit ✅"),
        ]
    )
    _send(subject, html)


def alert_daily_summary(date_str: str, stats: dict, account_balance: float):
    """
    Morning daily summary email — sent once per day covering yesterday's trading.
    Includes: P&L, win rate, trade count, best/worst trade, top pair.
    """
    today_data = stats.get("today", {})
    trades     = today_data.get("trades",  stats.get("total_trades", 0))
    wins       = today_data.get("wins",    stats.get("wins", 0))
    losses     = today_data.get("losses",  stats.get("losses", 0))
    pl         = today_data.get("pl",      stats.get("net_pl", 0))
    win_rate   = round(wins / trades * 100, 1) if trades else 0

    pl_color   = "#4ade80" if pl >= 0 else "#f87171"
    pl_sign    = "+" if pl >= 0 else ""
    icon       = "📈" if pl >= 0 else "📉"

    # Best instrument from all-time stats (for context)
    by_inst    = stats.get("by_instrument", {})
    top_pair   = next(iter(by_inst), "—") if by_inst else "—"

    best  = stats.get("best_trade")
    worst = stats.get("worst_trade")

    best_row  = f"{best['instrument']} +${best['pl']:.2f}"  if best  else "—"
    worst_row = f"{worst['instrument']} ${worst['pl']:.2f}" if worst else "—"

    subject = f"{icon} Daily Summary {date_str} | {pl_sign}${pl:.2f} | {win_rate}% win rate"

    # Build instrument table rows
    inst_rows = ""
    for inst, d in list(by_inst.items())[:5]:
        colour = "#4ade80" if d["pl"] >= 0 else "#f87171"
        inst_rows += f"""
        <tr>
          <td style='padding:5px 12px;font-size:12px'>{inst.replace('_','/')}</td>
          <td style='padding:5px 12px;font-size:12px;text-align:center'>{d['trades']}</td>
          <td style='padding:5px 12px;font-size:12px;text-align:center'>{d['win_rate']}%</td>
          <td style='padding:5px 12px;font-size:12px;font-weight:600;color:{colour};text-align:right'>
            {'+'if d['pl']>=0 else''}${d['pl']:.2f}
          </td>
        </tr>"""

    pair_table_html = f"""
      <div style='padding:10px 12px 4px;font-size:11px;text-transform:uppercase;
                  letter-spacing:.6px;color:#555;background:#12122a'>Top Pairs (all time)</div>
      <table style='width:100%;border-collapse:collapse;background:#12122a'>
        <thead>
          <tr style='font-size:10px;color:#555;text-transform:uppercase'>
            <th style='padding:5px 12px;text-align:left'>Pair</th>
            <th style='padding:5px 12px'>Trades</th>
            <th style='padding:5px 12px'>Win%</th>
            <th style='padding:5px 12px;text-align:right'>P&L</th>
          </tr>
        </thead>
        <tbody>{inst_rows}</tbody>
      </table>""" if inst_rows else ""

    html = f"""
    <div style='font-family:system-ui,sans-serif;max-width:560px;margin:0 auto;
                background:#1a1a2e;border-radius:12px;overflow:hidden;color:#e0e0e0'>

      <!-- Header -->
      <div style='background:{"#166534" if pl>=0 else "#7f1d1d"};padding:20px 24px'>
        <h2 style='margin:0;color:#fff;font-size:20px'>{icon} Daily Summary &mdash; {date_str}</h2>
        <p style='margin:6px 0 0;color:rgba(255,255,255,.7);font-size:13px'>
          Account balance: <b>${account_balance:,.2f}</b>
        </p>
      </div>

      <!-- Key stats row -->
      <div style='display:flex;background:#12122a;border-bottom:1px solid #2a2a4a'>
        <div style='flex:1;padding:14px;text-align:center;border-right:1px solid #2a2a4a'>
          <div style='font-size:22px;font-weight:700;color:{pl_color}'>{pl_sign}${pl:.2f}</div>
          <div style='font-size:11px;color:#666;margin-top:2px'>Day P&L</div>
        </div>
        <div style='flex:1;padding:14px;text-align:center;border-right:1px solid #2a2a4a'>
          <div style='font-size:22px;font-weight:700'>{trades}</div>
          <div style='font-size:11px;color:#666;margin-top:2px'>Trades</div>
        </div>
        <div style='flex:1;padding:14px;text-align:center;border-right:1px solid #2a2a4a'>
          <div style='font-size:22px;font-weight:700;color:#4ade80'>{wins}W</div>
          <div style='font-size:11px;color:#666;margin-top:2px'>Wins</div>
        </div>
        <div style='flex:1;padding:14px;text-align:center'>
          <div style='font-size:22px;font-weight:700;
                      color:{"#4ade80" if win_rate>=50 else "#f87171"}'>{win_rate}%</div>
          <div style='font-size:11px;color:#666;margin-top:2px'>Win Rate</div>
        </div>
      </div>

      <!-- Summary rows -->
      <table style='width:100%;border-collapse:collapse'>
        <tr><td style='padding:7px 12px;color:#888;font-size:13px'>Best Trade</td>
            <td style='padding:7px 12px;font-weight:600;color:#4ade80;font-size:13px'>{best_row}</td></tr>
        <tr><td style='padding:7px 12px;color:#888;font-size:13px'>Worst Trade</td>
            <td style='padding:7px 12px;font-weight:600;color:#f87171;font-size:13px'>{worst_row}</td></tr>
        <tr><td style='padding:7px 12px;color:#888;font-size:13px'>All-time Win Rate</td>
            <td style='padding:7px 12px;font-weight:600;font-size:13px'>{stats.get("win_rate",0)}%</td></tr>
        <tr><td style='padding:7px 12px;color:#888;font-size:13px'>All-time Net P&L</td>
            <td style='padding:7px 12px;font-weight:600;font-size:13px;
                       color:{"#4ade80" if stats.get("net_pl",0)>=0 else "#f87171"}'>
              {'+'if stats.get('net_pl',0)>=0 else''}${stats.get('net_pl',0):.2f}</td></tr>
        <tr><td style='padding:7px 12px;color:#888;font-size:13px'>Profit Factor</td>
            <td style='padding:7px 12px;font-weight:600;font-size:13px'>{stats.get("profit_factor",0)}</td></tr>
        <tr><td style='padding:7px 12px;color:#888;font-size:13px'>Max Drawdown</td>
            <td style='padding:7px 12px;font-weight:600;color:#f87171;font-size:13px'>
              -${stats.get("max_drawdown",0):.2f}</td></tr>
      </table>

      <!-- Per-pair table -->
      {pair_table_html}

      <div style='padding:12px 24px;background:#0d0d1f;font-size:11px;color:#444;text-align:center'>
        Tradalgo • Practice Account • OANDA
      </div>
    </div>"""

    _send(subject, html)
