"""
Database schema for tracking trading positions
"""

import psycopg2
from psycopg2.extras import RealDictCursor
import config
import time
from typing import List, Dict, Optional

MAX_RETRIES = 3
RETRY_DELAY = 2


def get_db_connection(retries=MAX_RETRIES):
    """Get database connection with retry logic for transient failures"""
    db_url = config.DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL / TRADING_DATABASE_URL is not set - cannot connect to database")
    last_error = None
    for attempt in range(retries):
        try:
            return psycopg2.connect(db_url)
        except psycopg2.OperationalError as e:
            last_error = e
            if attempt < retries - 1:
                wait = RETRY_DELAY * (attempt + 1)
                print(f"⚠️ DB connection attempt {attempt + 1}/{retries} failed, retrying in {wait}s...")
                time.sleep(wait)
    raise last_error


def init_database():
    """Initialize database tables for trading"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Trading jobs queue - markets waiting to be traded (with multi-market support)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading_jobs (
            id SERIAL PRIMARY KEY,
            market_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            error_message TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            market_created_at TIMESTAMP,
            market_closed_time TIMESTAMP,
            event_slug TEXT,
            question TEXT,
            clob_token_ids TEXT,
            outcomes TEXT
        )
    """)
    
    # Migration: add new columns for existing tables
    try:
        cur.execute("""
            ALTER TABLE trading_jobs 
            ADD COLUMN IF NOT EXISTS event_slug TEXT,
            ADD COLUMN IF NOT EXISTS question TEXT,
            ADD COLUMN IF NOT EXISTS clob_token_ids TEXT,
            ADD COLUMN IF NOT EXISTS outcomes TEXT
        """)
    except Exception:
        pass
    
    # Trading positions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading_positions (
            id SERIAL PRIMARY KEY,
            market_slug TEXT NOT NULL,
            market_question TEXT,
            order_id TEXT UNIQUE NOT NULL,
            token_id TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            price DECIMAL(10, 6) NOT NULL,
            size DECIMAL(18, 6) NOT NULL,
            status TEXT NOT NULL,
            buy_price DECIMAL(10, 6),
            profit_multiple DECIMAL(10, 2),
            filled_at TIMESTAMP,
            accumulated BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Migration: add accumulated column if it doesn't exist
    try:
        cur.execute("ALTER TABLE trading_positions ADD COLUMN IF NOT EXISTS accumulated BOOLEAN DEFAULT FALSE")
    except Exception:
        pass
    
    # Migration: add accumulated_amount column for tracking partial fills
    try:
        cur.execute("ALTER TABLE trading_positions ADD COLUMN IF NOT EXISTS accumulated_amount DECIMAL(18, 6) DEFAULT 0")
    except Exception:
        pass
    
    # Trading summary table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading_summary (
            id SERIAL PRIMARY KEY,
            market_slug TEXT NOT NULL UNIQUE,
            total_buys INTEGER DEFAULT 0,
            total_sells INTEGER DEFAULT 0,
            filled_buys INTEGER DEFAULT 0,
            filled_sells INTEGER DEFAULT 0,
            total_invested DECIMAL(18, 2) DEFAULT 0,
            total_returned DECIMAL(18, 2) DEFAULT 0,
            realized_pnl DECIMAL(18, 2) DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Accumulated fills table - tracks total filled shares per token for sell threshold
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
            shares_with_sells DECIMAL(18, 6) DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(market_slug, token_id, side)
        )
    """)
    
    # Migration: add shares_with_sells column if missing (existing installs)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'accumulated_fills' AND column_name = 'shares_with_sells'
            ) THEN
                ALTER TABLE accumulated_fills ADD COLUMN shares_with_sells DECIMAL(18, 6) DEFAULT 0;
                UPDATE accumulated_fills SET shares_with_sells = total_shares WHERE sell_placed = TRUE;
            END IF;
        END $$;
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    
    print("✅ Database tables initialized")


def save_order(order_data: Dict):
    """Save an order to the database"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        INSERT INTO trading_positions 
        (market_slug, order_id, token_id, side, order_type, price, size, status, buy_price, profit_multiple)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (order_id) DO UPDATE
        SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP
    """, (
        order_data.get("market_slug"),
        order_data.get("order_id"),
        order_data.get("token_id"),
        order_data.get("side"),
        order_data.get("order_type"),
        order_data.get("price"),
        order_data.get("size"),
        order_data.get("status"),
        order_data.get("buy_price"),
        order_data.get("profit_multiple"),
    ))
    
    conn.commit()
    cur.close()
    conn.close()


def get_open_orders(market_slug: Optional[str] = None, order_type: Optional[str] = None, max_age_hours: Optional[int] = 24) -> List[Dict]:
    """
    Get all open orders, optionally filtered by market and/or order type
    
    Args:
        market_slug: Filter by specific market
        order_type: Filter by order type (BUY/SELL)
        max_age_hours: Only return orders created within last N hours (default 24, None for no limit)
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Build WHERE clause based on filters
    conditions = ["status = 'OPEN'"]
    params = []
    
    if market_slug:
        conditions.append("market_slug = %s")
        params.append(market_slug)
    
    if order_type:
        conditions.append("order_type = %s")
        params.append(order_type)
    
    if max_age_hours is not None:
        conditions.append("created_at > NOW() - (%s * INTERVAL '1 hour')")
        params.append(max_age_hours)
    
    where_clause = " AND ".join(conditions)
    
    cur.execute(f"""
        SELECT * FROM trading_positions 
        WHERE {where_clause}
        ORDER BY created_at DESC
    """, tuple(params))
    
    orders = cur.fetchall()
    cur.close()
    conn.close()
    
    return [dict(row) for row in orders]


def update_order_status(order_id: str, status: str, filled_size: float = None, filled_price: float = None):
    """Update order status with actual fill data"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    if filled_size is not None and filled_price is not None:
        cur.execute("""
            UPDATE trading_positions 
            SET status = %s, 
                size = %s,
                price = %s,
                filled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
        """, (status, filled_size, filled_price, order_id))
    else:
        cur.execute("""
            UPDATE trading_positions 
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
        """, (status, order_id))
    
    conn.commit()
    cur.close()
    conn.close()


def mark_order_accumulated(order_id: str) -> bool:
    """
    Mark an order as having been accumulated (added to accumulated_fills).
    Uses atomic update to prevent double-counting.
    
    Returns:
        True if order was marked (first time), False if already accumulated
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Atomic update - only succeeds if accumulated is currently FALSE
    cur.execute("""
        UPDATE trading_positions 
        SET accumulated = TRUE, updated_at = CURRENT_TIMESTAMP
        WHERE order_id = %s AND (accumulated = FALSE OR accumulated IS NULL)
        RETURNING order_id
    """, (order_id,))
    
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return result is not None


def is_order_accumulated(order_id: str) -> bool:
    """Check if an order has already been accumulated"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT accumulated FROM trading_positions WHERE order_id = %s
    """, (order_id,))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    if result:
        return result[0] == True
    return False


def get_order_accumulated_amount(order_id: str) -> float:
    """Get the amount already accumulated from this order (for partial fills)"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT COALESCE(accumulated_amount, 0) FROM trading_positions WHERE order_id = %s
    """, (order_id,))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    return float(result[0]) if result else 0.0


def update_order_accumulated_amount(order_id: str, new_amount: float, is_fully_filled: bool = False) -> bool:
    """
    Update the accumulated amount for an order (for tracking partial fills).
    
    Args:
        order_id: The order ID
        new_amount: The new total accumulated amount
        is_fully_filled: If True, also marks the order as fully accumulated
        
    Returns:
        True if update succeeded
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    if is_fully_filled:
        cur.execute("""
            UPDATE trading_positions 
            SET accumulated_amount = %s, accumulated = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
            RETURNING order_id
        """, (new_amount, order_id))
    else:
        cur.execute("""
            UPDATE trading_positions 
            SET accumulated_amount = %s, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
            RETURNING order_id
        """, (new_amount, order_id))
    
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return result is not None


def update_market_summary(market_slug: str):
    """
    Update trading summary for a market based on filled orders
    
    Args:
        market_slug: Market identifier
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Count filled buys and sells
    cur.execute("""
        SELECT 
            COUNT(*) FILTER (WHERE order_type = 'BUY' AND status = 'FILLED') as filled_buys,
            COUNT(*) FILTER (WHERE order_type = 'SELL' AND status = 'FILLED') as filled_sells,
            SUM(price * size) FILTER (WHERE order_type = 'BUY' AND status = 'FILLED') as total_invested,
            SUM(price * size) FILTER (WHERE order_type = 'SELL' AND status = 'FILLED') as total_returned
        FROM trading_positions
        WHERE market_slug = %s
    """, (market_slug,))
    
    row = cur.fetchone()
    if row:
        filled_buys = row[0] or 0
        filled_sells = row[1] or 0
        total_invested = float(row[2] or 0)
        total_returned = float(row[3] or 0)
        realized_pnl = total_returned - total_invested
        
        # Upsert summary
        cur.execute("""
            INSERT INTO trading_summary (market_slug, filled_buys, filled_sells, total_invested, total_returned, realized_pnl)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (market_slug) 
            DO UPDATE SET
                filled_buys = EXCLUDED.filled_buys,
                filled_sells = EXCLUDED.filled_sells,
                total_invested = EXCLUDED.total_invested,
                total_returned = EXCLUDED.total_returned,
                realized_pnl = EXCLUDED.realized_pnl,
                updated_at = CURRENT_TIMESTAMP
        """, (market_slug, filled_buys, filled_sells, total_invested, total_returned, realized_pnl))
    
    conn.commit()
    cur.close()
    conn.close()


def get_open_sell_orders(market_slug: str) -> list:
    """Get all open sell orders for a market"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT order_id, token_id, side, price, size, buy_price, profit_multiple
        FROM trading_positions
        WHERE market_slug = %s AND order_type = 'SELL' AND status = 'OPEN'
        ORDER BY created_at DESC
    """, (market_slug,))
    
    orders = cur.fetchall()
    cur.close()
    conn.close()
    
    return [dict(row) for row in orders]


def queue_trading_job(market_id: str):
    """Queue a new market for trading"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        INSERT INTO trading_jobs (market_id, status)
        VALUES (%s, 'PENDING')
        ON CONFLICT (market_id) DO NOTHING
    """, (market_id,))
    
    conn.commit()
    cur.close()
    conn.close()


def get_pending_jobs(limit: int = 10) -> List[Dict]:
    """Get pending trading jobs (only jobs created within last 1 hour for fresh market trading)"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT id, market_id, created_at, market_created_at, market_closed_time,
               event_slug, question, clob_token_ids, outcomes
        FROM trading_jobs
        WHERE status = 'PENDING'
          AND created_at > NOW() - INTERVAL '1 hour'
        ORDER BY created_at ASC
        LIMIT %s
    """, (limit,))
    
    jobs = cur.fetchall()
    cur.close()
    conn.close()
    
    return [dict(row) for row in jobs]


def start_trading_job(job_id: int):
    """Mark a job as started"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        UPDATE trading_jobs
        SET status = 'RUNNING', started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (job_id,))
    
    conn.commit()
    cur.close()
    conn.close()


def complete_trading_job(job_id: int, error_message: str = None):
    """Mark a job as completed or failed"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    status = 'FAILED' if error_message else 'COMPLETED'
    
    cur.execute("""
        UPDATE trading_jobs
        SET status = %s, error_message = %s, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (status, error_message, job_id))
    
    conn.commit()
    cur.close()
    conn.close()


def add_accumulated_fill(market_slug: str, token_id: str, side: str, filled_shares: float, filled_price: float) -> Dict:
    """
    Add filled shares to the accumulated total for a token.
    Updates total shares, total cost, and recalculates average buy price.
    
    Args:
        market_slug: Market identifier
        token_id: Token ID
        side: YES or NO
        filled_shares: Number of shares filled in this order
        filled_price: Price per share for this fill
        
    Returns:
        Dict with current accumulated state including whether sell threshold is reached
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    fill_cost = filled_shares * filled_price
    
    # Upsert accumulated fill data
    cur.execute("""
        INSERT INTO accumulated_fills (market_slug, token_id, side, total_shares, total_cost, avg_buy_price)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (market_slug, token_id, side) 
        DO UPDATE SET
            total_shares = accumulated_fills.total_shares + EXCLUDED.total_shares,
            total_cost = accumulated_fills.total_cost + EXCLUDED.total_cost,
            avg_buy_price = (accumulated_fills.total_cost + EXCLUDED.total_cost) / 
                           NULLIF(accumulated_fills.total_shares + EXCLUDED.total_shares, 0),
            updated_at = CURRENT_TIMESTAMP
        RETURNING *
    """, (market_slug, token_id, side, filled_shares, fill_cost, filled_price))
    
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return dict(result) if result else {}


def get_accumulated_fill(market_slug: str, token_id: str, side: str) -> Optional[Dict]:
    """
    Get accumulated fill data for a specific token.
    
    Returns:
        Dict with total_shares, avg_buy_price, sell_placed, etc. or None
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT * FROM accumulated_fills
        WHERE market_slug = %s AND token_id = %s AND side = %s
    """, (market_slug, token_id, side))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    return dict(result) if result else None


def mark_sell_placed(market_slug: str, token_id: str, side: str, shares_covered: float = None) -> bool:
    """
    Mark that sell orders have been placed for this accumulated fill.
    Records how many shares have sell orders so incremental fills can be detected.
    
    Args:
        market_slug: Market identifier
        token_id: Token ID
        side: YES or NO
        shares_covered: Number of shares that now have sell orders placed.
                       If None, uses current total_shares.
    
    Returns:
        True if successfully marked, False otherwise
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    if shares_covered is not None:
        cur.execute("""
            UPDATE accumulated_fills
            SET sell_placed = TRUE, shares_with_sells = %s, updated_at = CURRENT_TIMESTAMP
            WHERE market_slug = %s AND token_id = %s AND side = %s
        """, (shares_covered, market_slug, token_id, side))
    else:
        cur.execute("""
            UPDATE accumulated_fills
            SET sell_placed = TRUE, shares_with_sells = total_shares, updated_at = CURRENT_TIMESTAMP
            WHERE market_slug = %s AND token_id = %s AND side = %s
        """, (market_slug, token_id, side))
    
    affected = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    
    return affected > 0


def check_sell_threshold(market_slug: str, token_id: str, side: str, min_shares: float = 5) -> Dict:
    """
    Check if accumulated fills have reached the sell threshold.
    
    Args:
        market_slug: Market identifier
        token_id: Token ID
        side: YES or NO
        min_shares: Minimum shares needed to trigger sell (default 5, Polymarket minimum)
        
    Returns:
        Dict with:
            - threshold_reached: bool - True if >= min_shares accumulated
            - sell_already_placed: bool - True if sell was already placed
            - total_shares: float - Current accumulated shares
            - avg_buy_price: float - Average buy price across all fills
            - ready_to_sell: bool - True if threshold reached AND sell not placed yet
    """
    fill_data = get_accumulated_fill(market_slug, token_id, side)
    
    if not fill_data:
        return {
            "threshold_reached": False,
            "sell_already_placed": False,
            "total_shares": 0,
            "avg_buy_price": 0,
            "ready_to_sell": False
        }
    
    total_shares = float(fill_data.get("total_shares", 0))
    avg_price = float(fill_data.get("avg_buy_price", 0))
    sell_placed = fill_data.get("sell_placed", False)
    
    threshold_reached = total_shares >= min_shares
    ready_to_sell = threshold_reached and not sell_placed
    
    return {
        "threshold_reached": threshold_reached,
        "sell_already_placed": sell_placed,
        "total_shares": total_shares,
        "avg_buy_price": avg_price,
        "ready_to_sell": ready_to_sell
    }


def get_all_sells_placed() -> Dict[str, Dict]:
    """
    Get a lookup dict of all accumulated fills keyed by (token_id, side).
    Used by order_monitor_v2 for quick comparison against portfolio positions.
    
    Returns:
        Dict keyed by "token_id|side" with fill data including sell_placed status.
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT token_id, side, sell_placed, total_shares, avg_buy_price, market_slug,
               COALESCE(shares_with_sells, 0) as shares_with_sells
        FROM accumulated_fills
    """)
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    result = {}
    for row in rows:
        key = f"{row['token_id']}|{row['side']}"
        result[key] = {
            "sell_placed": row.get("sell_placed", False),
            "total_shares": float(row.get("total_shares", 0)),
            "avg_buy_price": float(row.get("avg_buy_price", 0)),
            "shares_with_sells": float(row.get("shares_with_sells", 0)),
            "side": row["side"],
            "market_slug": row["market_slug"],
            "token_id": row["token_id"],
        }
    
    return result


def upsert_accumulated_fill(market_slug: str, token_id: str, side: str, total_shares: float, avg_buy_price: float) -> Dict:
    """
    Insert or update accumulated fill with absolute values (not incremental).
    Used by order_monitor_v2 to sync DB with Data API position data.
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    total_cost = total_shares * avg_buy_price
    
    cur.execute("""
        INSERT INTO accumulated_fills (market_slug, token_id, side, total_shares, total_cost, avg_buy_price)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (market_slug, token_id, side) 
        DO UPDATE SET
            total_shares = EXCLUDED.total_shares,
            total_cost = EXCLUDED.total_cost,
            avg_buy_price = EXCLUDED.avg_buy_price,
            updated_at = CURRENT_TIMESTAMP
        RETURNING *
    """, (market_slug, token_id, side, total_shares, total_cost, avg_buy_price))
    
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return dict(result) if result else {}


if __name__ == "__main__":
    init_database()
