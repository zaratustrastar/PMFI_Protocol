"""
Polymarket Automated Trading Bot

Strategy:
1. Place ladder buy orders (0.1¢ to 1¢) on YES and NO tokens
2. Monitor for filled orders
3. Auto-place ladder sell orders at 200%-1000% profit
"""

import os
import time
import json
from typing import List, Dict, Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
import config
from market_utils import get_market_info, get_market_info_from_job
from database import save_order, update_order_status, update_market_summary, get_open_sell_orders
from telegram_notifier import notify_sell_executed



class PolymarketTrader:
    def __init__(self):
        """Initialize the trading bot with Polymarket CLOB client"""
        print("🤖 Initializing Polymarket Trading Bot...")
        
        # Log proxy status
        http_proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
        if http_proxy:
            proxy_display = http_proxy.split('@')[1] if '@' in http_proxy else http_proxy
            print(f"   🌐 Using proxy: {proxy_display}")
        else:
            print("   ⚠️  No HTTP_PROXY/HTTPS_PROXY set - may be blocked by Cloudflare")
        
        # Initialize CLOB client
        self.client = ClobClient(
            config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=2,  # MetaMask proxy wallet
            funder=config.PROXY_ADDRESS
        )
        
        # Use explicit API credentials if provided, otherwise try to derive
        pm_api_key = os.getenv("POLYMARKET_API_KEY", "")
        pm_api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        pm_api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        
        if pm_api_key and pm_api_secret and pm_api_passphrase:
            print("🔑 Using explicit API credentials from environment...")
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=pm_api_key,
                api_secret=pm_api_secret,
                api_passphrase=pm_api_passphrase
            )
            self.client.set_api_creds(creds)
            print(f"   ✅ API key: {pm_api_key[:8]}...")
        else:
            print("🔑 Deriving trading credentials from private key...")
            try:
                creds = self.client.create_or_derive_api_creds()
                if creds and hasattr(creds, 'api_key') and creds.api_key:
                    self.client.set_api_creds(creds)
                    print(f"   ✅ Derived API key: {creds.api_key[:8]}...")
                else:
                    print(f"   ⚠️  Credential derivation returned: {creds}")
                    print("   💡 Set POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE")
            except Exception as e:
                print(f"   ❌ Credential derivation failed: {e}")
                print("   💡 Set POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE")
        
        # Track active positions
        self.active_positions = {}  # {order_id: order_details}
        
        print("✅ Trading bot initialized!")
    
    def calculate_order_size(self, price: float) -> float:
        """
        Return fixed number of shares to buy per order
        
        Args:
            price: Price per token (not used, kept for compatibility)
            
        Returns:
            Number of tokens (size) - fixed at ORDER_SIZE_SHARES
        """
        return config.ORDER_SIZE_SHARES
    
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
                        "order_type": "BUY",
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
        Place MULTIPLE sell orders in a ladder pattern with increasing profit targets.
        
        Strategy:
        - Reserve 10% of position for resolution (untouched)
        - Tier 1: 30% @ 3x (200% profit)
        - Tier 2: 30% @ 4x (300% profit)
        - Tier 3: 30% @ 5x (400% profit)
        
        Args:
            token_id: Token ID to sell
            buy_price: Original buy price (actual filled price)
            buy_size: Number of tokens bought (actual filled quantity)
            side_name: "YES" or "NO" for logging
            market_slug: Market identifier for database
            
        Returns:
            List of placed sell order responses
        """
        # Get ladder config from config.py
        reserve_ratio = getattr(config, 'SELL_RESERVE_RATIO', 0.10)
        ladder_config = getattr(config, 'SELL_LADDER_CONFIG', [
            {"ratio": 0.30, "profit_multiple": 3},
            {"ratio": 0.30, "profit_multiple": 4},
            {"ratio": 0.30, "profit_multiple": 5},
        ])
        
        reserved_size = buy_size * reserve_ratio
        sellable_size = buy_size - reserved_size
        
        print(f"\n💰 Placing SELL LADDER for {side_name} token (bought @ ${buy_price:.4f})...")
        print(f"   Token ID: {token_id}")
        print(f"   Buy size: {buy_size:.2f} tokens")
        print(f"   Reserved for resolution: {reserved_size:.2f} tokens ({reserve_ratio*100:.0f}%)")
        print(f"   Available for ladder: {sellable_size:.2f} tokens")
        
        # Validate inputs
        if not token_id:
            print(f"   ❌ CRITICAL: token_id is None or empty!")
            return []
        
        if buy_size <= 0:
            print(f"   ❌ CRITICAL: buy_size is {buy_size} (must be > 0)")
            return []
        
        placed_orders = []
        min_shares = getattr(config, 'MIN_SHARES_PER_ORDER', 5)
        
        # Calculate total ratio from ladder config to normalize
        total_ratio = sum(tier["ratio"] for tier in ladder_config)
        
        # Pre-calculate all tier sizes to determine consolidation strategy
        tier_sizes = []
        for tier in ladder_config:
            tier_size = sellable_size * (tier["ratio"] / total_ratio)
            tier_sizes.append({
                "size": tier_size,
                "profit_multiple": tier["profit_multiple"],
                "meets_minimum": tier_size >= min_shares
            })
        
        # Count how many tiers meet the minimum
        valid_tiers = sum(1 for t in tier_sizes if t["meets_minimum"])
        
        print(f"   📊 Tier analysis: {valid_tiers}/{len(ladder_config)} tiers meet {min_shares} share minimum")
        
        # CONSOLIDATION STRATEGY
        if valid_tiers == 0:
            # All tiers too small - consolidate into single sell at best price (3x)
            if sellable_size >= min_shares:
                print(f"   🔄 Consolidating: All tiers too small. Placing single sell @ 3x for {sellable_size:.2f} shares")
                consolidated_config = [{"size": sellable_size, "profit_multiple": 3}]
            else:
                print(f"   ❌ Position too small: {sellable_size:.2f} < {min_shares} minimum. No sells placed.")
                return []
        elif valid_tiers < len(ladder_config):
            # Some tiers too small - redistribute to valid tiers
            small_tier_total = sum(t["size"] for t in tier_sizes if not t["meets_minimum"])
            valid_tier_list = [t for t in tier_sizes if t["meets_minimum"]]
            
            # Distribute small tier shares equally to valid tiers
            redistribution_per_tier = small_tier_total / len(valid_tier_list) if valid_tier_list else 0
            
            consolidated_config = []
            for t in valid_tier_list:
                consolidated_config.append({
                    "size": t["size"] + redistribution_per_tier,
                    "profit_multiple": t["profit_multiple"]
                })
            
            print(f"   🔄 Redistributed {small_tier_total:.2f} shares from {len(ladder_config) - valid_tiers} small tier(s)")
        else:
            # All tiers valid - use standard config
            consolidated_config = tier_sizes
        
        # Place orders using the (potentially consolidated) config
        for i, tier in enumerate(consolidated_config):
            tier_size = tier["size"]
            profit_multiple = tier["profit_multiple"]
            sell_price = buy_price * profit_multiple
            profit_pct = (profit_multiple - 1) * 100
            
            print(f"\n   📊 Tier {i+1}: {tier_size:.2f} tokens @ {profit_multiple}x (+{profit_pct:.0f}%)")
            
            # Final safety check
            if tier_size < min_shares:
                print(f"      ⚠️  Tier size {tier_size:.2f} < {min_shares} minimum, skipping")
                continue
            
            # Cap sell price at Polymarket max
            if sell_price > 0.99:
                sell_price = 0.99
                print(f"      ⚠️  Capped sell price at $0.99 (max allowed)")
            
            if sell_price < 0.01:
                sell_price = 0.01
                print(f"      ⚠️  Floored sell price at $0.01 (min allowed)")
            
            try:
                print(f"      📝 Creating sell order @ ${sell_price:.4f}...")
                
                order_args = OrderArgs(
                    price=sell_price,
                    size=tier_size,
                    side=SELL,
                    token_id=token_id,
                )
                
                print(f"         OrderArgs: price={sell_price}, size={tier_size:.4f}, side=SELL")
                
                signed_order = self.client.create_order(order_args)
                print(f"         Order signed successfully")
                
                response = self.client.post_order(signed_order, OrderType.GTC)
                print(f"         API Response: {json.dumps(response, default=str)[:200]}...")
                
                if response.get("success"):
                    order_id = response.get("orderID", "")
                    print(f"      ✅ Tier {i+1}: Sell @ ${sell_price:.4f} ({tier_size:.2f} tokens, +{profit_pct:.0f}%) - Order ID: {order_id[:8]}...")
                    
                    order_data = {
                        "market_slug": market_slug,
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "order_type": "SELL",
                        "price": sell_price,
                        "size": tier_size,
                        "buy_price": buy_price,
                        "profit_multiple": profit_multiple,
                        "ladder_tier": i + 1,
                        "status": "OPEN"
                    }
                    placed_orders.append(order_data)
                    
                    # Save to database
                    save_order(order_data)
                else:
                    error = response.get("error", response.get("errorMsg", "Unknown error"))
                    error_code = response.get("errorCode", "N/A")
                    print(f"      ❌ Tier {i+1}: Sell @ ${sell_price:.4f} failed!")
                    print(f"         Error: {error}")
                    print(f"         Error code: {error_code}")
                    
            except Exception as e:
                import traceback
                print(f"      ❌ Tier {i+1}: Exception: {str(e)}")
                print(f"         Traceback: {traceback.format_exc()}")
        
        # Summary
        if placed_orders:
            print(f"\n   📊 Sell ladder: {len(placed_orders)}/{len(consolidated_config)} orders placed!")
            print(f"   📊 Reserved for resolution: {reserved_size:.2f} tokens ({reserve_ratio*100:.0f}%)")
        else:
            print(f"\n   ⚠️ Sell ladder: No orders placed (position may be too small)")
        
        return placed_orders
    
    def place_sell_order(self, token_id: str, avg_buy_price: float, sell_size: float, side_name: str, market_slug: str) -> List[Dict]:
        """
        Place MULTIPLE sell orders in a ladder pattern for accumulated fills.
        
        Updated Strategy (uses SELL_LADDER_CONFIG):
        - Reserve 10% of position for resolution (untouched)
        - Tier 1: 30% @ 3x (200% profit)
        - Tier 2: 30% @ 4x (300% profit)
        - Tier 3: 30% @ 5x (400% profit)
        
        Args:
            token_id: Token ID to sell
            avg_buy_price: Average buy price across all fills
            sell_size: Total tokens available to sell (will be split across ladder)
            side_name: "YES" or "NO" for logging
            market_slug: Market identifier for database
            
        Returns:
            List of placed sell order responses
        """
        # Get ladder config from config.py
        reserve_ratio = getattr(config, 'SELL_RESERVE_RATIO', 0.10)
        ladder_config = getattr(config, 'SELL_LADDER_CONFIG', [
            {"ratio": 0.30, "profit_multiple": 3},
            {"ratio": 0.30, "profit_multiple": 4},
            {"ratio": 0.30, "profit_multiple": 5},
        ])
        
        # Calculate reserved vs sellable from total position
        # Note: sell_size is the total accumulated, apply reserve to it
        reserved_size = sell_size * reserve_ratio
        sellable_size = sell_size - reserved_size
        
        print(f"\n💰 Placing SELL LADDER for {side_name} token (accumulated fills)...")
        print(f"   Token ID: {token_id}")
        print(f"   Avg buy price: ${avg_buy_price:.4f}")
        print(f"   Total accumulated: {sell_size:.2f} tokens")
        print(f"   Reserved for resolution: {reserved_size:.2f} tokens ({reserve_ratio*100:.0f}%)")
        print(f"   Available for ladder: {sellable_size:.2f} tokens")
        
        # Validate inputs
        if not token_id:
            print(f"   ❌ CRITICAL: token_id is None or empty!")
            return []
        
        if sell_size < 5:
            print(f"   ❌ CRITICAL: sell_size is {sell_size:.2f} (must be >= 5)")
            return []
        
        placed_orders = []
        
        # Calculate total ratio from ladder config
        total_ratio = sum(tier["ratio"] for tier in ladder_config)
        
        # Place orders for each tier in the ladder
        for i, tier in enumerate(ladder_config):
            tier_ratio = tier["ratio"]
            profit_multiple = tier["profit_multiple"]
            
            # Calculate size for this tier (proportional to sellable amount)
            tier_size = sellable_size * (tier_ratio / total_ratio)
            sell_price = avg_buy_price * profit_multiple
            profit_pct = (profit_multiple - 1) * 100
            
            print(f"\n   📊 Tier {i+1}: {tier_size:.2f} tokens @ {profit_multiple}x (+{profit_pct:.0f}%)")
            
            # Skip if tier size too small (Polymarket minimum)
            min_shares = getattr(config, 'MIN_SHARES_PER_ORDER', 5)
            if tier_size < min_shares:
                print(f"      ⚠️  Tier size {tier_size:.2f} < {min_shares} minimum, skipping this tier")
                continue
            
            # Cap sell price at Polymarket max/min
            if sell_price > 0.99:
                sell_price = 0.99
                print(f"      ⚠️  Capped sell price at $0.99 (max allowed)")
            
            if sell_price < 0.01:
                sell_price = 0.01
                print(f"      ⚠️  Floored sell price at $0.01 (min allowed)")
            
            try:
                print(f"      📝 Creating sell order @ ${sell_price:.4f}...")
                
                order_args = OrderArgs(
                    price=sell_price,
                    size=tier_size,
                    side=SELL,
                    token_id=token_id,
                )
                
                print(f"         OrderArgs: price={sell_price}, size={tier_size:.4f}, side=SELL")
                
                signed_order = self.client.create_order(order_args)
                print(f"         Order signed successfully")
                
                response = self.client.post_order(signed_order, OrderType.GTC)
                print(f"         API Response: {json.dumps(response, default=str)[:200]}...")
                
                if response.get("success"):
                    order_id = response.get("orderID", "")
                    print(f"      ✅ Tier {i+1}: Sell @ ${sell_price:.4f} ({tier_size:.2f} tokens, +{profit_pct:.0f}%) - Order ID: {order_id[:8]}...")
                    
                    order_data = {
                        "market_slug": market_slug,
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "order_type": "SELL",
                        "price": sell_price,
                        "size": tier_size,
                        "buy_price": avg_buy_price,
                        "profit_multiple": profit_multiple,
                        "ladder_tier": i + 1,
                        "status": "OPEN"
                    }
                    placed_orders.append(order_data)
                    
                    # Save to database
                    save_order(order_data)
                else:
                    error = response.get("error", response.get("errorMsg", "Unknown error"))
                    error_code = response.get("errorCode", "N/A")
                    print(f"      ❌ Tier {i+1}: Sell @ ${sell_price:.4f} failed!")
                    print(f"         Error: {error}")
                    print(f"         Error code: {error_code}")
                    
            except Exception as e:
                import traceback
                print(f"      ❌ Tier {i+1}: Exception: {str(e)}")
                print(f"         Traceback: {traceback.format_exc()}")
        
        # Summary
        if placed_orders:
            print(f"\n   📊 Sell ladder: {len(placed_orders)}/{len(ladder_config)} orders placed!")
            print(f"   📊 Reserved for resolution: {reserved_size:.2f} tokens ({reserve_ratio*100:.0f}%)")
        else:
            print(f"\n   📊 Sell ladder failed - no orders placed!")
        
        return placed_orders
    
    def check_order_fills(self, orders: List[Dict]) -> tuple[List[Dict], List[Dict]]:
        """
        Check which orders have been filled (fully or partially)
        
        Args:
            orders: List of order dictionaries (must include token_id from database)
            
        Returns:
            Tuple of (fully_filled_orders, partial_fill_orders)
            Each contains actual filled size, price, AND original token_id preserved
        """
        from database import get_order_accumulated_amount
        
        filled_orders = []
        partial_orders = []
        
        for order in orders:
            order_id = order["order_id"]
            
            # CRITICAL: Preserve token_id from original order (API response doesn't include it)
            original_token_id = order.get("token_id")
            if not original_token_id:
                print(f"   ⚠️  Order {order_id[:8]} missing token_id in database - cannot place sell ladder!")
                continue
            
            try:
                order_status = self.client.get_order(order_id)
                
                # Auto-cancel orders not found on exchange (returns None)
                if order_status is None:
                    print(f"   ⚠️  Order {order_id[:8]} not found on exchange (cancelled or expired)")
                    update_order_status(order_id, "CANCELLED")
                    continue
                
                status = order_status.get("status", "").upper()
                
                # Handle full fills
                if status in ["FILLED", "MATCHED"]:
                    filled_size = float(order_status.get("size_matched", order["size"]))
                    avg_price = float(order_status.get("avg_price", order["price"]))
                    
                    # Check how much we've already accumulated from this order
                    already_accumulated = get_order_accumulated_amount(order_id)
                    new_fill_amount = filled_size - already_accumulated
                    
                    if new_fill_amount > 0.01:  # Only if meaningful new fill
                        print(f"   🎯 Order FULLY filled: {order['order_type']} {order['side']} @ ${avg_price:.4f} ({filled_size:.2f} tokens)")
                        print(f"      Token ID: {original_token_id[:16]}...")
                        print(f"      Already accumulated: {already_accumulated:.2f}, New to add: {new_fill_amount:.2f}")
                        
                        filled_order = {
                            **order,
                            "status": "FILLED",
                            "filled_size": filled_size,
                            "new_fill_amount": new_fill_amount,
                            "filled_price": avg_price,
                            "token_id": original_token_id,
                            "is_fully_filled": True
                        }
                        filled_orders.append(filled_order)
                    
                elif status == "PARTIAL":
                    filled_size = float(order_status.get("size_matched", 0))
                    avg_price = float(order_status.get("avg_price", order["price"]))
                    
                    # Check how much we've already accumulated from this order
                    already_accumulated = get_order_accumulated_amount(order_id)
                    new_fill_amount = filled_size - already_accumulated
                    
                    if new_fill_amount > 0.01:  # Only if meaningful new fill
                        print(f"   ⏳ PARTIAL fill detected: {order['order_type']} {order['side']} @ ${avg_price:.4f}")
                        print(f"      Total filled: {filled_size:.2f}/{order['size']:.2f}")
                        print(f"      Already accumulated: {already_accumulated:.2f}, New to add: {new_fill_amount:.2f}")
                        
                        partial_order = {
                            **order,
                            "status": "PARTIAL",
                            "filled_size": filled_size,
                            "new_fill_amount": new_fill_amount,
                            "filled_price": avg_price,
                            "token_id": original_token_id,
                            "is_fully_filled": False
                        }
                        partial_orders.append(partial_order)
                    
            except Exception as e:
                error_str = str(e).lower()
                if "not found" in error_str or "does not exist" in error_str or "order_not_found" in error_str:
                    print(f"   ⚠️  Order {order_id[:8]} not found on exchange (cancelled or expired)")
                    update_order_status(order_id, "CANCELLED")
                else:
                    print(f"   ⚠️  Error checking order {order_id[:8]}: {str(e)}")
        
        return filled_orders, partial_orders
    
    def check_sell_fills(self, orders: List[Dict]) -> List[Dict]:
        """
        Check open sell orders for fills and notify via Telegram
        
        Args:
            orders: List of sell order dictionaries
            
        Returns:
            List of filled sell orders
        """
        filled_sells = []
        
        for order in orders:
            order_id = order["order_id"]
            
            try:
                order_status = self.client.get_order(order_id)
                
                # Auto-cancel orders not found on exchange (returns None)
                if order_status is None:
                    print(f"   ⚠️  Order {order_id[:8]} not found on exchange (cancelled or expired)")
                    update_order_status(order_id, "CANCELLED")
                    continue
                
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
                    
                    # Get market slug from order for summary
                    market_slug = order.get("market_slug", "")
                    if market_slug:
                        update_market_summary(market_slug)
                        
                        # DISABLED: Only show new markets on Telegram (no buy/sell notifications)
                        # notify_sell_executed(market_slug, {
                        #     "side": order["side"],
                        #     "buy_price": buy_price,
                        #     "sell_price": avg_price,
                        #     "size": filled_size,
                        #     "profit_usd": profit_usd,
                        #     "profit_pct": profit_pct
                        # })
                    
                    # Store fill data in order dict
                    order["status"] = "FILLED"
                    order["filled_size"] = filled_size
                    order["filled_price"] = avg_price
                    filled_sells.append(order)
                    
            except Exception as e:
                error_str = str(e).lower()
                # Only auto-cancel for explicit "not found" errors, NOT transient network errors
                if "not found" in error_str or "does not exist" in error_str or "order_not_found" in error_str:
                    print(f"   ⚠️  Order {order_id[:8]} not found on exchange (cancelled or expired)")
                    update_order_status(order_id, "CANCELLED")
                else:
                    # Transient errors (network, timeout) - keep order OPEN for retry
                    print(f"   ⚠️  Error checking sell order {order_id[:8]}: {str(e)}")
        
        return filled_sells
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order on Polymarket
        
        Args:
            order_id: The order ID to cancel
            
        Returns:
            True if cancellation was successful, False otherwise
        """
        try:
            response = self.client.cancel(order_id)
            
            # Handle different response types
            if response is None:
                print(f"   ❌ Failed to cancel {order_id[:8]}: Response is None")
                return False
            
            # If response is a dict
            if isinstance(response, dict):
                # Check if order was successfully canceled
                canceled_list = response.get("canceled", [])
                if order_id in canceled_list:
                    print(f"   ✅ Cancelled order {order_id[:8]}")
                    return True
                
                # Check if order is in not_canceled dict
                not_canceled = response.get("not_canceled", {})
                if order_id in not_canceled:
                    reason = not_canceled[order_id]
                    
                    # If already canceled/matched, that's success - order is gone
                    if "already canceled" in reason.lower() or "already matched" in reason.lower() or "can't be found" in reason.lower():
                        print(f"   ✅ Order {order_id[:8]} already gone ({reason})")
                        return True
                    else:
                        # Other reasons are actual failures
                        print(f"   ❌ Failed to cancel {order_id[:8]}: {reason}")
                        return False
                
                # Check for legacy success field
                if response.get("success", False):
                    print(f"   ✅ Cancelled order {order_id[:8]}")
                    return True
                
                # No clear success indicator
                error = response.get("error") or response.get("errorMsg") or "Unknown error"
                print(f"   ❌ Failed to cancel {order_id[:8]}: {error}")
                return False
            
            # If response is True/False boolean
            elif isinstance(response, bool):
                if response:
                    print(f"   ✅ Cancelled order {order_id[:8]}")
                    return True
                else:
                    print(f"   ❌ Failed to cancel {order_id[:8]}: Response is False")
                    return False
            
            # Unknown response type
            else:
                print(f"   ❌ Failed to cancel {order_id[:8]}: Unexpected response type {type(response)}")
                return False
                
        except Exception as e:
            print(f"   ❌ Error cancelling {order_id[:8]}: {str(e)}")
            import traceback
            print(f"   🔍 DEBUG: Full traceback:")
            traceback.print_exc()
            return False
    
    def place_orders_only(self, market_slug: str) -> int:
        """
        Place buy orders only without monitoring (for queue-based worker)
        
        Args:
            market_slug: Polymarket market slug
            
        Returns:
            Number of orders successfully placed (0 if market not found or all orders failed)
        """
        print(f"\n{'='*60}")
        print(f"🎯 Placing orders for: {market_slug}")
        print(f"{'='*60}")
        
        # Get market information
        market_info = get_market_info(market_slug)
        
        if not market_info:
            print(f"❌ Market '{market_slug}' not found!")
            return 0
        
        print(f"\n📋 Market: {market_info['question']}")
        print(f"   YES Token: {market_info['yes_token_id'][:16]}...")
        print(f"   NO Token: {market_info['no_token_id'][:16]}...")
        
        # Place buy ladders on both YES and NO
        yes_orders = self.place_buy_ladder(market_info['yes_token_id'], "YES", market_slug)
        no_orders = self.place_buy_ladder(market_info['no_token_id'], "NO", market_slug)
        
        total_orders = len(yes_orders) + len(no_orders)
        
        if total_orders > 0:
            print(f"\n✅ Placed {total_orders} buy orders total")
            print(f"   (Monitoring will be handled by separate process)\n")
        else:
            print(f"\n❌ No orders placed (all orders failed)")
        
        return total_orders
    
    def place_orders_only_from_job(self, job: dict) -> int:
        """
        Place buy orders using job data directly (no API lookup needed).
        
        This is the preferred method for queue-based trading as the job
        already contains the token IDs and condition_id from when the
        market was first detected.
        
        Args:
            job: Job dict with market_id (condition_id), clob_token_ids, question, etc.
            
        Returns:
            Number of orders successfully placed (0 if data missing or all orders failed)
        """
        question = job.get('question', 'Unknown Market')
        condition_id = job.get('market_id', '')
        event_slug = job.get('event_slug', '')
        
        print(f"\n{'='*60}")
        print(f"🎯 Placing orders for: {question[:50]}...")
        print(f"   Condition ID: {condition_id[:20]}..." if condition_id else "   No condition ID!")
        print(f"{'='*60}")
        
        # Get market info from job data (no API call)
        market_info = get_market_info_from_job(job)
        
        if not market_info:
            # Fallback: Try API lookup by event_slug (for old jobs without token data)
            print(f"⚠️  No token data in job, falling back to API lookup...")
            market_info = get_market_info(event_slug) if event_slug else None
        
        if not market_info:
            print(f"❌ Could not get market info for job!")
            return 0
        
        print(f"\n📋 Market: {market_info['question']}")
        print(f"   YES Token: {market_info['yes_token_id'][:16]}...")
        print(f"   NO Token: {market_info['no_token_id'][:16]}...")
        
        # Place buy ladders on both YES and NO
        # Use condition_id as the market identifier for order tracking
        market_identifier = condition_id if condition_id else event_slug
        
        yes_orders = self.place_buy_ladder(market_info['yes_token_id'], "YES", market_identifier)
        no_orders = self.place_buy_ladder(market_info['no_token_id'], "NO", market_identifier)
        
        total_orders = len(yes_orders) + len(no_orders)
        
        if total_orders > 0:
            print(f"\n✅ Placed {total_orders} buy orders total")
            print(f"   (Monitoring will be handled by separate process)\n")
        else:
            print(f"\n❌ No orders placed (all orders failed)")
        
        return total_orders
    
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
