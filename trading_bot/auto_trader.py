"""
Automatic Trading Coordinator
Monitors for new markets and automatically trades them
"""

import os
import sys
import time
import psycopg2
from datetime import datetime, timedelta
from polymarket_trader import PolymarketTrader

DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_new_markets(hours_back=1):
    """
    Get new markets from the last N hours that haven't been traded yet
    
    Args:
        hours_back: How many hours back to look for new markets
        
    Returns:
        List of market slugs
    """
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # Get markets seen in last N hours that we haven't traded
    cutoff_time = datetime.now() - timedelta(hours=hours_back)
    
    cur.execute("""
        SELECT spm.market_id, MAX(spm.seen_at) as last_seen
        FROM seen_polymarket_markets spm
        LEFT JOIN trading_positions tp ON spm.market_id = tp.market_slug
        WHERE spm.seen_at >= %s 
        AND tp.market_slug IS NULL
        GROUP BY spm.market_id
        ORDER BY last_seen DESC
        LIMIT 10
    """, (cutoff_time,))
    
    markets = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return markets


def trade_market(market_slug: str, trader: PolymarketTrader, max_runtime_minutes=60):
    """
    Trade a single market with timeout
    
    Args:
        market_slug: Market to trade
        trader: PolymarketTrader instance
        max_runtime_minutes: Maximum time to monitor this market
        
    Returns:
        True if successfully traded
    """
    print(f"\n{'='*60}")
    print(f"⚡ Auto-trading: {market_slug}")
    print(f"{'='*60}")
    
    try:
        # Start the trading strategy (will run indefinitely)
        # For auto-trader, we'll modify it to run for a limited time
        trader.run_strategy_limited(market_slug, max_runtime_minutes)
        return True
    except Exception as e:
        print(f"❌ Error trading {market_slug}: {e}")
        return False


def main():
    """Main coordinator loop"""
    print("\n🤖 Polymarket Auto-Trader Starting...")
    print(f"   Budget: $2 per market")
    print(f"   Notifications: Telegram @ponnymarket (sells only)")
    print(f"   Mode: Auto-detect new markets\n")
    
    # Initialize trader once
    trader = PolymarketTrader()
    
    # For scheduled deployment: run once and exit
    # For continuous: run in a loop
    mode = os.getenv("AUTO_TRADER_MODE", "once")  # "once" or "continuous"
    
    if mode == "continuous":
        print("📊 Running in CONTINUOUS mode (press Ctrl+C to stop)\n")
        
        try:
            while True:
                # Get new markets
                new_markets = get_new_markets(hours_back=1)
                
                if new_markets:
                    print(f"\n✅ Found {len(new_markets)} new markets to trade")
                    
                    for market_slug in new_markets:
                        trade_market(market_slug, trader, max_runtime_minutes=30)
                else:
                    print("⏸️  No new markets found")
                
                # Wait before checking again
                print(f"\n💤 Waiting 60 seconds before next check...")
                time.sleep(60)
                
        except KeyboardInterrupt:
            print("\n\n⏸️  Auto-trader stopped by user")
    
    else:
        print("📊 Running in ONCE mode (one-time check)\n")
        
        # Get new markets
        new_markets = get_new_markets(hours_back=1)
        
        if new_markets:
            print(f"\n✅ Found {len(new_markets)} new markets to trade\n")
            
            for market_slug in new_markets:
                trade_market(market_slug, trader, max_runtime_minutes=30)
            
            print("\n✅ Auto-trader completed")
        else:
            print("⏸️  No new markets found to trade")


if __name__ == "__main__":
    main()
