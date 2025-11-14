"""
Cleanup script to remove stale trading jobs from November 13
"""

import psycopg2
from datetime import datetime, timezone
import os

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.getenv("DATABASE_URL")

def cleanup_old_jobs():
    """Delete jobs older than 24 hours"""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # Get current UTC time
    now = datetime.now(timezone.utc)
    
    # Count old jobs
    cur.execute("""
        SELECT COUNT(*) FROM trading_jobs 
        WHERE created_at < NOW() - INTERVAL '24 hours'
        AND status = 'PENDING'
    """)
    old_count = cur.fetchone()[0]
    
    print(f"📊 Found {old_count} stale jobs (>24 hours old)")
    
    if old_count > 0:
        # Delete old jobs
        cur.execute("""
            UPDATE trading_jobs
            SET status = 'EXPIRED', 
                error_message = 'Job expired (>24 hours old)',
                updated_at = CURRENT_TIMESTAMP
            WHERE created_at < NOW() - INTERVAL '24 hours'
            AND status = 'PENDING'
        """)
        
        conn.commit()
        print(f"✅ Marked {cur.rowcount} stale jobs as EXPIRED")
    else:
        print("✅ No stale jobs to clean up")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    print("🧹 Cleaning up stale trading jobs...\n")
    cleanup_old_jobs()
    print("\n✅ Cleanup complete!")
