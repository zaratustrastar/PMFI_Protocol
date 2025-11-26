#!/usr/bin/env python3
"""
Standalone Polymarket Market Monitor

This script runs via system cron every minute to:
1. Fetch latest markets from Polymarket API
2. Check which markets are new (not seen before)
3. Post new markets to Telegram with referral code
4. Queue qualifying markets for trading
5. Mark markets as seen in database

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
from typing import List, Dict, Optional
import html

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
    
    # Seen markets table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_polymarket_markets (
            market_id TEXT PRIMARY KEY,
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
            market_closed_time TIMESTAMP
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()


def fetch_markets(limit: int = 20) -> List[Dict]:
    """Fetch latest markets from Polymarket Gamma API"""
    try:
        url = f"{GAMMA_API_URL}?order=id&ascending=false&closed=false&limit={limit}"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        events = response.json()
        
        # Extract individual markets from events
        markets = []
        for event in events:
            # Each event can have multiple markets
            if "markets" in event:
                for market in event["markets"]:
                    markets.append({
                        "id": str(market.get("id", "")),
                        "question": market.get("question", event.get("title", "")),
                        "description": market.get("description", event.get("description", "")),
                        "slug": market.get("slug", event.get("slug", "")),
                        "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                        "createdAt": market.get("createdAt", event.get("createdAt")),
                        "closedTime": market.get("endDate", event.get("endDate")),
                        "tags": event.get("tags", []),
                    })
            else:
                # Single market event
                markets.append({
                    "id": str(event.get("id", "")),
                    "question": event.get("title", event.get("question", "")),
                    "description": event.get("description", ""),
                    "slug": event.get("slug", ""),
                    "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                    "createdAt": event.get("createdAt"),
                    "closedTime": event.get("endDate"),
                    "tags": event.get("tags", []),
                })
        
        log(f"Fetched {len(markets)} markets from {len(events)} events")
        return markets
        
    except Exception as e:
        log(f"ERROR fetching markets: {e}")
        return []


def get_seen_market_ids(market_ids: List[str]) -> set:
    """Check which market IDs have already been seen"""
    if not market_ids:
        return set()
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Query for existing market IDs
    placeholders = ",".join(["%s"] * len(market_ids))
    cur.execute(f"SELECT market_id FROM seen_polymarket_markets WHERE market_id IN ({placeholders})", market_ids)
    
    seen = {row[0] for row in cur.fetchall()}
    
    cur.close()
    conn.close()
    
    return seen


def mark_as_seen(market_id: str):
    """Mark a market as seen in the database"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "INSERT INTO seen_polymarket_markets (market_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (market_id,)
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


def is_updown_market(market: Dict) -> bool:
    """Check if market is an up/down short-term market"""
    name = (market.get("question", "") or "").lower()
    slug = (market.get("slug", "") or "").lower()
    tags = [str(t).lower() for t in market.get("tags", [])]
    haystack = " ".join([name, slug] + tags)
    
    for keyword in UPDOWN_KEYWORDS:
        if keyword in haystack:
            return True
    
    if "updown" in tags:
        return True
    
    return False


def is_short_duration_market(market: Dict, min_hours: int = 15) -> tuple:
    """
    Check if market duration is too short (< 15 hours)
    Returns (is_short, duration_hours)
    """
    created_at = market.get("createdAt")
    closed_time = market.get("closedTime")
    
    if not created_at or not closed_time:
        return (False, None)
    
    try:
        # Parse ISO format dates
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        closed = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
        
        duration = closed - created
        duration_hours = duration.total_seconds() / 3600
        
        return (duration_hours < min_hours, round(duration_hours, 1))
    except Exception:
        return (False, None)


def queue_trading_job(market_slug: str, created_at: Optional[str] = None, closed_time: Optional[str] = None):
    """Queue a market for trading"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Parse dates if provided
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
        INSERT INTO trading_jobs (market_id, status, market_created_at, market_closed_time)
        VALUES (%s, 'PENDING', %s, %s)
        ON CONFLICT (market_id) DO NOTHING
    """, (market_slug, market_created, market_closed))
    
    conn.commit()
    cur.close()
    conn.close()


def process_market(market: Dict) -> bool:
    """
    Process a single new market:
    1. Post to Telegram
    2. Queue for trading if it passes filters
    3. Mark as seen
    
    Returns True if Telegram post succeeded
    """
    market_id = market["id"]
    question = escape_html(market.get("question", "New Market"))
    description = market.get("description", "")
    if description:
        description = escape_html(description[:200])
        if len(market.get("description", "")) > 200:
            description += "..."
    
    market_url = add_referral_code(market.get("url", "https://polymarket.com"))
    
    # Format Telegram message
    message = f"""🔮 <b>New Polymarket Market!</b>

<b>Question:</b> {question}

"""
    
    if description:
        message += f"📊 {description}\n\n"
    
    message += f"""<a href="{market_url}">🔗 Trade on Polymarket</a>

#Polymarket #PredictionMarkets"""
    
    # Post to Telegram
    telegram_success = post_to_telegram(message)
    
    if telegram_success:
        log(f"✅ Posted to Telegram: {question[:50]}...")
        
        # Mark as seen ONLY after successful Telegram post
        mark_as_seen(market_id)
        
        # Check if market qualifies for trading
        is_updown = is_updown_market(market)
        is_short, duration = is_short_duration_market(market)
        
        if is_updown:
            log(f"⏭️  Skipped trading (up/down market): {market.get('slug', '')}")
        elif is_short:
            log(f"⏭️  Skipped trading (short duration {duration}h): {market.get('slug', '')}")
        else:
            # Queue for trading
            market_slug = market.get("slug", "")
            if market_slug:
                queue_trading_job(
                    market_slug,
                    market.get("createdAt"),
                    market.get("closedTime")
                )
                log(f"💰 Queued for trading: {market_slug}")
    else:
        log(f"❌ Failed to post to Telegram, will retry: {market_id}")
    
    return telegram_success


def main():
    """Main monitoring loop"""
    log("🚀 Starting Polymarket market monitor")
    
    # Ensure tables exist
    init_tables()
    
    # Fetch latest markets
    markets = fetch_markets(limit=20)
    
    if not markets:
        log("No markets fetched, exiting")
        return
    
    # Check which are new
    market_ids = [m["id"] for m in markets if m.get("id")]
    seen_ids = get_seen_market_ids(market_ids)
    
    new_markets = [m for m in markets if m.get("id") and m["id"] not in seen_ids]
    
    log(f"Found {len(new_markets)} new markets out of {len(markets)} total")
    
    if not new_markets:
        log("No new markets to process")
        return
    
    # Process each new market
    success_count = 0
    for market in new_markets:
        if process_market(market):
            success_count += 1
        
        # Small delay between posts to avoid rate limiting
        import time
        time.sleep(1)
    
    log(f"✅ Completed: {success_count}/{len(new_markets)} markets posted to Telegram")


if __name__ == "__main__":
    main()
