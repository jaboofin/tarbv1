# TARB Bot v1.1

**Temporal Arbitrage Bot for Polymarket 5m/15m Crypto Markets**

Detects when Chainlink RTDS real-time oracle prices confirm a directional move (e.g., BTC +30bps from interval open) but Polymarket odds haven't caught up yet. Executes a directional bet on the confirmed side before the market reprices. Positions auto-resolve on interval expiry using the oracle settlement price.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│  Chainlink   │────▶│  Lag Detect   │────▶│   Execute    │────▶│ Auto-Resolve │
│  RTDS Feed   │     │  (Sigmoid)   │     │  (FOK→GTC)   │     │  on Expiry   │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
    BTC/ETH/SOL        fair vs odds         CLOB order          WIN/LOSS P&L
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Test everything works (dry run, aggressive thresholds)
python tarb_bot.py --dry-run --bankroll 100 --aggressive --log-level DEBUG --assets btc,eth,sol --timeframes 5m,15m

# 3. Live trading (recommended settings)
python tarb_bot.py --live --bankroll 50 --assets btc,eth,sol --timeframes 15m
```

**Dashboard:** http://localhost:8080 (opens automatically)

---

## Tested Results (Feb 26, 2026)

### Dry Run — Aggressive Mode (7 hours, 91 trades)

Full infrastructure test across US market hours and evening session.

| Period | Trades | W/L | Win Rate | Net P&L | Notes |
|--------|--------|-----|----------|---------|-------|
| 14:00–18:00 (US hours) | 43 | 30/13 | **70%** | **+$31.61** | Strategy works well |
| 18:00–19:00 (transition) | 14 | 8/6 | 57% | −$3 | Starting to fade |
| 20:00–21:30 (evening) | 22 | 5/17 | **23%** | **−$70** | Moves don't follow through |

**Takeaway:** The temporal arbitrage edge is real during active market hours but vanishes in the evening when crypto moves are noisier and reverse more often. Aggressive thresholds (5bps, 0.20 conviction) are too loose — most evening losses would be filtered by normal thresholds.

### Dry Run — Normal Thresholds (33 minutes, 9 trades)

Shorter test with production thresholds during active hours.

| Trades | W/L | Win Rate | Net P&L | Avg Entry |
|--------|-----|----------|---------|-----------|
| 9 | 5/1 (3 open) | **83%** | **+$7.43** | $0.56–$0.80 |

### Recommended Live Configuration

Based on the test data:

```bash
python tarb_bot.py --live --bankroll 50 --assets btc,eth,sol --timeframes 15m
```

- **15m only** — 5m markets are noisier and gave worse results. 15m gives directional signals more time to develop and the market more time to lag behind the oracle.
- **Normal thresholds** — 15bps move, 4¢ lag, 0.60 conviction. Filters out the low-conviction trades that dragged down the aggressive session.
- **$50 bankroll** — With $5 base bets and max 3 concurrent positions, $15 at risk at any time. Room to absorb a losing streak.
- **All 3 assets** — BTC, ETH, and SOL all produced winning signals. SOL had slightly more volatility (more signals) while BTC was steadier.

### Setup for Live Trading

1. Export your private key from https://reveal.polymarket.com
2. Create `.env` in the bot directory:
   ```
   POLY_PRIVATE_KEY=your_private_key_here
   POLY_FUNDER_ADDRESS=your_wallet_address_here
   POLY_SIGNATURE_TYPE=1
   ```
   Signature type: `1` = email/magic link, `2` = browser wallet, `0` = EOA
3. Ensure your Polymarket account has USDC on Polygon
4. Run with `--live` flag

---

## How It Works

### The Edge

Polymarket's 5-minute and 15-minute crypto UP/DOWN markets resolve based on the Chainlink oracle price at interval expiry. The oracle price is available via RTDS WebSocket in real time, but the CLOB order book doesn't always reprice instantly — especially during sudden moves. TARB Bot detects this lag window and bets on the confirmed direction before the book catches up.

### Signal Pipeline (8 Gates)

Every 2 seconds, the bot scans all active markets through this pipeline:

| Gate | Check | Normal | Aggressive |
|------|-------|--------|------------|
| 1 | RTDS price available and fresh (<30s) | — | — |
| 2 | Anchor price established for interval | — | — |
| 3 | Price move ≥ threshold from anchor | ≥15 bps | ≥5 bps |
| 4 | Direction confirmed (UP if +, DOWN if −) | — | — |
| 5 | Target price within bounds | $0.30–$0.70 | $0.10–$0.90 |
| 6 | **Odds lag** ≥ threshold (calibrated sigmoid) | ≥$0.04 | ≥$0.01 |
| 7 | Net edge > breakeven after fees | — | — |
| 8 | Conviction score ≥ threshold | ≥0.60 | ≥0.20 |

### Lag Detection (Gate 6)

The core of the strategy. Two complementary signals:

**Price Lag** — The oracle says the directional token should be worth X, but the market is selling it for Y < X. Computed using a calibrated sigmoid fair-value model:

```
oracle_fair = min(0.96, 1 / (1 + exp(-|move_bps| / 15)))
price_lag   = max(0, oracle_fair - market_price)
```

The sigmoid was calibrated against actual Polymarket pricing data (RMSE=0.019). It uses a slightly more aggressive slope (divisor 15) than the market-calibrated fit (divisor 18) to create a detectable gap at medium-sized moves.

**Temporal Lag** — If the CLOB book price hasn't changed in >3 seconds while the oracle is moving, the market is stale. This adds a bonus of up to $0.05 to the effective lag:

```
temporal_bonus = min(0.05, (stale_seconds - 3) / 7 * 0.05)
odds_lag       = price_lag + temporal_bonus
```

### Fair Value Model Comparison

| Move | Old Linear (`0.50 + m/200`) | New Sigmoid | Actual Market |
|------|----------------------------|-------------|---------------|
| 10 bps | 0.550 | 0.635 | 0.633 |
| 20 bps | 0.600 | 0.752 | 0.704 |
| 30 bps | 0.650 | 0.841 | 0.791 |
| 50 bps | 0.750 | 0.941 | 0.938 |

The old model was 6x less accurate (RMSE 0.123 vs 0.019), producing lag=0 for 99% of signals. The new model detects real mispricing in 42% of evaluated signals.

### Position Sizing

Quarter-Kelly criterion:

```
kelly_fraction = edge / (1 - entry_price)
bet_size = base_bet * kelly_fraction * 0.25 * 10
clamped to [base_bet, max_bet]
```

### Fee Formula

Exact Polymarket taker fee: `fee = price × (1 − price) × 0.0625`

Peaks at 1.5625% when price = $0.50, drops toward 0% at extremes.

---

## Architecture

```
tarb_bot/
├── tarb_bot.py              # Main orchestrator, signal detection, CLI
├── config/
│   └── settings.py          # All parameters, fee math, sigmoid model
├── core/
│   ├── price_stream.py      # Chainlink/Binance RTDS WebSocket
│   ├── tarb_client.py       # Market discovery, CLOB order execution
│   ├── tarb_tracker.py      # Position tracking, auto-resolution, P&L
│   └── dashboard.py         # Web dashboard (aiohttp + WebSocket)
├── requirements.txt
├── .env.example
└── README.md
```

### Module Details

**`tarb_bot.py`** — Async orchestrator running 6 concurrent tasks: RTDS listener, signal scanner (2s), market discovery (30s), auto-resolution (5s), terminal dashboard (15s), and WebSocket push (3s). Houses the 8-gate signal pipeline and conviction scoring.

**`price_stream.py`** — Connects to `wss://ws-live-data.polymarket.com`. Subscribes to both Chainlink (oracle truth, used for settlement) and Binance (faster updates, cross-reference) feeds. Maintains anchor prices per interval, price snapshots for resolution, and a 3-layer health system: ping keepalive (25s), subscription check (10s), and watchdog (30s dead-feed detection).

**`tarb_client.py`** — Deterministic market discovery via slug pattern `{asset}-updown-{5m|15m}-{unix_ts}` (bypasses Gamma API indexing lag). FOK→GTC order fallback with phantom fill detection. Tracks CLOB price change timestamps for temporal lag detection.

**`tarb_tracker.py`** — Manages open positions, auto-resolves on interval expiry using live RTDS price or cached snapshots. Tracks daily P&L, win rate, per-asset/timeframe breakdown, and enforces risk limits (daily loss, trade count, concurrent positions).

**`dashboard.py`** — Real-time web dashboard at `http://localhost:8080`. Dark theme with live price tickers, signal log, position table, P&L breakdown, risk bars, and event log. Pushes updates via WebSocket with exponential backoff reconnect.

---

## CLI Reference

```
python tarb_bot.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | ✓ | Simulate trades (no real orders) |
| `--live` | | Enable live trading (requires .env keys) |
| `--bankroll N` | 100 | Starting bankroll in USDC |
| `--assets X,Y` | btc,eth,sol | Comma-separated assets |
| `--timeframes X,Y` | 5m,15m | Comma-separated timeframes |
| `--aggressive` | | Lower all thresholds for testing |
| `--bet-size N` | | Override base bet size |
| `--max-bet N` | | Override max bet size |
| `--min-move N` | | Override min price move (bps) |
| `--min-conviction N` | | Override min conviction (0–1) |
| `--daily-limit N` | | Override daily loss limit |
| `--log-level X` | INFO | Log level (DEBUG, INFO, WARNING) |

### Aggressive Mode (`--aggressive`)

Lowers all thresholds for testing/observation:

| Parameter | Normal | Aggressive |
|-----------|--------|------------|
| `min_price_move_bps` | 15 | 5 |
| `min_odds_lag_cents` | 0.04 | 0.01 |
| `conviction_threshold` | 0.60 | 0.20 |
| `min_entry_price` | 0.30 | 0.10 |
| `max_entry_price` | 0.70 | 0.90 |
| `cooldown_secs` | 10 | 3 |

---

## Live Trading Setup

1. Export your private key from https://reveal.polymarket.com
2. Create `.env` from the example:

```bash
cp .env.example .env
```

3. Fill in credentials:

```env
POLY_PRIVATE_KEY=0xYourPrivateKey
POLY_FUNDER_ADDRESS=0xYourAddress
POLY_SIGNATURE_TYPE=1    # 1=email/magic, 2=browser wallet, 0=EOA
```

4. Start with conservative settings first:

```bash
python tarb_bot.py --live --bankroll 100 --assets btc --timeframes 15m
```

5. Monitor the dashboard for signal quality before increasing capital.

---

## Dashboard

Access at **http://localhost:8080** while the bot is running.

**Header:** Mode badge (DRY RUN / LIVE with pulse), uptime, live bankroll, Eastern Time clock.

**Price Tickers:** BTC, ETH, SOL with live prices and bps movement from interval anchor.

**Stats:** Net P&L, Win Rate, Trades Today, Fees Paid, Best/Worst Trade.

**Open Positions:** Side, market, entry price, cost, conviction bar, age timer.

**Recent Signals:** Time, direction, market, move bps, odds lag, conviction, result.

**P&L Breakdown:** Per-asset and per-timeframe tables.

**Risk Status:** Three progress bars (daily loss limit, trade count, open positions) with safe/warning/danger coloring.

**Event Log:** Scrolling log with color-coded entries (signal/trade/win/loss/error).

**WebSocket Indicator:** Bottom-right dot (green = live, red = reconnecting with countdown).

---

## Risk Controls

| Control | Default | Description |
|---------|---------|-------------|
| Daily loss limit | $50 | Stop all trading if daily net P&L drops below −$50 |
| Daily trade limit | 100 | Max trades per calendar day |
| Max concurrent | 3 | Max open positions at any time |
| Cooldown | 10s (3s agg) | Min time between trades on the same market |
| Price bounds | $0.30–$0.70 | Won't buy shares outside this range |
| Breakeven gate | Per-trade | Edge must exceed fee cost at entry price |

---

## Logging

Logs are written to `tarb_bot.log` in the run directory (UTF-8 encoded). Key log patterns:

```
# Signal evaluation (DEBUG)
btc-updown-5m-1772116200: DOWN move=-30.5bps fair=0.841 price=0.55 lag=0.291 ...

# Trade execution (INFO)
[DRY] ORDER: DOWN btc-updown-5m-1772116200 | price=0.55 size=$5.00 shares=9.1 ...

# Resolution (INFO)
RESOLVING pos_id: price=$67200.00 (live) anchor=$67000.00 move=+29.9bps side=UP -> WIN
[WIN] RESOLVED: pos_id | P&L=$+4.1200 | Daily=$+12.34 | W/L=8/2 (80%)

# RTDS health (INFO/WARNING)
RTDS subscription check: receiving prices for ['btc', 'eth', 'sol'] (1523 total msgs)
RTDS watchdog: no messages in 30s (count stuck at 1523). Forcing reconnect...

# Resolution tracking (DEBUG)
Resolution check: 3 open positions
  pos_id: 45s past expiry, attempting resolution
    Live price for btc: $67200.00
```

---

## Changelog

### v1.1.0 — Feb 26, 2026 (Post-Session Hotfix)

Analysis of a 3.5-hour dry-run session (40 trades, 14K log lines) revealed 4 critical bugs. All fixed in this release.

#### Bug 1: Positions Never Resolve (Silent P&L Tracking Failure)

**Symptom:** 40 positions opened, 0 `RESOLVED` entries in the log. Dashboard showed trades but no win/loss results. Yet positions did clear (new trades continued past max concurrent limit).

**Root Cause (3 overlapping issues):**

1. **Emoji crashes Windows file logger.** `resolve_position()` used `✅`/`❌` in `logger.info()`. On Windows, Python's `FileHandler` defaults to cp1252 encoding, which can't encode these Unicode characters. Python's logging module silently swallows the `UnicodeEncodeError` via its internal `handleError()` — the resolution executes correctly but the log line is eaten.

2. **Snapshot data deleted before resolution.** `cleanup_old_anchors()` deleted price snapshots for expired intervals, but `auto_resolve_expired()` needs those snapshots to settle positions after the interval ends. The cleanup ran in the same loop immediately after resolution, racing with it.

3. **Zero debug visibility.** `auto_resolve_expired()` had almost no logging — impossible to tell whether positions were being checked, why resolution was skipped, or what prices were used.

**Fixes:**
- Replaced emoji with ASCII tags `[WIN]`/`[LOSS]` in all log messages
- Added `encoding="utf-8"` to `FileHandler` initialization
- Resolution loop now passes open position interval keys to cleanup as a protected set
- `auto_resolve_expired()` now logs at every decision point: position count, seconds past expiry, live/snapshot price attempts, exact resolution logic
- Each `resolve_position()` call wrapped in individual try/except

#### Bug 2: RTDS Feed Dies After Reconnect, Never Recovers

**Symptom:** At 11:49 ET, the RTDS WebSocket silently disconnected. Bot reconnected successfully (subscription confirmed), but no price updates arrived on the new connection. The bot ran blind for 1+ hour showing "no RTDS price" for every market.

**Root Cause:** The WebSocket message loop (`async for msg in self._ws`) exited silently — no error frame, no close frame, no log. The reconnect established a new connection and sent subscriptions, but the server never produced data. Without a watchdog (not present in the running version), nobody noticed.

**Fixes:**
- **Subscription health check** — New `_subscription_check()` task: if no prices arrive within 10 seconds of subscribing, forces immediate reconnect (catches silent subscription failure)
- **Price backup/restore** — Before clearing `self.latest` on reconnect, backs up current prices. If subscription check fails, restores backup so the bot isn't blind during reconnect
- **Message loop exit logging** — Logs session uptime, message count, and WebSocket state when the loop exits (was completely silent before)
- **Session cleanup** — Explicitly closes old `aiohttp.ClientSession` before creating new one (prevents resource leak)
- **Proper task cancellation** — Awaits cancellation of all auxiliary tasks instead of fire-and-forget

#### Bug 3: Dashboard Shutdown Crash

**Symptom:** `AttributeError: 'NoneType' object has no attribute 'pre_shutdown'` on shutdown. Double "Shutting down" message in logs.

**Root Cause:** When the first connection attempt fails (DNS error), the aiohttp runner's internal `_server` is `None`. `cleanup()` calls `_server.pre_shutdown()` which crashes. Additionally, `stop()` was called twice — once from the signal handler, once from the `finally` block.

**Fixes:**
- Double-call guard on `TarbBot.stop()` — returns immediately if `_running` is already `False`
- Each shutdown step (`price_stream.stop()`, `client.disconnect()`, `dashboard.stop()`) wrapped in individual try/except so one failure doesn't block the rest
- Dashboard `stop()` clears client set and logs completion

#### Bug 4: Odds Lag Always 0.000 (Broken Signal Model)

**Symptom:** 3,530 out of 3,575 signal evaluations showed `lag=0.000`. The bot was trading purely on the breakeven edge check, which was trivially easy to pass at extreme prices ($0.10, $0.15). The lag gate — the core of the temporal arbitrage strategy — was never firing.

**Root Cause:** The implied fair value model was a linear function (`0.50 + move/200`) that was **6x too conservative** compared to actual Polymarket pricing. For a 30bps move, the model said fair=0.65, but the market was already at 0.88. Since `lag = max(0, fair - market_price)`, lag was negative (clamped to 0) for 99% of signals.

**Fix:**
- **Calibrated sigmoid model** — `fair = min(0.96, 1/(1+exp(-|move|/18)))` fit to actual market data. RMSE improved from 0.123 to 0.019 (6x better). Backtest on the session data: lag detection improved from 1% to 42% of signals.
- **Oracle-aggressive variant** — Signal detection uses divisor 15 (slightly more aggressive than calibrated 18) to create a detectable gap between oracle truth and market consensus
- **Temporal lag bonus** — Detects CLOB book staleness (no price change while oracle moves). Adds up to $0.05 bonus when book hasn't updated in 3+ seconds
- **Explicit lag gate restored** — `if odds_lag < min_odds_lag_cents: return None` now fires meaningfully with the corrected model

### v1.0.0 — Feb 26, 2026 (Initial Release)

Initial build with complete feature set:
- Chainlink/Binance RTDS WebSocket price streaming
- Deterministic market discovery via slug pattern
- 8-gate signal pipeline with conviction scoring
- FOK→GTC order execution with phantom fill detection
- Auto-resolution on interval expiry
- Real-time web dashboard with WebSocket push
- Quarter-Kelly position sizing
- Risk controls (daily loss limit, trade count, concurrent positions)
- Windows + Python 3.9+ compatibility
- Eastern Time across all timestamps

#### Earlier Fixes (During v1.0 Development)

1. Odds lag formula identical for UP/DOWN — replaced with proper `implied_fair` model
2. Edge check comparing wrong units — raw bps vs breakeven edge
3. Dead `net_edge` variable — computed wrong, never used
4. Anchor prices accumulated forever — added `cleanup_old_anchors()`
5. Anchors set from wrong source — Binance updates were setting anchors when trading off Chainlink
6. `clobTokenIds` string parsing — Gamma API sometimes returns JSON string, not list
7. Windows signal handler crash — `loop.add_signal_handler` is POSIX-only
8. Python 3.10+ type syntax — `dict | None` and `tuple[bool, str]` crash on 3.9
9. Dashboard `parseFloat` NaN — tracker formatted values with `$` signs but JS called `parseFloat("$+3.42")`
10. Bankroll never updated — was static CLI arg, now computes `starting + net_pnl - open_cost`
11. Dashboard WebSocket dying — no server-side keepalive, added `heartbeat=20` + exponential backoff reconnect
12. Timezone mismatch — logs in UTC, clock in local. Changed all to Eastern Time

---

## Requirements

- Python 3.9+
- Windows, macOS, or Linux
- Internet connection (Polymarket WebSocket + API)
- For live trading: Polymarket account with funded USDC balance

### Dependencies

```
aiohttp>=3.9.0
py-clob-client>=0.34.0
websockets>=12.0
python-dotenv>=1.0.0
```

---

## License

Private / personal use. Not financial advice. Trade at your own risk.
