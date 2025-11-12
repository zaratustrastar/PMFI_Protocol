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

### 1. From Replit (Already Running) ✅

The Mastra workflow is already running and will:
- Monitor markets every minute
- Post new markets to Telegram
- Queue trading jobs in database

**This part is working!** You can see it posting to your Telegram.

### 2. From Your Home Computer (Required for Trading) 🏠

**Trading is blocked on Replit due to Cloudflare IP filtering.**

👉 **Follow the super simple guide:** `EASY_SETUP.md`

**Quick version:**
1. Run `cd trading_bot && ./export_env.sh` on Replit to get your passwords
2. Download code to your computer
3. Run `pip install -r requirements.txt`
4. Create `.env` file with your passwords
5. Run `python3 auto_trader.py` and `python3 order_monitor.py`

**Full detailed instructions:** See `EASY_SETUP.md` for step-by-step guide!

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
