"""
Temporal Arb Position Tracker
=============================
Tracks open positions, resolved P&L, daily stats, and risk limits.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config.settings import STRATEGY, taker_fee

logger = logging.getLogger("tarb.tracker")


@dataclass
class Position:
    """A single directional bet."""
    id: str                    # Unique position ID
    asset: str                 # btc, eth, sol
    timeframe: str             # 5m, 15m
    side: str                  # UP or DOWN
    market_slug: str
    interval_ts: int           # Market interval timestamp
    entry_price: float         # Price paid per share
    shares: float              # Number of shares
    cost_usd: float            # Total cost in USDC
    fee_paid: float            # Taker fee paid
    entry_time: float          # Unix timestamp of entry
    order_id: str = ""

    # Signal data at entry
    rtds_price: float = 0.0   # RTDS oracle price at entry
    anchor_price: float = 0.0 # Anchor price at interval start
    move_bps: float = 0.0     # Price move in bps at entry
    conviction: float = 0.0   # Conviction score at entry
    odds_lag: float = 0.0     # Detected odds lag at entry

    # Resolution
    resolved: bool = False
    won: Optional[bool] = None
    pnl: float = 0.0          # Net P&L after fees
    resolved_at: float = 0.0

    @property
    def max_payout(self) -> float:
        """Payout if position wins ($1 per share)."""
        return self.shares * 1.0

    @property
    def expected_profit(self) -> float:
        """Expected profit if win (before resolution)."""
        return self.shares * (1.0 - self.entry_price) - self.fee_paid

    @property
    def age_secs(self) -> float:
        return time.time() - self.entry_time


@dataclass
class DailyStats:
    date: str                  # YYYY-MM-DD
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_wagered: float = 0.0
    total_fees: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0      # After fees
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_conviction: float = 0.0
    avg_entry_price: float = 0.0

    # Per-asset breakdown
    pnl_by_asset: Dict[str, float] = field(default_factory=dict)
    trades_by_asset: Dict[str, int] = field(default_factory=dict)
    pnl_by_timeframe: Dict[str, float] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def roi(self) -> float:
        return (self.net_pnl / self.total_wagered * 100) if self.total_wagered > 0 else 0.0


class TarbTracker:
    """
    Manages positions, enforces risk limits, tracks daily P&L.
    """

    def __init__(self):
        self.open_positions: Dict[str, Position] = {}
        self.closed_positions: List[Position] = []
        self.daily_stats: Dict[str, DailyStats] = {}
        self._cooldowns: Dict[str, float] = {}  # market_slug → last trade time

    @property
    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @property
    def today_stats(self) -> DailyStats:
        if self._today not in self.daily_stats:
            self.daily_stats[self._today] = DailyStats(date=self._today)
        return self.daily_stats[self._today]

    # ─── Risk Checks ─────────────────────────────────────────────────────────────

    def can_trade(self) -> Tuple[bool, str]:
        """Check all risk limits. Returns (allowed, reason)."""
        stats = self.today_stats

        # Daily loss limit
        if stats.net_pnl < -STRATEGY.daily_loss_limit_usd:
            return False, f"Daily loss limit hit: ${stats.net_pnl:.2f}"

        # Daily trade limit
        if stats.trades >= STRATEGY.daily_trade_limit:
            return False, f"Daily trade limit hit: {stats.trades}"

        # Max concurrent positions
        if len(self.open_positions) >= STRATEGY.max_concurrent_positions:
            return False, f"Max concurrent positions: {len(self.open_positions)}"

        return True, "OK"

    def check_cooldown(self, market_slug: str) -> bool:
        """Check if market is in cooldown period."""
        last = self._cooldowns.get(market_slug, 0)
        return (time.time() - last) >= STRATEGY.cooldown_secs

    # ─── Position Management ─────────────────────────────────────────────────────

    def open_position(
        self,
        asset: str,
        timeframe: str,
        side: str,
        market_slug: str,
        interval_ts: int,
        entry_price: float,
        shares: float,
        cost_usd: float,
        fee_paid: float,
        order_id: str = "",
        rtds_price: float = 0.0,
        anchor_price: float = 0.0,
        move_bps: float = 0.0,
        conviction: float = 0.0,
        odds_lag: float = 0.0,
    ) -> Position:
        """Record a new open position."""
        pos_id = f"{market_slug}_{side}_{int(time.time()*1000)}"

        pos = Position(
            id=pos_id,
            asset=asset,
            timeframe=timeframe,
            side=side,
            market_slug=market_slug,
            interval_ts=interval_ts,
            entry_price=entry_price,
            shares=shares,
            cost_usd=cost_usd,
            fee_paid=fee_paid,
            entry_time=time.time(),
            order_id=order_id,
            rtds_price=rtds_price,
            anchor_price=anchor_price,
            move_bps=move_bps,
            conviction=conviction,
            odds_lag=odds_lag,
        )

        self.open_positions[pos_id] = pos
        self._cooldowns[market_slug] = time.time()

        # Update daily stats
        stats = self.today_stats
        stats.trades += 1
        stats.total_wagered += cost_usd
        stats.total_fees += fee_paid
        stats.avg_entry_price = (
            (stats.avg_entry_price * (stats.trades - 1) + entry_price) / stats.trades
        )
        stats.avg_conviction = (
            (stats.avg_conviction * (stats.trades - 1) + conviction) / stats.trades
        )

        # Per-asset/timeframe tracking
        stats.trades_by_asset[asset] = stats.trades_by_asset.get(asset, 0) + 1

        logger.info(
            f"POSITION OPENED: {pos_id} | {side} {market_slug} | "
            f"price={entry_price:.2f} shares={shares:.1f} cost=${cost_usd:.2f} "
            f"fee=${fee_paid:.4f} conviction={conviction:.2f}"
        )

        return pos

    def resolve_position(self, pos_id: str, won: bool):
        """Resolve a position after market settlement."""
        pos = self.open_positions.pop(pos_id, None)
        if not pos:
            logger.warning(f"Position not found for resolution: {pos_id}")
            return

        pos.resolved = True
        pos.won = won
        pos.resolved_at = time.time()

        if won:
            # Won: receive $1 per share, subtract cost and fees
            pos.pnl = pos.shares * 1.0 - pos.cost_usd - pos.fee_paid
        else:
            # Lost: lose entire cost + fees
            pos.pnl = -(pos.cost_usd + pos.fee_paid)

        self.closed_positions.append(pos)

        # Update daily stats
        stats = self.today_stats
        stats.gross_pnl += pos.pnl + pos.fee_paid  # Gross before fees
        stats.net_pnl += pos.pnl

        if won:
            stats.wins += 1
        else:
            stats.losses += 1

        stats.best_trade = max(stats.best_trade, pos.pnl)
        stats.worst_trade = min(stats.worst_trade, pos.pnl)

        # Per-asset P&L
        stats.pnl_by_asset[pos.asset] = stats.pnl_by_asset.get(pos.asset, 0) + pos.pnl
        stats.pnl_by_timeframe[pos.timeframe] = (
            stats.pnl_by_timeframe.get(pos.timeframe, 0) + pos.pnl
        )

        result_tag = "[WIN]" if won else "[LOSS]"
        logger.info(
            f"{result_tag} RESOLVED: {pos_id} | "
            f"P&L=${pos.pnl:+.4f} | Daily=${stats.net_pnl:+.2f} "
            f"| W/L={stats.wins}/{stats.losses} ({stats.win_rate:.0%})"
        )

    def get_positions_for_market(self, market_slug: str) -> List[Position]:
        """Get all open positions for a specific market."""
        return [p for p in self.open_positions.values() if p.market_slug == market_slug]

    # ─── Auto-Resolution ─────────────────────────────────────────────────────────

    def auto_resolve_expired(self, get_price_fn, get_snapshot_fn=None):
        """
        Auto-resolve positions for expired markets.
        get_price_fn(asset) should return the current RTDS price (or None).
        get_snapshot_fn(asset, interval_ts) returns last known price for that interval.
        
        Resolution logic:
        - Try live price first, then snapshot from when RTDS was alive
        - Compare settlement price to anchor to determine UP/DOWN winner
        - If no data at all after 120s grace period, mark as loss
        """
        now = time.time()
        open_count = len(self.open_positions)
        
        if open_count > 0:
            logger.debug(f"Resolution check: {open_count} open positions")
        
        to_resolve = []

        for pos_id, pos in list(self.open_positions.items()):
            interval_secs = 300 if pos.timeframe == "5m" else 900
            interval_end = pos.interval_ts + interval_secs
            secs_past_expiry = now - interval_end
            
            if now < interval_end + 5:  # 5s buffer for settlement
                # Not expired yet — only log occasionally to avoid spam
                remaining = int(interval_end + 5 - now)
                if remaining % 30 == 0 or remaining <= 10:
                    logger.debug(
                        f"  {pos_id}: {remaining}s until resolution eligible "
                        f"(interval_end={interval_end}, now={now:.0f})"
                    )
                continue
            
            # Position is eligible for resolution
            logger.debug(
                f"  {pos_id}: {secs_past_expiry:.0f}s past expiry, attempting resolution"
            )
            
            # Try live price first
            settle_price = None
            source = "none"
            try:
                settle_price = get_price_fn(pos.asset)
                if settle_price:
                    source = "live"
                    logger.debug(f"    Live price for {pos.asset}: ${settle_price:.2f}")
                else:
                    logger.debug(f"    No live price for {pos.asset}")
            except Exception as e:
                logger.error(f"    Error getting live price for {pos.asset}: {e}")
            
            # Try snapshot if live failed
            if not settle_price and get_snapshot_fn:
                try:
                    settle_price = get_snapshot_fn(pos.asset, pos.interval_ts)
                    if settle_price:
                        source = "snapshot"
                        logger.debug(f"    Snapshot price for {pos.asset}: ${settle_price:.2f}")
                    else:
                        logger.debug(f"    No snapshot price for {pos.asset} interval {pos.interval_ts}")
                except Exception as e:
                    logger.error(f"    Error getting snapshot price: {e}")
            
            if settle_price and pos.anchor_price > 0:
                # We have price data — determine settlement
                if pos.side == "UP":
                    won = settle_price > pos.anchor_price
                else:
                    won = settle_price < pos.anchor_price
                    
                settlement_bps = ((settle_price - pos.anchor_price) / pos.anchor_price) * 10000
                logger.info(
                    f"RESOLVING {pos_id}: price=${settle_price:.2f} ({source}) "
                    f"anchor=${pos.anchor_price:.2f} move={settlement_bps:+.1f}bps "
                    f"side={pos.side} -> {'WIN' if won else 'LOSS'}"
                )
                to_resolve.append((pos_id, won))
                
            elif now >= interval_end + 120:
                # 120s past expiry with no data — give up, mark as loss
                logger.warning(
                    f"RESOLVING {pos_id}: no price data {secs_past_expiry:.0f}s past expiry, "
                    f"marking LOSS (settle={settle_price}, anchor={pos.anchor_price})"
                )
                to_resolve.append((pos_id, False))
            else:
                # Still within grace period — wait for price data
                logger.debug(
                    f"    Waiting: {secs_past_expiry:.0f}s past expiry, no price data yet "
                    f"(timeout at 120s)"
                )

        if to_resolve:
            logger.info(f"Resolving {len(to_resolve)} positions this cycle")
        
        for pos_id, won in to_resolve:
            try:
                self.resolve_position(pos_id, won)
            except Exception as e:
                logger.error(f"Error resolving {pos_id}: {e}", exc_info=True)

    # ─── Reporting ───────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Generate a summary of current state."""
        stats = self.today_stats
        return {
            "open_positions": len(self.open_positions),
            "today": {
                "trades": stats.trades,
                "wins": stats.wins,
                "losses": stats.losses,
                "win_rate": f"{stats.win_rate:.0%}",
                "net_pnl": f"${stats.net_pnl:+.2f}",
                "total_wagered": f"${stats.total_wagered:.2f}",
                "total_fees": f"${stats.total_fees:.4f}",
                "roi": f"{stats.roi:+.1f}%",
                "best": f"${stats.best_trade:+.4f}",
                "worst": f"${stats.worst_trade:+.4f}",
                "pnl_by_asset": {k: f"${v:+.2f}" for k, v in stats.pnl_by_asset.items()},
                "pnl_by_timeframe": {k: f"${v:+.2f}" for k, v in stats.pnl_by_timeframe.items()},
            },
            "positions": [
                {
                    "id": p.id,
                    "side": p.side,
                    "market": p.market_slug,
                    "entry": p.entry_price,
                    "cost": f"${p.cost_usd:.2f}",
                    "age": f"{p.age_secs:.0f}s",
                    "conviction": f"{p.conviction:.2f}",
                }
                for p in self.open_positions.values()
            ],
        }

    def print_dashboard(self):
        """Print a compact terminal dashboard."""
        s = self.summary()
        stats = s["today"]
        print("\n" + "=" * 70)
        print(f"  TARB BOT — {self._today}")
        print("=" * 70)
        print(
            f"  Trades: {stats['trades']}  |  "
            f"W/L: {stats['wins']}/{stats['losses']} ({stats['win_rate']})  |  "
            f"Net P&L: {stats['net_pnl']}  |  ROI: {stats['roi']}"
        )
        print(
            f"  Wagered: {stats['total_wagered']}  |  "
            f"Fees: {stats['total_fees']}  |  "
            f"Best: {stats['best']}  Worst: {stats['worst']}"
        )
        if stats['pnl_by_asset']:
            print(f"  By Asset: {stats['pnl_by_asset']}")
        if stats['pnl_by_timeframe']:
            print(f"  By TF: {stats['pnl_by_timeframe']}")
        if s["positions"]:
            print(f"  Open: {len(s['positions'])} positions")
            for p in s["positions"]:
                print(f"    → {p['side']} {p['market']} @ {p['entry']} ({p['cost']}) [{p['age']}]")
        print("=" * 70 + "\n")
