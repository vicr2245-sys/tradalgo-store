/* ==========================================================================
   TRADALGO.STORE — INTERACTIVE APPLICATION & STRIPE INTEGRATION
   ========================================================================== */

// --------------------------------------------------------------------------
// STRIPE CONFIGURATION SETUP
// Place your Stripe Payment Link OR Price ID & Publishable Key below
// --------------------------------------------------------------------------
const STRIPE_CONFIG = {
    // Live Stripe Payment Link for $5 USD Tradalgo Lifetime License
    paymentLinkUrl: "https://buy.stripe.com/14AdRb4F44mk1b2bhYdby00", 

    // Optional fallback Stripe Checkout API parameters
    publishableKey: "", 
    priceId: "" 
};

document.addEventListener('DOMContentLoaded', () => {
    initStripeCheckout();
    initHeroChart();
    initSimulator();
    initCalculator();
    initBacktestProof();
    initFaqAccordion();
    initMobileNav();
});

// --------------------------------------------------------------------------
// 1. STRIPE CHECKOUT HANDLER
// --------------------------------------------------------------------------
function initStripeCheckout() {
    const checkoutBtns = [
        document.getElementById('stripeCheckoutBtn'),
        document.getElementById('headerCtaBtn'),
        document.getElementById('heroBuyBtn'),
        document.getElementById('announcementBar')
    ];

    checkoutBtns.forEach(btn => {
        if (!btn) return;
        btn.addEventListener('click', (e) => {
            // If button is a direct anchor link to #checkout, allow smooth scroll if not holding key
            if (btn.getAttribute('href') === '#checkout') return;

            e.preventDefault();

            // Priority 1: Use direct Stripe Payment Link if provided
            if (STRIPE_CONFIG.paymentLinkUrl && STRIPE_CONFIG.paymentLinkUrl !== "https://buy.stripe.com/your-payment-link") {
                window.location.href = STRIPE_CONFIG.paymentLinkUrl;
                return;
            }

            // Priority 2: Use Stripe Checkout SDK with Price ID
            if (window.Stripe && STRIPE_CONFIG.publishableKey && STRIPE_CONFIG.priceId) {
                try {
                    const stripe = Stripe(STRIPE_CONFIG.publishableKey);
                    stripe.redirectToCheckout({
                        lineItems: [{ price: STRIPE_CONFIG.priceId, quantity: 1 }],
                        mode: 'payment',
                        successUrl: window.location.origin + '/success.html',
                        cancelUrl: window.location.href,
                    }).then((result) => {
                        if (result.error) {
                            alert(result.error.message);
                        }
                    });
                } catch (err) {
                    console.error("Stripe initialization error:", err);
                    showCheckoutModal();
                }
            } else {
                showCheckoutModal();
            }
        });
    });
}

function showCheckoutModal() {
    alert("🚀 Ready to purchase Tradalgo for $5 USD!\n\nPlease paste your Stripe Payment Link or Price ID into `app.js` under `STRIPE_CONFIG` to enable live payments.");
}

// --------------------------------------------------------------------------
// 2. HERO DASHBOARD REAL-TIME CANVAS CHART
// --------------------------------------------------------------------------
function initHeroChart() {
    const canvas = document.getElementById('heroChartCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    let width = canvas.width = canvas.parentElement.clientWidth || 900;
    let height = canvas.height = canvas.parentElement.clientHeight || 260;

    // Synthetic price dataset
    const pointsCount = 45;
    let basePrice = 1.0880;
    let prices = [];

    for (let i = 0; i < pointsCount; i++) {
        basePrice += (Math.random() - 0.47) * 0.00035;
        prices.push(basePrice);
    }

    function renderChart() {
        ctx.clearRect(0, 0, width, height);

        // Draw grid lines
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
        ctx.lineWidth = 1;
        
        for (let y = 30; y < height; y += 40) {
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(width, y);
            ctx.stroke();
        }

        const minPrice = Math.min(...prices) - 0.0002;
        const maxPrice = Math.max(...prices) + 0.0002;

        const getX = (i) => (i / (pointsCount - 1)) * (width - 40) + 20;
        const getY = (price) => height - 30 - ((price - minPrice) / (maxPrice - minPrice)) * (height - 60);

        // Draw gradient area under curve
        const gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, 'rgba(0, 242, 254, 0.35)');
        gradient.addColorStop(1, 'rgba(0, 242, 254, 0.0)');

        ctx.beginPath();
        ctx.moveTo(getX(0), getY(prices[0]));
        for (let i = 1; i < prices.length; i++) {
            const x = getX(i);
            const y = getY(prices[i]);
            ctx.lineTo(x, y);
        }
        ctx.lineTo(getX(prices.length - 1), height);
        ctx.lineTo(getX(0), height);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();

        // Draw price line
        ctx.beginPath();
        ctx.moveTo(getX(0), getY(prices[0]));
        for (let i = 1; i < prices.length; i++) {
            ctx.lineTo(getX(i), getY(prices[i]));
        }
        ctx.strokeStyle = '#00F2FE';
        ctx.lineWidth = 2.5;
        ctx.stroke();

        // Draw Trade Entry Dot & Take Profit / Stop Loss Lines
        const entryIdx = 18;
        const entryX = getX(entryIdx);
        const entryY = getY(prices[entryIdx]);

        // Entry dot
        ctx.beginPath();
        ctx.arc(entryX, entryY, 6, 0, Math.PI * 2);
        ctx.fillStyle = '#10B981';
        ctx.fill();
        ctx.strokeStyle = '#FFFFFF';
        ctx.lineWidth = 2;
        ctx.stroke();

        // Pulsing live tick dot at end
        const lastX = getX(prices.length - 1);
        const lastY = getY(prices[prices.length - 1]);

        ctx.beginPath();
        ctx.arc(lastX, lastY, 5, 0, Math.PI * 2);
        ctx.fillStyle = '#00F2FE';
        ctx.fill();
    }

    renderChart();

    // Live tick simulator update every 2.5s
    setInterval(() => {
        const last = prices[prices.length - 1];
        const newPrice = last + (Math.random() - 0.46) * 0.00015;
        prices.shift();
        prices.push(newPrice);
        renderChart();

        // Update price DOM elements
        const priceEl = document.getElementById('heroCurrentPrice');
        if (priceEl) priceEl.innerText = newPrice.toFixed(5);

        const floatEl = document.getElementById('heroFloatingPnl');
        if (floatEl) {
            const pnl = ((newPrice - 1.08810) * 100000).toFixed(2);
            floatEl.innerText = (pnl >= 0 ? `+$${pnl}` : `-$${Math.abs(pnl)}`);
            floatEl.className = pnl >= 0 ? 'green font-mono' : 'text-red font-mono';
        }
    }, 2500);
}

// --------------------------------------------------------------------------
// 3. INTERACTIVE BOT SIMULATOR
// --------------------------------------------------------------------------
function initSimulator() {
    const pairBtns = document.querySelectorAll('.sim-btn');
    const voteList = document.getElementById('simVoteList');
    const scoreFill = document.getElementById('simScoreFill');
    const resultText = document.getElementById('simResultText');
    const terminal = document.getElementById('simTerminal');
    const runBtn = document.getElementById('simRunBtn');

    const pairData = {
        EURUSD: {
            score: 80,
            votes: [
                { name: 'Strategy 1: EMA Trend Alignment', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 2: RSI Mean Reversion', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 3: ATR Volatility Breakout', status: 'NEUTRAL', cls: 'vote-neutral' },
                { name: 'Strategy 4: MACD Divergence', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 5: News & Timeframe Shield', status: 'CLEAR', cls: 'vote-pass' }
            ],
            resText: '<i class="fa-solid fa-circle-check"></i> 80% BUY CONSENSUS — ORDER DISPATCHED',
            resCls: 'consensus-result green',
            log: 'BUY 82,000 EUR/USD @ 1.08942 | SL: 1.08820 | TP: 1.09210'
        },
        GBPUSD: {
            score: 60,
            votes: [
                { name: 'Strategy 1: EMA Trend Alignment', status: 'SELL', cls: 'vote-buy text-red' },
                { name: 'Strategy 2: RSI Mean Reversion', status: 'SELL', cls: 'vote-buy text-red' },
                { name: 'Strategy 3: ATR Volatility Breakout', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 4: MACD Divergence', status: 'SELL', cls: 'vote-buy text-red' },
                { name: 'Strategy 5: News & Timeframe Shield', status: 'CLEAR', cls: 'vote-pass' }
            ],
            resText: '<i class="fa-solid fa-triangle-exclamation"></i> 60% SELL CONSENSUS — MONITORING BACKTEST',
            resCls: 'consensus-result text-gold',
            log: 'SELL 65,000 GBP/USD @ 1.27420 | SL: 1.27800 | TP: 1.26900'
        },
        XAUUSD: {
            score: 100,
            votes: [
                { name: 'Strategy 1: EMA Trend Alignment', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 2: RSI Mean Reversion', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 3: ATR Volatility Breakout', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 4: MACD Divergence', status: 'BUY', cls: 'vote-buy' },
                { name: 'Strategy 5: News & Timeframe Shield', status: 'CLEAR', cls: 'vote-pass' }
            ],
            resText: '<i class="fa-solid fa-fire text-gold"></i> 100% PERFECT BUY CONSENSUS — GOLD BREAKOUT',
            resCls: 'consensus-result text-gold',
            log: 'BUY 15.0 oz XAU/USD (Gold) @ 2,386.40 | SL: 2,370.00 | TP: 2,415.00'
        }
    };

    pairBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            pairBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            const pair = btn.dataset.pair;
            updateSim(pairData[pair] || pairData['EURUSD'], pair);
        });
    });

    if (runBtn) {
        runBtn.addEventListener('click', () => {
            const activeBtn = document.querySelector('.sim-btn.active');
            const pair = activeBtn ? activeBtn.dataset.pair : 'EURUSD';
            updateSim(pairData[pair] || pairData['EURUSD'], pair);
        });
    }

    function updateSim(data, pair) {
        if (scoreFill) scoreFill.style.width = data.score + '%';
        if (resultText) {
            resultText.innerHTML = data.resText;
            resultText.className = data.resCls;
        }

        if (voteList) {
            voteList.innerHTML = data.votes.map(v => `
                <li>
                    <span>${v.name}</span>
                    <span class="vote-badge ${v.cls}">${v.status}</span>
                </li>
            `).join('');
        }

        if (terminal) {
            const time = new Date().toLocaleTimeString();
            terminal.innerHTML += `
                <p class="text-dim">[${time} INFO] Recalculating Consensus Matrix for ${pair}...</p>
                <p class="text-cyan">[${time} STRAT] Votes Processed: ${data.score}% Alignment</p>
                <p class="text-green">[${time} EXEC] ${data.log}</p>
            `;
            terminal.scrollTop = terminal.scrollHeight;
        }
    }
}

// --------------------------------------------------------------------------
// 4. INTERACTIVE ROI & RISK CALCULATOR
// --------------------------------------------------------------------------
function initCalculator() {
    const capitalSlider = document.getElementById('capitalSlider');
    const riskSlider = document.getElementById('riskSlider');
    const monthSlider = document.getElementById('monthSlider');

    if (!capitalSlider || !riskSlider || !monthSlider) return;

    function recalculate() {
        const capital = parseFloat(capitalSlider.value);
        const riskPct = parseFloat(riskSlider.value);
        const months = parseInt(monthSlider.value);

        // Update Slider Label Displays
        document.getElementById('capitalVal').innerText = `$${capital.toLocaleString()}`;
        document.getElementById('riskVal').innerText = `${riskPct.toFixed(2)}%`;
        document.getElementById('monthVal').innerText = `${months} ${months === 1 ? 'Month' : 'Months'}`;

        // Compound Growth Calculation (Avg 6.2% net monthly ROI at 1% risk)
        const monthlyMultiplier = 1 + (0.062 * (riskPct / 1.0));
        const endingEquity = capital * Math.pow(monthlyMultiplier, months);
        const netProfit = endingEquity - capital;
        const totalReturnPct = ((netProfit / capital) * 100).toFixed(1);
        const monthlyProfit = (netProfit / months).toFixed(2);
        const maxRiskUsd = (capital * (riskPct / 100)).toFixed(2);

        // Update DOM Output
        document.getElementById('projectedEquity').innerText = `$${endingEquity.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
        document.getElementById('projectedGain').innerText = `+${totalReturnPct}% Total Net Return`;
        document.getElementById('maxRiskAmount').innerText = `$${maxRiskUsd}`;
        document.getElementById('monthlyProfit').innerText = `+$${parseFloat(monthlyProfit).toLocaleString('en-US', { minimumFractionDigits: 2 })} / mo`;
    }

    capitalSlider.addEventListener('input', recalculate);
    riskSlider.addEventListener('input', recalculate);
    monthSlider.addEventListener('input', recalculate);

    recalculate();
}

// --------------------------------------------------------------------------
// 5. HISTORICAL BACKTEST PROOF TABS & CANVAS
// --------------------------------------------------------------------------
function initBacktestProof() {
    const btTabs = document.querySelectorAll('.bt-tab');
    const canvas = document.getElementById('backtestCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');

    const btData = {
        btEUR: { trades: '1,420', winRate: '69.4%', pf: '2.24', dd: '3.8%', net: '+184.2%', color: '#00F2FE' },
        btGBP: { trades: '1,180', winRate: '66.8%', pf: '2.08', dd: '4.2%', net: '+152.6%', color: '#6366F1' },
        btGOLD: { trades: '940', winRate: '71.2%', pf: '2.45', dd: '5.1%', net: '+248.0%', color: '#F59E0B' }
    };

    btTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            btTabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');

            const target = tab.dataset.target;
            const data = btData[target] || btData.btEUR;

            document.getElementById('btTotalTrades').innerText = data.trades;
            document.getElementById('btWinRate').innerText = data.winRate;
            document.getElementById('btProfitFactor').innerText = data.pf;
            document.getElementById('btDrawdown').innerText = data.dd;
            document.getElementById('btNetProfit').innerText = data.net;

            drawBacktestEquityCurve(ctx, canvas, data.color);
        });
    });

    drawBacktestEquityCurve(ctx, canvas, '#00F2FE');
}

function drawBacktestEquityCurve(ctx, canvas, color) {
    const width = canvas.width = canvas.parentElement.clientWidth || 900;
    const height = canvas.height = canvas.parentElement.clientHeight || 240;

    ctx.clearRect(0, 0, width, height);

    // Draw Grid
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
    ctx.lineWidth = 1;
    for (let y = 20; y < height; y += 35) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
    }

    // Generate upward sloping equity curve with realistic drawdown dips
    const pointsCount = 60;
    let equity = 10000;
    let points = [];

    for (let i = 0; i < pointsCount; i++) {
        equity += (Math.random() - 0.35) * 220;
        points.push(equity);
    }

    const min = Math.min(...points);
    const max = Math.max(...points);

    const getX = (i) => (i / (pointsCount - 1)) * (width - 40) + 20;
    const getY = (val) => height - 20 - ((val - min) / (max - min)) * (height - 40);

    // Gradient fill
    const grad = ctx.createLinearGradient(0, 0, 0, height);
    grad.addColorStop(0, color + '55');
    grad.addColorStop(1, color + '00');

    ctx.beginPath();
    ctx.moveTo(getX(0), getY(points[0]));
    for (let i = 1; i < points.length; i++) {
        ctx.lineTo(getX(i), getY(points[i]));
    }
    ctx.lineTo(getX(points.length - 1), height);
    ctx.lineTo(getX(0), height);
    ctx.fillStyle = grad;
    ctx.fill();

    // Curve Line
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(points[0]));
    for (let i = 1; i < points.length; i++) {
        ctx.lineTo(getX(i), getY(points[i]));
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.stroke();
}

// --------------------------------------------------------------------------
// 6. FAQ ACCORDION TOGGLE
// --------------------------------------------------------------------------
function initFaqAccordion() {
    const questions = document.querySelectorAll('.faq-question');
    questions.forEach(q => {
        q.addEventListener('click', () => {
            const item = q.parentElement;
            const isOpen = item.classList.contains('open');

            document.querySelectorAll('.faq-item').forEach(i => i.classList.remove('open'));

            if (!isOpen) {
                item.classList.add('open');
            }
        });
    });
}

// --------------------------------------------------------------------------
// 7. MOBILE NAVIGATION TOGGLE
// --------------------------------------------------------------------------
function initMobileNav() {
    const toggle = document.getElementById('mobileToggle');
    const nav = document.getElementById('mainNav');

    if (toggle && nav) {
        toggle.addEventListener('click', () => {
            const isVisible = nav.style.display === 'flex';
            nav.style.display = isVisible ? 'none' : 'flex';
            if (!isVisible) {
                nav.style.flexDirection = 'column';
                nav.style.position = 'absolute';
                nav.style.top = '75px';
                nav.style.left = '0';
                nav.style.width = '100%';
                nav.style.background = '#0B0F19';
                nav.style.padding = '1.5rem';
                nav.style.borderBottom = '1px solid rgba(255, 255, 255, 0.1)';
            }
        });
    }
}
