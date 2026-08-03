"""
Tradalgo — Live Dashboard
Flask app serving a real-time trading dashboard at http://localhost:5000
Features:
  - Live candlestick chart (Lightweight Charts by TradingView — free)
  - Strategy signal overlays (EMA lines, BB bands, entry markers)
  - Open trades panel with live P&L
  - Account summary
  - Session status
  - Trade history feed
  - SSE (Server-Sent Events) for live price push — no page refresh needed
"""

import json
import time
import logging
import threading
from pathlib  import Path
from datetime import datetime, timezone
from flask    import Flask, jsonify, render_template_string, Response, stream_with_context

from utils.oanda_client import OandaClient
from utils.sessions     import session_info
from utils.trade_tracker import get_open, all_open_ids
from utils.performance    import get_stats, get_today
from performance_page     import PERF_HTML
from strategies.signals  import run_all_strategies, consensus_signal, STRATEGIES
from config              import INSTRUMENTS, STRATEGY_WEIGHTS, OANDA_ENV

app    = Flask(__name__)
client = OandaClient()
log    = logging.getLogger(__name__)

# ── SSE price broadcaster ──────────────────────────────────────────────────────
# Background thread fetches all prices every 5s and pushes to all SSE listeners
_price_cache: dict   = {}
_price_lock          = threading.Lock()
_sse_listeners: list = []
_sse_lock            = threading.Lock()

def _price_broadcast_loop():
    while True:
        try:
            prices = client.get_prices(INSTRUMENTS)
            with _price_lock:
                _price_cache.update(prices)
            msg = f"data: {json.dumps(prices)}\n\n"
            with _sse_lock:
                dead = []
                for q in _sse_listeners:
                    try:
                        q.put_nowait(msg)
                    except Exception:
                        dead.append(q)
                for q in dead:
                    _sse_listeners.remove(q)
        except Exception as e:
            log.debug(f"Price broadcast error: {e}")
        time.sleep(5)

threading.Thread(target=_price_broadcast_loop, daemon=True).start()

# ── HTML ──────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradalgo — Live Dashboard</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js">
// ── Smooth Page Transition Handler ──
document.addEventListener('DOMContentLoaded', function() {
  var mainContainer = document.getElementById('app') || document.querySelector('.layout') || document.body;
  if (mainContainer) mainContainer.classList.add('page-container');

  var navLinks = document.querySelectorAll('header nav a');
  navLinks.forEach(function(link) {
    link.addEventListener('click', function(e) {
      var href = link.getAttribute('href');
      if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
      if (window.location.pathname === href) return;
      e.preventDefault();
      
      var targetWrap = document.getElementById('app') || document.querySelector('.layout') || mainContainer;
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
<style>
:root {
  --bg:#0b0e1a; --bg2:#111827; --bg3:#1a2035; --bg4:#1f2a40;
  --border:#1e2d45; --text:#e2e8f0; --muted:#4b5563;
  --green:#22c55e; --red:#ef4444; --blue:#3b82f6;
  --gold:#f59e0b; --purple:#a78bfa;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font:13px/1.5 system-ui,sans-serif;overflow:hidden}

/* layout */
#app{display:grid;grid-template-rows:48px 1fr;grid-template-columns:260px 1fr 280px;height:100vh}
header{grid-column:1/-1;background:var(--bg2);border-bottom:1px solid var(--border);
       display:flex;align-items:center;padding:0 16px;gap:12px;z-index:10}
#sidebar-left{background:var(--bg2);border-right:1px solid var(--border);overflow-y:auto;padding:12px}
#main{display:flex;flex-direction:column;overflow:hidden}
#sidebar-right{background:var(--bg2);border-left:1px solid var(--border);overflow-y:auto;padding:12px}

/* header */
.logo{font-size:16px;font-weight:700;letter-spacing:.5px}
.logo span{color:var(--gold)}
.hbadge{background:var(--bg3);border:1px solid var(--border);border-radius:20px;
        padding:2px 10px;font-size:11px;color:var(--muted)}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);
          animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.hspacer{flex:1}
#session-badge{padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;
               background:var(--bg3);border:1px solid var(--border)}
#session-badge.active{border-color:#22c55e55;color:var(--green)}
#balance-display{font-size:13px;font-weight:600}

/* pair selector */
.section-title{font-size:10px;text-transform:uppercase;letter-spacing:.8px;
               color:var(--muted);margin:12px 0 6px}
.pair-btn{display:block;width:100%;text-align:left;background:none;border:none;
          color:var(--text);padding:7px 10px;border-radius:6px;cursor:pointer;
          font-size:13px;display:flex;justify-content:space-between;align-items:center}
.pair-btn:hover{background:var(--bg3)}
.pair-btn.active{background:var(--bg4);border-left:2px solid var(--blue)}
.pair-price{font-size:11px;color:var(--muted);font-family:monospace}
.pair-signal{font-size:10px;padding:1px 5px;border-radius:3px;font-weight:600}
.sig-buy{background:#16a34a22;color:#4ade80}
.sig-sell{background:#dc262622;color:#f87171}
.sig-none{color:var(--muted)}

/* chart area */
#chart-header{padding:10px 16px;background:var(--bg2);border-bottom:1px solid var(--border);
              display:flex;align-items:center;gap:12px;flex-shrink:0}
#chart-title{font-size:15px;font-weight:600}
#chart-price{font-size:15px;font-family:monospace}
#chart-change{font-size:12px;padding:2px 8px;border-radius:4px}
#chart-container{flex:1;position:relative}
#chart-container > div{width:100%!important;height:100%!important}

.tf-btn{padding:3px 9px;border-radius:4px;border:1px solid var(--border);
        background:none;color:var(--muted);cursor:pointer;font-size:11px}
.tf-btn.active,.tf-btn:hover{background:var(--blue);color:#fff;border-color:var(--blue)}

/* right sidebar */
.stat-row{display:flex;justify-content:space-between;padding:5px 0;
          border-bottom:1px solid var(--border);font-size:12px}
.stat-row:last-child{border:none}
.stat-val{font-weight:600}
.green{color:var(--green)} .red{color:var(--red)}
.gold{color:var(--gold)} .blue{color:var(--blue)}

.trade-card{background:var(--bg3);border-radius:8px;padding:10px;margin-bottom:8px;
            border-left:3px solid var(--border)}
.trade-card.buy{border-left-color:var(--green)}
.trade-card.sell{border-left-color:var(--red)}
.tc-header{display:flex;justify-content:space-between;margin-bottom:6px}
.tc-pair{font-weight:600;font-size:13px}
.tc-dir{font-size:11px;padding:1px 6px;border-radius:3px;font-weight:700}
.tc-buy{background:#16a34a22;color:#4ade80}
.tc-sell{background:#dc262622;color:#f87171}
.tc-row{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}
.tc-pl{font-size:13px;font-weight:700}

.hist-item{padding:7px 0;border-bottom:1px solid var(--border);font-size:11px}
.hist-item:last-child{border:none}
.hist-pair{font-weight:600}
.hist-pl{float:right;font-weight:600}

#no-trades{color:var(--muted);font-size:12px;padding:8px 0;text-align:center}

/* scrollbar */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── Glassmorphism Header ── */
header{
  background:rgba(17, 24, 39, 0.75) !important;
  backdrop-filter:blur(12px) saturate(180%) !important;
  -webkit-backdrop-filter:blur(12px) saturate(180%) !important;
  border-bottom:1px solid rgba(255, 255, 255, 0.08) !important;
  padding:0 16px;height:48px;display:flex;align-items:center;gap:6px;
  position:sticky;top:0;z-index:100;box-shadow:0 4px 20px rgba(0,0,0,.3);
}

/* ── Micro-interactions: Button depress on click ── */
button:active, .btn:active, .pair-btn:active, .tf:active, nav a:active, .tab-btn:active, .btn-primary:active, .btn-secondary:active{
  transform:scale(0.95) translateY(1px) !important;
  transition:transform 0.08s ease !important;
}

/* ── Micro-interactions: Pause button heartbeat pulse when running ── */
#pause-btn.running{
  animation:heartbeatPulse 2.2s infinite ease-in-out !important;
  box-shadow:0 0 12px rgba(16,185,129,.35) !important;
}
#pause-btn.running #pause-icon{
  display:inline-flex !important;
  align-items:center !important;
  justify-content:center !important;
  animation:heartbeatPulse 1.8s infinite ease-in-out !important;
}
@keyframes heartbeatPulse{
  0%,100%{transform:scale(1)}
  14%{transform:scale(1.07)}
  28%{transform:scale(1)}
  42%{transform:scale(1.04)}
  70%{transform:scale(1)}
}

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
  0%{background:rgba(34, 197, 94, 0.35);color:#4ade80;text-shadow:0 0 8px rgba(34, 197, 94, 0.6);}
  100%{background:transparent;color:inherit;}
}
@keyframes tickDownFlash{
  0%{background:rgba(239, 68, 68, 0.35);color:#f87171;text-shadow:0 0 8px rgba(239, 68, 68, 0.6);}
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
.page-container, #app{
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
<div id="app">

<!-- HEADER -->
<header>
  <div class="live-dot"></div>
  <div class="logo">Trad<span>algo</span></div>
  <div class="hbadge" id="env-badge">Practice</div>
  <div class="hbadge" id="session-badge">Loading…</div>
  <div class="hspacer"></div>
  <div id="balance-display">—</div>
</header>

<!-- LEFT SIDEBAR: pair list -->
<div id="sidebar-left">
  <div class="section-title">Instruments</div>
  <div id="pair-list"></div>
</div>

<!-- MAIN: chart -->
<div id="main">
  <div id="chart-header">
    <div id="chart-title">EUR/USD</div>
    <div id="chart-price">—</div>
    <div id="chart-change">—</div>
    <div style="flex:1"></div>
    <button class="tf-btn" data-tf="M5">M5</button>
    <button class="tf-btn" data-tf="M15">M15</button>
    <button class="tf-btn active" data-tf="H1">H1</button>
    <button class="tf-btn" data-tf="H4">H4</button>
    <button class="tf-btn" data-tf="D">D</button>
  </div>
  <div id="chart-container"></div>
</div>

<!-- RIGHT SIDEBAR: account + trades -->
<div id="sidebar-right">
  <div class="section-title">Account</div>
  <div id="account-panel">
    <div class="stat-row"><span>Balance</span><span class="stat-val" id="acc-balance">—</span></div>
    <div class="stat-row"><span>NAV</span><span class="stat-val" id="acc-nav">—</span></div>
    <div class="stat-row"><span>Unrealised P&L</span><span class="stat-val" id="acc-upl">—</span></div>
    <div class="stat-row"><span>Open Trades</span><span class="stat-val" id="acc-ot">—</span></div>
    <div class="stat-row"><span>Margin Used</span><span class="stat-val" id="acc-margin">—</span></div>
    <div class="stat-row"><span>Session</span><span class="stat-val" id="acc-session">—</span></div>
  </div>

  <div class="section-title" style="margin-top:16px">Open Trades</div>
  <div id="trades-panel"><div id="no-trades">No open trades</div></div>

  <div class="section-title" style="margin-top:16px">Signal Strength</div>
  <div id="signals-panel"></div>
</div>

</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
const INSTRUMENTS = {{ instruments|tojson }};
let activeInstrument = INSTRUMENTS[0];
let activeTf = 'H1';
let chart, candleSeries, emaSeries = [], bbSeries = [];
let prices = {};
let lastBarClose = {};

// ── Chart init ─────────────────────────────────────────────────────────────
function initChart() {
  const el = document.getElementById('chart-container');
  chart = LightweightCharts.createChart(el, {
    layout:     { background: { color: '#0b0e1a' }, textColor: '#9ca3af' },
    grid:       { vertLines: { color: '#1a2035' }, horzLines: { color: '#1a2035' } },
    crosshair:  { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#1e2d45' },
    timeScale:  { borderColor: '#1e2d45', timeVisible: true, secondsVisible: false },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: '#22c55e', downColor: '#ef4444',
    borderUpColor: '#22c55e', borderDownColor: '#ef4444',
    wickUpColor: '#22c55e', wickDownColor: '#ef4444',
  });

  // Resize observer
  new ResizeObserver(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  }).observe(el);

  chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
}

// ── Load candles + overlays ─────────────────────────────────────────────────
async function loadChart(instrument, granularity) {
  document.getElementById('chart-title').textContent =
    instrument.replace('_', '/');

  try {
    const r    = await fetch(`/api/candles?instrument=${instrument}&granularity=${granularity}&count=150`);
    const data = await r.json();

    if (!data.candles || !data.candles.length) return;

    // Candlesticks
    const candles = data.candles.map(c => ({
      time:  Math.floor(new Date(c.time).getTime() / 1000),
      open:  c.open, high: c.high, low: c.low, close: c.close
    }));
    candleSeries.setData(candles);

    // Track last close for price display
    if (candles.length) lastBarClose[instrument] = candles[candles.length-1].close;

    // Remove old overlay series
    emaSeries.forEach(s => chart.removeSeries(s));
    bbSeries.forEach(s  => chart.removeSeries(s));
    emaSeries = []; bbSeries = [];

    // EMA 9
    const ema9 = chart.addLineSeries({ color:'#3b82f6', lineWidth:1, priceLineVisible:false, lastValueVisible:false });
    ema9.setData(data.ema9.map((v,i) => v ? {time:candles[i].time, value:v} : null).filter(Boolean));
    emaSeries.push(ema9);

    // EMA 21
    const ema21 = chart.addLineSeries({ color:'#f59e0b', lineWidth:1, priceLineVisible:false, lastValueVisible:false });
    ema21.setData(data.ema21.map((v,i) => v ? {time:candles[i].time, value:v} : null).filter(Boolean));
    emaSeries.push(ema21);

    // EMA 50
    const ema50 = chart.addLineSeries({ color:'#a78bfa', lineWidth:1, priceLineVisible:false, lastValueVisible:false });
    ema50.setData(data.ema50.map((v,i) => v ? {time:candles[i].time, value:v} : null).filter(Boolean));
    emaSeries.push(ema50);

    // Bollinger upper/lower
    const bbUpper = chart.addLineSeries({ color:'#374151', lineWidth:1, priceLineVisible:false, lastValueVisible:false, lineStyle:2 });
    const bbLower = chart.addLineSeries({ color:'#374151', lineWidth:1, priceLineVisible:false, lastValueVisible:false, lineStyle:2 });
    bbUpper.setData(data.bb_upper.map((v,i) => v ? {time:candles[i].time, value:v} : null).filter(Boolean));
    bbLower.setData(data.bb_lower.map((v,i) => v ? {time:candles[i].time, value:v} : null).filter(Boolean));
    bbSeries = [bbUpper, bbLower];

    // Trade markers
    if (data.markers && data.markers.length) {
      candleSeries.setMarkers(data.markers.map(m => ({
        time:     Math.floor(new Date(m.time).getTime() / 1000),
        position: m.direction === 'BUY' ? 'belowBar' : 'aboveBar',
        color:    m.direction === 'BUY' ? '#22c55e'  : '#ef4444',
        shape:    m.direction === 'BUY' ? 'arrowUp'  : 'arrowDown',
        text:     m.direction,
      })));
    }

    chart.timeScale().fitContent();
  } catch(e) {
    console.error('Chart load error:', e);
  }
}

// ── Pair list ──────────────────────────────────────────────────────────────
function buildPairList() {
  const el = document.getElementById('pair-list');
  el.innerHTML = '';
  INSTRUMENTS.forEach(inst => {
    const btn = document.createElement('button');
    btn.className = 'pair-btn' + (inst === activeInstrument ? ' active' : '');
    btn.dataset.inst = inst;
    btn.innerHTML = `
      <span>${inst.replace('_','/')}</span>
      <span>
        <span class="pair-signal sig-none" id="sig-${inst}">—</span>
        <span class="pair-price" id="px-${inst}">—</span>
      </span>`;
    btn.onclick = () => selectInstrument(inst);
    el.appendChild(btn);
  });
}

function selectInstrument(inst) {
  activeInstrument = inst;
  document.querySelectorAll('.pair-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.inst === inst));
  document.getElementById('chart-title').textContent = inst.replace('_','/');
  loadChart(inst, activeTf);
}

// ── Live price SSE ─────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource('/api/prices/stream');
  es.onmessage = e => {
    prices = JSON.parse(e.data);
    // Update pair list prices
    Object.entries(prices).forEach(([inst, p]) => {
      const el = document.getElementById('px-' + inst);
      if (el) el.textContent = p.mid.toFixed(inst.includes('JPY') ? 3 : inst.includes('XAU') ? 2 : 5);
    });
    // Update chart header price for active instrument
    const ap = prices[activeInstrument];
    if (ap) {
      const px   = ap.mid;
      const base = lastBarClose[activeInstrument] || px;
      const chg  = ((px - base) / base * 100).toFixed(3);
      document.getElementById('chart-price').textContent =
        px.toFixed(activeInstrument.includes('JPY') ? 3 : activeInstrument.includes('XAU') ? 2 : 5);
      const chgEl = document.getElementById('chart-change');
      chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg}%`;
      chgEl.style.background = chg >= 0 ? '#16a34a33' : '#dc262633';
      chgEl.style.color       = chg >= 0 ? '#4ade80'  : '#f87171';
    }
  };
  es.onerror = () => setTimeout(connectSSE, 5000);
}

// ── Account + trades polling ────────────────────────────────────────────────
async function refreshAccount() {
  try {
    const a = await (await fetch('/api/account')).json();
    const bal = parseFloat(a.balance);
    const upl = parseFloat(a.unrealizedPL || 0);
    document.getElementById('acc-balance').textContent = '$' + bal.toLocaleString('en',{minimumFractionDigits:2});
    document.getElementById('acc-nav').textContent     = '$' + parseFloat(a.NAV||bal).toLocaleString('en',{minimumFractionDigits:2});
    document.getElementById('acc-upl').textContent     = (upl>=0?'+':'')+upl.toFixed(2);
    document.getElementById('acc-upl').className       = 'stat-val ' + (upl>=0?'green':'red');
    document.getElementById('acc-ot').textContent      = a.openTradeCount || 0;
    document.getElementById('acc-margin').textContent  = '$'+parseFloat(a.marginUsed||0).toFixed(2);
    document.getElementById('balance-display').textContent = '$' + bal.toLocaleString('en',{minimumFractionDigits:2});
  } catch(e) {}
}

async function refreshTrades() {
  try {
    const trades = await (await fetch('/api/trades')).json();
    const panel  = document.getElementById('trades-panel');
    if (!trades.length) {
      panel.innerHTML = '<div id="no-trades">No open trades</div>';
      return;
    }
    panel.innerHTML = trades.map(t => {
      const dir = parseInt(t.currentUnits) > 0 ? 'BUY' : 'SELL';
      const pl  = parseFloat(t.unrealizedPL || 0);
      return `<div class="trade-card ${dir.toLowerCase()}">
        <div class="tc-header">
          <span class="tc-pair">${t.instrument.replace('_','/')}</span>
          <span class="tc-dir tc-${dir.toLowerCase()}">${dir}</span>
        </div>
        <div class="tc-row"><span>Entry</span><span>${parseFloat(t.price).toFixed(5)}</span></div>
        <div class="tc-row"><span>SL</span><span>${t.stopLossOrder?parseFloat(t.stopLossOrder.price).toFixed(5):'—'}</span></div>
        <div class="tc-row"><span>TP</span><span>${t.takeProfitOrder?parseFloat(t.takeProfitOrder.price).toFixed(5):'—'}</span></div>
        <div class="tc-row" style="margin-top:4px">
          <span>P&L</span>
          <span class="tc-pl ${pl>=0?'green':'red'}">${pl>=0?'+':''}$${pl.toFixed(2)}</span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {}
}

async function refreshSignals() {
  try {
    const sigs = await (await fetch('/api/signals/quick')).json();
    const panel = document.getElementById('signals-panel');
    panel.innerHTML = '';
    Object.entries(sigs).forEach(([inst, sig]) => {
      const pxEl = document.getElementById('sig-' + inst);
      if (pxEl) {
        pxEl.textContent  = sig || '—';
        pxEl.className    = 'pair-signal ' + (sig==='BUY'?'sig-buy':sig==='SELL'?'sig-sell':'sig-none');
      }
    });
    const active = sigs[activeInstrument];
    if (active) {
      document.getElementById('signals-panel').innerHTML = `
        <div class="stat-row"><span>Consensus</span>
          <span class="stat-val ${active==='BUY'?'green':active==='SELL'?'red':''}">${active||'NONE'}</span>
        </div>`;
    }
  } catch(e) {}
}

async function refreshSession() {
  try {
    const s  = await (await fetch('/api/session')).json();
    const el = document.getElementById('session-badge');
    el.textContent = s.session;
    el.className   = 'hbadge ' + (s.trading_active ? 'active' : '');
    document.getElementById('acc-session').textContent = s.session;
    document.getElementById('acc-session').className   = 'stat-val ' + (s.trading_active?'green':'');
  } catch(e) {}
}

// ── Timeframe buttons ──────────────────────────────────────────────────────
document.querySelectorAll('.tf-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeTf = btn.dataset.tf;
    loadChart(activeInstrument, activeTf);
  };
});

// ── Boot ───────────────────────────────────────────────────────────────────
initChart();
buildPairList();
connectSSE();
loadChart(activeInstrument, activeTf);

// Stagger refreshes to avoid hammering the API all at once
refreshAccount();
refreshTrades();
refreshSignals();
refreshSession();

setInterval(refreshAccount,  15000);
setInterval(refreshTrades,   10000);
setInterval(refreshSignals,  60000);   // signals are slow (runs strategies)
setInterval(refreshSession,  30000);
setInterval(() => loadChart(activeInstrument, activeTf), 60000); // reload chart each bar
</script>
</body>
</html>"""

# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    from flask import render_template_string
    return render_template_string(DASHBOARD_HTML, instruments=INSTRUMENTS)


@app.route("/api/account")
def api_account():
    try:    return jsonify(client.get_account())
    except Exception as e: return jsonify({"error": str(e)}), 500


@app.route("/api/session")
def api_session():
    return jsonify(session_info())


@app.route("/api/trades")
def api_trades():
    try:    return jsonify(client.get_open_trades())
    except Exception as e: return jsonify({"error": str(e)}), 500


@app.route("/api/candles")
def api_candles():
    """
    Returns candles + pre-computed indicator arrays for the chart overlays.
    Query params: instrument, granularity, count
    """
    from flask import request
    from utils.indicators import closes, ema, bollinger_bands
    from utils.trade_tracker import get_open, all_open_ids
    import numpy as np

    instrument  = request.args.get("instrument", "EUR_USD")
    granularity = request.args.get("granularity", "H1")
    count       = int(request.args.get("count", 150))

    try:
        candles = client.get_candles(instrument, granularity, count)
        c       = closes(candles)

        def nan_to_none(arr):
            return [None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(float(v), 6)
                    for v in arr]

        ema9_arr  = nan_to_none(ema(c, 9))
        ema21_arr = nan_to_none(ema(c, 21))
        ema50_arr = nan_to_none(ema(c, 50))
        bb_u, _, bb_l = bollinger_bands(c, 20, 2.0)

        # Trade markers from our ledger
        markers = []
        open_ids = all_open_ids()
        for tid in open_ids:
            entry = get_open(tid)
            if entry.get("instrument") == instrument:
                markers.append({
                    "time":      entry.get("opened_at", ""),
                    "direction": entry.get("direction", ""),
                })

        return jsonify({
            "candles":   candles,
            "ema9":      ema9_arr,
            "ema21":     ema21_arr,
            "ema50":     ema50_arr,
            "bb_upper":  nan_to_none(bb_u),
            "bb_lower":  nan_to_none(bb_l),
            "markers":   markers,
        })
    except Exception as e:
        log.error(f"api_candles error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/prices/stream")
def api_prices_stream():
    """SSE endpoint — pushes price updates to all connected browser tabs."""
    import queue
    q = queue.Queue(maxsize=10)
    with _sse_lock:
        _sse_listeners.append(q)

    def generate():
        # Send current prices immediately on connect
        with _price_lock:
            if _price_cache:
                yield f"data: {json.dumps(_price_cache)}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except Exception:
                    yield ": ping\n\n"   # keep-alive
        except GeneratorExit:
            with _sse_lock:
                try: _sse_listeners.remove(q)
                except ValueError: pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/api/signals/quick")
def api_signals_quick():
    """
    Lightweight signals endpoint — returns just the consensus signal per pair.
    Uses cached candles where possible so it doesn't hammer the API.
    """
    result = {}
    for instrument in INSTRUMENTS:
        try:
            candles   = client.get_candles(instrument, "H1", 100)
            strat_res = run_all_strategies(candles, instrument)
            consensus = consensus_signal(strat_res, STRATEGY_WEIGHTS)
            result[instrument] = consensus["signal"]
        except Exception:
            result[instrument] = None
    return jsonify(result)


@app.route("/api/backtest/latest")
def api_backtest_latest():
    files = sorted(Path("backtest_results").glob("backtest_*.json"))
    if not files:
        return jsonify({}), 404
    return jsonify(json.loads(files[-1].read_text()))


@app.route("/performance")
def performance_page():
    from flask import render_template_string
    return render_template_string(PERF_HTML)


@app.route("/api/performance")
def api_performance():
    """Full performance stats — period param: all, 30, 7, 1"""
    from flask import request
    period = request.args.get("period", "all")
    days   = None if period == "all" else int(period)
    try:
        return jsonify(get_stats(days=days))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance/today")
def api_performance_today():
    try:
        return jsonify(get_today())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log.info("Dashboard starting at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
