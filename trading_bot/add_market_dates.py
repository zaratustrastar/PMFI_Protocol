"""
Migration script to add market_created_at and market_closed_time columns to trading_jobs table
"""

import psycopg2
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.getenv("DATABASE_URL")

def add_market_date_columns():
    """Add market_created_at and market_closed_time columns to trading_jobs table"""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    try:
        # Check if columns already exist
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'trading_jobs' 
            AND column_name IN ('market_created_at', 'market_closed_time')
        """)
        existing_columns = [row[0] for row in cur.fetchall()]
        
        if 'market_created_at' in existing_columns and 'market_closed_time' in existing_columns:
            print("✅ Columns already exist, no migration needed")
            return
        
        # Add market_created_at column if it doesn't exist
        if 'market_created_at' not in existing_columns:
            cur.execute("""
                ALTER TABLE trading_jobs 
                ADD COLUMN market_created_at TIMESTAMP
            """)
            print("✅ Added market_created_at column")
        
        # Add market_closed_time column if it doesn't exist  
        if 'market_closed_time' not in existing_columns:
            cur.execute("""
                ALTER TABLE trading_jobs 
                ADD COLUMN market_closed_time TIMESTAMP
            """)
            print("✅ Added market_closed_time column")
        
        conn.commit()
        print("✅ Migration complete!")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Migration failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    print("🔄 Running migration to add market date columns...")
    add_market_date_columns()
