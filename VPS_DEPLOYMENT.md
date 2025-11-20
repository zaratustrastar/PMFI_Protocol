# 🚀 VPS Deployment Guide - Full 24/7 Polymarket Trading System

This guide walks you through deploying the **entire system** (Mastra workflow + Python workers + Telegram bot) on a VPS with residential proxy for 24/7 automated trading.

---

## 📋 What You'll Deploy

Four processes running 24/7:

1. **Mastra Dev Server** - TypeScript framework server
2. **Inngest Server** - Workflow orchestration (cron trigger)
3. **Trading Worker** (`auto_trader.py`) - Processes job queue, places orders
4. **Order Monitor** (`order_monitor.py`) - Monitors fills, places sells, notifies Telegram

---

## ✅ Prerequisites

### VPS Requirements
- **OS**: Ubuntu 22.04 LTS (recommended)
- **RAM**: 2-4 GB minimum
- **CPU**: 2 vCPU cores
- **Storage**: 20-30 GB SSD
- **Network**: Must support residential proxy or be residential IP itself

### Residential Proxy Options

**Option A: VPS with Residential IP** (Easiest)
- Use VPS provider with residential IP addresses
- No additional proxy setup needed
- Examples: TradingVPS.io with residential option

**Option B: Datacenter VPS + Residential Proxy Service** (Most Common)
- Use any VPS (DigitalOcean, Vultr, Linode, etc.)
- Subscribe to residential proxy service:
  - **Bright Data** - $500 starter ($0.60-1.20/GB)
  - **IPRoyal** - From $7/month (residential)
  - **Oxylabs** - Enterprise solution
  - **NordVPN** (cheapest but limited) - $3-12/month

**⚠️ CRITICAL**: Polymarket's trading API blocks datacenter IPs. You **MUST** use residential proxy or residential VPS.

---

## 🛠️ Step 1: VPS Initial Setup

### Connect to VPS
```bash
ssh root@your-vps-ip
```

### Update System
```bash
apt update && apt upgrade -y
```

### Install Node.js 20+
```bash
# Install Node.js 20.x
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

# Verify
node --version  # Should show v20.x.x
npm --version
```

### Install Python 3.11+
```bash
apt install -y python3 python3-pip python3-venv
python3 --version  # Should show 3.11+
```

### Install PostgreSQL
```bash
apt install -y postgresql postgresql-contrib

# Start PostgreSQL
systemctl start postgresql
systemctl enable postgresql

# Create database
sudo -u postgres psql -c "CREATE DATABASE polymarket_trading;"
sudo -u postgres psql -c "CREATE USER polymarket WITH PASSWORD 'your_secure_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE polymarket_trading TO polymarket;"
sudo -u postgres psql -c "GRANT ALL ON SCHEMA public TO polymarket;"
```

### Install Git & Essential Tools
```bash
apt install -y git curl wget ufw
```

### Configure Firewall (Optional but Recommended)
```bash
ufw allow ssh
ufw allow 3000/tcp  # Mastra dev server
ufw allow 8288/tcp  # Inngest server
ufw enable
```

---

## 📦 Step 2: Deploy Application Code

### Clone Repository
```bash
cd /opt
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git polymarket-bot
cd polymarket-bot
```

### Setup Node.js Dependencies
```bash
npm install
```

### Setup Python Dependencies
```bash
cd trading_bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cd ..
```

---

## 🔐 Step 3: Configure Environment Variables

### Create .env File
```bash
nano .env
```

### Add Configuration
```bash
# Database (Local PostgreSQL)
DATABASE_URL=postgresql://polymarket:your_secure_password@localhost:5432/polymarket_trading
PGHOST=localhost
PGPORT=5432
PGUSER=polymarket
PGPASSWORD=your_secure_password
PGDATABASE=polymarket_trading

# Polymarket Trading (from Replit secrets)
POLYMARKET_PRIVATE_KEY=your_private_key_here
POLYMARKET_PROXY_ADDRESS=your_proxy_wallet_address
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret

# Telegram Bot
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# Twitter (Optional)
TWITTER_API_KEY=your_twitter_key
TWITTER_API_SECRET=your_twitter_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_SECRET=your_access_secret

# Session Secret
SESSION_SECRET=generate_random_string_here

# AI Integration (if using Replit AI)
AI_INTEGRATIONS_OPENAI_BASE_URL=https://your-base-url
AI_INTEGRATIONS_OPENAI_API_KEY=your_key

# Residential Proxy (if using external proxy service)
HTTP_PROXY=http://username:password@residential-proxy.com:port
HTTPS_PROXY=http://username:password@residential-proxy.com:port
```

**⚠️ Security**: Set proper permissions
```bash
chmod 600 .env
chown root:root .env
```

---

## 🗄️ Step 4: Initialize Database

### Run Migration Script
```bash
cd trading_bot
source venv/bin/activate
python3 add_market_dates.py
```

This adds the `market_created_at` column required by the worker.

### Push Drizzle Schema (if using Drizzle ORM)
```bash
cd /opt/polymarket-bot
npm run db:push
```

---

## 🔄 Step 5: Configure Residential Proxy

### Option A: Environment Variables (Simple)
Already done in Step 3 if using external proxy service.

### Option B: System-Wide Proxy (Advanced)
```bash
nano /etc/environment
```

Add:
```bash
http_proxy="http://username:password@proxy.com:port"
https_proxy="http://username:password@proxy.com:port"
no_proxy="localhost,127.0.0.1"
```

Reload:
```bash
source /etc/environment
```

### Test Proxy Connection
```bash
# Test with curl
curl --proxy http://username:password@proxy.com:port https://clob.polymarket.com/markets

# Should return JSON if proxy works
```

---

## ⚙️ Step 6: Create systemd Service Files

See `deployment/systemd/` folder for all service files:
- `mastra.service` - Mastra dev server
- `inngest.service` - Inngest server
- `polymarket-worker.service` - Trading worker
- `polymarket-monitor.service` - Order monitor

### Install Services
```bash
cp deployment/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
```

---

## 🚀 Step 7: Start All Services

### Enable Auto-Start on Boot
```bash
systemctl enable mastra.service
systemctl enable inngest.service
systemctl enable polymarket-worker.service
systemctl enable polymarket-monitor.service
```

### Start Services
```bash
systemctl start inngest.service      # Start first (required by Mastra)
sleep 5
systemctl start mastra.service       # Start second
systemctl start polymarket-worker.service
systemctl start polymarket-monitor.service
```

### Check Status
```bash
systemctl status mastra.service
systemctl status inngest.service
systemctl status polymarket-worker.service
systemctl status polymarket-monitor.service
```

---

## 📊 Step 8: Monitor Logs

### Real-Time Logs
```bash
# Mastra workflow logs
journalctl -u mastra.service -f

# Inngest server logs
journalctl -u inngest.service -f

# Worker logs (order placement)
journalctl -u polymarket-worker.service -f

# Monitor logs (fill detection)
journalctl -u polymarket-monitor.service -f
```

### View Last 100 Lines
```bash
journalctl -u polymarket-worker.service -n 100
```

### Check for Errors
```bash
journalctl -u mastra.service --since "1 hour ago" | grep ERROR
```

---

## 🔍 Step 9: Verify System is Working

### Check Workflow Execution
```bash
# Should see new markets being detected every minute
journalctl -u mastra.service -f | grep "New market detected"
```

### Check Telegram Notifications
- Open @ponnymarket channel
- Should see new markets posted

### Check Job Queue
```bash
sudo -u postgres psql polymarket_trading -c "SELECT * FROM trading_jobs ORDER BY created_at DESC LIMIT 10;"
```

### Check Worker Processing
```bash
journalctl -u polymarket-worker.service -f
# Should see: "Processing job..." when jobs are available
```

### Check Monitor Activity
```bash
journalctl -u polymarket-monitor.service -f
# Will show activity only when orders are filled
```

---

## 🛠️ Troubleshooting

### Service Won't Start
```bash
# Check logs for errors
journalctl -xe -u mastra.service

# Test manually
cd /opt/polymarket-bot
npm run dev  # Test Mastra
./scripts/inngest.sh  # Test Inngest
```

### Database Connection Errors
```bash
# Test connection
sudo -u postgres psql polymarket_trading

# Check DATABASE_URL in .env
cat .env | grep DATABASE_URL
```

### Cloudflare 403 Errors (Proxy Not Working)
```bash
# Test proxy
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate
python3 -c "
from polymarket_trader import PolymarketTrader
trader = PolymarketTrader()
print('Proxy test successful!')
"
```

### Worker Not Processing Jobs
```bash
# Check pending jobs
sudo -u postgres psql polymarket_trading -c "SELECT COUNT(*) FROM trading_jobs WHERE status = 'PENDING';"

# Check worker logs
journalctl -u polymarket-worker.service -n 50
```

### No Orders Being Placed
**Possible causes:**
1. All markets filtered out (up/down or <15h duration)
2. No pending jobs in queue
3. Cloudflare blocking requests
4. Insufficient USDC balance

**Check:**
```bash
# See filtered markets in logs
journalctl -u mastra.service | grep "filtered"

# Check USDC balance
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate
python3 -c "
from polymarket_trader import PolymarketTrader
trader = PolymarketTrader()
balance = trader.get_usdc_balance()
print(f'USDC Balance: ${balance}')
"
```

---

## 🔄 Common Maintenance Tasks

### Restart All Services
```bash
systemctl restart inngest.service
systemctl restart mastra.service
systemctl restart polymarket-worker.service
systemctl restart polymarket-monitor.service
```

### Update Code
```bash
cd /opt/polymarket-bot
git pull
npm install
cd trading_bot
source venv/bin/activate
pip install -r requirements.txt
systemctl restart mastra.service polymarket-worker.service polymarket-monitor.service
```

### View Resource Usage
```bash
htop
# Or
systemctl status mastra.service polymarket-worker.service
```

### Backup Database
```bash
sudo -u postgres pg_dump polymarket_trading > /opt/backups/polymarket_$(date +%Y%m%d).sql
```

---

## 📈 Performance Optimization

### Adjust Worker Poll Interval
Edit `trading_bot/auto_trader.py`:
```python
POLL_INTERVAL = 10  # Check queue every 10 seconds (default: 30)
```

### Adjust Monitor Poll Interval
Edit `trading_bot/order_monitor.py`:
```python
POLL_INTERVAL = 15  # Check fills every 15 seconds (default: 30)
```

### Workflow Frequency
Edit `src/mastra/workflows/polymarketWorkflow.ts`:
```typescript
cron: '*/2 * * * *',  // Every 2 minutes instead of 1
```

---

## 🔐 Security Checklist

- [ ] `.env` file has 600 permissions
- [ ] PostgreSQL password is strong
- [ ] UFW firewall enabled
- [ ] SSH key authentication enabled (disable password auth)
- [ ] Private keys never committed to Git
- [ ] Regular backups scheduled
- [ ] Proxy credentials secured

---

## 📞 Support

If you encounter issues:

1. Check logs: `journalctl -u SERVICE_NAME -n 100`
2. Test components manually (see troubleshooting)
3. Verify proxy connectivity
4. Check USDC balance
5. Review Telegram channel for notifications

---

## 🎯 Quick Start Summary

```bash
# 1. Install dependencies
apt update && apt install -y nodejs python3 postgresql git

# 2. Clone code
git clone YOUR_REPO /opt/polymarket-bot

# 3. Install packages
cd /opt/polymarket-bot
npm install
cd trading_bot && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

# 4. Configure .env
nano /opt/polymarket-bot/.env

# 5. Setup database
sudo -u postgres psql -c "CREATE DATABASE polymarket_trading;"
cd /opt/polymarket-bot/trading_bot && python3 add_market_dates.py

# 6. Install services
cp deployment/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

# 7. Start everything
systemctl enable --now inngest.service mastra.service polymarket-worker.service polymarket-monitor.service

# 8. Monitor
journalctl -u mastra.service -f
```

---

**Your system is now running 24/7! 🎉**

Check Telegram @ponnymarket for market notifications and fill confirmations.
