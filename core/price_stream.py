"""
RTDS Price Stream
=================
Connects to Polymarket's Real-Time Data Stream (RTDS) WebSocket
for live BTC, ETH, SOL prices from Chainlink oracles and Binance.

WebSocket: wss://ws-live-data.polymarket.com
Topics:
  - crypto_prices_chainlink  (btc/usd, eth/usd, sol/usd)
  - crypto_prices            (btcusdt, ethusdt, solusdt)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import aiohttp

from config.settings import RTDS_WS, ASSETS, STRATEGY

logger = logging.getLogger("tarb.price_stream")


@dataclass
class PriceUpdate:
    symbol: str          # Normalized asset key (btc, eth, sol)
    price: float         # Current price in USD
    timestamp_ms: int    # When the price was recorded
    received_ms: int     # When we received it
    source: str          # "chainlink" or "binance"

    @property
    def age_ms(self) -> int:
        return int(time.time() * 1000) - self.timestamp_ms

    @property
    def is_stale(self) -> bool:
        return self.age_ms > STRATEGY.price_staleness_ms


@dataclass
class AnchorPrice:
    """Price at the start of a market interval — used as reference for direction."""
    symbol: str
    price: float
    interval_ts: int     # Unix timestamp of the interval start
    captured_at_ms: int  # When we captured this anchor


class PriceStream:
    """
    Manages WebSocket connection to Polymarket RTDS and maintains
    latest prices + anchor prices for each asset/interval.
    """

    def __init__(self):
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._reconnect_delay = 1.0

        # Latest prices per asset
        self.latest: Dict[str, PriceUpdate] = {}
        
        # Backup of latest prices before reconnect (used if reconnect fails)
        self._latest_backup: Dict[str, PriceUpdate] = {}

        # Anchor prices per (asset, interval_ts)
        self.anchors: Dict[str, AnchorPrice] = {}
        
        # Last known price per (asset, interval_ts) — for resolution after RTDS drops
        self._interval_snapshots: Dict[str, float] = {}

        # Callbacks
        self._on_price: Optional[Callable] = None
        
        # RTDS health monitoring
        self._msg_count = 0
        self._last_msg_count = 0
        self._last_price_time: float = 0.0  # time.time() of last price update
        self._connect_time: float = 0.0     # when current connection was established

        # Symbol mapping: RTDS symbol → our asset key
        self._chainlink_map = {}
        self._binance_map = {}
        for key, cfg in ASSETS.items():
            if cfg.enabled:
                self._chainlink_map[cfg.rtds_chainlink] = key
                self._binance_map[cfg.rtds_binance] = key

    def on_price(self, callback: Callable):
        """Register callback for price updates: callback(PriceUpdate)"""
        self._on_price = callback

    async def start(self):
        """Start the RTDS WebSocket connection with auto-reconnect."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"RTDS connection error: {e}")
            if self._running:
                logger.info(f"Reconnecting RTDS in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def stop(self):
        """Gracefully stop the price stream."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _connect_and_listen(self):
        """Connect to RTDS WebSocket and process messages."""
        # Rebuild symbol maps in case assets were enabled after init
        self._chainlink_map = {}
        self._binance_map = {}
        for key, cfg in ASSETS.items():
            if cfg.enabled:
                self._chainlink_map[cfg.rtds_chainlink] = key
                self._binance_map[cfg.rtds_binance] = key
        
        # Close any existing session before creating a new one
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        
        self._session = aiohttp.ClientSession()
        try:
            logger.info(f"Connecting to RTDS: {RTDS_WS}")
            self._ws = await self._session.ws_connect(
                RTDS_WS,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=10)
            )
            logger.info("RTDS WebSocket connected")
            self._reconnect_delay = 1.0
            self._connect_time = time.time()

            # Backup current prices before clearing (used for resolution if reconnect fails)
            self._latest_backup = dict(self.latest)
            
            # Clear stale prices so we don't use old data before new updates arrive
            stale_keys = list(self.latest.keys())
            self.latest.clear()
            if stale_keys:
                logger.info(f"Cleared stale prices on reconnect: {stale_keys}")

            # Subscribe to price feeds
            await self._subscribe()

            # Start auxiliary tasks
            ping_task = asyncio.create_task(self._ping_loop())
            watchdog_task = asyncio.create_task(self._watchdog_loop())
            subscription_check_task = asyncio.create_task(self._subscription_check())

            try:
                msg_in_session = 0
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        msg_in_session += 1
                        await self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"RTDS WS error: {self._ws.exception()}")
                        break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        logger.warning("RTDS WebSocket closed by server")
                        break
                
                # Message loop exited — log why
                uptime = time.time() - self._connect_time
                logger.warning(
                    f"RTDS message loop exited after {uptime:.0f}s "
                    f"({msg_in_session} msgs this session, {self._msg_count} total). "
                    f"WS state: closed={self._ws.closed if self._ws else 'N/A'}"
                )
            finally:
                ping_task.cancel()
                watchdog_task.cancel()
                subscription_check_task.cancel()
                # Wait for cancellation to complete
                for task in [ping_task, watchdog_task, subscription_check_task]:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    async def _subscribe(self):
        """Subscribe to both Chainlink and Binance price feeds."""
        # Chainlink feed (used for settlement — this is the oracle truth)
        chainlink_sub = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": ""
            }]
        }
        await self._ws.send_json(chainlink_sub)
        logger.info("Subscribed to Chainlink RTDS (btc/usd, eth/usd, sol/usd)")

        # Binance feed (faster updates, useful for cross-reference)
        enabled_symbols = [cfg.rtds_binance for cfg in ASSETS.values() if cfg.enabled]
        binance_sub = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices",
                "type": "update",
                "filters": ",".join(enabled_symbols)
            }]
        }
        await self._ws.send_json(binance_sub)
        logger.info(f"Subscribed to Binance RTDS ({', '.join(enabled_symbols)})")

    async def _ping_loop(self):
        """Send periodic pings to keep connection alive."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(25)
                try:
                    await self._ws.ping()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _watchdog_loop(self):
        """Detect dead RTDS feed and force reconnect."""
        try:
            # Give initial subscription 15s to start producing data
            await asyncio.sleep(15)
            self._last_msg_count = self._msg_count
            
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(30)
                current = self._msg_count
                if current == self._last_msg_count:
                    # No new messages in 30s — feed is dead
                    logger.warning(
                        f"RTDS watchdog: no messages in 30s (count stuck at {current}). "
                        f"Forcing reconnect..."
                    )
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    break
                else:
                    logger.debug(f"RTDS watchdog: {current - self._last_msg_count} msgs in last 30s")
                self._last_msg_count = current
        except asyncio.CancelledError:
            pass

    async def _subscription_check(self):
        """Fast check that subscription actually produces data after connect.
        If no price updates within 10s of subscribe, force reconnect."""
        try:
            await asyncio.sleep(10)
            if not self.latest:
                # 10s after subscribe and still no prices — subscription failed silently
                logger.warning(
                    "RTDS subscription check: no prices received 10s after subscribe. "
                    "Restoring backup prices and forcing reconnect..."
                )
                # Restore backup so the bot isn't blind during reconnect
                if self._latest_backup:
                    self.latest = dict(self._latest_backup)
                    logger.info(f"Restored {len(self.latest)} backup prices")
                if self._ws and not self._ws.closed:
                    await self._ws.close()
            else:
                assets_received = list(self.latest.keys())
                logger.info(
                    f"RTDS subscription check: receiving prices for {assets_received} "
                    f"({self._msg_count} total msgs)"
                )
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw: str):
        """Parse RTDS message and update price state."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        topic = data.get("topic", "")
        payload = data.get("payload")
        if not payload:
            return

        symbol_raw = payload.get("symbol", "")
        value = payload.get("value")
        ts = payload.get("timestamp", 0)
        now_ms = int(time.time() * 1000)

        if value is None:
            return

        self._msg_count += 1

        # Map to our asset key
        asset_key = None
        source = None
        if topic == "crypto_prices_chainlink":
            asset_key = self._chainlink_map.get(symbol_raw)
            source = "chainlink"
        elif topic == "crypto_prices":
            asset_key = self._binance_map.get(symbol_raw)
            source = "binance"

        if not asset_key:
            return

        update = PriceUpdate(
            symbol=asset_key,
            price=float(value),
            timestamp_ms=int(ts),
            received_ms=now_ms,
            source=source,
        )

        # Only use the configured source for trading decisions
        if (STRATEGY.use_chainlink and source == "chainlink") or \
           (not STRATEGY.use_chainlink and source == "binance"):
            self.latest[asset_key] = update
            self._last_price_time = time.time()
            # Snapshot this price for all known intervals of this asset
            # (used for resolution if RTDS drops)
            for anchor_key, anchor in self.anchors.items():
                if anchor.symbol == asset_key:
                    self._interval_snapshots[anchor_key] = update.price

        # Fire callback
        if self._on_price:
            try:
                await self._on_price(update)
            except Exception as e:
                logger.error(f"Price callback error: {e}")

    def get_price(self, asset: str) -> Optional[PriceUpdate]:
        """Get latest non-stale price for an asset."""
        update = self.latest.get(asset)
        if update and not update.is_stale:
            return update
        return None

    def get_snapshot_price(self, asset: str, interval_ts: int) -> Optional[float]:
        """Get the last known price for an asset during a specific interval.
        Used for resolution when live RTDS feed is unavailable."""
        key = f"{asset}_{interval_ts}"
        return self._interval_snapshots.get(key)

    def set_anchor(self, asset: str, interval_ts: int, price: float):
        """Set anchor price for an asset at a specific interval start."""
        key = f"{asset}_{interval_ts}"
        self.anchors[key] = AnchorPrice(
            symbol=asset,
            price=price,
            interval_ts=interval_ts,
            captured_at_ms=int(time.time() * 1000),
        )
        # Also snapshot the initial price
        self._interval_snapshots[key] = price
        logger.debug(f"Anchor set: {asset} @ {price:.2f} for interval {interval_ts}")

    def get_anchor(self, asset: str, interval_ts: int) -> Optional[AnchorPrice]:
        """Get anchor price for an asset/interval."""
        return self.anchors.get(f"{asset}_{interval_ts}")

    def price_move_bps(self, asset: str, interval_ts: int) -> Optional[float]:
        """
        Calculate price movement from anchor in basis points.
        Positive = price moved UP, Negative = price moved DOWN.
        """
        current = self.get_price(asset)
        anchor = self.get_anchor(asset, interval_ts)
        if not current or not anchor or anchor.price == 0:
            return None
        return ((current.price - anchor.price) / anchor.price) * 10000

    def cleanup_old_anchors(self, max_age_secs: int = 1800, open_interval_keys: set = None):
        """Remove anchors older than max_age_secs (default 30 min).
        Preserves snapshots for intervals that still have open positions.
        
        Args:
            max_age_secs: Delete anchors older than this
            open_interval_keys: Set of "asset_interval_ts" keys to preserve
        """
        now_ms = int(time.time() * 1000)
        anchor_cutoff_ms = now_ms - (max_age_secs * 1000)
        
        # Preserve keys needed for open positions
        protected = open_interval_keys or set()
        
        expired_anchors = [
            k for k, v in self.anchors.items() 
            if v.captured_at_ms < anchor_cutoff_ms and k not in protected
        ]
        for k in expired_anchors:
            del self.anchors[k]
        
        # Clean snapshots — but NEVER delete ones needed for open positions
        expired_snapshots = [
            k for k in self._interval_snapshots 
            if k not in self.anchors and k not in protected
        ]
        for k in expired_snapshots:
            del self._interval_snapshots[k]
        
        if expired_anchors or expired_snapshots:
            logger.debug(
                f"Cleaned {len(expired_anchors)} anchors, {len(expired_snapshots)} snapshots "
                f"(protected: {len(protected)})"
            )
