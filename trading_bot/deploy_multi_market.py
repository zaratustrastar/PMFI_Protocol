#!/usr/bin/env python3
"""
Multi-Market Trading Update Deployment Script
Run this on your VPS to apply the update.

Usage:
    cd /opt/polymarket-bot/trading_bot
    source venv/bin/activate
    python3 deploy_multi_market.py
"""

import os
import sys
import shutil
from datetime import datetime

# Configuration
BASE_DIR = "/opt/polymarket-bot/trading_bot"
BACKUP_DIR = f"/opt/polymarket-bot/backups/{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def backup_files():
    """Backup all files before patching"""
    files = [
        "market_monitor.py",
        "market_utils.py", 
        "database.py",
        "auto_trader.py",
        "polymarket_trader.py"
    ]
    
    os.makedirs(BACKUP_DIR, exist_ok=True)
    print(f"📦 Backing up files to {BACKUP_DIR}")
    
    for f in files:
        src = os.path.join(BASE_DIR, f)
        dst = os.path.join(BACKUP_DIR, f)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"   ✅ {f}")
    
    print()

def patch_market_utils():
    """Add get_market_info_from_job function to market_utils.py"""
    filepath = os.path.join(BASE_DIR, "market_utils.py")
    
    new_function = '''
def get_market_info_from_job(job):
    """
    Get market info directly from job data (no API call needed).
    """
    import json as _json
    
    condition_id = job.get("market_id", "")
    clob_token_ids_str = job.get("clob_token_ids", "[]")
    outcomes_str = job.get("outcomes", "[]")
    question = job.get("question", "")
    event_slug = job.get("event_slug", "")
    
    if not condition_id:
        print(f"❌ No condition_id in job")
        return None
    
    try:
        clob_token_ids = _json.loads(clob_token_ids_str) if clob_token_ids_str else []
    except:
        clob_token_ids = []
    
    try:
        outcomes = _json.loads(outcomes_str) if outcomes_str else []
    except:
        outcomes = []
    
    if len(clob_token_ids) < 2:
        print(f"❌ Missing token IDs for {question[:40]}...")
        return None
    
    return {
        "slug": event_slug,
        "question": question,
        "market_id": condition_id,
        "condition_id": condition_id,
        "yes_token_id": clob_token_ids[0],
        "no_token_id": clob_token_ids[1],
        "outcomes": outcomes,
        "active": True,
        "closed": False,
    }

'''
    
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Check if already patched
    if 'get_market_info_from_job' in content:
        print("✅ market_utils.py already patched")
        return
    
    # Add after imports
    import_end = content.find('\n\ndef ')
    if import_end == -1:
        import_end = content.find('\ndef ')
    
    new_content = content[:import_end] + new_function + content[import_end:]
    
    with open(filepath, 'w') as f:
        f.write(new_content)
    
    print("✅ market_utils.py patched (added get_market_info_from_job)")

def patch_database():
    """Update database.py to include new columns"""
    filepath = os.path.join(BASE_DIR, "database.py")
    
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Patch 1: Add new columns to CREATE TABLE
    old_create = '''market_created_at TIMESTAMP,
            market_closed_time TIMESTAMP
        )
    """)'''
    
    new_create = '''market_created_at TIMESTAMP,
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
        pass'''
    
    if 'event_slug TEXT' not in content:
        content = content.replace(old_create, new_create)
        print("✅ database.py patched (added new columns to schema)")
    else:
        print("✅ database.py already has new columns")
    
    # Patch 2: Update get_pending_jobs to fetch new columns
    old_select = '''SELECT id, market_id, created_at, market_created_at, market_closed_time
        FROM trading_jobs'''
    
    new_select = '''SELECT id, market_id, created_at, market_created_at, market_closed_time,
               event_slug, question, clob_token_ids, outcomes
        FROM trading_jobs'''
    
    if 'event_slug, question, clob_token_ids' not in content:
        content = content.replace(old_select, new_select)
        print("✅ database.py patched (updated get_pending_jobs)")
    
    with open(filepath, 'w') as f:
        f.write(content)

def patch_polymarket_trader():
    """Add place_orders_only_from_job method and update import"""
    filepath = os.path.join(BASE_DIR, "polymarket_trader.py")
    
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Patch 1: Update import
    old_import = 'from market_utils import get_market_info'
    new_import = 'from market_utils import get_market_info, get_market_info_from_job'
    
    if 'get_market_info_from_job' not in content:
        content = content.replace(old_import, new_import)
        print("✅ polymarket_trader.py patched (updated import)")
    
    # Patch 2: Add new method after place_orders_only
    new_method = '''
    def place_orders_only_from_job(self, job: dict) -> int:
        """
        Place buy orders using job data directly (no API lookup needed).
        """
        question = job.get('question', 'Unknown Market')
        condition_id = job.get('market_id', '')
        event_slug = job.get('event_slug', '')
        
        print(f"\\n{'='*60}")
        print(f"🎯 Placing orders for: {question[:50]}...")
        print(f"   Condition ID: {condition_id[:20]}..." if condition_id else "   No condition ID!")
        print(f"{'='*60}")
        
        market_info = get_market_info_from_job(job)
        
        if not market_info:
            print(f"⚠️  No token data in job, falling back to API lookup...")
            market_info = get_market_info(event_slug) if event_slug else None
        
        if not market_info:
            print(f"❌ Could not get market info for job!")
            return 0
        
        print(f"\\n📋 Market: {market_info['question']}")
        print(f"   YES Token: {market_info['yes_token_id'][:16]}...")
        print(f"   NO Token: {market_info['no_token_id'][:16]}...")
        
        market_identifier = condition_id if condition_id else event_slug
        
        yes_orders = self.place_buy_ladder(market_info['yes_token_id'], "YES", market_identifier)
        no_orders = self.place_buy_ladder(market_info['no_token_id'], "NO", market_identifier)
        
        total_orders = len(yes_orders) + len(no_orders)
        
        if total_orders > 0:
            print(f"\\n✅ Placed {total_orders} buy orders total")
            print(f"   (Monitoring will be handled by separate process)\\n")
        else:
            print(f"\\n❌ No orders placed (all orders failed)")
        
        return total_orders
'''
    
    if 'place_orders_only_from_job' not in content:
        # Find where to insert (after place_orders_only method)
        marker = "return total_orders\n    \n    def run_strategy_limited"
        if marker in content:
            content = content.replace(marker, f"return total_orders\n{new_method}\n    def run_strategy_limited")
            print("✅ polymarket_trader.py patched (added place_orders_only_from_job)")
        else:
            print("⚠️  Could not find insertion point for new method - manual patch may be needed")
    else:
        print("✅ polymarket_trader.py already has place_orders_only_from_job")
    
    with open(filepath, 'w') as f:
        f.write(content)

def patch_auto_trader():
    """Update auto_trader.py process_trading_job function"""
    filepath = os.path.join(BASE_DIR, "auto_trader.py")
    
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Check if already patched
    if 'place_orders_only_from_job' in content:
        print("✅ auto_trader.py already patched")
        return
    
    # Find and replace the order placement section
    old_code = '''        # Place orders (without blocking on monitoring)
        # Monitoring is handled by a separate continuous process
        orders_placed = trader.place_orders_only(market_id)'''
    
    new_code = '''        # Place orders using job data (no API lookup needed for new jobs)
        # Monitoring is handled by a separate continuous process
        if job.get('clob_token_ids'):
            # New-style job with token data - use directly
            orders_placed = trader.place_orders_only_from_job(job)
        else:
            # Old-style job without token data - fallback to API lookup
            event_slug = job.get('event_slug', condition_id)
            orders_placed = trader.place_orders_only(event_slug)'''
    
    if old_code in content:
        content = content.replace(old_code, new_code)
        print("✅ auto_trader.py patched (updated to use job data)")
    else:
        print("⚠️  auto_trader.py structure different - may need manual patch")
    
    # Also update variable names
    content = content.replace("market_id = job['market_id']", "condition_id = job['market_id']  # Now contains condition_id")
    content = content.replace('question = job.get(\'question\', market_id)', 'question = job.get(\'question\', condition_id)')
    
    with open(filepath, 'w') as f:
        f.write(content)

def run_migration():
    """Run database migration to add new columns"""
    print("\n🗄️  Running database migration...")
    
    try:
        import psycopg2
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            # Try loading from .env
            env_file = "/opt/polymarket-bot/.env"
            if os.path.exists(env_file):
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("DATABASE_URL="):
                            db_url = line.split("=", 1)[1].strip().strip('"')
                            break
        
        if not db_url:
            print("⚠️  DATABASE_URL not found - run migration manually")
            return
        
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        cur.execute("""
            ALTER TABLE trading_jobs 
            ADD COLUMN IF NOT EXISTS event_slug TEXT,
            ADD COLUMN IF NOT EXISTS question TEXT,
            ADD COLUMN IF NOT EXISTS clob_token_ids TEXT,
            ADD COLUMN IF NOT EXISTS outcomes TEXT
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        
        print("✅ Database migration complete")
    except Exception as e:
        print(f"⚠️  Migration error (may already be done): {e}")

def main():
    print("=" * 60)
    print("🚀 Multi-Market Trading Update Deployment")
    print("=" * 60)
    print()
    
    # Check we're in the right directory
    if not os.path.exists(os.path.join(BASE_DIR, "polymarket_trader.py")):
        print(f"❌ Error: Cannot find files in {BASE_DIR}")
        print("   Make sure you're running this from the VPS")
        sys.exit(1)
    
    # Step 1: Backup
    backup_files()
    
    # Step 2: Apply patches
    print("📝 Applying patches...")
    patch_market_utils()
    patch_database()
    patch_polymarket_trader()
    patch_auto_trader()
    print()
    
    # Step 3: Run migration
    run_migration()
    
    print()
    print("=" * 60)
    print("✅ Deployment complete!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("1. Restart services:")
    print("   sudo systemctl restart polymarket-worker polymarket-monitor")
    print()
    print("2. Check logs:")
    print("   tail -f /var/log/order_monitor.log")
    print()
    print(f"Backup location: {BACKUP_DIR}")
    print()

if __name__ == "__main__":
    main()
