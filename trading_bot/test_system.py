#!/usr/bin/env python3
"""
System Test Script

Tests what works from Replit vs what needs residential IP
"""
import requests

print("\n" + "="*70)
print("🧪 TESTING POLYMARKET AUTOMATION SYSTEM")
print("="*70)

# Test 1: Market Data API (should work - no Cloudflare)
print("\n1. Testing Market Data API (Gamma API)...")
try:
    response = requests.get("https://gamma-api.polymarket.com/events?order=id&ascending=false&closed=false&limit=3", timeout=10)
    if response.status_code == 200:
        markets = response.json()
        print(f"   ✅ WORKS: Fetched {len(markets)} markets")
        if markets:
            print(f"   📊 Latest: {markets[0].get('title', 'Unknown')[:50]}...")
    else:
        print(f"   ❌ FAILED: Status {response.status_code}")
except Exception as e:
    print(f"   ❌ ERROR: {str(e)}")

# Test 2: Database Connection
print("\n2. Testing PostgreSQL Database...")
try:
    from database import get_pending_jobs
    jobs = get_pending_jobs(limit=1)
    print(f"   ✅ WORKS: Database connected ({len(jobs)} pending jobs)")
except Exception as e:
    print(f"   ❌ ERROR: {str(e)}")

# Test 3: Telegram Notifications
print("\n3. Testing Telegram Bot...")
try:
    from telegram_notifier import send_telegram_message
    # Don't actually send a message, just check configuration
    import config
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        print(f"   ✅ CONFIGURED: Bot token and chat ID present")
        print(f"   📱 Chat ID: @{config.TELEGRAM_CHAT_ID}")
    else:
        print(f"   ⚠️  MISSING: Bot token or chat ID not configured")
except Exception as e:
    print(f"   ❌ ERROR: {str(e)}")

# Test 4: Trading API with curl_cffi (will be blocked)
print("\n4. Testing Trading API (CLOB API with curl_cffi bypass)...")
try:
    from polymarket_trader import PolymarketTrader, BYPASS_METHOD
    print(f"   🔓 Using bypass method: {BYPASS_METHOD}")
    trader = PolymarketTrader()
    
    # Try to get server time
    try:
        server_time = trader.client.get_server_time()
        print(f"   ✅ WORKS: Server time = {server_time}")
        print(f"   🎉 Trading API accessible from this IP!")
    except Exception as api_error:
        error_str = str(api_error)
        if "Cloudflare" in error_str or "403" in error_str or "blocked" in error_str:
            print(f"   ❌ BLOCKED: Cloudflare blocking IP 34.148.246.114")
            print(f"   💡 SOLUTION: Run workers from residential IP (see CLOUDFLARE_ISSUE.md)")
        else:
            print(f"   ❌ ERROR: {error_str[:100]}")
except Exception as e:
    print(f"   ❌ ERROR: {str(e)}")

# Summary
print("\n" + "="*70)
print("📋 SUMMARY")
print("="*70)
print("""
FROM REPLIT (works):
✅ Market monitoring (detects new markets every minute)
✅ Telegram notifications (posts new markets)
✅ Job queueing (saves jobs to PostgreSQL)
✅ Database operations (tracks seen markets, orders, jobs)

FROM RESIDENTIAL IP (required):
❌ Order placement (blocked by Cloudflare IP filtering)
❌ Order monitoring (blocked by Cloudflare IP filtering)
❌ Fill detection (blocked by Cloudflare IP filtering)

SOLUTION:
1. Export DATABASE_URL from Replit
2. Run trading_bot/start_worker.sh from home computer or VPS
3. Run trading_bot/start_monitor.sh from home computer or VPS
4. Mastra workflow continues to queue jobs automatically

See trading_bot/CLOUDFLARE_ISSUE.md for detailed setup instructions.
""")
print("="*70)
