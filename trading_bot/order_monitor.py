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
from database import (
    get_open_orders, update_order_status, update_market_summary,
    add_accumulated_fill, check_sell_threshold, mark_sell_placed,
    mark_order_accumulated, update_order_accumulated_amount
)
from telegram_notifier import notify_buy_filled, notify_sell_executed, notify_sell_ladder_result

# Import from centralized config
from config import MIN_SHARES_PER_ORDER, SELL_RESERVE_RATIO

# Configuration for sell threshold
MIN_SHARES_FOR_SELL = MIN_SHARES_PER_ORDER  # Minimum accumulated shares before placing sell


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
    
    # Get all open buy orders (no age limit - monitor ALL open buys for fills)
    open_buys = get_open_orders(order_type="BUY", max_age_hours=None)
    
    if open_buys:
        print(f"🔍 Checking {len(open_buys)} open buy orders...")
        filled_buys, partial_buys = trader.check_order_fills(open_buys)
        
        # Combine both lists for processing
        all_fills = filled_buys + partial_buys
        
        if all_fills:
            full_count = len(filled_buys)
            partial_count = len(partial_buys)
            print(f"   ✅ Found {full_count} fully filled + {partial_count} partial fills!")
            
            for fill in all_fills:
                order_id = fill["order_id"]
                market_slug = fill["market_slug"]
                filled_price = fill.get('filled_price', fill['price'])
                filled_size = fill.get('filled_size', fill['size'])
                new_fill_amount = fill.get('new_fill_amount', filled_size)
                token_id = fill.get("token_id")
                side = fill["side"]
                is_fully_filled = fill.get("is_fully_filled", True)
                
                fill_type = "FULL" if is_fully_filled else "PARTIAL"
                print(f"\n🎉 {fill_type} fill: {side} @ ${filled_price:.4f} (new: {new_fill_amount:.2f} shares)")
                
                # Update buy order status with actual fill data
                if is_fully_filled:
                    update_order_status(
                        order_id,
                        "FILLED",
                        filled_size,
                        filled_price
                    )
                
                # Validate token_id before accumulating
                if not token_id:
                    error_msg = f"Missing token_id for order {order_id[:8]}"
                    print(f"   ❌ Cannot accumulate fill: {error_msg}")
                    continue
                
                # Add the NEW fill amount to accumulated fills (not the total)
                print(f"   📊 Adding {new_fill_amount:.2f} shares to accumulated fills...")
                add_accumulated_fill(market_slug, token_id, side, new_fill_amount, filled_price)
                
                # Update the order's accumulated_amount to prevent double-counting
                update_order_accumulated_amount(order_id, filled_size, is_fully_filled)
                
                # Check if we've reached the sell threshold
                threshold_check = check_sell_threshold(market_slug, token_id, side, MIN_SHARES_FOR_SELL)
                
                total_shares = threshold_check["total_shares"]
                avg_buy_price = threshold_check["avg_buy_price"]
                
                print(f"      Total accumulated: {total_shares:.2f} shares @ avg ${avg_buy_price:.4f}")
                print(f"      Threshold: {MIN_SHARES_FOR_SELL} shares | Ready to sell: {threshold_check['ready_to_sell']}")
                
                if threshold_check["ready_to_sell"]:
                    # We have enough shares and haven't placed a sell yet!
                    reserve_shares = total_shares * SELL_RESERVE_RATIO
                    sellable_shares = total_shares - reserve_shares
                    
                    print(f"\n   📈 Threshold reached! Placing SELL LADDER for {total_shares:.2f} shares...")
                    print(f"      Token ID: {token_id[:16]}...")
                    print(f"      Avg buy price: ${avg_buy_price:.4f}")
                    print(f"      Sellable shares: {sellable_shares:.2f} (90% of position)")
                    print(f"      Reserve (moonbag): {reserve_shares:.2f} shares (10%)")
                    
                    try:
                        # Place tiered sell ladder using full accumulated position
                        sell_orders = trader.place_sell_ladder(
                            token_id,
                            avg_buy_price,
                            total_shares,  # Full position - ladder handles reserve internally
                            side,
                            market_slug
                        )
                        
                        if sell_orders and len(sell_orders) > 0:
                            print(f"   ✅ Placed {len(sell_orders)} sell order(s) in ladder")
                            # Mark sell as placed to prevent duplicates
                            mark_sell_placed(market_slug, token_id, side)
                        else:
                            error_msg = "Sell ladder failed - no orders placed"
                            print(f"   ❌ {error_msg}")
                            
                    except Exception as e:
                        error_msg = str(e)
                        print(f"   ❌ Sell ladder error: {error_msg}")
                
                elif threshold_check["sell_already_placed"]:
                    print(f"   ⏭️  Sell already placed for this token - skipping")
                else:
                    remaining = MIN_SHARES_FOR_SELL - total_shares
                    print(f"   ⏳ Need {remaining:.2f} more shares to reach sell threshold")
                
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
