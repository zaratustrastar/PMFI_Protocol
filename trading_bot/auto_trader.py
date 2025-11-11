"""
Automatic Trading Worker
Continuously polls for queued trading jobs and executes them
"""

import os
import time
from polymarket_trader import PolymarketTrader
from database import get_pending_jobs, start_trading_job, complete_trading_job


def process_trading_job(job, trader: PolymarketTrader):
    """
    Process a single trading job
    
    Args:
        job: Job dict with id, market_id, created_at
        trader: PolymarketTrader instance
        
    Returns:
        True if successful, False if error
    """
    job_id = job['id']
    market_id = job['market_id']
    
    print(f"\n{'='*60}")
    print(f"⚡ Processing Job #{job_id}: {market_id}")
    print(f"{'='*60}")
    
    try:
        # Mark job as running
        start_trading_job(job_id)
        print(f"🔄 Job #{job_id} marked as RUNNING")
        
        # Place orders (without blocking on monitoring)
        # Monitoring is handled by a separate continuous process
        success = trader.place_orders_only(market_id)
        
        if success:
            # Mark job as completed
            complete_trading_job(job_id)
            print(f"✅ Job #{job_id} completed (orders placed)")
            return True
        else:
            # Mark job as failed
            complete_trading_job(job_id, "Failed to place orders")
            print(f"❌ Job #{job_id} failed to place orders")
            return False
            
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error processing job #{job_id}: {error_msg}")
        
        # Mark job as failed with error
        complete_trading_job(job_id, error_msg)
        return False


def main():
    """Main worker loop - continuously polls for jobs"""
    print("\n🤖 Polymarket Auto-Trading Worker Starting...")
    print(f"   Budget: $2 per market ($0.20 × 10 orders × 2 sides)")
    print(f"   Strategy: Ladder buys 0.1¢-1¢, ladder sells 3x-10x profit")
    print(f"   Notifications: Telegram @ponnymarket (sells only)")
    print(f"   Mode: Queue-based worker\n")
    
    # Initialize trader once
    trader = PolymarketTrader()
    
    # Poll interval in seconds
    poll_interval = int(os.getenv("POLL_INTERVAL", "30"))
    batch_size = int(os.getenv("BATCH_SIZE", "5"))
    
    print(f"📊 Configuration:")
    print(f"   Poll Interval: {poll_interval}s")
    print(f"   Batch Size: {batch_size} jobs\n")
    print("🔄 Starting worker loop (press Ctrl+C to stop)\n")
    
    try:
        while True:
            # Get pending jobs from database
            pending_jobs = get_pending_jobs(limit=batch_size)
            
            if pending_jobs:
                print(f"\n✅ Found {len(pending_jobs)} pending job(s)")
                
                # Process each job
                for job in pending_jobs:
                    process_trading_job(job, trader)
                    
                    # Small delay between jobs
                    time.sleep(2)
            else:
                print(f"⏸️  No pending jobs (checked at {time.strftime('%H:%M:%S')})")
            
            # Wait before polling again
            print(f"💤 Waiting {poll_interval}s before next poll...")
            time.sleep(poll_interval)
            
    except KeyboardInterrupt:
        print("\n\n⏸️  Worker stopped by user")
    except Exception as e:
        print(f"\n\n❌ Worker crashed: {e}")
        raise


if __name__ == "__main__":
    main()
