#!/usr/bin/env python3
"""
Backfill Accumulated Fills Script

This script:
1. Creates the accumulated_fills table if it doesn't exist
2. Backfills from all FILLED buy orders in trading_positions
3. Identifies markets with ≥5 accumulated shares that need sell orders
4. Places sell orders for those markets

Run this once on production to fix missing sell orders.
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ Loaded environment variables from .env file")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment variables")

import psycopg2
from psycopg2.extras import RealDictCursor
from decimal import Decimal

DATABASE_URL = os.getenv("TRADING_DATABASE_URL", os.getenv("DATABASE_URL"))
if not DATABASE_URL:
    print("❌ TRADING_DATABASE_URL / DATABASE_URL not set!")
    sys.exit(1)

# Import from centralized config
try:
    from config import MIN_SHARES_PER_ORDER
    MIN_SHARES_FOR_SELL = MIN_SHARES_PER_ORDER
except ImportError:
    MIN_SHARES_FOR_SELL = 5  # Fallback to Polymarket minimum

SELL_SHARES = 5
SELL_PROFIT_MULTIPLE = 3.0
DRY_RUN = "--dry-run" in sys.argv


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def create_accumulated_fills_table():
    """Create the accumulated_fills table if it doesn't exist"""
    print("\n📊 Step 1: Creating accumulated_fills table...")
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accumulated_fills (
            id SERIAL PRIMARY KEY,
            market_slug TEXT NOT NULL,
            token_id TEXT NOT NULL,
            side TEXT NOT NULL,
            total_shares DECIMAL(18, 6) DEFAULT 0,
            total_cost DECIMAL(18, 6) DEFAULT 0,
            avg_buy_price DECIMAL(10, 6) DEFAULT 0,
            sell_placed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(market_slug, token_id, side)
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    
    print("   ✅ Table created/verified")


def backfill_from_filled_orders():
    """Backfill accumulated_fills from all FILLED buy orders"""
    print("\n📊 Step 2: Backfilling from FILLED buy orders...")
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT 
            market_slug,
            token_id,
            side,
            SUM(size) as total_shares,
            SUM(size * price) as total_cost,
            AVG(price) as avg_price,
            COUNT(*) as order_count
        FROM trading_positions
        WHERE order_type = 'BUY' 
          AND status = 'FILLED'
          AND token_id IS NOT NULL
          AND token_id != ''
        GROUP BY market_slug, token_id, side
    """)
    
    filled_groups = cur.fetchall()
    print(f"   Found {len(filled_groups)} token groups from FILLED buy orders")
    
    backfilled = 0
    for group in filled_groups:
        market_slug = group['market_slug']
        token_id = group['token_id']
        side = group['side']
        total_shares = float(group['total_shares'])
        total_cost = float(group['total_cost'])
        avg_price = total_cost / total_shares if total_shares > 0 else 0
        
        cur.execute("""
            SELECT id, total_shares, sell_placed 
            FROM accumulated_fills 
            WHERE market_slug = %s AND token_id = %s AND side = %s
        """, (market_slug, token_id, side))
        
        existing = cur.fetchone()
        
        if existing:
            if existing['sell_placed']:
                print(f"   ⏭️  {market_slug} {side}: Already has sell placed, skipping")
                continue
            else:
                cur.execute("""
                    UPDATE accumulated_fills 
                    SET total_shares = %s, total_cost = %s, avg_buy_price = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE market_slug = %s AND token_id = %s AND side = %s
                """, (total_shares, total_cost, avg_price, market_slug, token_id, side))
                print(f"   🔄 Updated: {market_slug} {side} = {total_shares:.2f} shares @ ${avg_price:.4f}")
        else:
            cur.execute("""
                INSERT INTO accumulated_fills (market_slug, token_id, side, total_shares, total_cost, avg_buy_price)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (market_slug, token_id, side, total_shares, total_cost, avg_price))
            print(f"   ➕ Inserted: {market_slug} {side} = {total_shares:.2f} shares @ ${avg_price:.4f}")
        
        backfilled += 1
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"   ✅ Backfilled {backfilled} token groups")


def find_markets_needing_sells():
    """Find markets with ≥5 shares that haven't had sells placed"""
    print(f"\n📊 Step 3: Finding markets with ≥{MIN_SHARES_FOR_SELL} shares needing sells...")
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT market_slug, token_id, side, total_shares, avg_buy_price
        FROM accumulated_fills
        WHERE total_shares >= %s
          AND sell_placed = FALSE
        ORDER BY total_shares DESC
    """, (MIN_SHARES_FOR_SELL,))
    
    markets = cur.fetchall()
    
    cur.close()
    conn.close()
    
    print(f"   Found {len(markets)} markets needing sell orders:")
    for m in markets:
        print(f"      - {m['market_slug']} {m['side']}: {float(m['total_shares']):.2f} shares @ ${float(m['avg_buy_price']):.4f}")
    
    return markets


def place_sell_orders(markets_needing_sells):
    """Place sell orders for markets that need them"""
    print(f"\n📊 Step 4: Placing sell orders...")
    
    if DRY_RUN:
        print("   🔸 DRY RUN MODE - No orders will be placed")
    
    if not markets_needing_sells:
        print("   ✅ No markets need sell orders")
        return
    
    from polymarket_trader import PolymarketTrader
    
    trader = PolymarketTrader()
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    placed = 0
    for market in markets_needing_sells:
        market_slug = market['market_slug']
        token_id = market['token_id']
        side = market['side']
        total_shares = float(market['total_shares'])
        avg_buy_price = float(market['avg_buy_price'])
        
        sell_price = avg_buy_price * SELL_PROFIT_MULTIPLE
        if sell_price > 0.99:
            sell_price = 0.99
        if sell_price < 0.01:
            sell_price = 0.01
        
        print(f"\n   📈 {market_slug} {side}:")
        print(f"      Shares: {total_shares:.2f} (selling {SELL_SHARES})")
        print(f"      Avg buy: ${avg_buy_price:.4f}")
        print(f"      Sell @ ${sell_price:.4f} ({SELL_PROFIT_MULTIPLE}x)")
        
        if DRY_RUN:
            print(f"      🔸 [DRY RUN] Would place sell order")
            continue
        
        try:
            sell_orders = trader.place_sell_order(
                token_id,
                avg_buy_price,
                SELL_SHARES,
                side,
                market_slug
            )
            
            if sell_orders and len(sell_orders) > 0:
                print(f"      ✅ Sell order placed!")
                
                cur.execute("""
                    UPDATE accumulated_fills 
                    SET sell_placed = TRUE, updated_at = CURRENT_TIMESTAMP
                    WHERE market_slug = %s AND token_id = %s AND side = %s
                """, (market_slug, token_id, side))
                conn.commit()
                
                placed += 1
            else:
                print(f"      ❌ Sell order failed")
                
        except Exception as e:
            print(f"      ❌ Error: {e}")
    
    cur.close()
    conn.close()
    
    print(f"\n   ✅ Placed {placed} sell orders")


def main():
    print("=" * 60)
    print("🔧 BACKFILL ACCUMULATED FILLS SCRIPT")
    print("=" * 60)
    
    if DRY_RUN:
        print("\n🔸 DRY RUN MODE - No changes will be made to orders")
    
    create_accumulated_fills_table()
    
    backfill_from_filled_orders()
    
    markets = find_markets_needing_sells()
    
    if markets:
        if DRY_RUN:
            print("\n💡 Run without --dry-run to place actual sell orders")
        else:
            place_sell_orders(markets)
    
    print("\n" + "=" * 60)
    print("✅ BACKFILL COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
