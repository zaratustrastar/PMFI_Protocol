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
from market_utils import get_market_info, get_market_info_from_job
from database import save_order, update_order_status, update_market_summary, get_open_sell_orders
from telegram_notifier import notify_sell_executed

# Cloudflare bypass with curl_cffi
try:
    from curl_cffi import requests as curl_requests
    BYPASS_METHOD = "curl_cffi"
    print("🔓 Using curl_cffi for Cloudflare bypass (TLS fingerprint spoofing)")
except ImportError:
    import requests as curl_requests
    BYPASS_METHOD = "standard"
    print("⚠️  curl_cffi not available, using standard requests (may be blocked by Cloudflare)")


class PolymarketTrader:
    def __init__(self):
        """Initialize the trading bot with Polymarket CLOB client"""
        print("🤖 Initializing Polymarket Trading Bot...")
        print(f"   Bypass method: {BYPASS_METHOD}")
        
        # Initialize CLOB client
        self.client = ClobClient(
            config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=1,  # Email/Magic wallet
            funder=config.PROXY_ADDRESS
        )
        
        # Patch the client's HTTP session to use curl_cffi for Cloudflare bypass
        if BYPASS_METHOD == "curl_cffi":
            self._patch_client_session()
        
        # Derive trading API credentials from private key
        # NOTE: Builder API credentials are for fee rebates/attribution only, NOT for trading
        # Trading requires SDK credentials derived from your wallet's private key
        print("🔑 Deriving trading credentials from private key...")
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        
        # Track active positions
        self.active_positions = {}  # {order_id: order_details}
        
        print("✅ Trading bot initialized!")
    
    def _patch_client_session(self):
        """Patch py-clob-client to use curl_cffi for Cloudflare bypass"""
        import py_clob_client.http_helpers.helpers as http_helpers
        
        # Enhanced headers to look like real browser
        def get_browser_headers(original_headers: dict = None) -> dict:
            """Merge original headers with browser-like headers"""
            browser_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Referer': 'https://polymarket.com/',
                'Origin': 'https://polymarket.com',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-site',
                'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
            }
            
            # Merge with original headers (original takes precedence)
            if original_headers:
                browser_headers.update(original_headers)
            
            return browser_headers
        
        # Proxy configuration (if available)
        proxy_config = None
        if config.PROXY_URL:
            proxy_config = {"http": config.PROXY_URL, "https": config.PROXY_URL}
            print(f"   🌐 Using proxy: {config.PROXY_URL.split('@')[1] if '@' in config.PROXY_URL else config.PROXY_URL}")
        
        # Create wrapper functions using curl_cffi with chrome TLS fingerprint
        def patched_get(endpoint: str, headers: dict = None, params: dict = None):
            try:
                response = curl_requests.get(
                    endpoint,
                    headers=get_browser_headers(headers),
                    params=params,
                    impersonate="chrome120",  # Latest Chrome fingerprint
                    proxies=proxy_config,
                    timeout=30,
                    allow_redirects=True
                )
                
                # Detect Cloudflare blocking
                if response.status_code == 403 or "cloudflare" in response.text.lower():
                    print(f"   🚫 CLOUDFLARE BLOCK: GET {endpoint}")
                    print(f"   💡 This IP (Replit) is blocked. Run workers from residential IP.")
                    print(f"   📄 See CLOUDFLARE_ISSUE.md for setup instructions.")
                    raise Exception(f"Cloudflare blocked (403): {endpoint}")
                
                return response.json() if response.text else {}
            except Exception as e:
                if "Cloudflare" not in str(e):
                    print(f"   ❌ GET {endpoint[:40]}... failed: {str(e)[:80]}")
                raise
        
        def patched_post(endpoint: str, headers: dict = None, body: dict = None):
            try:
                response = curl_requests.post(
                    endpoint,
                    headers=get_browser_headers(headers),
                    json=body,
                    impersonate="chrome120",
                    proxies=proxy_config,
                    timeout=30,
                    allow_redirects=True
                )
                
                # Detect Cloudflare blocking
                if response.status_code == 403 or "cloudflare" in response.text.lower():
                    print(f"   🚫 CLOUDFLARE BLOCK: POST {endpoint}")
                    print(f"   💡 This IP (Replit) is blocked. Run workers from residential IP.")
                    print(f"   📄 See CLOUDFLARE_ISSUE.md for setup instructions.")
                    raise Exception(f"Cloudflare blocked (403): {endpoint}")
                
                return response.json() if response.text else {}
            except Exception as e:
                if "Cloudflare" not in str(e):
                    print(f"   ❌ POST {endpoint[:40]}... failed: {str(e)[:80]}")
                raise
        
        def patched_delete(endpoint: str, headers: dict = None):
            try:
                response = curl_requests.delete(
                    endpoint,
                    headers=get_browser_headers(headers),
                    impersonate="chrome120",
                    proxies=proxy_config,
                    timeout=30,
                    allow_redirects=True
                )
                
                # Detect Cloudflare blocking
                if response.status_code == 403 or "cloudflare" in response.text.lower():
                    print(f"   🚫 CLOUDFLARE BLOCK: DELETE {endpoint}")
                    print(f"   💡 This IP (Replit) is blocked. Run workers from residential IP.")
                    print(f"   📄 See CLOUDFLARE_ISSUE.md for setup instructions.")
                    raise Exception(f"Cloudflare blocked (403): {endpoint}")
                
                return response.json() if response.text else {}
            except Exception as e:
                if "Cloudflare" not in str(e):
                    print(f"   ❌ DELETE {endpoint[:40]}... failed: {str(e)[:80]}")
                raise
        
        # Monkey-patch the http_helpers module
        http_helpers.get = patched_get
        http_helpers.post = patched_post
        http_helpers.delete = patched_delete
        
        print("   🔧 Patched HTTP client with curl_cffi (Chrome 120 TLS + browser headers)")
    
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
        print(f"   Token ID: {token_id}")
        print(f"   Buy size: {buy_size:.2f} tokens")
        print(f"   Profit multiples: {config.SELL_PROFIT_MULTIPLES}")
        
        # Validate inputs
        if not token_id:
            print(f"   ❌ CRITICAL: token_id is None or empty!")
            return []
        
        if buy_size <= 0:
            print(f"   ❌ CRITICAL: buy_size is {buy_size} (must be > 0)")
            return []
        
        placed_orders = []
        failed_orders = []
        
        # Divide position across sell ladder
        size_per_order = buy_size / len(config.SELL_PROFIT_MULTIPLES)
        print(f"   Size per order: {size_per_order:.4f} tokens")
        
        for multiple in config.SELL_PROFIT_MULTIPLES:
            sell_price = buy_price * multiple
            
            # Polymarket prices must be between 0.01 and 0.99
            if sell_price > 0.99:
                sell_price = 0.99
            
            profit_pct = (multiple - 1) * 100
            
            try:
                print(f"\n   📝 Creating sell order @ ${sell_price:.4f} (+{profit_pct:.0f}%)...")
                
                order_args = OrderArgs(
                    price=sell_price,
                    size=size_per_order,
                    side=SELL,
                    token_id=token_id,
                )
                
                print(f"      OrderArgs: price={sell_price}, size={size_per_order:.4f}, side=SELL, token_id={token_id[:16]}...")
                
                signed_order = self.client.create_order(order_args)
                print(f"      Order signed successfully")
                
                response = self.client.post_order(signed_order, OrderType.GTC)
                print(f"      API Response: {json.dumps(response, default=str)[:200]}...")
                
                if response.get("success"):
                    order_id = response.get("orderID", "")
                    print(f"   ✅ Sell @ ${sell_price:.4f} ({size_per_order:.2f} tokens, +{profit_pct:.0f}%) - Order ID: {order_id[:8]}...")
                    
                    order_data = {
                        "market_slug": market_slug,
                        "order_id": order_id,
                        "token_id": token_id,
                        "side": side_name,
                        "order_type": "SELL",
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
                    error = response.get("error", response.get("errorMsg", "Unknown error"))
                    error_code = response.get("errorCode", "N/A")
                    print(f"   ❌ Sell @ ${sell_price:.4f} failed!")
                    print(f"      Error: {error}")
                    print(f"      Error code: {error_code}")
                    print(f"      Full response: {json.dumps(response, default=str)}")
                    failed_orders.append({"price": sell_price, "error": error})
                    
            except Exception as e:
                import traceback
                print(f"   ❌ Sell @ ${sell_price:.4f} exception: {str(e)}")
                print(f"      Traceback: {traceback.format_exc()}")
                failed_orders.append({"price": sell_price, "error": str(e)})
        
        # Summary
        print(f"\n   📊 Sell ladder summary:")
        print(f"      Placed: {len(placed_orders)}/{len(config.SELL_PROFIT_MULTIPLES)}")
        print(f"      Failed: {len(failed_orders)}/{len(config.SELL_PROFIT_MULTIPLES)}")
        
        if failed_orders:
            print(f"      Failed orders:")
            for fo in failed_orders:
                print(f"         - ${fo['price']:.4f}: {fo['error'][:50]}...")
        
        return placed_orders
    
    def check_order_fills(self, orders: List[Dict]) -> List[Dict]:
        """
        Check which orders have been filled
        
        Args:
            orders: List of order dictionaries (must include token_id from database)
            
        Returns:
            List of filled orders with actual filled size, price, AND original token_id preserved
        """
        filled_orders = []
        
        for order in orders:
            order_id = order["order_id"]
            
            # CRITICAL: Preserve token_id from original order (API response doesn't include it)
            original_token_id = order.get("token_id")
            if not original_token_id:
                print(f"   ⚠️  Order {order_id[:8]} missing token_id in database - cannot place sell ladder!")
                continue
            
            try:
                order_status = self.client.get_order(order_id)
                status = order_status.get("status", "").upper()
                
                # Handle both full and partial fills
                if status in ["FILLED", "MATCHED"]:
                    # Get actual filled quantity and average price
                    filled_size = float(order_status.get("size_matched", order["size"]))
                    avg_price = float(order_status.get("avg_price", order["price"]))
                    
                    print(f"   🎯 Order filled: {order['order_type']} {order['side']} @ ${avg_price:.4f} ({filled_size:.2f} tokens)")
                    print(f"      Token ID: {original_token_id[:16]}...")
                    
                    # Build filled order with ALL required fields preserved
                    filled_order = {
                        **order,  # Keep all original fields including token_id
                        "status": "FILLED",
                        "filled_size": filled_size,
                        "filled_price": avg_price,
                        "token_id": original_token_id  # Explicitly ensure token_id is present
                    }
                    filled_orders.append(filled_order)
                    
                elif status == "PARTIAL":
                    # Partial fill - track but don't trigger sell yet
                    filled_size = float(order_status.get("size_matched", 0))
                    print(f"   ⏳ Partial fill: {order['order_type']} {order['side']} ({filled_size:.2f}/{order['size']:.2f})")
                    
            except Exception as e:
                print(f"   ⚠️  Error checking order {order_id[:8]}: {str(e)}")
        
        return filled_orders
    
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
                    
                    # Get market slug from order for summary and notification
                    market_slug = order.get("market_slug", "")
                    if market_slug:
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
                    
                    # Store fill data in order dict
                    order["status"] = "FILLED"
                    order["filled_size"] = filled_size
                    order["filled_price"] = avg_price
                    filled_sells.append(order)
                    
            except Exception as e:
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
