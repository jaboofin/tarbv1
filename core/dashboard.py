"""
TARB Dashboard Server
=====================
aiohttp web server that serves a real-time dark-theme dashboard
and pushes live bot state via WebSocket.

GRIDPHANTOMDEV aesthetic: dark backgrounds, neon cyan/green accents,
monospace terminal feel, glowing elements, live-updating panels.

Usage (standalone test):
    python -m core.dashboard

Integrated: called by tarb_bot.py via DashboardServer.start()
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Set

from aiohttp import web

logger = logging.getLogger("tarb.dashboard")

# Eastern Time (UTC-5, no DST handling — good enough for display)
ET = timezone(timedelta(hours=-5))

def et_now() -> str:
    """Current time formatted as HH:MM:SS in Eastern."""
    return datetime.now(ET).strftime("%H:%M:%S")


# ─── Dashboard HTML ──────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TARB · Temporal Arbitrage Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Outfit:wght@300;400;600;700&display=swap');

  :root {
    --bg-primary: #0a0e17;
    --bg-secondary: #0f1520;
    --bg-card: #111827;
    --bg-card-hover: #161f33;
    --border: #1e293b;
    --border-glow: #00e5ff22;
    --cyan: #00e5ff;
    --cyan-dim: #00e5ff88;
    --cyan-glow: #00e5ff33;
    --green: #00ff88;
    --green-dim: #00ff8888;
    --green-glow: #00ff8833;
    --red: #ff3366;
    --red-dim: #ff336688;
    --red-glow: #ff336633;
    --yellow: #ffd600;
    --yellow-dim: #ffd60088;
    --purple: #bf5af2;
    --text-primary: #e2e8f0;
    --text-secondary: #94a3b8;
    --text-dim: #64748b;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Outfit', sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: var(--mono);
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── Scanline overlay ── */
  body::after {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0, 229, 255, 0.015) 2px,
      rgba(0, 229, 255, 0.015) 4px
    );
    pointer-events: none;
    z-index: 9999;
  }

  /* ── Header ── */
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
    position: sticky;
    top: 0;
    z-index: 100;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .logo {
    font-family: var(--sans);
    font-weight: 700;
    font-size: 20px;
    letter-spacing: 3px;
    text-transform: uppercase;
    background: linear-gradient(135deg, var(--cyan), var(--green));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    text-shadow: 0 0 30px var(--cyan-glow);
  }

  .logo-sub {
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 2px;
    text-transform: uppercase;
  }

  .status-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    text-transform: uppercase;
  }

  .status-badge.live {
    background: var(--green-glow);
    border: 1px solid var(--green-dim);
    color: var(--green);
  }

  .status-badge.dry {
    background: var(--yellow-dim);
    border: 1px solid var(--yellow);
    color: #000;
  }

  .status-badge.offline {
    background: var(--red-glow);
    border: 1px solid var(--red-dim);
    color: var(--red);
  }

  .pulse {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 4px currentColor; }
    50% { opacity: 0.4; box-shadow: 0 0 12px currentColor; }
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 20px;
    font-size: 11px;
    color: var(--text-secondary);
  }

  .header-right .val { color: var(--cyan); font-weight: 600; }

  /* ── Grid Layout ── */
  .dashboard {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    grid-template-rows: auto auto 1fr;
    gap: 12px;
    padding: 16px 24px;
    max-width: 1800px;
    margin: 0 auto;
  }

  /* ── Stat Cards Row ── */
  .stat-cards {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 12px;
  }

  .stat-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.3s;
  }

  .stat-card:hover {
    border-color: var(--cyan-dim);
  }

  .stat-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--cyan), transparent);
    opacity: 0.5;
  }

  .stat-label {
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 8px;
  }

  .stat-value {
    font-size: 22px;
    font-weight: 700;
    font-family: var(--sans);
  }

  .stat-value.positive { color: var(--green); }
  .stat-value.negative { color: var(--red); }
  .stat-value.neutral { color: var(--cyan); }

  .stat-sub {
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 4px;
  }

  /* ── Panel ── */
  .panel {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
  }

  .panel-title {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--text-secondary);
  }

  .panel-badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    background: var(--cyan-glow);
    color: var(--cyan);
    border: 1px solid var(--cyan-dim);
  }

  .panel-body {
    padding: 12px 16px;
    flex: 1;
    overflow-y: auto;
  }

  /* ── Price Tickers ── */
  .price-row {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
  }

  .ticker {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .ticker-left {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .ticker-icon {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 13px;
    font-family: var(--sans);
  }

  .ticker-icon.btc { background: #f7931a22; color: #f7931a; border: 1px solid #f7931a44; }
  .ticker-icon.eth { background: #627eea22; color: #627eea; border: 1px solid #627eea44; }
  .ticker-icon.sol { background: #9945ff22; color: #9945ff; border: 1px solid #9945ff44; }

  .ticker-name { font-weight: 600; font-size: 14px; }
  .ticker-source { font-size: 10px; color: var(--text-dim); }

  .ticker-price {
    font-size: 20px;
    font-weight: 700;
    font-family: var(--sans);
    text-align: right;
  }

  .ticker-move {
    font-size: 11px;
    text-align: right;
    margin-top: 2px;
  }

  .ticker-move.up { color: var(--green); }
  .ticker-move.down { color: var(--red); }
  .ticker-move.flat { color: var(--text-dim); }

  /* ── Positions Table ── */
  .positions { grid-column: 1 / 3; }
  .signals { grid-column: 3 / 5; }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  th {
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg-card);
  }

  td {
    padding: 8px 10px;
    font-size: 12px;
    border-bottom: 1px solid #1e293b44;
    white-space: nowrap;
  }

  tr:hover td {
    background: var(--bg-card-hover);
  }

  .side-up {
    color: var(--green);
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--green-glow);
  }

  .side-down {
    color: var(--red);
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--red-glow);
  }

  .conviction-bar {
    width: 50px;
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
    display: inline-block;
    vertical-align: middle;
    margin-left: 6px;
  }

  .conviction-fill {
    height: 100%;
    border-radius: 3px;
    background: linear-gradient(90deg, var(--cyan), var(--green));
    transition: width 0.5s;
  }

  /* ── Event Log ── */
  .log-panel { grid-column: 1 / -1; max-height: 260px; }

  .log-entry {
    padding: 4px 0;
    font-size: 11px;
    color: var(--text-secondary);
    border-bottom: 1px solid #1e293b22;
    display: flex;
    gap: 12px;
  }

  .log-ts {
    color: var(--text-dim);
    min-width: 80px;
    flex-shrink: 0;
  }

  .log-type {
    min-width: 70px;
    flex-shrink: 0;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 1px;
  }

  .log-type.signal { color: var(--yellow); }
  .log-type.trade { color: var(--cyan); }
  .log-type.win { color: var(--green); }
  .log-type.loss { color: var(--red); }
  .log-type.info { color: var(--text-dim); }
  .log-type.error { color: var(--red); }

  .log-msg { flex: 1; }

  /* ── P&L Breakdown ── */
  .breakdown { grid-column: 1 / 3; }
  .risk { grid-column: 3 / 5; }

  .breakdown-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 0;
    border-bottom: 1px solid #1e293b44;
  }

  .breakdown-label {
    color: var(--text-secondary);
    font-size: 12px;
  }

  .breakdown-val {
    font-weight: 600;
    font-size: 13px;
  }

  /* ── Risk Gauge ── */
  .risk-bar-container {
    padding: 8px 0;
    border-bottom: 1px solid #1e293b44;
  }

  .risk-bar-label {
    display: flex;
    justify-content: space-between;
    margin-bottom: 6px;
    font-size: 11px;
    color: var(--text-secondary);
  }

  .risk-bar {
    width: 100%;
    height: 8px;
    background: var(--border);
    border-radius: 4px;
    overflow: hidden;
  }

  .risk-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.5s, background 0.3s;
  }

  .risk-bar-fill.safe { background: var(--green); }
  .risk-bar-fill.warning { background: var(--yellow); }
  .risk-bar-fill.danger { background: var(--red); }

  /* ── Connection indicator ── */
  .ws-status {
    position: fixed;
    bottom: 12px;
    right: 16px;
    font-size: 10px;
    color: var(--text-dim);
    display: flex;
    align-items: center;
    gap: 6px;
    z-index: 100;
  }

  .ws-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .ws-dot.connected { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .ws-dot.disconnected { background: var(--red); box-shadow: 0 0 6px var(--red); }

  /* ── Animations ── */
  @keyframes fadeInUp {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .fade-in {
    animation: fadeInUp 0.3s ease-out;
  }

  @keyframes flash-green {
    0%, 100% { background: transparent; }
    50% { background: var(--green-glow); }
  }

  @keyframes flash-red {
    0%, 100% { background: transparent; }
    50% { background: var(--red-glow); }
  }

  .flash-win { animation: flash-green 0.6s ease-out; }
  .flash-loss { animation: flash-red 0.6s ease-out; }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--cyan-dim); }

  /* ── Responsive ── */
  @media (max-width: 1200px) {
    .stat-cards { grid-template-columns: repeat(3, 1fr); }
    .dashboard { grid-template-columns: 1fr 1fr; }
    .positions, .signals, .breakdown, .risk, .log-panel { grid-column: 1 / -1; }
  }
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="header-left">
    <div>
      <div class="logo">TARB</div>
      <div class="logo-sub">Temporal Arbitrage Engine</div>
    </div>
    <div id="statusBadge" class="status-badge dry">
      <div class="pulse"></div>
      <span id="statusText">DRY RUN</span>
    </div>
  </div>
  <div class="header-right">
    <span>UPTIME <span class="val" id="uptime">00:00:00</span></span>
    <span>BANKROLL <span class="val" id="bankroll">$0.00</span></span>
    <span id="clockDisplay"></span>
  </div>
</div>

<!-- DASHBOARD GRID -->
<div class="dashboard">

  <!-- PRICE TICKERS -->
  <div class="price-row">
    <div class="ticker" id="ticker-btc">
      <div class="ticker-left">
        <div class="ticker-icon btc">₿</div>
        <div>
          <div class="ticker-name">BTC/USD</div>
          <div class="ticker-source">Chainlink RTDS</div>
        </div>
      </div>
      <div>
        <div class="ticker-price" id="price-btc">—</div>
        <div class="ticker-move flat" id="move-btc">—</div>
      </div>
    </div>
    <div class="ticker" id="ticker-eth">
      <div class="ticker-left">
        <div class="ticker-icon eth">Ξ</div>
        <div>
          <div class="ticker-name">ETH/USD</div>
          <div class="ticker-source">Chainlink RTDS</div>
        </div>
      </div>
      <div>
        <div class="ticker-price" id="price-eth">—</div>
        <div class="ticker-move flat" id="move-eth">—</div>
      </div>
    </div>
    <div class="ticker" id="ticker-sol">
      <div class="ticker-left">
        <div class="ticker-icon sol">◎</div>
        <div>
          <div class="ticker-name">SOL/USD</div>
          <div class="ticker-source">Chainlink RTDS</div>
        </div>
      </div>
      <div>
        <div class="ticker-price" id="price-sol">—</div>
        <div class="ticker-move flat" id="move-sol">—</div>
      </div>
    </div>
  </div>

  <!-- STAT CARDS -->
  <div class="stat-cards">
    <div class="stat-card">
      <div class="stat-label">Net P&L</div>
      <div class="stat-value neutral" id="statPnl">$0.00</div>
      <div class="stat-sub" id="statRoi">0.0% ROI</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value neutral" id="statWinRate">—</div>
      <div class="stat-sub" id="statWL">0W / 0L</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Trades Today</div>
      <div class="stat-value neutral" id="statTrades">0</div>
      <div class="stat-sub" id="statWagered">$0.00 wagered</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Fees Paid</div>
      <div class="stat-value neutral" id="statFees">$0.00</div>
      <div class="stat-sub" id="statFeeRate">0.0% avg</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Best Trade</div>
      <div class="stat-value positive" id="statBest">—</div>
      <div class="stat-sub">today</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Worst Trade</div>
      <div class="stat-value negative" id="statWorst">—</div>
      <div class="stat-sub">today</div>
    </div>
  </div>

  <!-- OPEN POSITIONS -->
  <div class="panel positions">
    <div class="panel-header">
      <span class="panel-title">Open Positions</span>
      <span class="panel-badge" id="posCount">0</span>
    </div>
    <div class="panel-body">
      <table>
        <thead>
          <tr>
            <th>Side</th>
            <th>Market</th>
            <th>Entry</th>
            <th>Cost</th>
            <th>Conv.</th>
            <th>Age</th>
          </tr>
        </thead>
        <tbody id="positionsBody">
          <tr><td colspan="6" style="color:var(--text-dim);text-align:center;padding:24px">No open positions</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- RECENT SIGNALS -->
  <div class="panel signals">
    <div class="panel-header">
      <span class="panel-title">Recent Signals</span>
      <span class="panel-badge" id="sigCount">0</span>
    </div>
    <div class="panel-body">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Side</th>
            <th>Market</th>
            <th>Move</th>
            <th>Lag</th>
            <th>Conv.</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody id="signalsBody">
          <tr><td colspan="7" style="color:var(--text-dim);text-align:center;padding:24px">Awaiting signals...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- P&L BREAKDOWN -->
  <div class="panel breakdown">
    <div class="panel-header">
      <span class="panel-title">P&L Breakdown</span>
    </div>
    <div class="panel-body" id="breakdownBody">
      <div class="breakdown-row">
        <span class="breakdown-label">Loading...</span>
        <span class="breakdown-val">—</span>
      </div>
    </div>
  </div>

  <!-- RISK STATUS -->
  <div class="panel risk">
    <div class="panel-header">
      <span class="panel-title">Risk Status</span>
    </div>
    <div class="panel-body" id="riskBody">
      <div class="risk-bar-container">
        <div class="risk-bar-label">
          <span>Daily Loss</span>
          <span id="riskLossVal">$0 / $50</span>
        </div>
        <div class="risk-bar">
          <div class="risk-bar-fill safe" id="riskLossBar" style="width:0%"></div>
        </div>
      </div>
      <div class="risk-bar-container">
        <div class="risk-bar-label">
          <span>Trade Count</span>
          <span id="riskTradeVal">0 / 100</span>
        </div>
        <div class="risk-bar">
          <div class="risk-bar-fill safe" id="riskTradeBar" style="width:0%"></div>
        </div>
      </div>
      <div class="risk-bar-container">
        <div class="risk-bar-label">
          <span>Open Positions</span>
          <span id="riskPosVal">0 / 3</span>
        </div>
        <div class="risk-bar">
          <div class="risk-bar-fill safe" id="riskPosBar" style="width:0%"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- EVENT LOG -->
  <div class="panel log-panel">
    <div class="panel-header">
      <span class="panel-title">Event Log</span>
      <span class="panel-badge" id="logCount">0</span>
    </div>
    <div class="panel-body" id="logBody" style="overflow-y:auto;max-height:200px">
    </div>
  </div>

</div>

<!-- WS Status -->
<div class="ws-status">
  <div class="ws-dot disconnected" id="wsDot"></div>
  <span id="wsLabel">Connecting...</span>
</div>

<script>
// ─── State ──────────────────────────────────────────────────────
let ws = null;
let startTime = Date.now();
let signals = [];
const MAX_LOG = 200;
const MAX_SIGNALS = 50;
let logEntries = [];

function parseVal(v) {
  if (v === null || v === undefined) return 0;
  return parseFloat(String(v).replace(/[$,]/g, '')) || 0;
}

// ─── WebSocket ──────────────────────────────────────────────────
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 10000;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    document.getElementById('wsDot').className = 'ws-dot connected';
    document.getElementById('wsLabel').textContent = 'Live';
    reconnectAttempts = 0;
  };

  ws.onclose = () => {
    document.getElementById('wsDot').className = 'ws-dot disconnected';
    reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), MAX_RECONNECT_DELAY);
    document.getElementById('wsLabel').textContent = `Reconnecting (${Math.round(delay/1000)}s)...`;
    setTimeout(connectWS, delay);
  };

  ws.onerror = () => {
    try { ws.close(); } catch(e) {}
  };

  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMessage(msg);
    } catch(e) {}
  };
}

// ─── Message Handler ────────────────────────────────────────────
function handleMessage(msg) {
  switch(msg.type) {
    case 'state': updateFullState(msg.data); break;
    case 'price': updatePrice(msg.data); break;
    case 'signal': addSignal(msg.data); break;
    case 'log': addLog(msg.data); break;
    case 'trade': addLog({ type: 'trade', ...msg.data }); break;
    case 'resolve': addLog({ type: msg.data.won ? 'win' : 'loss', ...msg.data }); break;
  }
}

// ─── Full State Update ──────────────────────────────────────────
function updateFullState(s) {
  if (!s) return;
  const t = s.today || {};

  // Mode
  const badge = document.getElementById('statusBadge');
  const statusText = document.getElementById('statusText');
  if (s.mode === 'LIVE') {
    badge.className = 'status-badge live';
    statusText.textContent = 'LIVE';
  } else {
    badge.className = 'status-badge dry';
    statusText.textContent = 'DRY RUN';
  }

  if (s.bankroll) document.getElementById('bankroll').textContent = '$' + parseVal(s.bankroll).toFixed(2);

  // Stats
  const pnl = parseVal(t.net_pnl);
  const pnlEl = document.getElementById('statPnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
  pnlEl.className = 'stat-value ' + (pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral');

  document.getElementById('statRoi').textContent = (t.roi || '0.0%') + (String(t.roi||'').includes('%') ? '' : '%') + ' ROI';

  const wr = t.win_rate || '—';
  document.getElementById('statWinRate').textContent = wr;
  document.getElementById('statWL').textContent = `${t.wins||0}W / ${t.losses||0}L`;

  document.getElementById('statTrades').textContent = t.trades || 0;
  document.getElementById('statWagered').textContent = (t.total_wagered || '$0.00') + ' wagered';

  document.getElementById('statFees').textContent = t.total_fees || '$0.0000';
  const wagered = parseVal(t.total_wagered);
  const fees = parseVal(t.total_fees);
  document.getElementById('statFeeRate').textContent = wagered > 0 ? (fees/wagered*100).toFixed(1) + '% avg' : '0.0% avg';

  document.getElementById('statBest').textContent = t.best || '—';
  document.getElementById('statWorst').textContent = t.worst || '—';

  // Positions
  const positions = s.positions || [];
  document.getElementById('posCount').textContent = positions.length;
  const tbody = document.getElementById('positionsBody');
  if (positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-dim);text-align:center;padding:24px">No open positions</td></tr>';
  } else {
    tbody.innerHTML = positions.map(p => `
      <tr class="fade-in">
        <td><span class="side-${p.side.toLowerCase()}">${p.side}</span></td>
        <td>${formatSlug(p.market)}</td>
        <td>${p.entry.toFixed(2)}</td>
        <td>${p.cost}</td>
        <td>
          ${p.conviction}
          <div class="conviction-bar"><div class="conviction-fill" style="width:${parseFloat(p.conviction)*100}%"></div></div>
        </td>
        <td>${p.age}</td>
      </tr>
    `).join('');
  }

  // P&L Breakdown
  const bBody = document.getElementById('breakdownBody');
  let bHtml = '';
  if (t.pnl_by_asset) {
    for (const [k,v] of Object.entries(t.pnl_by_asset)) {
      const val = parseVal(v);
      bHtml += `<div class="breakdown-row">
        <span class="breakdown-label">${k.toUpperCase()}</span>
        <span class="breakdown-val" style="color:${val>=0?'var(--green)':'var(--red)'}">${v}</span>
      </div>`;
    }
  }
  if (t.pnl_by_timeframe) {
    for (const [k,v] of Object.entries(t.pnl_by_timeframe)) {
      const val = parseVal(v);
      bHtml += `<div class="breakdown-row">
        <span class="breakdown-label">${k}</span>
        <span class="breakdown-val" style="color:${val>=0?'var(--green)':'var(--red)'}">${v}</span>
      </div>`;
    }
  }
  bBody.innerHTML = bHtml || '<div class="breakdown-row"><span class="breakdown-label">No data yet</span><span class="breakdown-val">—</span></div>';

  // Risk bars
  const lossLimit = parseVal(s.daily_loss_limit) || 50;
  const tradeLimit = parseInt(s.daily_trade_limit) || 100;
  const posLimit = parseInt(s.max_concurrent) || 3;
  const curLoss = Math.abs(Math.min(0, pnl));
  const curTrades = parseInt(t.trades || 0);
  const curPos = positions.length;

  updateRiskBar('riskLoss', curLoss, lossLimit, `$${curLoss.toFixed(0)} / $${lossLimit}`);
  updateRiskBar('riskTrade', curTrades, tradeLimit, `${curTrades} / ${tradeLimit}`);
  updateRiskBar('riskPos', curPos, posLimit, `${curPos} / ${posLimit}`);
}

function updateRiskBar(prefix, value, max, label) {
  const pct = Math.min(100, (value / max) * 100);
  document.getElementById(prefix + 'Val').textContent = label;
  const bar = document.getElementById(prefix + 'Bar');
  bar.style.width = pct + '%';
  bar.className = 'risk-bar-fill ' + (pct < 50 ? 'safe' : pct < 80 ? 'warning' : 'danger');
}

// ─── Price Updates ──────────────────────────────────────────────
function updatePrice(d) {
  if (!d || !d.symbol) return;
  const sym = d.symbol.toLowerCase();
  const priceEl = document.getElementById('price-' + sym);
  const moveEl = document.getElementById('move-' + sym);
  if (!priceEl) return;

  priceEl.textContent = '$' + formatNum(d.price);

  if (d.move_bps !== undefined && d.move_bps !== null) {
    const bps = d.move_bps;
    moveEl.textContent = (bps >= 0 ? '+' : '') + bps.toFixed(1) + ' bps';
    moveEl.className = 'ticker-move ' + (bps > 0 ? 'up' : bps < 0 ? 'down' : 'flat');
  }
}

// ─── Signals ────────────────────────────────────────────────────
function addSignal(d) {
  signals.unshift(d);
  if (signals.length > MAX_SIGNALS) signals.pop();
  document.getElementById('sigCount').textContent = signals.length;

  const tbody = document.getElementById('signalsBody');
  tbody.innerHTML = signals.map(s => `
    <tr class="fade-in">
      <td>${s.time || '—'}</td>
      <td><span class="side-${(s.direction||'').toLowerCase()}">${s.direction}</span></td>
      <td>${formatSlug(s.market||'')}</td>
      <td>${s.move_bps ? (s.move_bps > 0 ? '+' : '') + s.move_bps.toFixed(1) : '—'}</td>
      <td>${s.odds_lag ? s.odds_lag.toFixed(3) : '—'}</td>
      <td>
        ${s.conviction ? s.conviction.toFixed(2) : '—'}
        ${s.conviction ? `<div class="conviction-bar"><div class="conviction-fill" style="width:${s.conviction*100}%"></div></div>` : ''}
      </td>
      <td>${s.result || '—'}</td>
    </tr>
  `).join('');
}

// ─── Log ────────────────────────────────────────────────────────
function addLog(d) {
  logEntries.unshift(d);
  if (logEntries.length > MAX_LOG) logEntries.pop();
  document.getElementById('logCount').textContent = logEntries.length;

  const body = document.getElementById('logBody');
  const entry = document.createElement('div');
  entry.className = 'log-entry fade-in';
  const type = d.type || 'info';
  entry.innerHTML = `
    <span class="log-ts">${d.time || new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York' })}</span>
    <span class="log-type ${type}">${type}</span>
    <span class="log-msg">${d.message || d.msg || JSON.stringify(d)}</span>
  `;
  body.prepend(entry);
  if (body.children.length > MAX_LOG) body.lastChild.remove();
}

// ─── Helpers ────────────────────────────────────────────────────
function formatSlug(slug) {
  return slug.replace(/^(btc|eth|sol)-updown-/, (_, a) => a.toUpperCase() + ' ').replace(/-/g, ' ');
}

function formatNum(n) {
  const v = parseVal(n);
  if (v >= 1000) return v.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
  return v.toFixed(2);
}

// ─── Clock & Uptime (Eastern Time) ──────────────────────────────
setInterval(() => {
  const now = new Date();
  const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  document.getElementById('clockDisplay').textContent = et.toLocaleTimeString('en-US', { timeZone: 'America/New_York' }) + ' ET';

  const elapsed = Math.floor((Date.now() - startTime) / 1000);
  const h = String(Math.floor(elapsed/3600)).padStart(2,'0');
  const m = String(Math.floor((elapsed%3600)/60)).padStart(2,'0');
  const s = String(elapsed%60).padStart(2,'0');
  document.getElementById('uptime').textContent = `${h}:${m}:${s}`;
}, 1000);

// ─── Init ───────────────────────────────────────────────────────
connectWS();
</script>
</body>
</html>
"""


# ─── WebSocket Server ────────────────────────────────────────────────────────────

class DashboardServer:
    """
    aiohttp web server for the TARB dashboard.
    Serves the HTML page at / and pushes live updates via WebSocket at /ws.
    """

    def __init__(self, port: int = 8080):
        self.port = port
        self._app = web.Application()
        self._clients: Set[web.WebSocketResponse] = set()
        self._runner = None

        # State provider functions (set by tarb_bot.py)
        self._get_state = None
        self._get_prices = None
        
        # Trade history provider (set by tarb_bot.py)
        self._get_trade_history = None

        # Event history ring buffer — survives client reconnects
        self._event_history: list = []
        self._max_history = 200

        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/ws", self._handle_ws)
        self._app.router.add_get("/api/state", self._handle_api_state)

    def set_state_provider(self, fn):
        """Register a function that returns the current bot state dict."""
        self._get_state = fn

    def set_price_provider(self, fn):
        """Register a function that returns current prices dict."""
        self._get_prices = fn

    def set_trade_history_provider(self, fn):
        """Register a function that returns list of closed position dicts."""
        self._get_trade_history = fn

    def _record_event(self, event: dict):
        """Store an event in the ring buffer for replay on reconnect."""
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]

    async def start(self):
        """Start the web server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Dashboard running at http://localhost:{self.port}")

    async def stop(self):
        """Stop the web server."""
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.debug(f"Runner cleanup error (safe to ignore): {e}")
        logger.info("Dashboard stopped")

    # ─── HTTP Handlers ───────────────────────────────────────────

    async def _handle_index(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_api_state(self, request):
        state = self._get_state() if self._get_state else {}
        return web.json_response(state)

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)  # Server-side ping every 20s
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info(f"Dashboard client connected ({len(self._clients)} total)")

        # Send initial state
        if self._get_state:
            try:
                await ws.send_json({"type": "state", "data": self._get_state()})
            except Exception:
                pass

        # Replay trade history from tracker (closed positions)
        if self._get_trade_history:
            try:
                trades = self._get_trade_history()
                for trade in trades:
                    await ws.send_json(trade)
            except Exception as e:
                logger.debug(f"Trade history replay error: {e}")

        # Replay event history (signals, logs, trades, resolutions)
        for event in self._event_history:
            try:
                await ws.send_json(event)
            except Exception:
                break

        try:
            async for msg in ws:
                # Handle pong or any client messages
                pass
        except Exception as e:
            logger.debug(f"WS handler error: {e}")
        finally:
            self._clients.discard(ws)
            logger.info(f"Dashboard client disconnected ({len(self._clients)} total)")

        return ws

    # ─── Push Methods ────────────────────────────────────────────

    async def broadcast(self, msg: dict):
        """Send a message to all connected dashboard clients."""
        if not self._clients:
            return
        data = json.dumps(msg)
        dead = set()
        for ws in self._clients:
            try:
                await ws.send_str(data)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def push_state(self):
        """Push full state update to all clients."""
        if self._get_state:
            await self.broadcast({"type": "state", "data": self._get_state()})

    async def push_price(self, symbol: str, price: float, move_bps: float = None):
        """Push a price update."""
        await self.broadcast({
            "type": "price",
            "data": {"symbol": symbol, "price": price, "move_bps": move_bps}
        })

    async def push_signal(self, signal: dict):
        """Push a signal event."""
        signal["time"] = et_now()
        msg = {"type": "signal", "data": signal}
        self._record_event(msg)
        await self.broadcast(msg)

    async def push_log(self, log_type: str, message: str):
        """Push a log event."""
        msg = {
            "type": "log",
            "data": {
                "type": log_type,
                "message": message,
                "time": et_now(),
            }
        }
        self._record_event(msg)
        await self.broadcast(msg)

    async def push_trade(self, trade_data: dict):
        """Push a trade execution event."""
        trade_data["time"] = et_now()
        msg = {"type": "trade", "data": trade_data}
        self._record_event(msg)
        await self.broadcast(msg)

    async def push_resolve(self, resolve_data: dict):
        """Push a resolution event."""
        resolve_data["time"] = et_now()
        msg = {"type": "resolve", "data": resolve_data}
        self._record_event(msg)
        await self.broadcast(msg)

    # ─── Periodic State Push ─────────────────────────────────────

    async def run_push_loop(self, interval: float = 3.0):
        """Periodically push full state to all connected clients."""
        while True:
            try:
                await self.push_state()
            except Exception as e:
                logger.debug(f"Dashboard push error: {e}")
            await asyncio.sleep(interval)
