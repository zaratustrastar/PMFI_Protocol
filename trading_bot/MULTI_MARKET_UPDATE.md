# Multi-Market Trading Update

## What Changed

Previously, when an event had multiple sub-markets (like "Honor of Kings" with "Match Winner", "O/U 4.5", "O/U 5.5", etc.), the bot only placed orders on the **first** sub-market (Match Winner).

**Now**, the bot places orders on **ALL qualifying sub-markets** in an event.

## Files Changed

1. **market_monitor.py** - Now queues each sub-market separately with its unique `condition_id`
2. **market_utils.py** - Added `get_market_info_from_job()` to use job data directly (no API lookup needed)
3. **database.py** - Updated to fetch new columns (`event_slug`, `question`, `clob_token_ids`, `outcomes`)
4. **auto_trader.py** - Updated to use new job-based trading method
5. **polymarket_trader.py** - Added `place_orders_only_from_job()` method

## Database Schema Update

The `trading_jobs` table now has these additional columns:
- `event_slug` - Parent event slug
- `question` - Sub-market question (e.g., "Match Winner", "O/U 4.5")
- `clob_token_ids` - JSON string with YES/NO token IDs
- `outcomes` - JSON string with outcome names

These columns are auto-created when the first new job is queued.

## VPS Deployment Steps

### 1. Stop the services
```bash
sudo systemctl stop polymarket-worker
sudo systemctl stop polymarket-monitor
```

### 2. Backup existing files
```bash
cd /opt/polymarket-bot/trading_bot
cp market_monitor.py market_monitor.py.bak
cp market_utils.py market_utils.py.bak
cp database.py database.py.bak
cp auto_trader.py auto_trader.py.bak
cp polymarket_trader.py polymarket_trader.py.bak
```

### 3. Apply the update (use patch script)
Run the patch script from Replit or copy files manually:
```bash
# Option A: Use the patch script
python3 apply_multi_market_patch.py

# Option B: Manual copy (if patch fails)
# Copy content from Replit to each file
```

### 4. Verify the database columns exist
```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate
python3 -c "
import psycopg2
import os
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute(\"\"\"
    ALTER TABLE trading_jobs 
    ADD COLUMN IF NOT EXISTS event_slug TEXT,
    ADD COLUMN IF NOT EXISTS question TEXT,
    ADD COLUMN IF NOT EXISTS clob_token_ids TEXT,
    ADD COLUMN IF NOT EXISTS outcomes TEXT
\"\"\")
conn.commit()
print('✅ Database columns ready')
"
```

### 5. Test market_monitor.py
```bash
cd /opt/polymarket-bot
source trading_bot/venv/bin/activate
python3 trading_bot/market_monitor.py
```

You should see logs like:
- `💰 Queued for trading: Match Winner... (condition: abc123...)`
- `💰 Queued for trading: O/U 4.5... (condition: def456...)`

### 6. Restart services
```bash
sudo systemctl start polymarket-worker
sudo systemctl start polymarket-monitor
```

### 7. Verify services are running
```bash
sudo systemctl status polymarket-worker
sudo systemctl status polymarket-monitor
```

## Expected Behavior After Update

When an event like "Honor of Kings: FULL SENSE vs Team Flash (BO7)" is detected:

**Before (old behavior):**
- 1 Telegram post (event-level)
- 20 orders on Match Winner only

**After (new behavior):**
- Multiple Telegram posts (one per sub-market)
- 20 orders on Match Winner
- 20 orders on O/U 4.5
- 20 orders on O/U 5.5
- 20 orders on O/U 6.5
- 20 orders on Game 1-4 Winners (if they pass duration filter)

## Rollback Instructions

If something goes wrong:
```bash
sudo systemctl stop polymarket-worker
sudo systemctl stop polymarket-monitor

cd /opt/polymarket-bot/trading_bot
cp market_monitor.py.bak market_monitor.py
cp market_utils.py.bak market_utils.py
cp database.py.bak database.py
cp auto_trader.py.bak auto_trader.py
cp polymarket_trader.py.bak polymarket_trader.py

sudo systemctl start polymarket-worker
sudo systemctl start polymarket-monitor
```
