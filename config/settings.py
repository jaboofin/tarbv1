"""
TARB Bot Configuration
======================
Temporal Arbitrage Bot for Polymarket 5m/15m Crypto Markets
All tunable parameters, fee calculations, and endpoint configuration.
"""

import os
from dataclasses import dataclass, field
from typing import List

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional — env vars can be set directly

# ─── Endpoints ──────────────────────────────────────────────────────────────────

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RTDS_WS = "wss://ws-live-data.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAIN_ID = 137  # Polygon

# ─── Credentials (from env) ─────────────────────────────────────────────────────

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLY_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))  # 1=email/magic, 2=browser wallet, 0=EOA

# ─── Asset Configuration ────────────────────────────────────────────────────────

@dataclass
class AssetConfig:
    name: str               # Display name
    slug: str               # URL slug prefix (e.g., "btc")
    rtds_binance: str       # Binance symbol (e.g., "btcusdt")
    rtds_chainlink: str     # Chainlink symbol (e.g., "btc/usd")
    enabled: bool = True

ASSETS = {
    "btc": AssetConfig("Bitcoin", "btc", "btcusdt", "btc/usd"),
    "eth": AssetConfig("Ethereum", "eth", "ethusdt", "eth/usd"),
    "sol": AssetConfig("Solana", "sol", "solusdt", "sol/usd"),
}

TIMEFRAMES = ["5m", "15m"]  # Supported market durations
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900}

# ─── Polymarket Fee Formula ─────────────────────────────────────────────────────

FEE_RATE = 0.0625  # Polymarket taker fee rate constant

def taker_fee(price: float) -> float:
    """
    Exact Polymarket taker fee per $1 of shares.
    Fee = p * (1 - p) * FEE_RATE
    Peaks at 1.5625% when p=0.50, drops toward 0 at extremes.
    """
    return price * (1.0 - price) * FEE_RATE

def taker_fee_pct(price: float) -> float:
    """Fee as percentage of share cost."""
    if price <= 0 or price >= 1:
        return 0.0
    return taker_fee(price) / price * 100

def net_payout(price: float, size_usd: float) -> float:
    """
    Net profit if the position wins, after fees.
    Buy C shares at price p: cost = C * p, payout = C * 1.0
    Fee = C * p * (1 - p) * FEE_RATE
    Net = C * (1 - p) - fee
    """
    if price <= 0 or price >= 1:
        return 0.0
    shares = size_usd / price
    gross_profit = shares * (1.0 - price)
    fee = shares * taker_fee(price)
    return gross_profit - fee

def breakeven_edge(price: float) -> float:
    """
    Minimum required edge (true prob - market prob) to break even after fees.
    """
    if price <= 0 or price >= 1:
        return 0.0
    fee = taker_fee(price)
    return fee / (1.0 - price)

def implied_fair_price(move_bps: float) -> float:
    """
    Implied fair probability from RTDS price move (basis points).
    
    Calibrated sigmoid model fit to actual Polymarket 5m/15m market pricing:
      fair = min(0.96, 1 / (1 + exp(-|move| / 18)))
    
    Empirical calibration (Feb 2026 data):
      Move     Market   Model   Error
      7.5bps   0.633    0.603   -0.030
      15bps    0.704    0.697   -0.007
      25bps    0.791    0.800   +0.009
      40bps    0.880    0.902   +0.022
      75bps    0.938    0.960   +0.022
    
    RMSE=0.019 vs old linear model RMSE=0.123 (6x better fit).
    Slightly conservative at small moves (avoids false signals),
    slightly aggressive at medium moves (detects real lag).
    """
    import math
    magnitude = abs(move_bps)
    return min(0.96, 1.0 / (1.0 + math.exp(-magnitude / 18.0)))

# ─── Strategy Parameters ────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    # ── Entry Thresholds ──
    min_price_move_bps: float = 15.0        # Min RTDS price move from anchor (basis points)
    min_odds_lag_cents: float = 0.04         # Min mispricing: RTDS says UP but odds still < 0.54
    min_net_edge: float = 0.02              # Min net edge after fees (2%)
    max_entry_price: float = 0.70           # Don't buy shares priced above this
    min_entry_price: float = 0.30           # Don't buy shares priced below this (too cheap = uncertain)

    # ── Position Sizing ──
    base_bet_usd: float = 5.0              # Default position size in USDC
    max_bet_usd: float = 25.0             # Maximum position size
    kelly_fraction: float = 0.25           # Quarter-Kelly for safety
    max_concurrent_positions: int = 3       # Max open positions at once

    # ── Timing ──
    min_remaining_secs: int = 60           # Don't enter with < 60s remaining
    max_entry_window_pct: float = 0.70     # Don't enter after 70% of market duration elapsed
    cooldown_secs: int = 10                # Cooldown between trades on same market

    # ── Risk Controls ──
    daily_loss_limit_usd: float = 50.0     # Stop trading if daily losses exceed this
    daily_trade_limit: int = 100           # Max trades per day
    max_slippage_cents: float = 0.03       # Max acceptable slippage from expected fill

    # ── Order Execution ──
    order_type: str = "FOK"                # FOK first, fallback to GTC
    gtc_fallback: bool = True              # If FOK fails, try GTC with tight price
    gtc_timeout_secs: int = 5              # Cancel GTC if not filled in 5s
    fill_verify_delay_secs: float = 1.0    # Wait before verifying fill

    # ── Conviction Scoring ──
    conviction_threshold: float = 0.60     # Min conviction score to enter (0-1)

    # ── Price Source ──
    use_chainlink: bool = True             # True = Chainlink RTDS, False = Binance RTDS
    price_staleness_ms: int = 30000        # Ignore price data older than 30s (chainlink can be slow)

    # ── Dashboard ──
    dashboard_port: int = 8080

STRATEGY = StrategyConfig()

# ─── Logging ─────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("TARB_LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("TARB_LOG_FILE", "tarb_bot.log")

# ─── Dry Run ─────────────────────────────────────────────────────────────────────

DRY_RUN = os.getenv("TARB_DRY_RUN", "true").lower() == "true"
