#!/usr/bin/env python3
"""
TARB Bot — Temporal Arbitrage Bot for Polymarket
=================================================
Exploits the latency between Chainlink RTDS real-time prices and
Polymarket odds on 5m/15m crypto UP/DOWN markets.

Core Loop:
1. Stream real-time prices from RTDS (BTC, ETH, SOL)
2. For each active 5m/15m market, track the anchor price (interval open)
3. When RTDS price confirms direction, check if Polymarket odds lag
4. If lagging odds detected → slam directional bet on confirmed side
5. Auto-resolve on market expiry, recycle capital

Usage:
    python tarb_bot.py --bankroll 100 --dry-run --assets btc,eth,sol --timeframes 5m,15m
    python tarb_bot.py --bankroll 500 --assets btc --timeframes 15m
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from config.settings import (
    ASSETS, TIMEFRAMES, TIMEFRAME_SECONDS, STRATEGY, DRY_RUN,
    taker_fee, net_payout, breakeven_edge, LOG_LEVEL, LOG_FILE,
)
from core.price_stream import PriceStream, PriceUpdate
from core.tarb_client import TarbClient, MarketInfo
from core.tarb_tracker import TarbTracker
from core.dashboard import DashboardServer

# ─── Logging Setup ───────────────────────────────────────────────────────────────

def setup_logging(level: str = LOG_LEVEL):
    fmt = "%(asctime)s | %(name)-18s | %(levelname)-5s | %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ]
    logging.basicConfig(level=getattr(logging, level.upper()), format=fmt, handlers=handlers)

logger = logging.getLogger("tarb.main")


# ─── Conviction Scoring ─────────────────────────────────────────────────────────

def compute_conviction(
    move_bps: float,
    odds_lag: float,
    time_remaining_pct: float,
    entry_price: float,
) -> float:
    """
    Score from 0.0 to 1.0 indicating how confident we are in the signal.

    Factors:
    - move_bps: Larger RTDS price move = stronger directional signal (0-0.35)
    - odds_lag/edge: How much the market underprices the move (0-0.25)
    - time_remaining_pct: Sweet spot is 20-65% elapsed (0-0.20)
    - entry_price: Best when market is still near 50% (0-0.20)
    """
    # Price move strength (0-0.35): normalize 5-100 bps to 0-0.35
    move_score = min(0.35, max(0, (abs(move_bps) - STRATEGY.min_price_move_bps) / 95 * 0.35))

    # Edge strength (0-0.25): normalize edge 0.01-0.15 to 0-0.25
    edge_score = min(0.25, max(0, (odds_lag - 0.01) / 0.14 * 0.25))

    # Time factor (0-0.20): better if we're in the middle of the interval
    # Too early = anchor unreliable, too late = less time for arb
    elapsed = 1.0 - time_remaining_pct
    time_score = 0.0
    if 0.20 <= elapsed <= 0.65:
        time_score = 0.20  # Sweet spot
    elif 0.10 <= elapsed <= 0.75:
        time_score = 0.10  # Acceptable

    # Price position factor (0-0.20): best when price is 0.35-0.65
    price_score = 0.0
    if 0.35 <= entry_price <= 0.65:
        price_score = 0.20  # Maximum mispricing potential
    elif 0.25 <= entry_price <= 0.75:
        price_score = 0.10

    return min(1.0, move_score + edge_score + time_score + price_score)


# ─── Signal Detection ───────────────────────────────────────────────────────────

async def evaluate_signal(
    market: MarketInfo,
    price_stream: PriceStream,
    tracker: TarbTracker,
    dashboard=None,
) -> Optional[dict]:
    """
    Evaluate whether there's a tradeable temporal arb signal.
    Returns signal dict if actionable, None otherwise.
    
    Signal gates (in order):
    1. RTDS price available and fresh
    2. Anchor price established for this interval
    3. Price move >= min_price_move_bps from anchor
    4. Direction determined (UP if move positive, DOWN if negative)
    5. Target price within tradeable bounds
    6. Odds lag detected (price mispricing OR temporal staleness)
    7. Net edge > breakeven edge after fees
    8. Conviction score >= threshold
    """
    asset = market.asset
    interval_ts = market.interval_ts

    # Get current RTDS price
    current = price_stream.get_price(asset)
    if not current:
        logger.debug(f"  {market.slug}: no RTDS price")
        return None

    # Get or set anchor price
    anchor = price_stream.get_anchor(asset, interval_ts)
    if not anchor:
        # First time seeing this interval — set anchor
        price_stream.set_anchor(asset, interval_ts, current.price)
        return None

    # Calculate price move from anchor
    move_bps = price_stream.price_move_bps(asset, interval_ts)
    if move_bps is None:
        return None

    # Check minimum price movement
    if abs(move_bps) < STRATEGY.min_price_move_bps:
        logger.debug(f"  {market.slug}: move={move_bps:+.1f}bps < {STRATEGY.min_price_move_bps}bps min")
        return None

    # Determine direction
    if move_bps > 0:
        direction = "UP"
        target_price = market.up_price
    else:
        direction = "DOWN"
        target_price = market.down_price

    # Check price bounds
    if target_price < STRATEGY.min_entry_price or target_price > STRATEGY.max_entry_price:
        logger.debug(f"  {market.slug}: price={target_price:.2f} out of bounds [{STRATEGY.min_entry_price}-{STRATEGY.max_entry_price}]")
        return None

    # ── Lag Detection ──────────────────────────────────────────────────────────
    # 
    # Two complementary signals:
    # 1. PRICE LAG: implied_fair - market_price (market underprices the move)
    # 2. TEMPORAL LAG: market odds haven't changed despite oracle movement
    #    (measured by how long since CLOB odds last moved)
    #
    # We use a slightly more aggressive fair value model than the market-calibrated
    # sigmoid. The calibrated model tracks what the market DOES; we want what the
    # market SHOULD price based on the oracle signal.
    
    move_magnitude = abs(move_bps)
    
    # Oracle-implied fair value — more aggressive than market calibration
    # Uses a tighter sigmoid (divisor 15 vs calibrated 18) so it ramps faster,
    # creating a gap between oracle-truth and market-consensus at medium moves
    import math
    oracle_fair = min(0.96, 1.0 / (1.0 + math.exp(-move_magnitude / 15.0)))
    
    # Price-based lag: oracle says it should be worth X, market says Y
    price_lag = max(0, oracle_fair - target_price)
    
    # Temporal lag: how long since the CLOB odds last changed?
    # If oracle moved but market hasn't budged, the market is slow
    temporal_lag_secs = 0.0
    if market.last_price_change_at > 0:
        temporal_lag_secs = time.time() - market.last_price_change_at
    
    # Temporal lag bonus: if market odds are stale (>3s since last change) 
    # while oracle is moving, boost the effective lag
    # Scale: 0 bonus at 0-3s, up to 0.05 bonus at 10s+
    temporal_bonus = 0.0
    if temporal_lag_secs > 3.0 and move_magnitude >= STRATEGY.min_price_move_bps:
        temporal_bonus = min(0.05, (temporal_lag_secs - 3.0) / 7.0 * 0.05)
    
    # Combined odds lag = price mispricing + temporal staleness bonus
    odds_lag = price_lag + temporal_bonus
    
    # GATE: Minimum odds lag — reject signals where market already priced the move
    if odds_lag < STRATEGY.min_odds_lag_cents:
        if move_magnitude >= STRATEGY.min_price_move_bps * 2:
            # Only log near-misses for significant moves (avoid spam)
            logger.debug(
                f"  {market.slug}: {direction} move={move_bps:+.1f}bps "
                f"fair={oracle_fair:.3f} price={target_price:.2f} "
                f"lag={odds_lag:.3f} (price={price_lag:.3f} temporal={temporal_bonus:.3f} "
                f"stale={temporal_lag_secs:.1f}s) < {STRATEGY.min_odds_lag_cents} min"
            )
        return None

    # The "edge" is lag minus the fee cost — our actual expected profit per share
    edge = odds_lag
    
    # GATE: Net edge must exceed breakeven after fees
    be_edge = breakeven_edge(target_price)

    if edge < be_edge:
        logger.debug(
            f"  {market.slug}: {direction} edge={edge:.4f} < breakeven={be_edge:.4f} "
            f"(move={move_bps:+.1f}bps price={target_price:.2f} fair={oracle_fair:.3f} "
            f"lag={odds_lag:.3f} stale={temporal_lag_secs:.1f}s)"
        )
        return None

    # Compute conviction
    conviction = compute_conviction(
        move_bps=move_bps,
        odds_lag=odds_lag,
        time_remaining_pct=1.0 - market.elapsed_pct,
        entry_price=target_price,
    )

    if conviction < STRATEGY.conviction_threshold:
        # Near miss — log it and push to dashboard so user can see activity
        logger.info(
            f"  {market.slug}: NEAR MISS {direction} conv={conviction:.2f} < {STRATEGY.conviction_threshold} "
            f"(move={move_bps:+.1f}bps edge={edge:.3f} price={target_price:.2f} stale={temporal_lag_secs:.1f}s)"
        )
        if dashboard:
            await dashboard.push_signal({
                "direction": direction,
                "market": market.slug,
                "move_bps": move_bps,
                "odds_lag": odds_lag,
                "conviction": conviction,
                "result": "skip (conv)",
            })
        return None

    # Position sizing (quarter-Kelly)
    kelly = edge / (1.0 - target_price) if target_price < 1 else 0
    bet_size = min(
        STRATEGY.max_bet_usd,
        max(STRATEGY.base_bet_usd, STRATEGY.base_bet_usd * kelly * STRATEGY.kelly_fraction * 10),
    )

    fee = taker_fee(target_price)

    return {
        "asset": asset,
        "direction": direction,
        "market": market,
        "target_price": target_price,
        "move_bps": move_bps,
        "odds_lag": odds_lag,
        "conviction": conviction,
        "bet_size": bet_size,
        "rtds_price": current.price,
        "anchor_price": anchor.price,
        "breakeven_edge": be_edge,
        "fee": fee,
        "oracle_fair": oracle_fair,
        "price_lag": price_lag,
        "temporal_lag_secs": temporal_lag_secs,
        "temporal_bonus": temporal_bonus,
    }


# ─── Main Bot ────────────────────────────────────────────────────────────────────

class TarbBot:
    def __init__(self, bankroll: float, assets: list, timeframes: list, dry_run: bool):
        self.bankroll = bankroll
        self.enabled_assets = assets
        self.enabled_timeframes = timeframes
        self.dry_run = dry_run

        self.price_stream = PriceStream()
        self.client = TarbClient()
        self.tracker = TarbTracker()
        self.dashboard = DashboardServer(port=STRATEGY.dashboard_port)

        self._running = False
        self._scan_interval = 2.0  # Scan every 2 seconds
        self._discovery_interval = 30.0  # Rediscover markets every 30s
        self._dashboard_interval = 15.0  # Print dashboard every 15s

        # Configure enabled assets
        for key in ASSETS:
            ASSETS[key].enabled = key in self.enabled_assets

    async def start(self):
        """Start the bot."""
        self._running = True

        banner = f"""
╔══════════════════════════════════════════════════════════════╗
║                    TARB BOT v1.0                             ║
║           Temporal Arbitrage · Polymarket                    ║
╠══════════════════════════════════════════════════════════════╣
║  Mode:       {'DRY RUN' if self.dry_run else 'LIVE 🔴'}                                   ║
║  Bankroll:   ${self.bankroll:<10.2f}                              ║
║  Assets:     {', '.join(self.enabled_assets):<30s}             ║
║  Timeframes: {', '.join(self.enabled_timeframes):<30s}             ║
║  Bet Size:   ${STRATEGY.base_bet_usd:.2f} - ${STRATEGY.max_bet_usd:.2f}                          ║
║  Min Edge:   {STRATEGY.min_net_edge:.0%} | Min Move: {STRATEGY.min_price_move_bps:.0f}bps               ║
║  Daily Limit: ${STRATEGY.daily_loss_limit_usd:.0f} loss / {STRATEGY.daily_trade_limit} trades               ║
╚══════════════════════════════════════════════════════════════╝
"""
        logger.info(banner)

        # Connect CLOB client
        await self.client.connect()

        # Setup dashboard
        self.dashboard.set_state_provider(self._get_dashboard_state)
        self.dashboard.set_price_provider(
            lambda: {k: v.price for k, v in self.price_stream.latest.items()}
        )
        self.dashboard.set_trade_history_provider(self._get_trade_history)
        await self.dashboard.start()
        logger.info(f"Dashboard: http://localhost:{STRATEGY.dashboard_port}")

        # Push startup info to dashboard
        await self.dashboard.push_log("info",
            f"TARB Bot v1.0 started — {'DRY RUN' if self.dry_run else 'LIVE'} — bankroll ${self.bankroll:.2f}"
        )
        await self.dashboard.push_log("info",
            f"Assets: {', '.join(a.upper() for a in self.enabled_assets)} | "
            f"Timeframes: {', '.join(self.enabled_timeframes)}"
        )
        await self.dashboard.push_log("info", "Connecting to Chainlink RTDS price feed...")

        # Register price callback
        self.price_stream.on_price(self._on_price_update)

        # Run concurrent tasks
        try:
            await asyncio.gather(
                self.price_stream.start(),        # RTDS WebSocket listener
                self._scan_loop(),                # Signal scanner
                self._discovery_loop(),           # Market discovery
                self._resolution_loop(),          # Auto-resolve expired positions
                self._dashboard_loop(),           # Terminal dashboard
                self.dashboard.run_push_loop(3),  # WebSocket push to dashboard
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self):
        """Gracefully stop the bot."""
        if not self._running:
            return  # Already stopped — prevent double shutdown
        self._running = False
        logger.info("Shutting down TARB Bot...")
        try:
            await self.price_stream.stop()
        except Exception as e:
            logger.error(f"Error stopping price stream: {e}")
        try:
            await self.client.disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting client: {e}")
        try:
            await self.dashboard.stop()
        except Exception as e:
            logger.error(f"Error stopping dashboard: {e}")
        try:
            self.tracker.print_dashboard()
        except Exception:
            pass
        logger.info("TARB Bot stopped.")

    async def _on_price_update(self, update: PriceUpdate):
        """Called on every RTDS price update."""
        # Only set anchors from the configured trading source
        is_trading_source = (
            (STRATEGY.use_chainlink and update.source == "chainlink") or
            (not STRATEGY.use_chainlink and update.source == "binance")
        )
        if not is_trading_source:
            return

        # Set anchor for any new intervals we haven't seen
        for tf in self.enabled_timeframes:
            interval_secs = TIMEFRAME_SECONDS[tf]
            interval_ts = int(time.time() // interval_secs) * interval_secs
            if not self.price_stream.get_anchor(update.symbol, interval_ts):
                self.price_stream.set_anchor(update.symbol, interval_ts, update.price)

        # Push price to dashboard
        move = None
        for tf in self.enabled_timeframes:
            interval_secs = TIMEFRAME_SECONDS[tf]
            interval_ts = int(time.time() // interval_secs) * interval_secs
            m = self.price_stream.price_move_bps(update.symbol, interval_ts)
            if m is not None:
                move = m
                break
        await self.dashboard.push_price(update.symbol, update.price, move)

    def _get_dashboard_state(self) -> dict:
        """Build state dict for the dashboard."""
        summary = self.tracker.summary()
        summary["mode"] = "DRY RUN" if self.dry_run else "LIVE"
        # Bankroll = starting + net P&L - currently locked in open positions
        open_cost = sum(p.cost_usd for p in self.tracker.open_positions.values())
        summary["bankroll"] = self.bankroll + self.tracker.today_stats.net_pnl - open_cost
        summary["daily_loss_limit"] = STRATEGY.daily_loss_limit_usd
        summary["daily_trade_limit"] = STRATEGY.daily_trade_limit
        summary["max_concurrent"] = STRATEGY.max_concurrent_positions
        return summary

    def _get_trade_history(self) -> list:
        """Build list of trade/resolve events from closed positions for dashboard replay."""
        events = []
        for pos in self.tracker.closed_positions:
            # Trade entry event
            from datetime import datetime, timezone, timedelta
            et = timezone(timedelta(hours=-5))
            entry_time = datetime.fromtimestamp(pos.entry_time, tz=et).strftime("%I:%M:%S %p")
            events.append({
                "type": "trade",
                "data": {
                    "message": f"{'[DRY] ' if self.dry_run else ''}FILLED {pos.side} {pos.market_slug} @ {pos.entry_price:.2f} (${pos.cost_usd:.2f})",
                    "time": entry_time,
                }
            })
            # Resolution event
            result_tag = "WIN" if pos.won else "LOSS"
            resolve_time = datetime.fromtimestamp(pos.resolved_at, tz=et).strftime("%I:%M:%S %p")
            events.append({
                "type": "resolve",
                "data": {
                    "won": pos.won,
                    "message": f"{result_tag} {pos.side} {pos.market_slug} P&L={pos.pnl:+.4f}",
                    "time": resolve_time,
                }
            })
        return events

    async def _discovery_loop(self):
        """Periodically discover active markets."""
        while self._running:
            try:
                markets = await self.client.discover_all_markets()
                if markets:
                    logger.debug(f"Active markets: {len(markets)}")
                    for m in markets:
                        if m.is_tradeable:
                            await self.dashboard.push_log("info",
                                f"Market: {m.slug} | UP={m.up_price:.2f} DOWN={m.down_price:.2f} | {m.time_remaining}s left"
                            )
            except Exception as e:
                logger.error(f"Discovery error: {e}")
            await asyncio.sleep(self._discovery_interval)

    async def _scan_loop(self):
        """Main trading loop — scan for signals and execute."""
        # Wait for initial price data
        await asyncio.sleep(5)

        while self._running:
            try:
                await self._scan_once()
            except Exception as e:
                logger.error(f"Scan error: {e}")
            await asyncio.sleep(self._scan_interval)

    async def _scan_once(self):
        """Single scan iteration across all active markets."""
        # Check risk limits
        can_trade, reason = self.tracker.can_trade()
        if not can_trade:
            logger.debug(f"Trading blocked: {reason}")
            return

        for asset_key in self.enabled_assets:
            for tf in self.enabled_timeframes:
                # Discover/get current market
                market = await self.client.discover_market(asset_key, tf)
                if not market or not market.is_tradeable:
                    continue

                # Refresh prices
                await self.client.refresh_prices(market)

                # Check cooldown
                if not self.tracker.check_cooldown(market.slug):
                    continue

                # Check if we already have a position in this market
                existing = self.tracker.get_positions_for_market(market.slug)
                if existing:
                    continue

                # Evaluate signal
                signal = await evaluate_signal(market, self.price_stream, self.tracker, self.dashboard)
                if not signal:
                    continue

                # Execute!
                logger.info(
                    f"SIGNAL: {signal['direction']} {market.slug} | "
                    f"move={signal['move_bps']:+.1f}bps lag={signal['odds_lag']:.3f} "
                    f"(price={signal['price_lag']:.3f} +temporal={signal['temporal_bonus']:.3f} "
                    f"stale={signal['temporal_lag_secs']:.1f}s) "
                    f"conviction={signal['conviction']:.2f} size=${signal['bet_size']:.2f} "
                    f"price={signal['target_price']:.2f} fee={signal['fee']:.4f}"
                )

                # Push signal to dashboard
                await self.dashboard.push_signal({
                    "direction": signal["direction"],
                    "market": market.slug,
                    "move_bps": signal["move_bps"],
                    "odds_lag": signal["odds_lag"],
                    "conviction": signal["conviction"],
                    "result": "pending",
                })
                await self.dashboard.push_log("signal",
                    f"{signal['direction']} {market.slug} | "
                    f"move={signal['move_bps']:+.1f}bps conv={signal['conviction']:.2f}"
                )

                result = await self.client.place_order(
                    market=market,
                    side=signal["direction"],
                    size_usd=signal["bet_size"],
                )

                if result.success:
                    self.tracker.open_position(
                        asset=asset_key,
                        timeframe=tf,
                        side=signal["direction"],
                        market_slug=market.slug,
                        interval_ts=market.interval_ts,
                        entry_price=result.fill_price,
                        shares=result.fill_size,
                        cost_usd=signal["bet_size"],
                        fee_paid=result.fee_paid,
                        order_id=result.order_id,
                        rtds_price=signal["rtds_price"],
                        anchor_price=signal["anchor_price"],
                        move_bps=signal["move_bps"],
                        conviction=signal["conviction"],
                        odds_lag=signal["odds_lag"],
                    )
                    await self.dashboard.push_trade({
                        "message": f"FILLED {signal['direction']} {market.slug} @ {result.fill_price:.2f} (${signal['bet_size']:.2f})",
                    })
                else:
                    logger.warning(
                        f"Order failed: {result.error}"
                        f"{' (PHANTOM)' if result.is_phantom else ''}"
                    )
                    await self.dashboard.push_log("error",
                        f"Order failed: {result.error}"
                    )

    async def _resolution_loop(self):
        """Auto-resolve expired positions."""
        logger.info("Resolution loop started")
        while self._running:
            try:
                # Snapshot open positions before resolution
                before = set(self.tracker.open_positions.keys())

                def get_live_price(asset):
                    """Get live price for resolution (bypass staleness for resolution)."""
                    update = self.price_stream.latest.get(asset)
                    if update:
                        return update.price
                    return None

                def get_snapshot_price(asset, interval_ts):
                    """Get last known price during the interval."""
                    return self.price_stream.get_snapshot_price(asset, interval_ts)

                # Pass both price getters
                self.tracker.auto_resolve_expired(
                    get_live_price,
                    get_snapshot_fn=get_snapshot_price,
                )
                self.client.cleanup_expired()
                # Protect anchors/snapshots for any interval with open positions
                open_keys = {
                    f"{p.asset}_{p.interval_ts}" 
                    for p in self.tracker.open_positions.values()
                }
                self.price_stream.cleanup_old_anchors(open_interval_keys=open_keys)

                # Check for newly resolved positions
                after = set(self.tracker.open_positions.keys())
                resolved_ids = before - after
                if resolved_ids:
                    logger.info(f"Resolved {len(resolved_ids)} positions this cycle")
                for pos in self.tracker.closed_positions[-len(resolved_ids):] if resolved_ids else []:
                    if pos.id in resolved_ids:
                        result_tag = "WIN" if pos.won else "LOSS"
                        try:
                            await self.dashboard.push_resolve({
                                "won": pos.won,
                                "message": (
                                    f"{result_tag} {pos.side} {pos.market_slug} "
                                    f"P&L={pos.pnl:+.4f}"
                                ),
                            })
                            result = "win" if pos.won else "loss"
                            await self.dashboard.push_log(result,
                                f"{result_tag} {pos.side} {pos.market_slug} | "
                                f"P&L=${pos.pnl:+.2f} | Daily=${self.tracker.today_stats.net_pnl:+.2f}"
                            )
                        except Exception as e:
                            logger.error(f"Dashboard push error for {pos.id}: {e}")
            except Exception as e:
                logger.error(f"Resolution error: {e}", exc_info=True)
            await asyncio.sleep(5)

    async def _dashboard_loop(self):
        """Periodically print dashboard."""
        while self._running:
            await asyncio.sleep(self._dashboard_interval)
            try:
                self.tracker.print_dashboard()

                # Log price status
                for asset in self.enabled_assets:
                    p = self.price_stream.get_price(asset)
                    if p:
                        logger.debug(
                            f"  {asset.upper()}: ${p.price:,.2f} "
                            f"(age={p.age_ms}ms, {p.source})"
                        )
            except Exception:
                pass


# ─── CLI ─────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="TARB Bot — Temporal Arbitrage for Polymarket Crypto Markets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tarb_bot.py --dry-run --bankroll 100
  python tarb_bot.py --bankroll 500 --assets btc,eth --timeframes 15m
  python tarb_bot.py --bankroll 1000 --assets btc,eth,sol --min-move 20 --min-conviction 0.65
        """,
    )
    parser.add_argument("--bankroll", type=float, default=100.0, help="Starting bankroll in USDC")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate trades (default)")
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires keys)")
    parser.add_argument("--assets", type=str, default="btc,eth,sol", help="Comma-separated assets")
    parser.add_argument("--timeframes", type=str, default="5m,15m", help="Comma-separated timeframes")
    parser.add_argument("--bet-size", type=float, default=None, help="Override base bet size")
    parser.add_argument("--max-bet", type=float, default=None, help="Override max bet size")
    parser.add_argument("--min-move", type=float, default=None, help="Override min price move (bps)")
    parser.add_argument("--min-conviction", type=float, default=None, help="Override min conviction (0-1)")
    parser.add_argument("--daily-limit", type=float, default=None, help="Override daily loss limit")
    parser.add_argument("--aggressive", action="store_true", help="Lower all thresholds for testing")
    parser.add_argument("--log-level", type=str, default="INFO", help="Log level")
    return parser.parse_args()


async def main():
    args = parse_args()
    setup_logging(args.log_level)

    # Apply overrides
    dry_run = not args.live
    if args.aggressive:
        STRATEGY.min_price_move_bps = 5.0
        STRATEGY.min_odds_lag_cents = 0.01
        STRATEGY.conviction_threshold = 0.20
        STRATEGY.min_entry_price = 0.10
        STRATEGY.max_entry_price = 0.90
        STRATEGY.cooldown_secs = 3
        logger.info("AGGRESSIVE MODE: thresholds lowered for testing")
    if args.bet_size:
        STRATEGY.base_bet_usd = args.bet_size
    if args.max_bet:
        STRATEGY.max_bet_usd = args.max_bet
    if args.min_move:
        STRATEGY.min_price_move_bps = args.min_move
    if args.min_conviction:
        STRATEGY.conviction_threshold = args.min_conviction
    if args.daily_limit:
        STRATEGY.daily_loss_limit_usd = args.daily_limit

    assets = [a.strip().lower() for a in args.assets.split(",")]
    timeframes = [t.strip() for t in args.timeframes.split(",")]

    # Validate
    for a in assets:
        if a not in ASSETS:
            logger.error(f"Unknown asset: {a}. Available: {list(ASSETS.keys())}")
            sys.exit(1)
    for t in timeframes:
        if t not in TIMEFRAME_SECONDS:
            logger.error(f"Unknown timeframe: {t}. Available: {list(TIMEFRAME_SECONDS.keys())}")
            sys.exit(1)

    bot = TarbBot(
        bankroll=args.bankroll,
        assets=assets,
        timeframes=timeframes,
        dry_run=dry_run,
    )

    # Handle shutdown signals (Unix only — Windows uses KeyboardInterrupt)
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
