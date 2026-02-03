#!/usr/bin/env python3
"""
Standalone Polymarket Market Monitor

This script runs via system cron every minute to:
1. Fetch latest EVENTS from Polymarket API
2. Post to Telegram once per new EVENT
3. Queue each qualifying sub-market for trading (even if event was already seen)
4. Track events and conditions separately

Usage:
    python market_monitor.py

Cron setup:
    * * * * * cd /opt/polymarket-bot && /usr/bin/python3 trading_bot/market_monitor.py >> /var/log/market_monitor.log 2>&1
"""

import os
import sys
import re
import requests
import psycopg2
from datetime import datetime
from typing import List, Dict, Optional, Set
import html
import time

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = "@ponnymarket"
REFERRAL_CODE = "via=q2XDjZW"

# Polymarket API
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"

# Up/down market keywords to filter out for trading
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
]

# === CRYPTO TICKERS ===
# Safe tokens: can match with word boundaries
CRYPTO_SAFE = [
    "btc", "bitcoin", "eth", "ethereum", "xrp", "ripple", "bnb", "avax",
    "ltc", "litecoin", "apt", "aptos", "sui", "arb", "arbitrum", "op", "optimism",
    "doge", "dogecoin", "shib", "matic", "polygon", "pepe", "wif", "bonk"
]

# Risky tokens: common English words, require stronger evidence
# Only count if: $TOKEN format, paired with full name, or paired with hard-finance keyword
CRYPTO_RISKY = ["sol", "ada", "dot", "uni", "near", "link", "atom"]

# Full names for risky tokens (to confirm risky token match)
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

# Risky: common words that are also company names, require second signal
STOCK_RISKY = ["meta", "apple", "amazon", "google"]

# === FINANCIAL KEYWORDS ===
# Hard finance: safe to use, strong signal
HARD_FINANCE_KEYWORDS = [
    "etf", "sec", "futures", "spot etf", "approval", "halving", 
    "market cap", "ath", "all-time high"
]

# Soft/generic: only count if ticker already hit (boosters, not triggers)
SOFT_FINANCE_KEYWORDS = [
    "price", "trading", "breakout", "resistance", "support", "bull", "bear",
    "above", "below", "hits", "reaches", "breaks", "crosses", "surpass",
    "100k", "50k", "200k", "1000", "10000", "all time high", "new high"
]

# Maximum outcomes per market - spray 12 bets (4 start, 4 middle, 4 end) for large markets
MAX_OUTCOMES_PER_MARKET = 12

# Category tags that indicate crypto/stock markets
CRYPTO_STOCK_TAGS = [
    "crypto", "cryptocurrency", "bitcoin", "ethereum", "defi",
    "stocks", "equities", "nasdaq", "finance"
]


def apply_outcome_limit(clob_token_ids_json: str, outcomes_json: str) -> tuple:
    """
    Apply outcome limit for markets with many outcomes.
    
    For markets with > MAX_OUTCOMES_PER_MARKET outcomes, select exactly 12:
    - 4 from start (likely favorites)
    - 4 from middle
    - 4 from end (likely longshots)
    
    Uses evenly spaced selection to avoid overlaps and ensure exactly 12 outcomes.
    
    Args:
        clob_token_ids_json: JSON array of token IDs
        outcomes_json: JSON array of outcome names
        
    Returns:
        Tuple of (limited_token_ids_json, limited_outcomes_json, was_limited)
    """
    import json
    
    try:
        token_ids = json.loads(clob_token_ids_json) if clob_token_ids_json else []
        outcomes = json.loads(outcomes_json) if outcomes_json else []
    except (json.JSONDecodeError, TypeError) as e:
        log(f"   ⚠️ JSON parse error in apply_outcome_limit: {e}")
        return clob_token_ids_json, outcomes_json, False
    
    # Use minimum length to ensure alignment
    total_outcomes = min(len(token_ids), len(outcomes))
    if len(token_ids) != len(outcomes):
        log(f"   ⚠️ Mismatched lengths: token_ids={len(token_ids)}, outcomes={len(outcomes)}")
    
    # If within limit, return as-is
    if total_outcomes <= MAX_OUTCOMES_PER_MARKET:
        return clob_token_ids_json, outcomes_json, False
    
    # Use evenly spaced selection to get exactly 12 outcomes
    # This prevents overlaps that occur with start/middle/end approach
    selected_indices = []
    
    # Calculate spacing to distribute 12 picks across the total range
    # Pick from 3 zones: start (0-33%), middle (33%-66%), end (66%-100%)
    zone_size = total_outcomes // 3
    
    # Zone 1: Start (indices 0 to zone_size-1), pick 4 evenly spaced
    for i in range(4):
        idx = (i * zone_size) // 4
        selected_indices.append(idx)
    
    # Zone 2: Middle (indices zone_size to 2*zone_size-1), pick 4 evenly spaced
    for i in range(4):
        idx = zone_size + (i * zone_size) // 4
        selected_indices.append(idx)
    
    # Zone 3: End (indices 2*zone_size to total_outcomes-1), pick 4 evenly spaced
    end_zone_size = total_outcomes - (2 * zone_size)
    for i in range(4):
        idx = (2 * zone_size) + (i * end_zone_size) // 4
        if idx >= total_outcomes:
            idx = total_outcomes - 1
        selected_indices.append(idx)
    
    # Deduplicate and sort (should rarely have duplicates with this approach)
    selected_indices = sorted(set(selected_indices))
    
    # If we have fewer than 12 due to small total or dedup, fill in gaps
    if len(selected_indices) < MAX_OUTCOMES_PER_MARKET:
        all_indices = set(range(total_outcomes))
        remaining = sorted(all_indices - set(selected_indices))
        needed = MAX_OUTCOMES_PER_MARKET - len(selected_indices)
        # Add evenly spaced from remaining
        for i in range(min(needed, len(remaining))):
            idx = remaining[(i * len(remaining)) // needed] if needed > 0 else 0
            selected_indices.append(idx)
        selected_indices = sorted(set(selected_indices))
    
    # Limit to exactly MAX_OUTCOMES_PER_MARKET
    selected_indices = selected_indices[:MAX_OUTCOMES_PER_MARKET]
    
    # Select the outcomes
    limited_tokens = [token_ids[i] for i in selected_indices]
    limited_outcomes = [outcomes[i] for i in selected_indices]
    
    return json.dumps(limited_tokens), json.dumps(limited_outcomes), True


def log(message: str):
    """Log message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def get_db_connection():
    """Get database connection"""
    if not DATABASE_URL:
        log("ERROR: DATABASE_URL not set")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)


def init_tables():
    """Ensure required tables exist"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Seen events table (for Telegram - one post per event)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_polymarket_events (
            event_slug TEXT PRIMARY KEY,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Seen conditions table (for trading - track each sub-market)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_trading_conditions (
            condition_id TEXT PRIMARY KEY,
            event_slug TEXT,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Trading jobs table
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
    
    # Migration for existing tables
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
    
    conn.commit()
    cur.close()
    conn.close()


def fetch_events(limit: int = 20) -> List[Dict]:
    """Fetch latest EVENTS from Polymarket Gamma API.
    
    Uses exclude_tag_id to filter out crypto (21) and finance (120) markets
    at the API level, preventing them from ever entering the pipeline.
    """
    try:
        # Exclude crypto (tag_id=21) and finance (tag_id=120) markets at API level
        # This is more reliable than keyword filtering
        url = (
            f"{GAMMA_API_URL}"
            f"?order=id&ascending=false&closed=false&limit={limit}"
            f"&exclude_tag_id=21"   # Exclude crypto markets
            f"&exclude_tag_id=120"  # Exclude finance markets
        )
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        events = response.json()
        
        log(f"Fetched {len(events)} events from API (excluding crypto/finance tags)")
        return events
        
    except Exception as e:
        log(f"ERROR fetching events: {e}")
        return []


def get_seen_event_slugs(event_slugs: List[str]) -> Set[str]:
    """Check which event slugs have already been seen (for Telegram)"""
    if not event_slugs:
        return set()
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    placeholders = ",".join(["%s"] * len(event_slugs))
    cur.execute(f"SELECT event_slug FROM seen_polymarket_events WHERE event_slug IN ({placeholders})", event_slugs)
    
    seen = {row[0] for row in cur.fetchall()}
    
    cur.close()
    conn.close()
    
    return seen


def get_seen_condition_ids(condition_ids: List[str]) -> Set[str]:
    """Check which condition IDs have already been processed (for trading)"""
    if not condition_ids:
        return set()
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    placeholders = ",".join(["%s"] * len(condition_ids))
    cur.execute(f"SELECT condition_id FROM seen_trading_conditions WHERE condition_id IN ({placeholders})", condition_ids)
    
    seen = {row[0] for row in cur.fetchall()}
    
    cur.close()
    conn.close()
    
    return seen


def try_claim_event(event_slug: str) -> bool:
    """
    Atomically try to claim an event for Telegram posting.
    Uses INSERT RETURNING to ensure only ONE process can ever claim a given event.
    
    Returns:
        True if this process claimed the event (should post to Telegram)
        False if another process already claimed it (skip posting)
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute(
            "INSERT INTO seen_polymarket_events (event_slug) VALUES (%s) ON CONFLICT DO NOTHING RETURNING event_slug",
            (event_slug,)
        )
        
        result = cur.fetchone()
        conn.commit()
        
        claimed = result is not None
        if not claimed:
            log(f"⏭️  Event already claimed by another process: {event_slug[:30]}...")
        
        return claimed
        
    except Exception as e:
        log(f"❌ Error claiming event {event_slug}: {e}")
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def mark_event_as_seen(event_slug: str):
    """Legacy function - use try_claim_event instead for atomic claiming"""
    try_claim_event(event_slug)


def mark_condition_as_seen(condition_id: str, event_slug: str):
    """Mark a condition as seen for trading"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "INSERT INTO seen_trading_conditions (condition_id, event_slug) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (condition_id, event_slug)
    )
    
    conn.commit()
    cur.close()
    conn.close()


def add_referral_code(url: str) -> str:
    """Add referral code to URL"""
    if "?" in url:
        return f"{url}&{REFERRAL_CODE}"
    else:
        return f"{url}?{REFERRAL_CODE}"


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram"""
    return html.escape(text)


def post_to_telegram(message: str, max_retries: int = 3) -> bool:
    """Post message to Telegram channel with retry logic for rate limiting"""
    if not TELEGRAM_BOT_TOKEN:
        log("WARNING: TELEGRAM_BOT_TOKEN not set, skipping Telegram post")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                return True
            elif response.status_code == 429:
                try:
                    error_data = response.json()
                    retry_after = error_data.get("parameters", {}).get("retry_after", 30)
                    log(f"⏳ Rate limited, waiting {retry_after}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(retry_after + 1)
                except:
                    log(f"⏳ Rate limited, waiting 30s before retry {attempt + 1}/{max_retries}")
                    time.sleep(31)
            else:
                log(f"WARNING: Telegram post failed: {response.text}")
                return False
        except Exception as e:
            log(f"ERROR posting to Telegram: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return False
    
    log("WARNING: Telegram post failed after max retries")
    return False


def is_updown_market(text: str, tags: List = None) -> bool:
    """Check if market/event is an up/down short-term market"""
    haystack = text.lower()
    
    if tags:
        tag_strings = []
        for tag in tags:
            if isinstance(tag, dict):
                tag_strings.append(str(tag.get("label", "")).lower())
                tag_strings.append(str(tag.get("slug", "")).lower())
            else:
                tag_strings.append(str(tag).lower())
        haystack += " " + " ".join(tag_strings)
    
    for keyword in UPDOWN_KEYWORDS:
        if keyword in haystack:
            return True
    
    if tags and "updown" in [str(t).lower() for t in tags]:
        return True
    
    return False


def is_short_duration(created_at: str, closed_time: str, min_hours: int = 15) -> tuple:
    """Check if market closes too soon (< 15 hours from now)"""
    if not closed_time:
        return (False, None)
    
    try:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        closed = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
        
        # Calculate time remaining until market closes
        time_remaining = closed - now
        hours_remaining = time_remaining.total_seconds() / 3600
        
        # Skip if market closes in less than min_hours
        is_short = hours_remaining < min_hours
        
        if is_short:
            log(f"   ⏱️ Short duration: closes in {round(hours_remaining, 1)}h (min: {min_hours}h)")
        
        return (is_short, round(hours_remaining, 1))
    except Exception as e:
        log(f"   ⚠️ Date parse error: {e} | closed_time={closed_time}")
        return (False, None)


def is_crypto_stock_market(title: str, outcomes: str, tags: List = None, threshold: int = 5) -> tuple:
    """
    Improved hybrid NLP scoring to detect crypto/stock markets with reduced false positives.
    
    Key improvements:
    1. Risky tokens (sol, ada, dot, etc.) require stronger evidence ($TOKEN, full name, or hard-finance keyword)
    2. Soft keywords (price, trading) only count if a ticker already hit
    3. Risky stock names (meta, apple, amazon) require a second signal
    4. Must have at least one ticker hit to filter (keywords+tags alone won't filter)
    
    Returns (is_match, score, reasons) tuple.
    """
    import re
    
    score = 0
    ticker_score = 0  # Track ticker hits separately
    matched_items = {"crypto": [], "stock": [], "hard_finance": [], "soft_finance": [], "tags": []}
    
    # Normalize text for matching
    title_lower = title.lower()
    outcomes_lower = str(outcomes).lower() if outcomes else ""
    combined_text = f"{title_lower} {outcomes_lower}"
    
    # Helper: check for $TOKEN format (e.g., $SOL, $BTC)
    def has_dollar_format(token):
        return bool(re.search(rf'\${token}\b', combined_text, re.IGNORECASE))
    
    # Helper: check if full name is present alongside short ticker
    def has_fullname(token, fullname):
        return bool(re.search(rf'\b{re.escape(fullname)}\b', combined_text))
    
    # Helper: check if any hard-finance keyword is present
    def has_hard_finance():
        for kw in HARD_FINANCE_KEYWORDS:
            if kw in combined_text:
                return True
        return False
    
    # === CHECK SAFE CRYPTO TICKERS (+3 each, max 2 = +6) ===
    crypto_count = 0
    for ticker in CRYPTO_SAFE:
        if re.search(rf'\b{re.escape(ticker)}\b', combined_text):
            matched_items["crypto"].append(ticker)
            if crypto_count < 2:
                score += 3
                ticker_score += 3
                crypto_count += 1
    
    # === CHECK RISKY CRYPTO TICKERS (require confirmation) ===
    for ticker in CRYPTO_RISKY:
        if re.search(rf'\b{re.escape(ticker)}\b', combined_text):
            fullname = CRYPTO_RISKY_FULLNAMES.get(ticker, "")
            
            # Only count if: $TOKEN format, fullname present, or hard-finance keyword present
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
                matched_items["crypto"].append(f"{ticker}(unconfirmed-skipped)")
    
    # === CHECK SAFE STOCK TICKERS (+3 each, max 2 = +6) ===
    stock_count = 0
    for ticker in STOCK_SAFE:
        if re.search(rf'\b{re.escape(ticker)}\b', combined_text):
            matched_items["stock"].append(ticker)
            if stock_count < 2:
                score += 3
                ticker_score += 3
                stock_count += 1
    
    # === CHECK RISKY STOCK NAMES (require second signal) ===
    for ticker in STOCK_RISKY:
        if re.search(rf'\b{re.escape(ticker)}\b', combined_text):
            # Only count if hard-finance keyword present
            confirmed = has_hard_finance()
            
            if confirmed:
                matched_items["stock"].append(f"{ticker}(confirmed)")
                if stock_count < 2:
                    score += 3
                    ticker_score += 3
                    stock_count += 1
            else:
                matched_items["stock"].append(f"{ticker}(unconfirmed-skipped)")
    
    # === CHECK HARD FINANCE KEYWORDS (+2 each, max 2 = +4) ===
    # Always count - these are strong signals
    hard_fin_count = 0
    for keyword in HARD_FINANCE_KEYWORDS:
        if keyword in combined_text:
            matched_items["hard_finance"].append(keyword)
            if hard_fin_count < 2:
                score += 2
                hard_fin_count += 1
    
    # === CHECK SOFT FINANCE KEYWORDS (+2 each, max 2 = +4) ===
    # ONLY count if we already have a ticker hit (boosters, not triggers)
    if ticker_score > 0:
        soft_fin_count = 0
        for keyword in SOFT_FINANCE_KEYWORDS:
            if keyword in combined_text:
                matched_items["soft_finance"].append(keyword)
                if soft_fin_count < 2:
                    score += 2
                    soft_fin_count += 1
    
    # === CHECK TAGS (+2 per matching tag, max 2 = +4) ===
    if tags:
        tag_strings = []
        for tag in tags:
            if isinstance(tag, dict):
                tag_strings.append(str(tag.get("label", "")).lower())
                tag_strings.append(str(tag.get("slug", "")).lower())
            else:
                tag_strings.append(str(tag).lower())
        
        tag_count = 0
        for check_tag in CRYPTO_STOCK_TAGS:
            if any(check_tag in t for t in tag_strings):
                matched_items["tags"].append(check_tag)
                if tag_count < 2:
                    score += 2
                    tag_count += 1
    
    # === EXCLUSION LOGIC ===
    # Require at least one CONFIRMED ticker hit before excluding
    has_ticker_hit = ticker_score > 0
    is_match = has_ticker_hit and (score >= threshold)
    
    # Build reason string for logging
    reasons = []
    if matched_items["crypto"]:
        reasons.append(f"crypto:{','.join(matched_items['crypto'][:4])}")
    if matched_items["stock"]:
        reasons.append(f"stock:{','.join(matched_items['stock'][:4])}")
    if matched_items["hard_finance"]:
        reasons.append(f"hard_fin:{','.join(matched_items['hard_finance'][:3])}")
    if matched_items["soft_finance"]:
        reasons.append(f"soft_fin:{','.join(matched_items['soft_finance'][:3])}")
    if matched_items["tags"]:
        reasons.append(f"tags:{','.join(matched_items['tags'][:3])}")
    
    if is_match:
        log(f"   🚫 FILTERED crypto/stock (score={score}, ticker_score={ticker_score}): {reasons}")
    elif score > 0:
        log(f"   ✅ NOT filtered (score={score}, ticker_score={ticker_score}, need ticker+threshold): {reasons}")
    
    return (is_match, score, reasons)


def queue_trading_job(
    condition_id: str, 
    event_slug: str,
    question: str,
    clob_token_ids: str,
    outcomes: str,
    created_at: Optional[str] = None, 
    closed_time: Optional[str] = None
) -> bool:
    """Queue a sub-market for trading using condition_id as unique identifier."""
    if not condition_id:
        return False
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Parse dates
    market_created = None
    market_closed = None
    
    if created_at:
        try:
            market_created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except:
            pass
    
    if closed_time:
        try:
            market_closed = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
        except:
            pass
    
    cur.execute("""
        INSERT INTO trading_jobs (market_id, event_slug, question, clob_token_ids, outcomes, status, market_created_at, market_closed_time)
        VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s)
        ON CONFLICT (market_id) DO NOTHING
    """, (condition_id, event_slug, question, clob_token_ids, outcomes, market_created, market_closed))
    
    rows_inserted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    
    return rows_inserted > 0


def format_tags_as_hashtags(tags: List) -> str:
    """Convert API tags to hashtags, filtering out internal Polymarket tags"""
    if not tags:
        return ""
    
    # Internal tags to filter out
    excluded_tags = {"hidefromnew", "recurring", "polymarket"}
    
    hashtags = []
    for tag in tags:
        if tag:
            if isinstance(tag, dict):
                tag_name = tag.get("label") or tag.get("slug") or ""
            else:
                tag_name = str(tag)
            
            if not tag_name:
                continue
                
            clean_tag = tag_name.replace(" ", "").replace("-", "").replace("&", "And")
            clean_tag = "".join(c for c in clean_tag if c.isalnum())
            
            # Skip excluded internal tags
            if clean_tag.lower() in excluded_tags:
                continue
                
            if clean_tag:
                hashtags.append(f"#{clean_tag}")
    
    return " ".join(hashtags[:5])


def post_event_to_telegram(event: Dict) -> bool:
    """Post a single Telegram message for an event"""
    event_slug = event.get("slug", "")
    title = event.get("title", "New Market")
    tags = event.get("tags", [])
    
    question = escape_html(title)
    event_url = add_referral_code(f"https://polymarket.com/event/{event_slug}")
    category_hashtags = format_tags_as_hashtags(tags)
    
    # Link at end for proper Telegram image preview
    message = f"""🔮 <b>New Market!</b>

{question}

{category_hashtags}

🔗 <a href="{event_url}">Trade</a>"""
    
    return post_to_telegram(message)


def process_sub_market(market: Dict, event: Dict, seen_conditions: Set[str]) -> dict:
    """
    Process a single sub-market for trading queue.
    Returns result dict.
    """
    event_slug = event.get("slug", "")
    tags = event.get("tags", [])
    
    market_question = market.get("question", event.get("title", ""))
    condition_id = market.get("conditionId", market.get("condition_id", ""))
    clob_token_ids = market.get("clobTokenIds", "[]")
    outcomes = market.get("outcomes", "[]")
    created_at = market.get("createdAt", event.get("createdAt"))
    closed_time = market.get("endDate", event.get("endDate"))
    
    result = {"queued": False, "skipped": False, "reason": ""}
    
    # Skip if no condition_id
    if not condition_id:
        result["skipped"] = True
        result["reason"] = "no_condition_id"
        return result
    
    # Skip if already seen
    if condition_id in seen_conditions:
        result["skipped"] = True
        result["reason"] = "already_seen"
        return result
    
    # Check if up/down market
    search_text = f"{market_question} {event_slug}"
    if is_updown_market(search_text, tags):
        mark_condition_as_seen(condition_id, event_slug)
        result["skipped"] = True
        result["reason"] = "updown"
        return result
    
    # Check if crypto/stock market (hybrid scoring filter)
    is_crypto_stock, cs_score, cs_reasons = is_crypto_stock_market(
        title=market_question,
        outcomes=outcomes,
        tags=tags,
        threshold=5
    )
    if is_crypto_stock:
        mark_condition_as_seen(condition_id, event_slug)
        result["skipped"] = True
        result["reason"] = f"crypto_stock_score_{cs_score}"
        return result
    
    # Check duration
    is_short, duration = is_short_duration(created_at, closed_time)
    if is_short:
        mark_condition_as_seen(condition_id, event_slug)
        result["skipped"] = True
        result["reason"] = f"short_{duration}h"
        return result
    
    # Apply outcome limit for multi-outcome markets (spray distribution)
    limited_token_ids, limited_outcomes, was_limited = apply_outcome_limit(clob_token_ids, outcomes)
    if was_limited:
        import json
        original_count = len(json.loads(clob_token_ids)) if clob_token_ids else 0
        limited_count = len(json.loads(limited_token_ids)) if limited_token_ids else 0
        log(f"   🎯 Outcome limit applied: {original_count} → {limited_count} (spray distribution)")
    
    # Queue for trading
    if queue_trading_job(
        condition_id=condition_id,
        event_slug=event_slug,
        question=market_question,
        clob_token_ids=limited_token_ids,
        outcomes=limited_outcomes,
        created_at=created_at,
        closed_time=closed_time
    ):
        mark_condition_as_seen(condition_id, event_slug)
        result["queued"] = True
        log(f"   💰 Queued: {market_question[:40]}...")
    else:
        result["skipped"] = True
        result["reason"] = "already_queued"
    
    return result


def main():
    """Main monitoring loop"""
    log("🚀 Starting Polymarket market monitor")
    
    # Ensure tables exist
    init_tables()
    
    # Fetch latest events (100 to catch all new markets)
    events = fetch_events(limit=100)
    
    if not events:
        log("No events fetched, exiting")
        return
    
    # Get all event slugs and condition IDs
    event_slugs = [e.get("slug", "") for e in events if e.get("slug")]
    
    all_condition_ids = []
    for event in events:
        markets = event.get("markets", [event])
        for market in markets:
            cid = market.get("conditionId", market.get("condition_id", ""))
            if cid:
                all_condition_ids.append(cid)
    
    # Check what we've already seen
    seen_event_slugs = get_seen_event_slugs(event_slugs)
    seen_conditions = get_seen_condition_ids(all_condition_ids)
    
    # Stats
    telegram_posted = 0
    markets_queued = 0
    markets_skipped = 0
    
    for event in events:
        event_slug = event.get("slug", "")
        if not event_slug:
            continue
        
        title = event.get("title", "")[:50]
        
        # TELEGRAM: Post once per new event (ATOMIC claim prevents duplicates across processes)
        if event_slug not in seen_event_slugs:
            # Atomically try to claim this event - only one process can ever succeed
            if try_claim_event(event_slug):
                seen_event_slugs.add(event_slug)  # Update in-memory set for same-batch dedup
                if post_event_to_telegram(event):
                    log(f"✅ Telegram: {title}...")
                    telegram_posted += 1
                    time.sleep(1)  # Rate limit
                else:
                    log(f"❌ Telegram failed: {title}...")
                    continue  # Don't process sub-markets if Telegram fails
            else:
                # Another process already claimed this event
                seen_event_slugs.add(event_slug)
        
        # TRADING: Process each sub-market (even for already-seen events)
        markets = event.get("markets", [])
        if not markets:
            markets = [event]  # Single-market event
        
        for market in markets:
            result = process_sub_market(market, event, seen_conditions)
            
            if result["queued"]:
                markets_queued += 1
            elif result["skipped"] and result["reason"] not in ["already_seen", "already_queued"]:
                markets_skipped += 1
    
    # Summary
    new_events = len([e for e in events if e.get("slug") and e["slug"] not in seen_event_slugs])
    log(f"Found {new_events} new events, {len(events) - new_events} existing")
    log(f"✅ Done: {telegram_posted} Telegram, {markets_queued} queued, {markets_skipped} filtered")


if __name__ == "__main__":
    main()
