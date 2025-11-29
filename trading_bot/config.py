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
SELL_SHARE_RATIO = 0.5  # Sell half the shares (5), keep half to ride

# Buy ladder: 1¢ to 3¢ (0.01 to 0.03) - 10 levels
# These prices respect Polymarket's minimum price requirements
BUY_LADDER_PRICES = [0.01, 0.012, 0.014, 0.016, 0.018, 0.020, 0.022, 0.025, 0.028, 0.03]

# Sell target: 3x buy price (200% profit)
SELL_PROFIT_MULTIPLE = 3

# Monitoring settings
POLL_INTERVAL_SECONDS = 10  # Check for filled orders every 10 seconds

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Proxy settings (optional - for residential IP bypass)
# Format: "http://username:password@proxy.example.com:port"
# Leave empty to use direct connection
PROXY_URL = os.getenv("PROXY_URL", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
