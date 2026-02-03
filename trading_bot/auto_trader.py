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


# === CRYPTO TICKERS ===
# Safe tokens: can match with word boundaries
CRYPTO_SAFE = [
    "btc", "bitcoin", "eth", "ethereum", "xrp", "ripple", "bnb", "avax",
    "ltc", "litecoin", "apt", "aptos", "sui", "arb", "arbitrum", "op", "optimism",
    "doge", "dogecoin", "shib", "matic", "polygon", "pepe", "wif", "bonk"
]

# Risky tokens: common English words, require stronger evidence
CRYPTO_RISKY = ["sol", "ada", "dot", "uni", "near", "link", "atom"]

# Full names for risky tokens
CRYPTO_RISKY_FULLNAMES = {
    "sol": "solana", "ada": "cardano", "dot": "polkadot", 
    "uni": "uniswap", "near": "near protocol", "link": "chainlink", "atom": "cosmos"
}

# === STOCK TICKERS ===
# Safe: indices and unambiguous tickers
STOCK_SAFE = [
    "nasdaq", "s&p", "sp500", "spx", "dow", "djia", "nyse", "russell",
    "aapl", "msft", "microsoft", "googl", "tsla", "tesla", "nvda", "nvidia", "nflx", "netflix"
]

# Risky: common words that are also company names
STOCK_RISKY = ["meta", "apple", "amazon", "google"]

# === FINANCIAL KEYWORDS ===
# Hard finance: always count
HARD_FINANCE_KEYWORDS = [
    "etf", "sec", "futures", "spot etf", "approval", "halving", 
    "market cap", "ath", "all-time high"
]

# Soft/generic: only count if ticker already hit
SOFT_FINANCE_KEYWORDS = [
    "price", "trading", "breakout", "resistance", "support", "bull", "bear",
    "above", "below", "hits", "reaches", "breaks", "crosses", "surpass",
    "100k", "50k", "200k", "1000", "10000", "all time high", "new high"
]


def is_crypto_stock_market(text: str, threshold: int = 5) -> tuple:
    """
    Defense-in-depth crypto/stock filter with reduced false positives.
    
    Key improvements:
    1. Risky tokens (sol, ada, dot) require confirmation ($TOKEN, full name, or hard-finance keyword)
    2. Soft keywords only count if ticker already hit
    3. Risky stock names require second signal
    4. Must have ticker hit to filter
    
    Returns (is_match, score, reasons) tuple.
    """
    import re
    
    score = 0
    ticker_score = 0
    matched_items = {"crypto": [], "stock": [], "hard_finance": [], "soft_finance": []}
    text_lower = text.lower()
    
    # Helper: check for $TOKEN format
    def has_dollar_format(token):
        return bool(re.search(rf'\${token}\b', text_lower, re.IGNORECASE))
    
    # Helper: check if full name present
    def has_fullname(token, fullname):
        return bool(re.search(rf'\b{re.escape(fullname)}\b', text_lower))
    
    # Helper: check for hard-finance keyword
    def has_hard_finance():
        for kw in HARD_FINANCE_KEYWORDS:
            if kw in text_lower:
                return True
        return False
    
    # === SAFE CRYPTO TICKERS (+3 each, max 2) ===
    crypto_count = 0
    for ticker in CRYPTO_SAFE:
        if re.search(rf'\b{re.escape(ticker)}\b', text_lower):
            matched_items["crypto"].append(ticker)
            if crypto_count < 2:
                score += 3
                ticker_score += 3
                crypto_count += 1
    
    # === RISKY CRYPTO (require confirmation) ===
    for ticker in CRYPTO_RISKY:
        if re.search(rf'\b{re.escape(ticker)}\b', text_lower):
            fullname = CRYPTO_RISKY_FULLNAMES.get(ticker, "")
            confirmed = (
                has_dollar_format(ticker) or
                (fullname and has_fullname(ticker, fullname)) or
                has_hard_finance()
            )
            if confirmed:
                matched_items["crypto"].append(f"{ticker}(confirmed)")
                if crypto_count < 2:
                    score += 3
                    ticker_score += 3
                    crypto_count += 1
            else:
                matched_items["crypto"].append(f"{ticker}(skipped)")
    
    # === SAFE STOCK TICKERS (+3 each, max 2) ===
    stock_count = 0
    for ticker in STOCK_SAFE:
        if re.search(rf'\b{re.escape(ticker)}\b', text_lower):
            matched_items["stock"].append(ticker)
            if stock_count < 2:
                score += 3
                ticker_score += 3
                stock_count += 1
    
    # === RISKY STOCK (require second signal) ===
    for ticker in STOCK_RISKY:
        if re.search(rf'\b{re.escape(ticker)}\b', text_lower):
            confirmed = has_hard_finance()
            if confirmed:
                matched_items["stock"].append(f"{ticker}(confirmed)")
                if stock_count < 2:
                    score += 3
                    ticker_score += 3
                    stock_count += 1
            else:
                matched_items["stock"].append(f"{ticker}(skipped)")
    
    # === HARD FINANCE KEYWORDS (+2 each, max 2) ===
    hard_fin_count = 0
    for keyword in HARD_FINANCE_KEYWORDS:
        if keyword in text_lower:
            matched_items["hard_finance"].append(keyword)
            if hard_fin_count < 2:
                score += 2
                hard_fin_count += 1
    
    # === SOFT FINANCE (only if ticker hit) ===
    if ticker_score > 0:
        soft_fin_count = 0
        for keyword in SOFT_FINANCE_KEYWORDS:
            if keyword in text_lower:
                matched_items["soft_finance"].append(keyword)
                if soft_fin_count < 2:
                    score += 2
                    soft_fin_count += 1
    
    # Must have ticker hit to filter
    has_ticker_hit = ticker_score > 0
    is_match = has_ticker_hit and (score >= threshold)
    
    reasons = []
    if matched_items["crypto"]:
        reasons.append(f"crypto:{','.join(matched_items['crypto'][:4])}")
    if matched_items["stock"]:
        reasons.append(f"stock:{','.join(matched_items['stock'][:4])}")
    if matched_items["hard_finance"]:
        reasons.append(f"hard_fin:{','.join(matched_items['hard_finance'][:3])}")
    if matched_items["soft_finance"]:
        reasons.append(f"soft_fin:{','.join(matched_items['soft_finance'][:3])}")
    
    return (is_match, score, reasons)


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
        job: Job dict with id, market_id (condition_id), created_at, market_created_at, 
             market_closed_time, event_slug, question, clob_token_ids, outcomes
        trader: PolymarketTrader instance
        
    Returns:
        True if successful, False if error
    """
    job_id = job['id']
    condition_id = job['market_id']  # Now contains condition_id (unique per sub-market)
    question = job.get('question', condition_id)  # Use question if available
    created_at = job['created_at']
    market_created_at = job.get('market_created_at')
    market_closed_time = job.get('market_closed_time')
    
    print(f"\n{'='*60}")
    print(f"⚡ Processing Job #{job_id}: {question[:50]}...")
    print(f"   Condition ID: {condition_id[:20]}..." if condition_id else "")
    print(f"{'='*60}")
    
    # Check if it's an up/down market (defense in depth - should already be filtered in workflow)
    # Check both condition_id and question for keywords
    search_text = f"{condition_id} {question}".lower()
    if is_updown_market(search_text):
        print(f"⏭️  Job #{job_id} is an UP/DOWN market - skipping to avoid short-term capital lockup")
        print(f"   Market: {question[:60]}")
        complete_trading_job(job_id, "Skipped: Up/Down market (short-term)")
        return False
    
    # Check if market duration is too short (defense in depth - should already be filtered in workflow)
    is_short, duration_hours = is_short_duration_market(market_created_at, market_closed_time)
    if is_short:
        print(f"⏭️  Job #{job_id} has short duration ({duration_hours}h < 15h minimum)")
        print(f"   Market: {question[:60]}")
        print(f"   Created: {market_created_at}, Closes: {market_closed_time}")
        complete_trading_job(job_id, f"Skipped: Short duration ({duration_hours}h < 15h)")
        return False
    
    # Check if crypto/stock market (defense in depth - should already be filtered in workflow)
    is_crypto_stock, cs_score, cs_reasons = is_crypto_stock_market(search_text)
    if is_crypto_stock:
        print(f"⏭️  Job #{job_id} is a CRYPTO/STOCK market (score={cs_score})")
        print(f"   Reasons: {cs_reasons}")
        print(f"   Market: {question[:60]}")
        complete_trading_job(job_id, f"Skipped: Crypto/Stock market (score={cs_score})")
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
        
        # Place orders using job data (no API lookup needed for new jobs)
        # Monitoring is handled by a separate continuous process
        if job.get('clob_token_ids'):
            # New-style job with token data - use directly
            orders_placed = trader.place_orders_only_from_job(job)
        else:
            # Old-style job without token data - fallback to API lookup
            event_slug = job.get('event_slug', condition_id)
            orders_placed = trader.place_orders_only(event_slug)
        
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
