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
    """Fetch latest EVENTS from Polymarket Gamma API."""
    try:
        url = f"{GAMMA_API_URL}?order=id&ascending=false&closed=false&limit={limit}"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        events = response.json()
        
        log(f"Fetched {len(events)} events from API")
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


def mark_event_as_seen(event_slug: str):
    """Mark an event as seen for Telegram"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "INSERT INTO seen_polymarket_events (event_slug) VALUES (%s) ON CONFLICT DO NOTHING",
        (event_slug,)
    )
    
    conn.commit()
    cur.close()
    conn.close()


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


def post_to_telegram(message: str) -> bool:
    """Post message to Telegram channel"""
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
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return True
        else:
            log(f"WARNING: Telegram post failed: {response.text}")
            return False
    except Exception as e:
        log(f"ERROR posting to Telegram: {e}")
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
    """Check if market duration is too short (< 15 hours)"""
    if not created_at or not closed_time:
        return (False, None)
    
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        closed = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
        
        duration = closed - created
        duration_hours = duration.total_seconds() / 3600
        
        return (duration_hours < min_hours, round(duration_hours, 1))
    except Exception:
        return (False, None)


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
    """Convert API tags to hashtags"""
    if not tags:
        return ""
    
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
    
    message = f"""🔮 <b>New Market!</b>

{question}

🔗 <a href="{event_url}">Trade</a>

#Polymarket {category_hashtags}"""
    
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
    
    # Check duration
    is_short, duration = is_short_duration(created_at, closed_time)
    if is_short:
        mark_condition_as_seen(condition_id, event_slug)
        result["skipped"] = True
        result["reason"] = f"short_{duration}h"
        return result
    
    # Queue for trading
    if queue_trading_job(
        condition_id=condition_id,
        event_slug=event_slug,
        question=market_question,
        clob_token_ids=clob_token_ids,
        outcomes=outcomes,
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
    
    # Fetch latest events
    events = fetch_events(limit=20)
    
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
        
        # TELEGRAM: Post once per new event
        if event_slug not in seen_event_slugs:
            if post_event_to_telegram(event):
                log(f"✅ Telegram: {title}...")
                mark_event_as_seen(event_slug)
                telegram_posted += 1
                time.sleep(1)  # Rate limit
            else:
                log(f"❌ Telegram failed: {title}...")
                continue  # Don't process sub-markets if Telegram fails
        
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
