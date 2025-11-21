# Complete VPS Deployment Guide - Replit Independence

This guide will help you deploy the **entire Polymarket trading system** to your VPS, making you 100% independent from Replit.

## Overview

After following this guide, you'll have 4 services running on your VPS:

1. **mastra** - Market detection workflow (runs every minute)
2. **inngest** - Workflow orchestration engine
3. **polymarket-worker** - Order placement (already running ✅)
4. **polymarket-monitor** - Fill monitoring (already running ✅)

## Prerequisites

- DigitalOcean VPS (already set up)
- Root SSH access
- All environment variables (from Replit secrets)

## Installation Steps

### Step 1: Download Deployment Package

On your **local computer**, download the deployment package from Replit:

```bash
# In Replit Shell, create a web-accessible link
cd ~/workspace
python3 -m http.server 8080
```

Then in your browser, go to your Replit's webview URL and add `:8080/deployment/mastra-deployment.tar.gz` to download the file.

Or use this alternative method - copy the file content directly:

```bash
# On your VPS
cd /opt/polymarket-bot
wget <YOUR_REPLIT_URL>/deployment/mastra-deployment.tar.gz
# Or use scp from your local machine if you downloaded it
```

### Step 2: Install Node.js on VPS

```bash
# Install Node.js 20.x (required by Mastra)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Verify installation
node --version  # Should show v20.x.x
npm --version   # Should show 10.x.x
```

### Step 3: Extract and Set Up Mastra

```bash
# Create mastra directory
sudo mkdir -p /opt/polymarket-bot/mastra
cd /opt/polymarket-bot/mastra

# Extract deployment package
sudo tar -xzf ../mastra-deployment.tar.gz

# Set permissions
sudo chown -R root:root /opt/polymarket-bot/mastra

# Install npm dependencies (this takes 2-3 minutes)
npm install

# Verify installation
npx mastra --version
```

### Step 4: Configure Environment Variables

```bash
# Copy the template
cd /opt/polymarket-bot/mastra
cp .env.template .env

# Edit the .env file
nano .env
```

**Fill in all the values from your Replit secrets:**

- `DATABASE_URL` - Already filled with Neon URL
- `POLYMARKET_PRIVATE_KEY` - From Replit
- `POLYMARKET_PROXY_ADDRESS` - From Replit
- `POLYMARKET_API_KEY` - From Replit
- `POLYMARKET_API_SECRET` - From Replit
- `TELEGRAM_BOT_TOKEN` - From Replit
- `TWITTER_API_KEY` - From Replit
- `TWITTER_API_SECRET` - From Replit
- `TWITTER_ACCESS_TOKEN` - From Replit
- `TWITTER_ACCESS_SECRET` - From Replit
- `AI_INTEGRATIONS_OPENAI_API_KEY` - From Replit (or leave blank if using your own OpenAI key)
- `SESSION_SECRET` - Generate a random string: `openssl rand -hex 32`

**IMPORTANT:** Make sure there are NO comments in the .env file (systemd can't parse them).

Save and exit (Ctrl+X, then Y, then Enter).

### Step 5: Install Systemd Services

```bash
# Copy service files
sudo cp /opt/polymarket-bot/mastra/systemd/mastra.service /etc/systemd/system/
sudo cp /opt/polymarket-bot/mastra/systemd/inngest.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable services to start on boot
sudo systemctl enable mastra.service
sudo systemctl enable inngest.service
```

### Step 6: Start All Services

```bash
# Start Mastra and Inngest
sudo systemctl start mastra.service
sudo systemctl start inngest.service

# Check status
sudo systemctl status mastra.service --no-pager -l
sudo systemctl status inngest.service --no-pager -l

# Worker and monitor should already be running
sudo systemctl status polymarket-worker.service --no-pager -l
sudo systemctl status polymarket-monitor.service --no-pager -l
```

### Step 7: Verify Everything Works

```bash
# Watch Mastra logs (should show workflow running every minute)
journalctl -u mastra.service -f

# You should see:
# - Workflow triggered every minute
# - Markets being fetched from Polymarket
# - New markets posted to Telegram
# - Jobs queued in database

# In another terminal, watch worker logs
journalctl -u polymarket-worker.service -f

# You should see:
# - Worker polling for jobs
# - Jobs being picked up and processed
# - Orders being placed
```

### Step 8: Test End-to-End Flow

Wait for the workflow to run (every minute on the minute), then:

```bash
# Check recent jobs in database
psql "postgresql://neondb_owner:npg_sPnNQtm3xf8h@ep-noisy-lab-ahf3wcvu.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require" -c "SELECT market_id, status, created_at FROM trading_jobs ORDER BY created_at DESC LIMIT 5;"

# Check Telegram channel @ponnymarket for new market notifications

# Monitor worker processing jobs
journalctl -u polymarket-worker.service --since "5 minutes ago" --no-pager
```

## Management Commands

### Check Service Status

```bash
# All services at once
sudo systemctl status mastra polymarket-worker polymarket-monitor inngest

# Individual services
sudo systemctl status mastra.service
sudo systemctl status inngest.service
sudo systemctl status polymarket-worker.service
sudo systemctl status polymarket-monitor.service
```

### View Logs

```bash
# Real-time logs (Ctrl+C to stop)
journalctl -u mastra.service -f
journalctl -u inngest.service -f
journalctl -u polymarket-worker.service -f
journalctl -u polymarket-monitor.service -f

# Last 50 lines
journalctl -u mastra.service -n 50 --no-pager
journalctl -u polymarket-worker.service -n 50 --no-pager

# Logs from last hour
journalctl -u mastra.service --since "1 hour ago" --no-pager
```

### Start/Stop/Restart Services

```bash
# Restart a service (after config changes)
sudo systemctl restart mastra.service
sudo systemctl restart polymarket-worker.service

# Stop a service
sudo systemctl stop mastra.service

# Start a service
sudo systemctl start mastra.service

# Restart all services
sudo systemctl restart mastra inngest polymarket-worker polymarket-monitor
```

### Update Code

If you make changes to the Mastra workflow:

```bash
# 1. Upload new deployment package to VPS
cd /opt/polymarket-bot
# Upload mastra-deployment.tar.gz

# 2. Stop services
sudo systemctl stop mastra.service inngest.service

# 3. Backup current code
sudo mv /opt/polymarket-bot/mastra /opt/polymarket-bot/mastra.backup

# 4. Extract new code
sudo mkdir /opt/polymarket-bot/mastra
cd /opt/polymarket-bot/mastra
sudo tar -xzf ../mastra-deployment.tar.gz

# 5. Restore .env file
sudo cp /opt/polymarket-bot/mastra.backup/.env .

# 6. Install dependencies
npm install

# 7. Start services
sudo systemctl start mastra.service inngest.service

# 8. Check logs
journalctl -u mastra.service -f
```

## Troubleshooting

### Mastra won't start

```bash
# Check logs for errors
journalctl -u mastra.service -n 50 --no-pager

# Common issues:
# - Missing environment variables: Check .env file
# - Node modules not installed: Run `npm install` in /opt/polymarket-bot/mastra
# - Port 5000 already in use: Check what's using it with `sudo lsof -i :5000`
```

### Inngest won't start

```bash
# Check logs
journalctl -u inngest.service -n 50 --no-pager

# Make sure Mastra started first
sudo systemctl status mastra.service

# Inngest connects to Mastra on port 5000
```

### Worker not processing jobs

```bash
# Check worker logs
journalctl -u polymarket-worker.service -n 50 --no-pager

# Check database connection
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM trading_jobs WHERE status = 'PENDING';"

# Verify environment variables loaded
journalctl -u polymarket-worker.service | grep "Loaded environment"
```

### No jobs being created

```bash
# Check Mastra workflow logs
journalctl -u mastra.service | grep "queueTradingJob"

# Verify workflow is running every minute
journalctl -u mastra.service --since "10 minutes ago" | grep "workflow"

# Check database
psql "$DATABASE_URL" -c "SELECT * FROM trading_jobs ORDER BY created_at DESC LIMIT 5;"
```

## Success Indicators

✅ **Everything is working when you see:**

1. **Mastra logs**: Workflow runs every minute, fetches markets, posts to Telegram
2. **Inngest logs**: No errors, processing workflow steps
3. **Worker logs**: Polling every 30s, picking up jobs, placing orders
4. **Monitor logs**: Checking for fills, canceling stale orders
5. **Telegram**: New markets appearing in @ponnymarket channel
6. **Database**: Jobs transitioning from PENDING → RUNNING → COMPLETED

## Cost Breakdown

- **VPS**: $6/month (DigitalOcean)
- **Database**: Free (Neon free tier)
- **Total**: **$6/month** + trading capital

**You are now 100% independent from Replit!** 🎉

## Next Steps

1. Monitor the system for 24 hours to ensure stability
2. Check Telegram for market notifications
3. Watch for successful order placements
4. Once confident, you can cancel your Replit subscription
