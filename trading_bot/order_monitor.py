"""
Order Monitor - Continuous monitoring of all active orders
Runs independently to monitor buy/sell fills across all markets
"""

import os
import time

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ Loaded environment variables from .env file")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment variables")

from polymarket_trader import PolymarketTrader
from database import get_open_orders, update_order_status, update_market_summary
from telegram_notifier import notify_sell_executed


def monitor_all_orders(trader: PolymarketTrader):
    """
    Monitor all open orders for fills
    - Buys: Place sell ladder when filled
    - Sells: Notify Telegram when filled
    """
    # Get all open buy orders
    open_buys = get_open_orders(order_type="BUY")
    
    if open_buys:
        print(f"🔍 Checking {len(open_buys)} open buy orders...")
        filled_buys = trader.check_order_fills(open_buys)
        
        if filled_buys:
            print(f"   ✅ {len(filled_buys)} buy order(s) filled!")
            
            for filled_buy in filled_buys:
                order_id = filled_buy["order_id"]
                market_slug = filled_buy["market_slug"]
                
                print(f"\n🎉 Buy filled: {filled_buy['side']} @ ${filled_buy.get('filled_price', filled_buy['price']):.4f}")
                
                # Update buy order status with actual fill data
                update_order_status(
                    order_id,
                    "FILLED",
                    filled_buy.get("filled_size"),
                    filled_buy.get("filled_price")
                )
                
                # Place sell ladder
                print(f"   📈 Placing sell ladder...")
                sell_orders = trader.place_sell_ladder(
                    filled_buy["token_id"],
                    filled_buy.get("filled_price", filled_buy["price"]),
                    filled_buy.get("filled_size", filled_buy["size"]),
                    filled_buy["side"],
                    market_slug
                )
                
                print(f"   ✅ Placed {len(sell_orders)} sell orders")
                
                # Update summary
                update_market_summary(market_slug)
    
    # Get all open sell orders
    open_sells = get_open_orders(order_type="SELL")
    
    if open_sells:
        print(f"🔍 Checking {len(open_sells)} open sell orders...")
        filled_sells = trader.check_sell_fills(open_sells)
        
        if filled_sells:
            print(f"   ✅ {len(filled_sells)} sell order(s) filled!")
            
            for filled_sell in filled_sells:
                market_slug = filled_sell["market_slug"]
                
                # Telegram notification already sent by check_sell_fills
                print(f"   💰 Sell filled: {filled_sell['side']} @ ${filled_sell.get('filled_price', filled_sell['price']):.4f}")
                
                # Update summary
                update_market_summary(market_slug)


def main():
    """Main monitoring loop"""
    print("\n👁️  Polymarket Order Monitor Starting...")
    print(f"   Monitors: All open buy/sell orders")
    print(f"   Buys filled: Auto-place sell ladder")
    print(f"   Sells filled: Notify Telegram (@ponnymarket)\n")
    
    # Initialize trader
    trader = PolymarketTrader()
    
    # Poll interval
    poll_interval = int(os.getenv("MONITOR_POLL_INTERVAL", "30"))
    
    print(f"📊 Configuration:")
    print(f"   Poll Interval: {poll_interval}s\n")
    print("🔄 Starting monitor loop (press Ctrl+C to stop)\n")
    
    try:
        while True:
            try:
                monitor_all_orders(trader)
            except Exception as e:
                print(f"⚠️  Error in monitoring cycle: {e}")
            
            # Wait before next check
            time.sleep(poll_interval)
            
    except KeyboardInterrupt:
        print("\n\n⏸️  Monitor stopped by user")
    except Exception as e:
        print(f"\n\n❌ Monitor crashed: {e}")
        raise


if __name__ == "__main__":
    main()
