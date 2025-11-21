# Mastra VPS Deployment Package

This package contains everything you need to run your Polymarket trading bot completely independently on your VPS, without Replit.

## Quick Start

### 1. Get This Package to Your VPS

**Option A: Direct Upload (Easiest)**

If you have this folder on your local computer:

```bash
# On your local computer
cd path/to/deployment/mastra-vps
tar -czf mastra-deployment.tar.gz .

# Upload to VPS
scp mastra-deployment.tar.gz root@147.182.239.195:/opt/polymarket-bot/
```

**Option B: From Replit via Web Server**

```bash
# In Replit Shell
cd ~/workspace
python3 -m http.server 8080

# Then in browser, visit: <YOUR_REPLIT_URL>:8080/deployment/
# Download mastra-deployment.tar.gz

# Upload to VPS using scp or FileZilla
```

**Option C: GitHub (If you have a private repo)**

```bash
# On VPS
cd /opt/polymarket-bot
git clone <your-private-repo> mastra
cd mastra
```

### 2. Extract and Install on VPS

```bash
# SSH into your VPS
ssh root@147.182.239.195

# Extract package
cd /opt/polymarket-bot
tar -xzf mastra-deployment.tar.gz -C /opt/polymarket-bot/mastra

# Run installation script
cd /opt/polymarket-bot/mastra
sudo bash install.sh
```

### 3. Configure Environment Variables

```bash
# Edit .env file
nano /opt/polymarket-bot/mastra/.env

# Copy values from Replit (see GET_ENV_VARS.md for details)
# Save: Ctrl+X, Y, Enter
```

### 4. Start Services

```bash
sudo systemctl start mastra.service inngest.service
sudo systemctl status mastra inngest
```

### 5. Verify Everything Works

```bash
# Watch logs
journalctl -u mastra.service -f

# You should see:
# - Workflow running every minute
# - Markets being fetched
# - Jobs being queued
```

## What's Included

- `src/` - All Mastra source code (agents, tools, workflows)
- `package.json` - npm dependencies
- `tsconfig.json` - TypeScript configuration
- `scripts/inngest.sh` - Inngest server startup script
- `systemd/` - Service files for mastra and inngest
- `.env.template` - Environment variables template
- `install.sh` - Automated installation script
- `INSTALLATION_GUIDE.md` - Complete step-by-step guide
- `GET_ENV_VARS.md` - How to get values from Replit

## After Installation

You'll have 4 services running:

1. ✅ **mastra** - Market detection workflow
2. ✅ **inngest** - Workflow orchestration
3. ✅ **polymarket-worker** - Order placement (already running)
4. ✅ **polymarket-monitor** - Fill monitoring (already running)

## Support

See INSTALLATION_GUIDE.md for:
- Detailed installation steps
- Troubleshooting
- Management commands
- Update procedures

**Total cost: $6/month** (VPS only, database is free)

**You are independent from Replit!** 🎉
