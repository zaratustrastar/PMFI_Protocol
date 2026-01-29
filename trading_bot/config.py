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
SELL_RESERVE_RATIO = 0.10  # Keep 10% of position untouched until resolution

# Buy ladder: 1¢ to 3¢ (0.01 to 0.03) - 10 levels
# These prices respect Polymarket's minimum price requirements
BUY_LADDER_PRICES = [0.01, 0.012, 0.014, 0.016, 0.018, 0.020, 0.022, 0.025, 0.028, 0.03]

# Sell ladder: Start at 200% profit (3x), increase by 100% for each tier
# After reserving 10% for resolution, remaining 90% is split across tiers:
#   Tier 1: 30% of total @ 3x (200% profit)
#   Tier 2: 30% of total @ 4x (300% profit)
#   Tier 3: 30% of total @ 5x (400% profit)
# Remaining 10% held to resolution
SELL_LADDER_CONFIG = [
    {"ratio": 0.30, "profit_multiple": 3},  # 200% profit
    {"ratio": 0.30, "profit_multiple": 4},  # 300% profit  
    {"ratio": 0.30, "profit_multiple": 5},  # 400% profit
]

# Legacy single sell target (kept for compatibility with existing code paths)
SELL_PROFIT_MULTIPLE = 3
SELL_SHARE_RATIO = 0.5  # Deprecated - use SELL_LADDER_CONFIG instead

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
