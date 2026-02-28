"""
Temporal Arb CLOB Client
========================
Handles Polymarket market discovery (deterministic URL pattern),
order execution (FOK→GTC fallback), and fill verification.

Market URL pattern: {asset}-updown-{5m|15m}-{unix_timestamp}
Gamma API: https://gamma-api.polymarket.com
CLOB API: https://clob.polymarket.com
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import aiohttp

from config.settings import (
    CLOB_HOST, GAMMA_API, CHAIN_ID, PRIVATE_KEY, FUNDER_ADDRESS,
    SIGNATURE_TYPE, ASSETS, TIMEFRAMES, TIMEFRAME_SECONDS,
    STRATEGY, DRY_RUN, taker_fee, net_payout, breakeven_edge,
)

logger = logging.getLogger("tarb.clob_client")


@dataclass
class MarketInfo:
    """Represents an active 5m/15m crypto UP/DOWN market."""
    asset: str                 # btc, eth, sol
    timeframe: str             # 5m, 15m
    interval_ts: int           # Unix timestamp of interval start
    market_id: str             # Polymarket market ID
    condition_id: str          # Condition ID
    slug: str                  # Market slug
    up_token_id: str           # Token ID for UP/YES outcome
    down_token_id: str         # Token ID for DOWN/NO outcome
    up_price: float = 0.0     # Current best ask for UP
    down_price: float = 0.0   # Current best ask for DOWN
    expires_at: int = 0       # When this market expires (unix ts)
    neg_risk: bool = False     # negRisk flag for order signing
    tick_size: str = "0.01"    # Tick size for this market
    
    # Price change tracking for temporal lag detection
    prev_up_price: float = 0.0    # Previous UP price (before last refresh)
    prev_down_price: float = 0.0  # Previous DOWN price (before last refresh)
    last_price_change_at: float = 0.0  # time.time() when odds last changed
    last_refresh_at: float = 0.0       # time.time() of last CLOB fetch

    @property
    def time_remaining(self) -> int:
        """Seconds until market expires."""
        return max(0, self.expires_at - int(time.time()))

    @property
    def elapsed_pct(self) -> float:
        """Percentage of market duration elapsed."""
        duration = TIMEFRAME_SECONDS.get(self.timeframe, 300)
        elapsed = duration - self.time_remaining
        return min(1.0, max(0.0, elapsed / duration))

    @property
    def is_tradeable(self) -> bool:
        """Can we still enter this market?"""
        return (
            self.time_remaining > STRATEGY.min_remaining_secs
            and self.elapsed_pct < STRATEGY.max_entry_window_pct
        )


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    fill_price: float = 0.0
    fill_size: float = 0.0
    fee_paid: float = 0.0
    error: str = ""
    is_phantom: bool = False   # True if fill couldn't be verified


class TarbClient:
    """
    Polymarket CLOB client for the temporal arb bot.
    Handles market discovery via deterministic URL pattern and order execution.
    """

    def __init__(self):
        self._clob_client = None  # py-clob-client instance (initialized on connect)
        self._session: Optional[aiohttp.ClientSession] = None
        self._active_markets: Dict[str, MarketInfo] = {}  # key: "{asset}_{timeframe}_{interval_ts}"

    async def connect(self):
        """Initialize CLOB client and HTTP session."""
        self._session = aiohttp.ClientSession()

        if not DRY_RUN and PRIVATE_KEY:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
                from py_clob_client.order_builder.constants import BUY

                if FUNDER_ADDRESS:
                    self._clob_client = ClobClient(
                        CLOB_HOST,
                        key=PRIVATE_KEY,
                        chain_id=CHAIN_ID,
                        signature_type=SIGNATURE_TYPE,
                        funder=FUNDER_ADDRESS,
                    )
                else:
                    self._clob_client = ClobClient(
                        CLOB_HOST,
                        key=PRIVATE_KEY,
                        chain_id=CHAIN_ID,
                    )
                self._clob_client.set_api_creds(
                    self._clob_client.create_or_derive_api_creds()
                )
                logger.info("CLOB client initialized (LIVE mode)")
            except ImportError:
                logger.warning("py-clob-client not installed — falling back to dry run")
                self._clob_client = None
        else:
            logger.info("CLOB client in DRY RUN mode")

    async def disconnect(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── Market Discovery ────────────────────────────────────────────────────────

    def _compute_interval_ts(self, timeframe: str, now: Optional[float] = None) -> int:
        """
        Compute the current interval start timestamp.
        5m intervals align to :00, :05, :10, ... :55
        15m intervals align to :00, :15, :30, :45
        """
        now = now or time.time()
        interval_secs = TIMEFRAME_SECONDS[timeframe]
        return int(now // interval_secs) * interval_secs

    def _build_market_slug(self, asset: str, timeframe: str, interval_ts: int) -> str:
        """Build the deterministic market slug."""
        return f"{asset}-updown-{timeframe}-{interval_ts}"

    async def discover_market(self, asset: str, timeframe: str) -> Optional[MarketInfo]:
        """
        Discover the current active market for an asset/timeframe pair
        using the deterministic URL pattern + Gamma API lookup.
        """
        interval_ts = self._compute_interval_ts(timeframe)
        slug = self._build_market_slug(asset, timeframe, interval_ts)
        cache_key = f"{asset}_{timeframe}_{interval_ts}"

        # Check cache
        if cache_key in self._active_markets:
            cached = self._active_markets[cache_key]
            if cached.time_remaining > 0:
                return cached

        # Query Gamma API
        try:
            url = f"{GAMMA_API}/events?slug={slug}&closed=false"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    logger.debug(f"Gamma API {resp.status} for {slug}")
                    return None
                events = await resp.json()

            if not events or len(events) == 0:
                logger.debug(f"No event found for {slug}")
                return None

            event = events[0]
            markets = event.get("markets", [])
            if not markets:
                return None

            market = markets[0]
            token_ids = market.get("clobTokenIds", [])
            # Gamma API sometimes returns clobTokenIds as a JSON string
            if isinstance(token_ids, str):
                try:
                    import json
                    token_ids = json.loads(token_ids)
                except (json.JSONDecodeError, TypeError):
                    token_ids = []
            if len(token_ids) < 2:
                logger.warning(f"Insufficient token IDs for {slug}")
                return None

            # Determine expiry
            interval_secs = TIMEFRAME_SECONDS[timeframe]
            expires_at = interval_ts + interval_secs

            info = MarketInfo(
                asset=asset,
                timeframe=timeframe,
                interval_ts=interval_ts,
                market_id=str(market.get("id", "")),
                condition_id=market.get("conditionId", ""),
                slug=slug,
                up_token_id=token_ids[0],    # First token = UP/YES
                down_token_id=token_ids[1],  # Second token = DOWN/NO
                expires_at=expires_at,
                neg_risk=market.get("negRisk", False),
                tick_size=market.get("minimumTickSize", "0.01"),
            )

            # Seed prices from Gamma outcomePrices if available
            outcome_prices = market.get("outcomePrices", "")
            if isinstance(outcome_prices, str) and outcome_prices:
                try:
                    import json as _json
                    prices = _json.loads(outcome_prices)
                    if len(prices) >= 2:
                        info.up_price = float(prices[0])
                        info.down_price = float(prices[1])
                except Exception:
                    pass

            # Fetch current CLOB prices (overwrites Gamma seed if available)
            await self._update_prices(info)

            self._active_markets[cache_key] = info
            logger.info(
                f"Discovered: {slug} | UP={info.up_price:.2f} DOWN={info.down_price:.2f} "
                f"| {info.time_remaining}s remaining"
            )
            return info

        except Exception as e:
            logger.error(f"Market discovery error for {slug}: {e}")
            return None

    async def _update_prices(self, market: MarketInfo):
        """Fetch current best prices from CLOB for a market's tokens."""
        try:
            old_up = market.up_price
            old_down = market.down_price
            
            # UP token price
            url = f"{CLOB_HOST}/price?token_id={market.up_token_id}&side=buy"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    market.up_price = float(data.get("price", 0))

            # DOWN token price
            url = f"{CLOB_HOST}/price?token_id={market.down_token_id}&side=buy"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    market.down_price = float(data.get("price", 0))

            # Track price changes for temporal lag detection
            now = time.time()
            market.last_refresh_at = now
            market.prev_up_price = old_up
            market.prev_down_price = old_down
            
            # Detect if odds actually changed since last refresh
            price_changed = (
                abs(market.up_price - old_up) >= 0.005 or
                abs(market.down_price - old_down) >= 0.005
            )
            if price_changed or market.last_price_change_at == 0:
                market.last_price_change_at = now

        except Exception as e:
            logger.error(f"Price fetch error for {market.slug}: {e}")

    async def refresh_prices(self, market: MarketInfo):
        """Public method to refresh market prices."""
        await self._update_prices(market)

    async def discover_all_markets(self) -> List[MarketInfo]:
        """Discover all active markets for enabled assets and timeframes."""
        tasks = []
        for asset_key, cfg in ASSETS.items():
            if not cfg.enabled:
                continue
            for tf in TIMEFRAMES:
                tasks.append(self.discover_market(asset_key, tf))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        markets = []
        for r in results:
            if isinstance(r, MarketInfo):
                markets.append(r)
            elif isinstance(r, Exception):
                logger.error(f"Discovery error: {r}")
        return markets

    # ─── Order Execution ─────────────────────────────────────────────────────────

    async def place_order(
        self,
        market: MarketInfo,
        side: str,           # "UP" or "DOWN"
        size_usd: float,
    ) -> OrderResult:
        """
        Place a directional bet on UP or DOWN.
        Uses FOK (Fill-or-Kill) first, falls back to GTC if configured.
        """
        token_id = market.up_token_id if side == "UP" else market.down_token_id
        current_price = market.up_price if side == "UP" else market.down_price

        if current_price <= 0 or current_price >= 1:
            return OrderResult(success=False, error=f"Invalid price: {current_price}")

        # Calculate shares and expected fee
        shares = size_usd / current_price
        fee = shares * taker_fee(current_price)
        expected_profit = net_payout(current_price, size_usd)
        edge = breakeven_edge(current_price)

        logger.info(
            f"{'[DRY] ' if DRY_RUN else ''}ORDER: {side} {market.slug} | "
            f"price={current_price:.2f} size=${size_usd:.2f} shares={shares:.1f} "
            f"fee=${fee:.4f} expected_profit=${expected_profit:.2f} min_edge={edge:.4f}"
        )

        if DRY_RUN or not self._clob_client:
            return OrderResult(
                success=True,
                order_id=f"DRY_{int(time.time())}",
                fill_price=current_price,
                fill_size=shares,
                fee_paid=fee,
            )

        # Live execution
        try:
            return await self._execute_fok(token_id, current_price, size_usd, market)
        except Exception as e:
            logger.error(f"Order execution error: {e}")
            return OrderResult(success=False, error=str(e))

    async def _execute_fok(
        self, token_id: str, price: float, amount_usd: float, market: MarketInfo
    ) -> OrderResult:
        """Execute a Fill-or-Kill market order."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=round(amount_usd, 2),
                side=BUY,
            )
            signed = self._clob_client.create_market_order(mo)
            resp = self._clob_client.post_order(signed, OrderType.FOK)

            if resp and resp.get("success"):
                order_id = resp.get("orderID", "")
                logger.info(f"FOK order placed: {order_id}")

                # Verify fill
                await asyncio.sleep(STRATEGY.fill_verify_delay_secs)
                verified = await self._verify_fill(order_id)
                if not verified:
                    logger.warning(f"PHANTOM FILL detected: {order_id}")
                    return OrderResult(
                        success=False,
                        order_id=order_id,
                        error="Phantom fill — order not verified",
                        is_phantom=True,
                    )

                return OrderResult(
                    success=True,
                    order_id=order_id,
                    fill_price=price,
                    fill_size=amount_usd / price,
                    fee_paid=taker_fee(price) * (amount_usd / price),
                )
            else:
                error_msg = resp.get("errorMsg", "Unknown error") if resp else "No response"
                logger.warning(f"FOK rejected: {error_msg}")

                # GTC fallback
                if STRATEGY.gct_fallback:
                    return await self._execute_gtc_fallback(
                        token_id, price, amount_usd, market
                    )
                return OrderResult(success=False, error=f"FOK rejected: {error_msg}")

        except Exception as e:
            logger.error(f"FOK execution error: {e}")
            return OrderResult(success=False, error=str(e))

    async def _execute_gtc_fallback(
        self, token_id: str, price: float, amount_usd: float, market: MarketInfo
    ) -> OrderResult:
        """Place a tight GTC limit order as fallback, cancel if not filled quickly."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY

            # Place at current price (aggressive limit)
            tick = float(market.tick_size)
            limit_price = round(min(price + tick, 0.99), 2)
            shares = amount_usd / limit_price

            order_args = OrderArgs(
                price=limit_price,
                size=round(shares, 2),
                side=BUY,
                token_id=token_id,
            )
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed, OrderType.GTC)

            if not resp or not resp.get("success"):
                return OrderResult(success=False, error="GTC fallback rejected")

            order_id = resp.get("orderID", "")
            logger.info(f"GTC fallback placed: {order_id} @ {limit_price}")

            # Wait for fill or timeout
            await asyncio.sleep(STRATEGY.gct_timeout_secs)
            filled = await self._verify_fill(order_id)

            if filled:
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    fill_price=limit_price,
                    fill_size=shares,
                    fee_paid=taker_fee(limit_price) * shares,
                )
            else:
                # Cancel unfilled GTC
                try:
                    self._clob_client.cancel(order_id)
                    logger.info(f"GTC cancelled (unfilled): {order_id}")
                except Exception:
                    pass
                return OrderResult(success=False, error="GTC timeout — cancelled")

        except Exception as e:
            logger.error(f"GTC fallback error: {e}")
            return OrderResult(success=False, error=str(e))

    async def _verify_fill(self, order_id: str) -> bool:
        """Check if an order was actually filled (phantom fill detection)."""
        try:
            if not self._clob_client:
                return True  # Can't verify in dry run
            order = self._clob_client.get_order(order_id)
            if order:
                status = order.get("status", "")
                return status in ("MATCHED", "FILLED")
            return False
        except Exception as e:
            logger.warning(f"Fill verification error: {e}")
            return False

    # ─── Cleanup ─────────────────────────────────────────────────────────────────

    def cleanup_expired(self):
        """Remove expired markets from cache."""
        now = int(time.time())
        expired = [k for k, v in self._active_markets.items() if v.expires_at < now]
        for k in expired:
            del self._active_markets[k]
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired markets")
