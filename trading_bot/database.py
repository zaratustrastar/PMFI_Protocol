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


def get_open_orders(market_slug: Optional[str] = None) -> List[Dict]:
    """Get all open orders, optionally filtered by market"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if market_slug:
        cur.execute("""
            SELECT * FROM trading_positions 
            WHERE status = 'OPEN' AND market_slug = %s
            ORDER BY created_at DESC
        """, (market_slug,))
    else:
        cur.execute("""
            SELECT * FROM trading_positions 
            WHERE status = 'OPEN'
            ORDER BY created_at DESC
        """)
    
    orders = cur.fetchall()
    cur.close()
    conn.close()
    
    return [dict(row) for row in orders]


def update_order_status(order_id: str, status: str):
    """Update order status"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        UPDATE trading_positions 
        SET status = %s, updated_at = CURRENT_TIMESTAMP
        WHERE order_id = %s
    """, (status, order_id))
    
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    init_database()
