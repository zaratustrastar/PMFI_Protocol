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
    
    def place_buy_ladder(self, token_id: str, side_name: str) -> List[Dict]:
        """
        Place ladder buy orders from 0.1¢ to 1¢
        
        Args:
            token_id: Token ID to trade
            side_name: "YES" or "NO" for logging
            
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
                    placed_orders.append({
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "type": "BUY",
                        "price": price,
                        "size": size,
                        "status": "OPEN"
                    })
                else:
                    error = response.get("error", "Unknown error")
                    print(f"   ❌ Buy @ ${price:.4f} failed: {error}")
                    
            except Exception as e:
                print(f"   ❌ Buy @ ${price:.4f} error: {str(e)}")
        
        return placed_orders
    
    def place_sell_ladder(self, token_id: str, buy_price: float, buy_size: float, side_name: str) -> List[Dict]:
        """
        Place ladder sell orders at 200%-1000% profit
        
        Args:
            token_id: Token ID to sell
            buy_price: Original buy price
            buy_size: Number of tokens bought
            side_name: "YES" or "NO" for logging
            
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
                    
                    placed_orders.append({
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "type": "SELL",
                        "price": sell_price,
                        "size": size_per_order,
                        "buy_price": buy_price,
                        "profit_multiple": multiple,
                        "status": "OPEN"
                    })
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
            List of filled orders
        """
        filled_orders = []
        
        for order in orders:
            order_id = order["order_id"]
            
            try:
                order_status = self.client.get_order(order_id)
                status = order_status.get("status", "").upper()
                
                if status == "MATCHED":
                    print(f"   🎯 Order filled: {order['type']} {order['side']} @ ${order['price']:.4f}")
                    order["status"] = "FILLED"
                    filled_orders.append(order)
                    
            except Exception as e:
                print(f"   ⚠️  Error checking order {order_id[:8]}: {str(e)}")
        
        return filled_orders
    
    def run_strategy(self, market_slug: str):
        """
        Run the complete trading strategy for a market
        
        Args:
            market_slug: Polymarket market slug
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
        
        yes_orders = self.place_buy_ladder(market_info['yes_token_id'], "YES")
        all_buy_orders.extend(yes_orders)
        
        no_orders = self.place_buy_ladder(market_info['no_token_id'], "NO")
        all_buy_orders.extend(no_orders)
        
        print(f"\n✅ Placed {len(all_buy_orders)} buy orders total")
        print(f"\n🔍 Monitoring for fills (checking every {config.POLL_INTERVAL_SECONDS}s)...")
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
                        
                        sell_orders = self.place_sell_ladder(
                            filled_buy["token_id"],
                            filled_buy["price"],
                            filled_buy["size"],
                            filled_buy["side"]
                        )
                        
                        processed_orders.add(order_id)
                        print(f"   ✅ Placed {len(sell_orders)} sell orders")
                
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
