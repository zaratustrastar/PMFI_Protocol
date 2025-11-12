"""
Polymarket Automated Trading Bot

Strategy:
1. Place ladder buy orders (0.1¢ to 1¢) on YES and NO tokens
2. Monitor for filled orders
3. Auto-place ladder sell orders at 200%-1000% profit
"""

import time
import json
from typing import List, Dict, Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
import config
from market_utils import get_market_info
from database import save_order, update_order_status, update_market_summary, get_open_sell_orders
from telegram_notifier import notify_sell_executed


class PolymarketTrader:
    def __init__(self):
        """Initialize the trading bot with Polymarket CLOB client"""
        print("🤖 Initializing Polymarket Trading Bot...")
        
        # Initialize CLOB client
        self.client = ClobClient(
            config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=1,  # Email/Magic wallet
            funder=config.PROXY_ADDRESS
        )
        
        # Create or derive API credentials
        print("🔑 Setting up API credentials...")
        
        # Use Builder API credentials if available, otherwise derive from private key
        if config.BUILDER_API_KEY and config.BUILDER_API_SECRET:
            print("   Using Builder API credentials...")
            from py_clob_client.clob_types import ApiCreds
            api_creds = ApiCreds(
                api_key=config.BUILDER_API_KEY,
                api_secret=config.BUILDER_API_SECRET,
                api_passphrase=""  # Builder credentials don't use passphrase
            )
            self.client.set_api_creds(api_creds)
        else:
            print("   Deriving credentials from private key...")
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
        
        # Track active positions
        self.active_positions = {}  # {order_id: order_details}
        
        print("✅ Trading bot initialized!")
    
    def calculate_order_size(self, price: float) -> float:
        """
        Calculate number of tokens to buy for $1 order
        
        Args:
            price: Price per token
            
        Returns:
            Number of tokens (size)
        """
        return config.ORDER_SIZE_USD / price
    
    def place_buy_ladder(self, token_id: str, side_name: str, market_slug: str) -> List[Dict]:
        """
        Place ladder buy orders from 1¢ to 3¢
        
        Args:
            token_id: Token ID to trade
            side_name: "YES" or "NO" for logging
            market_slug: Market identifier for database
            
        Returns:
            List of placed order responses
        """
        print(f"\n📊 Placing BUY ladder for {side_name} token...")
        print(f"   Token ID: {token_id}")
        
        placed_orders = []
        
        for price in config.BUY_LADDER_PRICES:
            size = self.calculate_order_size(price)
            
            try:
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side=BUY,
                    token_id=token_id,
                )
                
                signed_order = self.client.create_order(order_args)
                response = self.client.post_order(signed_order, OrderType.GTC)
                
                if response.get("success"):
                    order_id = response.get("orderID", "")
                    print(f"   ✅ Buy @ ${price:.4f} ({size:.2f} tokens) - Order ID: {order_id[:8]}...")
                    
                    # Track this order
                    order_data = {
                        "market_slug": market_slug,
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "type": "BUY",
                        "price": price,
                        "size": size,
                        "status": "OPEN"
                    }
                    placed_orders.append(order_data)
                    
                    # Save to database
                    save_order(order_data)
                else:
                    error = response.get("error", "Unknown error")
                    print(f"   ❌ Buy @ ${price:.4f} failed: {error}")
                    
            except Exception as e:
                print(f"   ❌ Buy @ ${price:.4f} error: {str(e)}")
        
        return placed_orders
    
    def place_sell_ladder(self, token_id: str, buy_price: float, buy_size: float, side_name: str, market_slug: str) -> List[Dict]:
        """
        Place ladder sell orders at 200%-1000% profit
        
        Args:
            token_id: Token ID to sell
            buy_price: Original buy price (actual filled price)
            buy_size: Number of tokens bought (actual filled quantity)
            side_name: "YES" or "NO" for logging
            market_slug: Market identifier for database
            
        Returns:
            List of placed sell order responses
        """
        print(f"\n💰 Placing SELL ladder for {side_name} token (bought @ ${buy_price:.4f})...")
        
        placed_orders = []
        
        # Divide position across sell ladder
        size_per_order = buy_size / len(config.SELL_PROFIT_MULTIPLES)
        
        for multiple in config.SELL_PROFIT_MULTIPLES:
            sell_price = buy_price * multiple
            
            # Polymarket prices must be between 0.01 and 0.99
            if sell_price > 0.99:
                sell_price = 0.99
            
            try:
                order_args = OrderArgs(
                    price=sell_price,
                    size=size_per_order,
                    side=SELL,
                    token_id=token_id,
                )
                
                signed_order = self.client.create_order(order_args)
                response = self.client.post_order(signed_order, OrderType.GTC)
                
                if response.get("success"):
                    order_id = response.get("orderID", "")
                    profit_pct = (multiple - 1) * 100
                    print(f"   ✅ Sell @ ${sell_price:.4f} ({size_per_order:.2f} tokens, +{profit_pct:.0f}%) - Order ID: {order_id[:8]}...")
                    
                    order_data = {
                        "market_slug": market_slug,
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "type": "SELL",
                        "price": sell_price,
                        "size": size_per_order,
                        "buy_price": buy_price,
                        "profit_multiple": multiple,
                        "status": "OPEN"
                    }
                    placed_orders.append(order_data)
                    
                    # Save to database
                    save_order(order_data)
                else:
                    error = response.get("error", "Unknown error")
                    print(f"   ❌ Sell @ ${sell_price:.4f} failed: {error}")
                    
            except Exception as e:
                print(f"   ❌ Sell @ ${sell_price:.4f} error: {str(e)}")
        
        return placed_orders
    
    def check_order_fills(self, orders: List[Dict]) -> List[Dict]:
        """
        Check which orders have been filled
        
        Args:
            orders: List of order dictionaries
            
        Returns:
            List of filled orders with actual filled size and price
        """
        filled_orders = []
        
        for order in orders:
            order_id = order["order_id"]
            
            try:
                order_status = self.client.get_order(order_id)
                status = order_status.get("status", "").upper()
                
                # Handle both full and partial fills
                if status in ["FILLED", "MATCHED"]:
                    # Get actual filled quantity and average price
                    filled_size = float(order_status.get("size_matched", order["size"]))
                    avg_price = float(order_status.get("avg_price", order["price"]))
                    
                    print(f"   🎯 Order filled: {order['type']} {order['side']} @ ${avg_price:.4f} ({filled_size:.2f} tokens)")
                    
                    order["status"] = "FILLED"
                    order["filled_size"] = filled_size
                    order["filled_price"] = avg_price
                    filled_orders.append(order)
                    
                elif status == "PARTIAL":
                    # Partial fill - track but don't trigger sell yet
                    filled_size = float(order_status.get("size_matched", 0))
                    print(f"   ⏳ Partial fill: {order['type']} {order['side']} ({filled_size:.2f}/{order['size']:.2f})")
                    
            except Exception as e:
                print(f"   ⚠️  Error checking order {order_id[:8]}: {str(e)}")
        
        return filled_orders
    
    def check_sell_fills(self, market_slug: str) -> List[Dict]:
        """
        Check open sell orders for fills and notify via Telegram
        
        Args:
            market_slug: Market identifier
            
        Returns:
            List of filled sell orders
        """
        open_sells = get_open_sell_orders(market_slug)
        filled_sells = []
        
        for order in open_sells:
            order_id = order["order_id"]
            
            try:
                order_status = self.client.get_order(order_id)
                status = order_status.get("status", "").upper()
                
                if status in ["FILLED", "MATCHED"]:
                    # Get actual filled data
                    filled_size = float(order_status.get("size_matched", order["size"]))
                    avg_price = float(order_status.get("avg_price", order["price"]))
                    
                    # Calculate profit
                    buy_price = order.get("buy_price", 0)
                    profit_usd = (avg_price - buy_price) * filled_size
                    profit_pct = ((avg_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                    
                    print(f"   💰 SELL FILLED: {order['side']} @ ${avg_price:.4f} (+${profit_usd:.2f})")
                    
                    # Update database
                    update_order_status(order_id, "FILLED", filled_size, avg_price)
                    update_market_summary(market_slug)
                    
                    # Notify Telegram
                    notify_sell_executed(market_slug, {
                        "side": order["side"],
                        "buy_price": buy_price,
                        "sell_price": avg_price,
                        "size": filled_size,
                        "profit_usd": profit_usd,
                        "profit_pct": profit_pct
                    })
                    
                    filled_sells.append(order)
                    
            except Exception as e:
                print(f"   ⚠️  Error checking sell order {order_id[:8]}: {str(e)}")
        
        return filled_sells
    
    def place_orders_only(self, market_slug: str):
        """
        Place buy orders only without monitoring (for queue-based worker)
        
        Args:
            market_slug: Polymarket market slug
            
        Returns:
            True if orders placed successfully, False otherwise
        """
        print(f"\n{'='*60}")
        print(f"🎯 Placing orders for: {market_slug}")
        print(f"{'='*60}")
        
        # Get market information
        market_info = get_market_info(market_slug)
        
        if not market_info:
            print(f"❌ Market '{market_slug}' not found!")
            return False
        
        print(f"\n📋 Market: {market_info['question']}")
        print(f"   YES Token: {market_info['yes_token_id'][:16]}...")
        print(f"   NO Token: {market_info['no_token_id'][:16]}...")
        
        # Place buy ladders on both YES and NO
        yes_orders = self.place_buy_ladder(market_info['yes_token_id'], "YES", market_slug)
        no_orders = self.place_buy_ladder(market_info['no_token_id'], "NO", market_slug)
        
        total_orders = len(yes_orders) + len(no_orders)
        print(f"\n✅ Placed {total_orders} buy orders total")
        print(f"   (Monitoring will be handled by separate process)\n")
        
        return True
    
    def run_strategy_limited(self, market_slug: str, max_runtime_minutes: int = 60):
        """
        Run trading strategy with time limit (for auto-trader)
        
        Args:
            market_slug: Polymarket market slug
            max_runtime_minutes: Maximum runtime in minutes
        """
        import time as time_module
        start_time = time_module.time()
        max_runtime_seconds = max_runtime_minutes * 60
        
        self._run_strategy_internal(market_slug, start_time, max_runtime_seconds)
    
    def run_strategy(self, market_slug: str):
        """
        Run the complete trading strategy for a market (unlimited time)
        
        Args:
            market_slug: Polymarket market slug
        """
        self._run_strategy_internal(market_slug, None, None)
    
    def _run_strategy_internal(self, market_slug: str, start_time=None, max_runtime_seconds=None):
        """
        Internal strategy runner
        
        Args:
            market_slug: Polymarket market slug
            start_time: Start timestamp (for limited runtime)
            max_runtime_seconds: Max runtime in seconds (None = unlimited)
        """
        print(f"\n{'='*60}")
        print(f"🎯 Starting trading strategy for: {market_slug}")
        print(f"{'='*60}")
        
        # Get market information
        market_info = get_market_info(market_slug)
        
        if not market_info:
            print(f"❌ Market '{market_slug}' not found!")
            return
        
        print(f"\n📋 Market: {market_info['question']}")
        print(f"   YES Token: {market_info['yes_token_id'][:16]}...")
        print(f"   NO Token: {market_info['no_token_id'][:16]}...")
        
        # Place buy ladders on both YES and NO
        all_buy_orders = []
        
        yes_orders = self.place_buy_ladder(market_info['yes_token_id'], "YES", market_slug)
        all_buy_orders.extend(yes_orders)
        
        no_orders = self.place_buy_ladder(market_info['no_token_id'], "NO", market_slug)
        all_buy_orders.extend(no_orders)
        
        print(f"\n✅ Placed {len(all_buy_orders)} buy orders total")
        print(f"\n🔍 Monitoring for fills (checking every {config.POLL_INTERVAL_SECONDS}s)...")
        print("   Buys → Auto-place sells | Sells → Notify Telegram")
        print("   Press Ctrl+C to stop monitoring\n")
        
        # Monitor for fills and auto-sell
        try:
            processed_orders = set()
            
            while True:
                # Check for filled buy orders
                filled_buys = self.check_order_fills(
                    [o for o in all_buy_orders if o["status"] == "OPEN"]
                )
                
                # Place sell orders for newly filled buys
                for filled_buy in filled_buys:
                    order_id = filled_buy["order_id"]
                    
                    if order_id not in processed_orders:
                        print(f"\n🎉 Buy order filled! Placing sell ladder...")
                        
                        # Update buy order status in database with actual fill data
                        update_order_status(
                            order_id, 
                            "FILLED",
                            filled_buy.get("filled_size"),
                            filled_buy.get("filled_price")
                        )
                        
                        # Use actual filled price and size
                        sell_orders = self.place_sell_ladder(
                            filled_buy["token_id"],
                            filled_buy.get("filled_price", filled_buy["price"]),
                            filled_buy.get("filled_size", filled_buy["size"]),
                            filled_buy["side"],
                            market_slug
                        )
                        
                        processed_orders.add(order_id)
                        print(f"   ✅ Placed {len(sell_orders)} sell orders")
                
                # Check for filled sell orders and notify Telegram
                filled_sells = self.check_sell_fills(market_slug)
                if filled_sells:
                    print(f"   📱 Notified Telegram about {len(filled_sells)} sell fills")
                
                # Check if time limit reached (for auto-trader)
                if max_runtime_seconds is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= max_runtime_seconds:
                        print(f"\n⏰ Time limit reached ({max_runtime_seconds/60:.0f} minutes)")
                        print("   Orders remain active on Polymarket")
                        break
                
                # Wait before next check
                time.sleep(config.POLL_INTERVAL_SECONDS)
                
        except KeyboardInterrupt:
            print("\n\n⏸️  Monitoring stopped by user")
            print("   Note: Orders remain active on Polymarket")


def main():
    """Main entry point"""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python polymarket_trader.py <market-slug>")
        print("Example: python polymarket_trader.py bitcoin-above-100k-on-december-31")
        sys.exit(1)
    
    market_slug = sys.argv[1]
    
    trader = PolymarketTrader()
    trader.run_strategy(market_slug)


if __name__ == "__main__":
    main()
