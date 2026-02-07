"""
Order Monitor V2 - Portfolio-based position monitoring

Instead of checking thousands of individual orders via the CLOB API,
this monitor fetches all positions from the Polymarket Data API in a
single call every 1 minute and places sell ladders for any positions
that don't have sell orders yet.

Stale buy orders (>12h old) are cancelled every 30 minutes.
"""

import os
import time
import requests
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ Loaded environment variables from .env file")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment variables")

import config
from polymarket_trader import PolymarketTrader
from database import (
    get_all_sells_placed,
    upsert_accumulated_fill,
    mark_sell_placed,
    get_open_orders,
    update_order_status,
)

DATA_API_URL = "https://data-api.polymarket.com/positions"
MIN_SHARES_FOR_SELL = config.MIN_SHARES_PER_ORDER
POSITION_CHECK_INTERVAL = 60
STALE_ORDER_INTERVAL = 1800
STALE_ORDER_MAX_AGE_HOURS = 12

PROXY_URL = os.getenv("PROXY_URL", "")


def get_proxy_config():
    """Build requests proxies dict from PROXY_URL env var."""
    if PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def fetch_portfolio_positions(proxy_address: str) -> list:
    """
    Fetch all current positions from Polymarket Data API.
    Single API call, no auth required.
    """
    try:
        params = {
            "user": proxy_address,
            "sizeThreshold": MIN_SHARES_FOR_SELL,
        }
        proxies = get_proxy_config()
        resp = requests.get(DATA_API_URL, params=params, proxies=proxies, timeout=30)
        resp.raise_for_status()
        positions = resp.json()
        print(f"📊 Fetched {len(positions)} positions from Data API")
        return positions
    except Exception as e:
        print(f"❌ Error fetching portfolio: {e}")
        return []


def process_positions(trader: PolymarketTrader, positions: list):
    """
    Compare portfolio positions against accumulated_fills DB.
    Place sell ladders for positions that don't have sells yet.
    Uses composite key (token_id|side) for unambiguous lookup.
    """
    sells_lookup = get_all_sells_placed()

    new_sells = 0
    skipped = 0

    for pos in positions:
        token_id = pos.get("asset", "")
        size = float(pos.get("size", 0))
        avg_price = float(pos.get("avgPrice", 0))
        slug = pos.get("slug", "unknown")
        outcome = pos.get("outcome", "YES")
        side = outcome.upper()

        if size < MIN_SHARES_FOR_SELL:
            continue

        lookup_key = f"{token_id}|{side}"
        db_entry = sells_lookup.get(lookup_key)

        if db_entry and db_entry.get("sell_placed"):
            skipped += 1
            continue

        print(f"\n🆕 Position needs sell ladder:")
        print(f"   Market: {slug}")
        print(f"   Side: {side}")
        print(f"   Size: {size:.2f} shares @ avg ${avg_price:.4f}")
        print(f"   Token: {token_id[:20]}...")

        print(f"   📝 Syncing position to accumulated_fills DB...")
        upsert_accumulated_fill(slug, token_id, side, size, avg_price)

        try:
            sell_orders = trader.place_sell_ladder(
                token_id=token_id,
                buy_price=avg_price,
                buy_size=size,
                side_name=side,
                market_slug=slug,
            )

            if sell_orders and len(sell_orders) > 0:
                print(f"   ✅ Placed {len(sell_orders)} sell order(s)")
                mark_sell_placed(slug, token_id, side)
                new_sells += 1
            else:
                print(f"   ⚠️  No sell orders placed (position may be too small for ladder)")

        except Exception as e:
            print(f"   ❌ Sell ladder error: {e}")

    print(f"\n📈 Summary: {new_sells} new sell ladder(s) placed, {skipped} already have sells")


def cancel_stale_orders(trader: PolymarketTrader):
    """
    Cancel BUY orders older than STALE_ORDER_MAX_AGE_HOURS.
    SELL orders are never cancelled.
    """
    all_open_buys = get_open_orders(order_type="BUY", max_age_hours=None)

    if not all_open_buys:
        print("🔍 No open buy orders to check for staleness")
        return

    now = datetime.now()
    stale_cutoff = now - timedelta(hours=STALE_ORDER_MAX_AGE_HOURS)
    cancelled_count = 0

    for order in all_open_buys:
        created_at = order.get("created_at")
        if not created_at:
            continue

        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))

        if created_at < stale_cutoff:
            order_id = order["order_id"]
            order_age_hours = (now - created_at).total_seconds() / 3600

            print(f"   ⏰ Cancelling stale buy order (age: {order_age_hours:.1f}h)")
            print(f"      {order['side']} @ ${order['price']:.4f} - {order['market_slug']}")

            try:
                if trader.cancel_order(order_id):
                    update_order_status(order_id, "CANCELLED")
                    cancelled_count += 1
                else:
                    print(f"      ⚠️  Cancellation failed")
            except Exception as e:
                print(f"      ⚠️  Cancel error: {e}")

    if cancelled_count > 0:
        print(f"🗑️  Cancelled {cancelled_count} stale buy order(s)")
    else:
        print(f"✅ No stale buy orders to cancel")


def main():
    print("\n👁️  Order Monitor V2 (Portfolio-based) Starting...")
    print(f"   Mode: Fetch positions from Data API every {POSITION_CHECK_INTERVAL}s")
    print(f"   Sell threshold: {MIN_SHARES_FOR_SELL} shares minimum")
    print(f"   Stale order cleanup: every {STALE_ORDER_INTERVAL}s (>{STALE_ORDER_MAX_AGE_HOURS}h old)")
    print(f"   Proxy address: {config.PROXY_ADDRESS[:10]}...")
    if PROXY_URL:
        proxy_display = PROXY_URL.split('@')[1] if '@' in PROXY_URL else PROXY_URL
        print(f"   Proxy URL: {proxy_display}")
    else:
        print(f"   Proxy URL: None (direct connection)")
    print()

    trader = PolymarketTrader()

    last_stale_check = 0

    print("🔄 Starting monitor loop (press Ctrl+C to stop)\n")

    try:
        while True:
            now = time.time()

            try:
                positions = fetch_portfolio_positions(config.PROXY_ADDRESS)
                if positions:
                    process_positions(trader, positions)
                else:
                    print("📭 No positions found (or API error)")
            except Exception as e:
                print(f"⚠️  Error in position check: {e}")

            if now - last_stale_check >= STALE_ORDER_INTERVAL:
                print(f"\n🧹 Running stale order cleanup...")
                try:
                    cancel_stale_orders(trader)
                except Exception as e:
                    print(f"⚠️  Error cancelling stale orders: {e}")
                last_stale_check = now

            print(f"\n⏳ Next check in {POSITION_CHECK_INTERVAL}s...")
            time.sleep(POSITION_CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n⏸️  Monitor V2 stopped by user")
    except Exception as e:
        print(f"\n\n❌ Monitor V2 crashed: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
