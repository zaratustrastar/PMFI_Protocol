# Polymarket Automated Trading System

Fully automated system for monitoring Polymarket markets, posting Telegram notifications, and executing ladder trading strategies.

## 🎯 What It Does

1. **Detects new markets** every minute
2. **Posts to Telegram** (@ponnymarket)
3. **Automatically trades** on new markets:
   - Buy ladders: 1¢-3¢ per share ($0.20/order, $2 total)
   - Sell ladders: 3x-10x profit on fills
4. **Notifies Telegram** when sells execute

## 🏗️ Architecture

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

## ⚠️ Critical: Cloudflare IP Blocking

Polymarket blocks datacenter IPs (including Replit). **Trading workers MUST run from residential IP.**

**What works from Replit:**
- ✅ Market monitoring
- ✅ Telegram notifications  
- ✅ Database job queueing

**What requires residential IP:**
- ❌ Order placement
- ❌ Order monitoring
- ❌ Fill detection

## 🚀 Quick Start

### 1. From Replit (Already Running)

The Mastra workflow is already running and will:
- Monitor markets every minute
- Post new markets to Telegram
- Queue trading jobs in database

### 2. From External Server (Required for Trading)

**Export database connection:**
```bash
# From Replit, get your DATABASE_URL
echo $DATABASE_URL
```

**Set up on your home computer or VPS:**
```bash
# Clone repository
git clone <your-repo-url>
cd trading_bot

# Install dependencies
pip install -r requirements.txt

# Configure environment
export DATABASE_URL="postgresql://..."
export POLYMARKET_PRIVATE_KEY="..."
export POLYMARKET_PROXY_ADDRESS="..."
export POLYMARKET_API_KEY="..."
export POLYMARKET_API_SECRET="..."
export TELEGRAM_BOT_TOKEN="..."

# Optional: Use residential proxy (if on VPS)
export PROXY_URL="http://user:pass@proxy.com:port"

# Start workers
./start_worker.sh    # Places orders
./start_monitor.sh   # Monitors fills & places sells
```

## 🔧 Configuration

Edit `config.py` to customize:
- `BUY_LADDER_PRICES`: Buy prices (currently 1¢-3¢)
- `SELL_PROFIT_MULTIPLES`: Sell multipliers (currently 3x-10x)
- `ORDER_SIZE_USD`: Order size (currently $0.20)
- `POLL_INTERVAL_SECONDS`: Check interval (currently 10s)

## 📊 Database Schema

**`trading_jobs`**: Queued trading jobs
- `id`: Job ID
- `market_id`: Market slug
- `status`: PENDING → RUNNING → COMPLETED/FAILED
- `created_at`, `updated_at`: Timestamps

**`orders`**: Tracked orders
- `order_id`: Polymarket order ID
- `market_slug`: Market identifier
- `token_id`: YES/NO token
- `side`: YES or NO
- `type`: BUY or SELL
- `price`, `size`: Order details
- `status`: OPEN → FILLED

## 🧪 Testing

From Replit:
```bash
cd trading_bot
python3 test_system.py
```

From external server:
```bash
# Test connection
python3 -c "from polymarket_trader import PolymarketTrader; t = PolymarketTrader(); print('✅ Connected')"
```

## 📝 Files

**Core:**
- `polymarket_trader.py`: Trading logic with curl_cffi Cloudflare bypass
- `auto_trader.py`: Worker that processes queued jobs
- `order_monitor.py`: Monitors fills and places sells
- `config.py`: Configuration settings

**Utilities:**
- `database.py`: PostgreSQL operations
- `telegram_notifier.py`: Telegram notifications
- `market_utils.py`: Market data fetching

**Deployment:**
- `start_worker.sh`: Start trading worker
- `start_monitor.sh`: Start order monitor
- `CLOUDFLARE_ISSUE.md`: Detailed bypass documentation

## 🐛 Troubleshooting

**"Cloudflare blocked (403)"**
- You're running from a blocked IP
- Solution: Run workers from residential IP

**"No orders placed"**
- Market may not exist or have ended
- Check token IDs in market data

**"Database connection failed"**
- Verify DATABASE_URL is correct
- Check network connectivity to Replit PostgreSQL

## 📖 More Info

See `CLOUDFLARE_ISSUE.md` for detailed Cloudflare bypass documentation.
