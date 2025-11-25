# Fix VPS Autonomy - Make System Run Without Replit

## The Problem

Your Telegram posts stop when Replit goes to sleep because the **Mastra and Inngest systemd services on your VPS are misconfigured**. They're pointing to the wrong directories and using the wrong commands, so they fail to start properly.

**What's happening:**
- The VPS systemd services try to run from `/opt/polymarket-bot` (doesn't exist)
- They use `npm run dev` (Replit-specific command)
- Services fail to start or crash immediately
- **Only Replit's dev server is actually running the workflow**

**What should happen:**
- VPS services run from `/opt/polymarket-bot/mastra` (correct location)
- They use `npx mastra dev` (production command)
- Services run 24/7 independently
- Replit is NOT needed at all

## The Solution

### Quick Fix (5 minutes)

SSH into your VPS and run this one command:

```bash
# Download and run the fix script
cd /tmp
curl -O https://raw.githubusercontent.com/YOUR_REPO/deployment/FIX_VPS_AUTONOMY.sh
sudo bash FIX_VPS_AUTONOMY.sh
```

Or manually copy the fix script to your VPS:

```bash
# On VPS
cd /tmp
nano fix-vps.sh
# Paste the contents of deployment/FIX_VPS_AUTONOMY.sh
# Save and exit (Ctrl+X, Y, Enter)

sudo chmod +x fix-vps.sh
sudo ./fix-vps.sh
```

### What the Fix Does

1. Stops current mastra/inngest services
2. Updates `/etc/systemd/system/mastra.service` with correct paths:
   - WorkingDirectory: `/opt/polymarket-bot/mastra`
   - EnvironmentFile: `/opt/polymarket-bot/mastra/.env`
   - ExecStart: `/usr/bin/npx mastra dev`
3. Updates `/etc/systemd/system/inngest.service` similarly
4. Reloads systemd configuration
5. Starts services with correct settings

### Verify It Works

After running the fix:

```bash
# 1. Check service status
sudo systemctl status mastra.service --no-pager -l

# Should show:
# ✅ Active: active (running)
# ✅ Working Directory: /opt/polymarket-bot/mastra

# 2. Watch logs in real-time
journalctl -u mastra.service -f

# Should show (within 1 minute):
# ✅ Workflow triggered
# ✅ Markets fetched
# ✅ Telegram posts sent

# 3. Verify Telegram channel
# Check @ponnymarket - you should see new market posts

# 4. ULTIMATE TEST: Stop Replit
# Close Replit browser tab completely
# Wait 2 minutes
# Check @ponnymarket again - new posts should STILL appear!
```

### Verify All 4 Services

```bash
sudo systemctl status mastra inngest polymarket-worker polymarket-monitor

# All should show "active (running)"
```

## Manual Fix (if script doesn't work)

### Step 1: Update mastra.service

```bash
sudo nano /etc/systemd/system/mastra.service
```

Replace entire contents with:

```ini
[Unit]
Description=Mastra Workflow Server - Market Detection & Job Queuing
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/polymarket-bot/mastra
EnvironmentFile=/opt/polymarket-bot/mastra/.env
Environment="NODE_ENV=production"
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=/usr/bin/npx mastra dev
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mastra

[Install]
WantedBy=multi-user.target
```

Save (Ctrl+X, Y, Enter)

### Step 2: Update inngest.service

```bash
sudo nano /etc/systemd/system/inngest.service
```

Replace entire contents with:

```ini
[Unit]
Description=Inngest Workflow Orchestration Server
After=network.target mastra.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/polymarket-bot/mastra
EnvironmentFile=/opt/polymarket-bot/mastra/.env
Environment="NODE_ENV=production"
Environment="PATH=/usr/bin:/usr/local/bin"
ExecStart=/bin/bash /opt/polymarket-bot/mastra/scripts/inngest.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=inngest

[Install]
WantedBy=multi-user.target
```

Save (Ctrl+X, Y, Enter)

### Step 3: Apply Changes

```bash
# Reload systemd
sudo systemctl daemon-reload

# Restart services
sudo systemctl restart mastra.service
sleep 5
sudo systemctl restart inngest.service

# Check status
sudo systemctl status mastra.service --no-pager -l
```

## Troubleshooting

### mastra.service fails to start

```bash
# Check exact error
journalctl -u mastra.service -n 50 --no-pager

# Common issues:

# 1. Missing /opt/polymarket-bot/mastra directory
ls -la /opt/polymarket-bot/mastra
# If missing, you need to deploy Mastra code to VPS first (see INSTALLATION_GUIDE.md)

# 2. Missing .env file
ls -la /opt/polymarket-bot/mastra/.env
# If missing, copy from Replit or create from template

# 3. Node.js not installed
node --version
# If missing: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs

# 4. npm modules not installed
cd /opt/polymarket-bot/mastra && npm install
```

### inngest.service fails to start

```bash
# Check if Mastra is running first
sudo systemctl status mastra.service

# Inngest depends on Mastra being available at http://localhost:5000
# If Mastra isn't running, Inngest will fail

# Check Inngest logs
journalctl -u inngest.service -n 50 --no-pager
```

### Services start but no workflow runs

```bash
# Check if cron trigger is registered
journalctl -u inngest.service | grep "cron-trigger"

# Should see: "initializing fn function=cron-trigger"

# Check Mastra logs for workflow execution
journalctl -u mastra.service | grep "polymarket-monitor"

# Should see workflow execution every minute
```

## Verification Checklist

✅ **Autonomy achieved when:**

1. `systemctl status mastra.service` shows "active (running)"
2. `systemctl status inngest.service` shows "active (running)"
3. `journalctl -u mastra.service -f` shows workflow running every minute
4. New markets appear in @ponnymarket Telegram channel
5. **Most importantly: Close Replit completely and posts STILL continue!**

## Cost & Independence

After this fix:
- **VPS**: $6/month (DigitalOcean)
- **Database**: Free (Neon)
- **Replit**: NOT NEEDED - cancel subscription!
- **Total**: $6/month + trading capital

**You are now 100% independent!** 🎉

## Quick Reference Commands

```bash
# Check all services
sudo systemctl status mastra inngest polymarket-worker polymarket-monitor

# Watch Mastra logs
journalctl -u mastra.service -f

# Watch worker logs
journalctl -u polymarket-worker.service -f

# Restart everything after config changes
sudo systemctl restart mastra inngest polymarket-worker polymarket-monitor

# Check recent workflow runs
journalctl -u mastra.service --since "10 minutes ago" | grep "workflow"
```
