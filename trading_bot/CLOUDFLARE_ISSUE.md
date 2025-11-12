# Cloudflare IP Blocking Issue

## Problem
Polymarket's CLOB API (`clob.polymarket.com`) uses **aggressive Cloudflare protection** that blocks datacenter IPs, including Replit's infrastructure (IP: 34.148.246.114).

**Error**: `403 Forbidden - "Sorry, you have been blocked"`

This affects:
- ❌ Order placement (`create_order`, `post_order`)
- ❌ Order status checks (`get_order`)
- ✅ Market data fetching (works - uses different API)

## Why curl_cffi Doesn't Work
We've implemented TLS fingerprint spoofing using `curl_cffi` with Chrome 120 browser emulation, but Cloudflare's latest version (as of Nov 2024) now blocks based on:
1. **IP reputation** (datacenter/VPS IPs are flagged)
2. **Request patterns** (even with perfect browser headers)
3. **Challenge responses** (requires JavaScript execution)

**Reference**: [py-clob-client Issue #91](https://github.com/Polymarket/py-clob-client/issues/91)

## Solution: Run Trading Workers Externally

### Architecture
```
┌─────────────────────────────────────────────────────┐
│ REPLIT (Mastra Workflow)                            │
│ ✅ Market monitoring every minute                   │
│ ✅ Telegram notifications                           │
│ ✅ Queue jobs in PostgreSQL database                │
└─────────────────────────┬───────────────────────────┘
                          │
                          │ (Database connection)
                          │
┌─────────────────────────▼───────────────────────────┐
│ EXTERNAL SERVER (Residential IP / VPS)              │
│ ✅ Trading worker (auto_trader.py)                  │
│ ✅ Order monitor (order_monitor.py)                 │
│ ✅ Place orders & monitor fills                     │
└──────────────────────────────────────────────────────┘
```

### Required Setup

#### 1. **Export Database Credentials**
From Replit, get your PostgreSQL connection string:
```bash
echo $DATABASE_URL
```

#### 2. **Set Up External Server**
Choose one option:

**Option A: Residential Network (Best)**
- Run workers from home computer
- Uses your ISP's residential IP
- Lowest chance of blocking

**Option B: VPS with Residential Proxy**
- Rent from: Bright Data, Smartproxy, IPRoyal
- Cost: ~$75-300/month
- Configure via environment variable:
  ```bash
  export PROXY_URL="http://username:password@proxy.example.com:port"
  ```
- The code will automatically use the proxy for all trading API calls

**Option C: VPS with IP Whitelist**
- Request IP allowlist from Polymarket
- Not guaranteed to work

#### 3. **Install Dependencies on External Server**
```bash
# Clone repository
git clone <your-repo-url>
cd trading_bot

# Install Python dependencies
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL="postgresql://..."
export POLYMARKET_PRIVATE_KEY="..."
export POLYMARKET_PROXY_ADDRESS="..."
export POLYMARKET_API_KEY="..."
export POLYMARKET_API_SECRET="..."
export TELEGRAM_BOT_TOKEN="..."
```

#### 4. **Run Workers**
```bash
# Start trading worker (processes queued jobs)
./start_worker.sh

# Start order monitor (monitors fills & places sells)
./start_monitor.sh
```

### How It Works
1. **Replit** detects new markets → posts Telegram → queues job in DB
2. **External worker** polls DB → places orders → marks complete
3. **External monitor** watches orders → places sells → notifies Telegram

### Testing from External Server
```bash
# Test connection
python3 -c "from polymarket_trader import PolymarketTrader; t = PolymarketTrader(); print(t.client.get_server_time())"

# Should output server timestamp (NOT Cloudflare error)
```

### Alternative: Playwright/Selenium
If you must run from Replit, you can use browser automation (very slow):
```bash
pip install playwright
playwright install chromium
```

Then modify `polymarket_trader.py` to use Playwright for API calls. **Not recommended** due to complexity and resource usage.

## Current Status
- ✅ curl_cffi implemented with Chrome 120 TLS spoofing
- ✅ Enhanced browser headers (User-Agent, Sec-Ch-Ua, etc.)
- ❌ Still blocked by Cloudflare IP-based filtering
- ✅ Solution: Run workers from residential IP

## Next Steps
1. Export `DATABASE_URL` from Replit
2. Set up external server (home computer or VPS)
3. Install dependencies and configure environment
4. Run `start_worker.sh` and `start_monitor.sh`
5. Monitor Telegram for notifications

The Mastra workflow will continue to detect markets and queue jobs automatically.
