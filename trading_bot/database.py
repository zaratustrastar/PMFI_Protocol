"""
Database schema for tracking trading positions
"""

import psycopg2
from psycopg2.extras import RealDictCursor
import config
from typing import List, Dict, Optional


def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(config.DATABASE_URL)


def init_database():
    """Initialize database tables for trading"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Trading jobs queue - markets waiting to be traded
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trading_jobs (
            id SERIAL PRIMARY KEY,
            market_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            error_message TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
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
        order_data.get("type"),
        order_data.get("price"),
        order_data.get("size"),
        order_data.get("status"),
        order_data.get("buy_price"),
        order_data.get("profit_multiple"),
    ))
    
    conn.commit()
    cur.close()
    conn.close()


def get_open_orders(market_slug: Optional[str] = None, order_type: Optional[str] = None) -> List[Dict]:
    """Get all open orders, optionally filtered by market and/or order type"""
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
    """Get pending trading jobs"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("""
        SELECT id, market_id, created_at
        FROM trading_jobs
        WHERE status = 'PENDING'
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


if __name__ == "__main__":
    init_database()
