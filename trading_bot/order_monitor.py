"""
Order Monitor - Continuous monitoring of all active orders
Runs independently to monitor buy/sell fills across all markets
"""

import os
import time
from datetime import datetime, timedelta

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ Loaded environment variables from .env file")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment variables")

from polymarket_trader import PolymarketTrader
from database import get_open_orders, update_order_status, update_market_summary
from telegram_notifier import notify_buy_filled, notify_sell_executed, notify_sell_ladder_result


def cancel_stale_orders(trader: PolymarketTrader, max_age_hours: int = 12):
    """
    Cancel BUY orders that have been open for more than max_age_hours
    IMPORTANT: Only cancels BUY orders - SELL orders are left open indefinitely
    
    Args:
        trader: PolymarketTrader instance
        max_age_hours: Maximum age in hours before canceling (default 12)
    """
    # Get all open BUY orders only (never cancel SELL orders - they're profit targets!)
    all_open_orders = get_open_orders(order_type="BUY")
    
    if not all_open_orders:
        return
    
    now = datetime.now()
    stale_cutoff = now - timedelta(hours=max_age_hours)
    
    cancelled_count = 0
    
    for order in all_open_orders:
        created_at = order.get("created_at")
        
        if not created_at:
            continue
        
        # Convert to datetime if it's a string
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        
        # Check if order is stale
        if created_at < stale_cutoff:
            order_id = order["order_id"]
            order_age_hours = (now - created_at).total_seconds() / 3600
            
            print(f"\n⏰ Cancelling stale order (age: {order_age_hours:.1f}h)")
            print(f"   Order: {order['order_type']} {order['side']} @ ${order['price']:.4f}")
            print(f"   Market: {order['market_slug']}")
            
            # Cancel the order
            if trader.cancel_order(order_id):
                # Update database
                update_order_status(order_id, "CANCELLED")
                cancelled_count += 1
                print(f"   ✅ Order cancelled and marked in database")
            else:
                print(f"   ⚠️  Cancellation failed")
    
    if cancelled_count > 0:
        print(f"\n🗑️  Cancelled {cancelled_count} stale order(s)")


def monitor_all_orders(trader: PolymarketTrader):
    """
    Monitor all open orders for fills
    - Buys: Place sell ladder when filled
    - Sells: Notify Telegram when filled
    - Cancel stale orders (>12 hours old)
    """
    # First, cancel any stale orders (>12 hours old)
    try:
        cancel_stale_orders(trader, max_age_hours=12)
    except Exception as e:
        print(f"⚠️  Error cancelling stale orders: {e}")
        # Continue with monitoring even if cancellation fails
    
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
                filled_price = filled_buy.get('filled_price', filled_buy['price'])
                filled_size = filled_buy.get('filled_size', filled_buy['size'])
                token_id = filled_buy.get("token_id")
                side = filled_buy["side"]
                
                print(f"\n🎉 Buy filled: {side} @ ${filled_price:.4f}")
                
                # Update buy order status with actual fill data
                update_order_status(
                    order_id,
                    "FILLED",
                    filled_size,
                    filled_price
                )
                
                # Send Telegram notification about buy fill
                notify_buy_filled(market_slug, {
                    "side": side,
                    "price": filled_price,
                    "size": filled_size
                })
                
                # Validate token_id before placing sell ladder
                if not token_id:
                    error_msg = f"Missing token_id for order {order_id[:8]}"
                    print(f"   ❌ Cannot place sell ladder: {error_msg}")
                    notify_sell_ladder_result(market_slug, side, success=False, error=error_msg)
                    continue
                
                # Place sell ladder with error handling
                print(f"   📈 Placing sell ladder...")
                print(f"      Token ID: {token_id[:16]}...")
                print(f"      Buy price: ${filled_price:.4f}")
                print(f"      Size: {filled_size:.2f} tokens")
                
                try:
                    sell_orders = trader.place_sell_ladder(
                        token_id,
                        filled_price,
                        filled_size,
                        side,
                        market_slug
                    )
                    
                    if sell_orders and len(sell_orders) > 0:
                        print(f"   ✅ Placed {len(sell_orders)} sell orders")
                        notify_sell_ladder_result(market_slug, side, success=True, sell_count=len(sell_orders))
                    else:
                        error_msg = "No sell orders were placed (all failed)"
                        print(f"   ❌ {error_msg}")
                        notify_sell_ladder_result(market_slug, side, success=False, error=error_msg)
                        
                except Exception as e:
                    error_msg = str(e)
                    print(f"   ❌ Sell ladder error: {error_msg}")
                    notify_sell_ladder_result(market_slug, side, success=False, error=error_msg)
                
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
