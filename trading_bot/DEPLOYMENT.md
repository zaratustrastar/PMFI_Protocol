# Deployment Guide - Polymarket Auto-Trader

## Overview
This guide will help you deploy the Polymarket Auto-Trader as a Replit Scheduled Deployment that automatically:
1. Detects new markets from your monitoring system
2. Trades them with $2 per market
3. Notifies Telegram when sells execute

## Prerequisites

✅ All required secrets are already set in your Replit:
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_PROXY_ADDRESS`
- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`

✅ Your monitoring system is already running and detecting new markets

## Deployment Steps

### 1. Open Publishing Tool
- Click on the **Tool dock** (left sidebar)
- Select "All tools" → "Publishing"
- Or type "Publishing" in the Search bar

### 2. Select Scheduled Deployment
- Choose "Scheduled" option
- Click "Set up your published app"

### 3. Configure Schedule
Fill in these fields:

**Schedule description:**
```
Every 15 minutes
```

**Timezone:**
Select your timezone (e.g., "America/New_York")

**Job Timeout:**
```
10 minutes
```

### 4. Build Command
Leave empty (no build needed for Python)

### 5. Run Command

**Option A: Auto-detect markets (requires integration testing):**
```bash
cd trading_bot && python3 auto_trader.py
```

**Option B: Manual market list (recommended for testing):**
Create a file `trading_bot/markets_to_trade.txt` with one market slug per line, then:
```bash
cd trading_bot && python3 -c "import sys; from polymarket_trader import PolymarketTrader; trader = PolymarketTrader(); [trader.run_strategy_limited(line.strip(), 30) for line in open('markets_to_trade.txt') if line.strip()]"
```

Start with Option B for testing, then move to Option A once you verify the integration works.

### 6. Deployment Secrets
These should auto-populate from your Replit secrets:
- POLYMARKET_PRIVATE_KEY
- POLYMARKET_PROXY_ADDRESS  
- TELEGRAM_BOT_TOKEN
- DATABASE_URL

If not, add them manually.

### 7. Deploy!
Click "Deploy" and your auto-trader will start running every 15 minutes!

## How It Works

### Every 15 Minutes:
1. ⏰ Scheduled deployment runs
2. 🔍 Checks for new markets (last 1 hour)
3. 💰 Trades each new market ($2 budget)
4. 📊 Places buy orders (0.1¢ to 1¢)
5. 🎯 Monitors for 30 minutes max per market
6. 💸 Auto-places sell orders when buys fill
7. 📱 Notifies Telegram when sells execute
8. ⏹️  Exits, waits for next scheduled run

### What Gets Posted to Telegram:
**ONLY executed sells:**
```
🎯 SELL EXECUTED

Market: bitcoin-100k-dec-31
Side: YES
Tokens: 12.50

Buy: $0.0020
Sell: $0.0060

💰 Profit: $0.50 (+200%)
```

## Monitoring Your Deployment

### View Logs:
- Go to Publishing tool
- Click on your scheduled deployment
- View execution logs

### Check Database:
```bash
cd trading_bot
python3 -c "from database import get_all_positions; import json; print(json.dumps(get_all_positions(), indent=2))"
```

### Telegram Channel:
Monitor @ponnymarket for sell execution notifications

## Cost Estimate

**Per market traded:**
- $2 investment per market
- Runs for max 30 minutes per market
- Multiple markets can be queued

**Replit usage:**
- ~10 minutes compute per run
- Runs every 15 minutes
- Approximately ~$5-10/month in compute (varies by activity)

## Manual Override

### Test a single market manually:
```bash
cd trading_bot
python3 run_trader.py <market-slug>
```

### Run auto-trader once (not scheduled):
```bash
cd trading_bot  
python3 auto_trader.py
```

## Troubleshooting

### No markets being traded:
- Check that your monitoring system is detecting new markets
- Verify markets are saved to `polymarket_markets` table
- Run: `python3 -c "from auto_trader import get_new_markets; print(get_new_markets())"`

### Telegram notifications not working:
- Verify `TELEGRAM_BOT_TOKEN` is set
- Check bot has permission to post to @ponnymarket

### Orders failing:
- Check USDC balance in your Polymarket wallet
- Verify private key and proxy address are correct
- Check Polymarket API status

## Stopping Auto-Trader

### Pause Deployment:
- Go to Publishing tool
- Click your scheduled deployment
- Click "Pause" or "Delete"

### Cancel All Orders:
You'll need to manually cancel orders on polymarket.com
- Go to polymarket.com
- Connect your wallet
- View and cancel open orders

## Support

- Check logs first for error messages
- Review database for position status
- Test manually with single market before scaling
