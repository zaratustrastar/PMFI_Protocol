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

DATABASE_URL = os.getenv("TRADING_DATABASE_URL", os.getenv("DATABASE_URL"))

def cleanup_old_jobs(nuclear: bool = False):
    """
    Clean up stale trading jobs
    
    Args:
        nuclear: If True, mark ALL pending jobs as expired (fresh start)
                 If False, only mark jobs >1 hour old as expired
    """
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    if nuclear:
        # NUCLEAR: Mark ALL pending jobs as expired
        cur.execute("""
            SELECT COUNT(*) FROM trading_jobs 
            WHERE status = 'PENDING'
        """)
        old_count = cur.fetchone()[0]
        
        print(f"🔴 NUCLEAR CLEANUP: Found {old_count} pending jobs (ALL will be marked EXPIRED)")
        
        if old_count > 0:
            cur.execute("""
                UPDATE trading_jobs
                SET status = 'EXPIRED', 
                    error_message = 'Nuclear cleanup - fresh start',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'PENDING'
            """)
            
            conn.commit()
            print(f"✅ Marked {cur.rowcount} jobs as EXPIRED (nuclear cleanup)")
        else:
            print("✅ No pending jobs found")
    else:
        # NORMAL: Mark jobs >1 hour old as expired
        cur.execute("""
            SELECT COUNT(*) FROM trading_jobs 
            WHERE created_at < NOW() - INTERVAL '1 hour'
            AND status = 'PENDING'
        """)
        old_count = cur.fetchone()[0]
        
        print(f"📊 Found {old_count} stale jobs (>1 hour old)")
        
        if old_count > 0:
            cur.execute("""
                UPDATE trading_jobs
                SET status = 'EXPIRED', 
                    error_message = 'Job expired (>1 hour old)',
                    updated_at = CURRENT_TIMESTAMP
                WHERE created_at < NOW() - INTERVAL '1 hour'
                AND status = 'PENDING'
            """)
            
            conn.commit()
            print(f"✅ Marked {cur.rowcount} stale jobs as EXPIRED")
        else:
            print("✅ No stale jobs to clean up")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    import sys
    
    # Check for --nuclear flag
    nuclear_mode = "--nuclear" in sys.argv
    
    if nuclear_mode:
        print("🧹 NUCLEAR CLEANUP MODE: Marking ALL pending jobs as expired...\n")
        print("⚠️  This will clear the entire queue for a fresh start!\n")
    else:
        print("🧹 Cleaning up stale trading jobs (>1 hour old)...\n")
        print("💡 Use --nuclear flag to mark ALL pending jobs as expired\n")
    
    cleanup_old_jobs(nuclear=nuclear_mode)
    print("\n✅ Cleanup complete!")
