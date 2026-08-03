"""
Performance Dashboard — served at http://localhost:5000/performance
A standalone page showing full trading stats, equity curve, trade history.
Add this route to dashboard.py via: from performance_page import PERF_HTML
"""

PERF_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradalgo — Performance</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js">
document.addEventListener('DOMContentLoaded', function() {
  var navLinks = document.querySelectorAll('header nav a');
  navLinks.forEach(function(link) {
    link.addEventListener('click', function(e) {
      var href = link.getAttribute('href');
      if (!href || href.startsWith('#')) return;
      if (window.location.pathname === href) return;
      e.preventDefault();
      var wrap = document.querySelector('.container') || document.body;
      if (wrap) {
        wrap.classList.remove('page-container');
        wrap.classList.add('page-exit');
      }
      setTimeout(function() {
        window.location.href = href;
      }, 160);
    });
  });
});

</script>
<style>
:root{--bg:#0b0e1a;--bg2:#111827;--bg3:#1a2035;--bg4:#1f2a40;
     --border:#1e2d45;--text:#e2e8f0;--muted:#4b5563;
     --green:#22c55e;--red:#ef4444;--blue:#3b82f6;--gold:#f59e0b;--purple:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:13px/1.5 system-ui,sans-serif;min-height:100vh}

header{background:var(--bg2);border-bottom:1px solid var(--border);
       padding:0 24px;height:48px;display:flex;align-items:center;gap:12px}
.logo{font-size:16px;font-weight:700}.logo span{color:var(--gold)}
nav a{color:var(--muted);text-decoration:none;padding:4px 10px;border-radius:6px;font-size:13px}
nav a:hover,nav a.active{background:var(--bg3);color:var(--text)}
.hspacer{flex:1}

main{max-width:1200px;margin:0 auto;padding:20px 16px}

/* Period tabs */
.period-tabs{display:flex;gap:8px;margin-bottom:20px}
.period-btn{padding:6px 16px;border-radius:8px;border:1px solid var(--border);
            background:none;color:var(--muted);cursor:pointer;font-size:12px;font-weight:500}
.period-btn.active,.period-btn:hover{background:var(--blue);color:#fff;border-color:var(--blue)}

/* KPI cards */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.kpi{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.kpi-val{font-size:22px;font-weight:700;line-height:1.2;margin-bottom:2px}
.kpi-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.green{color:var(--green)}.red{color:var(--red)}.blue{color:var(--blue)}
.gold{color:var(--gold)}.purple{color:var(--purple)}

/* Charts row */
.charts-row{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:20px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px}
.card-title{font-size:11px;text-transform:uppercase;letter-spacing:.7px;
            color:var(--muted);margin-bottom:14px}
.chart-wrap{position:relative;height:200px}

/* Bottom row */
.bottom-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--muted);padding:5px 8px;border-bottom:1px solid var(--border);
   font-size:10px;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #161c2e}
tr:last-child td{border:none}
tr:hover td{background:#141828}
.pill{display:inline-block;padding:1px 7px;border-radius:3px;font-size:10px;font-weight:700}
.buy{background:#16a34a22;color:#4ade80}.sell{background:#dc262622;color:#f87171}
.pos{color:var(--green);font-weight:600}.neg{color:var(--red);font-weight:600}

/* Streak badge */
.streak{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;
        border-radius:8px;font-size:13px;font-weight:600}
.streak-win{background:#16a34a22;color:#4ade80;border:1px solid #16a34a44}
.streak-loss{background:#dc262622;color:#f87171;border:1px solid #dc262644}

/* Responsive */
@media(max-width:900px){.charts-row,.bottom-row{grid-template-columns:1fr}}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

header{
  background:rgba(17, 24, 39, 0.75) !important;
  backdrop-filter:blur(12px) saturate(180%) !important;
  -webkit-backdrop-filter:blur(12px) saturate(180%) !important;
  border-bottom:1px solid rgba(255, 255, 255, 0.08) !important;
  position:sticky;top:0;z-index:100;box-shadow:0 4px 20px rgba(0,0,0,.3);
}
.page-container, .container, body{
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
button:active, .btn:active, nav a:active{
  transform:scale(0.95) translateY(1px) !important;
  transition:transform 0.08s ease !important;
}

</style>
</head>
<body>
<header>
  <div class="logo">Trad<span>algo</span></div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/performance" class="active">Performance</a>
  </nav>
  <div class="hspacer"></div>
  <span id="last-update" style="font-size:11px;color:var(--muted)"></span>
</header>

<main>
  <!-- Period selector -->
  <div class="period-tabs">
    <button class="period-btn" data-period="1">Today</button>
    <button class="period-btn" data-period="7">7 Days</button>
    <button class="period-btn" data-period="30">30 Days</button>
    <button class="period-btn active" data-period="all">All Time</button>
  </div>

  <!-- KPI row -->
  <div class="kpi-grid" id="kpi-grid">
    <div class="kpi"><div class="kpi-val" id="k-pl">—</div><div class="kpi-label">Net P&L</div></div>
    <div class="kpi"><div class="kpi-val" id="k-wr">—</div><div class="kpi-label">Win Rate</div></div>
    <div class="kpi"><div class="kpi-val" id="k-trades">—</div><div class="kpi-label">Total Trades</div></div>
    <div class="kpi"><div class="kpi-val blue" id="k-pf">—</div><div class="kpi-label">Profit Factor</div></div>
    <div class="kpi"><div class="kpi-val red" id="k-dd">—</div><div class="kpi-label">Max Drawdown</div></div>
    <div class="kpi"><div class="kpi-val gold" id="k-avgw">—</div><div class="kpi-label">Avg Win</div></div>
    <div class="kpi"><div class="kpi-val red" id="k-avgl">—</div><div class="kpi-label">Avg Loss</div></div>
    <div class="kpi"><div class="kpi-val" id="k-streak">—</div><div class="kpi-label">Current Streak</div></div>
  </div>

  <!-- Charts -->
  <div class="charts-row">
    <div class="card">
      <div class="card-title">Daily P&L — Last 14 Days</div>
      <div class="chart-wrap"><canvas id="daily-chart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Win / Loss Split</div>
      <div class="chart-wrap"><canvas id="donut-chart"></canvas></div>
    </div>
  </div>

  <!-- Bottom row: pairs, strategies, recent trades -->
  <div class="bottom-row">
    <div class="card">
      <div class="card-title">Performance by Pair</div>
      <table id="pair-table">
        <thead><tr><th>Pair</th><th>Trades</th><th>Win%</th><th>P&L</th></tr></thead>
        <tbody id="pair-body"></tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-title">Performance by Strategy</div>
      <table id="strat-table">
        <thead><tr><th>Strategy</th><th>Trades</th><th>Win%</th><th>P&L</th></tr></thead>
        <tbody id="strat-body"></tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-title">Best &amp; Worst Trades</div>
      <div id="best-worst"></div>
    </div>
  </div>

  <!-- Trade history -->
  <div class="card" style="margin-bottom:20px">
    <div class="card-title">Recent Trade History (last 50)</div>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr><th>Time</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th>
              <th>P&L</th><th>Reason</th><th>Strategy</th></tr>
        </thead>
        <tbody id="trade-history"></tbody>
      </table>
    </div>
  </div>
</main>

<script>
let activePeriod = 'all';
let dailyChart, donutChart;

function fmt(n, decimals=2) {
  return parseFloat(n||0).toFixed(decimals);
}
function fmtPL(n) {
  const v = parseFloat(n||0);
  return `<span class="${v>=0?'pos':'neg'}">${v>=0?'+':''}$${Math.abs(v).toFixed(2)}</span>`;
}
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-GB',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}

async function load(period) {
  const url = period === 'all' ? '/api/performance' : `/api/performance?period=${period}`;
  const data = await (await fetch(url)).json();
  if (data.error) return;
  render(data);
  document.getElementById('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function render(d) {
  // KPIs
  const pl = parseFloat(d.net_pl||0);
  const wr = parseFloat(d.win_rate||0);
  document.getElementById('k-pl').innerHTML = `<span class="${pl>=0?'green':'red'}">${pl>=0?'+':''}$${Math.abs(pl).toFixed(2)}</span>`;
  document.getElementById('k-wr').innerHTML = `<span class="${wr>=50?'green':'red'}">${wr}%</span>`;
  document.getElementById('k-trades').textContent = d.total_trades || 0;
  document.getElementById('k-pf').textContent = fmt(d.profit_factor);
  document.getElementById('k-dd').textContent = `-$${fmt(d.max_drawdown)}`;
  document.getElementById('k-avgw').textContent = `+$${fmt(d.avg_win)}`;
  document.getElementById('k-avgl').textContent = `-$${Math.abs(parseFloat(d.avg_loss||0)).toFixed(2)}`;

  const streak = parseInt(d.current_streak||0);
  document.getElementById('k-streak').innerHTML =
    `<span class="streak ${streak>=0?'streak-win':'streak-loss'}">${streak>=0?'🔥':'❄️'} ${Math.abs(streak)} ${streak>=0?'W':'L'}</span>`;

  // Daily bar chart
  const history = d.daily_history || [];
  const labels  = history.map(h => h.date.slice(5));
  const pls     = history.map(h => parseFloat(h.pl||0));
  const colors  = pls.map(v => v >= 0 ? '#22c55e88' : '#ef444488');
  const borders = pls.map(v => v >= 0 ? '#22c55e' : '#ef4444');

  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(document.getElementById('daily-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ data: pls, backgroundColor: colors, borderColor: borders, borderWidth: 1, borderRadius: 3 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: ctx => ` $${ctx.raw >= 0 ? '+' : ''}${ctx.raw.toFixed(2)}`
      }}},
      scales: {
        x: { ticks: { color: '#4b5563', font: { size: 10 } }, grid: { color: '#1a2035' } },
        y: { ticks: { color: '#4b5563', font: { size: 10 }, callback: v => `$${v}` }, grid: { color: '#1a2035' } }
      }
    }
  });

  // Donut
  if (donutChart) donutChart.destroy();
  donutChart = new Chart(document.getElementById('donut-chart'), {
    type: 'doughnut',
    data: {
      labels: ['Wins', 'Losses'],
      datasets: [{ data: [d.wins||0, d.losses||0], backgroundColor: ['#22c55e88','#ef444488'],
                   borderColor: ['#22c55e','#ef4444'], borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '65%',
      plugins: {
        legend: { position: 'bottom', labels: { color: '#9ca3af', font: { size: 11 }, boxWidth: 12 } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.raw}` } }
      }
    }
  });

  // Pair table
  document.getElementById('pair-body').innerHTML =
    Object.entries(d.by_instrument||{}).map(([inst, r]) => `
      <tr>
        <td><b>${inst.replace('_','/')}</b></td>
        <td>${r.trades}</td>
        <td><span class="${r.win_rate>=50?'pos':''}">${r.win_rate}%</span></td>
        <td>${fmtPL(r.pl)}</td>
      </tr>`).join('') || '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:16px">No trades yet</td></tr>';

  // Strategy table
  document.getElementById('strat-body').innerHTML =
    Object.entries(d.by_strategy||{}).map(([strat, r]) => `
      <tr>
        <td><b>${strat}</b></td>
        <td>${r.trades}</td>
        <td><span class="${r.win_rate>=50?'pos':''}">${r.win_rate}%</span></td>
        <td>${fmtPL(r.pl)}</td>
      </tr>`).join('') || '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:16px">No trades yet</td></tr>';

  // Best & worst
  const best  = d.best_trade;
  const worst = d.worst_trade;
  document.getElementById('best-worst').innerHTML = `
    ${best ? `
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Best Trade</div>
      <div style="background:#16a34a11;border:1px solid #16a34a33;border-radius:8px;padding:10px">
        <div style="font-weight:600;font-size:14px">${best.instrument.replace('_','/')}</div>
        <div style="color:#4ade80;font-size:18px;font-weight:700">+$${parseFloat(best.pl).toFixed(2)}</div>
        <div style="font-size:11px;color:var(--muted)">${best.strategy||''}</div>
        <div style="font-size:11px;color:var(--muted)">${fmtDate(best.closed_at)}</div>
      </div>
    </div>` : ''}
    ${worst ? `
    <div>
      <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Worst Trade</div>
      <div style="background:#dc262611;border:1px solid #dc262633;border-radius:8px;padding:10px">
        <div style="font-weight:600;font-size:14px">${worst.instrument.replace('_','/')}</div>
        <div style="color:#f87171;font-size:18px;font-weight:700">$${parseFloat(worst.pl).toFixed(2)}</div>
        <div style="font-size:11px;color:var(--muted)">${worst.strategy||''}</div>
        <div style="font-size:11px;color:var(--muted)">${fmtDate(worst.closed_at)}</div>
      </div>
    </div>` : ''}
    ${!best && !worst ? '<div style="color:var(--muted);text-align:center;padding:20px">No closed trades yet</div>' : ''}
  `;

  // Trade history
  const trades = (d.trades||[]).slice().reverse();
  document.getElementById('trade-history').innerHTML = trades.length
    ? trades.map(t => `
      <tr>
        <td style="white-space:nowrap">${fmtDate(t.closed_at)}</td>
        <td><b>${(t.instrument||'').replace('_','/')}</b></td>
        <td><span class="pill ${(t.direction||'').toLowerCase()}">${t.direction||'?'}</span></td>
        <td style="font-family:monospace">${fmt(t.entry,5)}</td>
        <td style="font-family:monospace">${fmt(t.exit,5)}</td>
        <td>${fmtPL(t.pl)}</td>
        <td style="color:var(--muted)">${t.reason||'—'}</td>
        <td style="color:var(--muted);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(t.strategy||'—').split('|')[0].trim()}</td>
      </tr>`).join('')
    : '<tr><td colspan="8" style="color:var(--muted);text-align:center;padding:24px">No closed trades yet — they will appear here once trades close.</td></tr>';
}

// Period buttons
document.querySelectorAll('.period-btn').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activePeriod = btn.dataset.period;
    load(activePeriod);
  };
});

// Initial load + auto-refresh every 30s
load(activePeriod);
setInterval(() => load(activePeriod), 30000);
</script>
</body>
</html>"""
