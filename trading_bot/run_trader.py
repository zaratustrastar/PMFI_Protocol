#!/usr/bin/env python3
"""
Launcher script for Polymarket Trading Bot

Usage:
    python run_trader.py <market-slug>
    
Example:
    python run_trader.py bitcoin-above-100k-on-december-31
"""

import sys
from polymarket_trader import PolymarketTrader
from database import init_database


def main():
    if len(sys.argv) < 2:
        print("❌ Error: Market slug required")
        print("\nUsage: python run_trader.py <market-slug>")
        print("\nExample: python run_trader.py bitcoin-above-100k-on-december-31")
        print("\nTo find market slugs:")
        print("  1. Go to polymarket.com")
        print("  2. Find a market")
        print("  3. The slug is in the URL: polymarket.com/event/SLUG")
        sys.exit(1)
    
    market_slug = sys.argv[1]
    
    print("🚀 Polymarket Automated Trading Bot")
    print("=" * 60)
    
    # Initialize database
    print("\n📊 Initializing database...")
    init_database()
    
    # Start trading
    trader = PolymarketTrader()
    trader.run_strategy(market_slug)


if __name__ == "__main__":
    main()
