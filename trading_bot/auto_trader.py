"""
Automatic Trading Worker
Continuously polls for queued trading jobs and executes them
"""

import os
import time

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ Loaded environment variables from .env file")
except ImportError:
    print("⚠️  python-dotenv not installed, using system environment variables")

from polymarket_trader import PolymarketTrader
from database import get_pending_jobs, start_trading_job, complete_trading_job


def is_updown_market(market_slug: str) -> bool:
    """
    Check if a market is an up/down short-term market
    
    Args:
        market_slug: Market slug/identifier
        
    Returns:
        True if it's an up/down market that should be skipped
    """
    UPDOWN_KEYWORDS = [
        "up or down",
        "up-or-down",
        "updown",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "12pm et",
        "1pm et",
        "2pm et",
        "3pm et",
        "4pm et",
        "5pm et",
        "6pm et",
        "7pm et",
        "8pm et",
    ]
    
    slug_lower = market_slug.lower()
    return any(keyword in slug_lower for keyword in UPDOWN_KEYWORDS)


def is_short_duration_market(market_created_at, market_closed_time, min_hours=15):
    """
    Check if a market's duration is too short (< 15 hours)
    Markets with short durations (same-day sports, quick events) should not be traded
    
    Args:
        market_created_at: Market creation timestamp (datetime or None)
        market_closed_time: Market close timestamp (datetime or None)
        min_hours: Minimum duration in hours (default 15)
        
    Returns:
        Tuple of (is_short: bool, duration_hours: float or None)
    """
    if not market_created_at or not market_closed_time:
        # No date info - can't determine duration, allow trading (fallback to keyword filter)
        return (False, None)
    
    try:
        from datetime import timezone
        
        # Ensure both have timezone info
        if market_created_at.tzinfo is None:
            market_created_at = market_created_at.replace(tzinfo=timezone.utc)
        if market_closed_time.tzinfo is None:
            market_closed_time = market_closed_time.replace(tzinfo=timezone.utc)
        
        duration_seconds = (market_closed_time - market_created_at).total_seconds()
        duration_hours = duration_seconds / 3600
        
        return (duration_hours < min_hours, round(duration_hours, 1))
    except Exception as e:
        # Invalid date format - can't parse, allow trading (fallback to keyword filter)
        print(f"⚠️  Error parsing market dates: {e}")
        return (False, None)


def process_trading_job(job, trader: PolymarketTrader):
    """
    Process a single trading job
    
    Args:
        job: Job dict with id, market_id, created_at, market_created_at, market_closed_time
        trader: PolymarketTrader instance
        
    Returns:
        True if successful, False if error
    """
    job_id = job['id']
    market_id = job['market_id']
    created_at = job['created_at']
    market_created_at = job.get('market_created_at')
    market_closed_time = job.get('market_closed_time')
    
    print(f"\n{'='*60}")
    print(f"⚡ Processing Job #{job_id}: {market_id}")
    print(f"{'='*60}")
    
    # Check if it's an up/down market (defense in depth - should already be filtered in workflow)
    if is_updown_market(market_id):
        print(f"⏭️  Job #{job_id} is an UP/DOWN market - skipping to avoid short-term capital lockup")
        print(f"   Market: {market_id}")
        complete_trading_job(job_id, "Skipped: Up/Down market (short-term)")
        return False
    
    # Check if market duration is too short (defense in depth - should already be filtered in workflow)
    is_short, duration_hours = is_short_duration_market(market_created_at, market_closed_time)
    if is_short:
        print(f"⏭️  Job #{job_id} has short duration ({duration_hours}h < 15h minimum)")
        print(f"   Market: {market_id}")
        print(f"   Created: {market_created_at}, Closes: {market_closed_time}")
        complete_trading_job(job_id, f"Skipped: Short duration ({duration_hours}h < 15h)")
        return False
    
    # Check if job is too old (older than 24 hours)
    from datetime import datetime, timezone
    job_age_hours = (datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
    
    if job_age_hours > 24:
        print(f"⏰ Job #{job_id} is {job_age_hours:.1f} hours old (created: {created_at})")
        print(f"❌ Skipping job - too old (>24 hours)")
        complete_trading_job(job_id, f"Job expired (age: {job_age_hours:.1f}h)")
        return False
    
    try:
        # Mark job as running
        start_trading_job(job_id)
        print(f"🔄 Job #{job_id} marked as RUNNING (age: {job_age_hours:.1f}h)")
        
        # Place orders (without blocking on monitoring)
        # Monitoring is handled by a separate continuous process
        orders_placed = trader.place_orders_only(market_id)
        
        if orders_placed > 0:
            # Mark job as completed
            complete_trading_job(job_id)
            print(f"✅ Job #{job_id} completed ({orders_placed} orders placed)")
            return True
        else:
            # Mark job as failed
            complete_trading_job(job_id, "No orders placed (all failed)")
            print(f"❌ Job #{job_id} failed - no orders placed")
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
    print(f"   Strategy: Ladder buys 1¢-3¢, ladder sells 3x-10x profit")
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
