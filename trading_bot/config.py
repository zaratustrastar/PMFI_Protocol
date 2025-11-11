"""
Trading Bot Configuration
"""

import os

# Polymarket CLOB settings
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# Credentials from environment
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")

# Trading strategy parameters
ORDER_SIZE_USD = 0.2  # $0.20 per order = $2 total per market (10 orders × 2 sides)

# Buy ladder: 0.1¢ to 1¢ (0.001 to 0.01)
BUY_LADDER_PRICES = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.01]

# Sell ladder: 200% to 1000% profit
# For a buy at price P, sell at: 3P, 4P, 5P, 6P, 7P, 8P, 9P, 10P
SELL_PROFIT_MULTIPLES = [3, 4, 5, 6, 7, 8, 9, 10]

# Monitoring settings
POLL_INTERVAL_SECONDS = 10  # Check for filled orders every 10 seconds

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "")
