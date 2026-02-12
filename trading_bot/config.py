"""
Trading Bot Configuration
"""

import os

# Polymarket CLOB settings
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# Credentials from environment
# NOTE: Trading API credentials are automatically derived from your private key
# Builder API credentials are NOT used for trading - they're only for fee rebates/attribution
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")

# Trading strategy parameters
ORDER_SIZE_SHARES = 10  # Buy 10 shares per order
SELL_RESERVE_RATIO = 0.10  # Keep 10% of position untouched until resolution (no orders placed)
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum: 5 shares per limit order
MIN_SHARES_FOR_SELL_LADDER = 25  # Wait until position accumulates this many shares before placing sells

# Buy ladder: 1¢ to 3¢ (0.01 to 0.03) - 10 levels
BUY_LADDER_PRICES = [0.01, 0.012, 0.014, 0.016, 0.018, 0.020, 0.022, 0.025, 0.028, 0.03]

# Sell ladder tiers (applied to 90% of position after reserving 10%):
#   Tier 1: 33% of total @ 3x (200% profit)
#   Tier 2: 27% of total @ 4x (300% profit)
#   Tier 3: 30% of total @ 8x (700% profit)
#   Remaining 10% reserved for resolution (no orders)
SELL_LADDER_CONFIG = [
    {"ratio": 0.33, "profit_multiple": 3},  # 200% profit
    {"ratio": 0.27, "profit_multiple": 4},  # 300% profit
    {"ratio": 0.30, "profit_multiple": 8},  # 700% profit
]

# Monitoring settings
POLL_INTERVAL_SECONDS = 10  # Check for filled orders every 10 seconds

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Proxy settings (optional - for residential IP bypass)
# Format: Use socks5:// for SOCKS proxies, http:// for HTTP proxies
# Example: "socks5://username:password@proxy.example.com:port"
# Leave empty to use direct connection
PROXY_URL = os.getenv("PROXY_URL", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
