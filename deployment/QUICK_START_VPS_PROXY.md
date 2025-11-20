# 🚀 Quick Start: VPS + Residential Proxy Setup

Complete guide to deploy your Polymarket trading bot on a VPS with residential proxy.

**Setup:** DigitalOcean VPS ($6/month) + IPRoyal Proxy ($7-80/month)  
**Total:** $13-86/month for 24/7 automated trading

---

## 📝 Step 1: Purchase DigitalOcean VPS

### 1.1 Create Account
1. Go to https://www.digitalocean.com
2. Sign up (get $200 free credit for 60 days)
3. Verify email

### 1.2 Create Droplet (VPS)
1. Click **Create** → **Droplets**
2. **Choose Region**: New York or San Francisco
3. **Choose Image**: Ubuntu 22.04 LTS
4. **Choose Size**: 
   - Basic plan
   - Regular Intel ($6/month)
   - 1 GB / 1 CPU / 25 GB SSD
5. **Authentication**:
   - Create SSH key on your Mac:
     ```bash
     ssh-keygen -t rsa -b 4096 -C "polymarket-bot"
     cat ~/.ssh/id_rsa.pub  # Copy this
     ```
   - Paste public key in DigitalOcean
6. **Hostname**: `polymarket-bot`
7. Click **Create Droplet**

### 1.3 Get IP Address
- Wait 1-2 minutes for creation
- Copy the IP address (e.g., `167.99.123.45`)

---

## 🌐 Step 2: Purchase Residential Proxy

### 2.1 Sign Up for IPRoyal

1. Go to https://iproyal.com/residential-proxies/
2. Click **Get Started**
3. Create account
4. Verify email

### 2.2 Purchase Plan

**For Testing (Recommended Start):**
- Plan: **1 GB** - $7/month
- Good for 1-2 weeks of moderate trading

**For Production:**
- Plan: **Unlimited** - $80/month
- No worries about running out

**Purchase:**
1. Go to **Dashboard** → **Residential Proxies**
2. Click **Add Funds** or **Subscribe**
3. Choose plan
4. Complete payment

### 2.3 Get Proxy Credentials

1. Go to **Dashboard** → **Residential Proxies**
2. Click **Setup**
3. You'll see credentials like:
   ```
   Proxy Host: geo.iproyal.com
   Proxy Port: 12321
   Username: your_username
   Password: your_password
   ```
4. **Save these!** You'll need them.

**Test Format:**
```
http://your_username:your_password@geo.iproyal.com:12321
```

---

## 🔧 Step 3: Initial VPS Setup

### 3.1 Connect to VPS

**On your Mac terminal:**
```bash
ssh root@YOUR_VPS_IP
# Example: ssh root@167.99.123.45
```

Say "yes" to fingerprint prompt.

### 3.2 Run System Setup

**On VPS:**
```bash
# Update system
apt update && apt upgrade -y

# Install Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

# Install Python
apt install -y python3 python3-pip python3-venv

# Install PostgreSQL
apt install -y postgresql postgresql-contrib

# Install Git
apt install -y git curl wget

# Start PostgreSQL
systemctl start postgresql
systemctl enable postgresql
```

### 3.3 Create PostgreSQL Database

```bash
sudo -u postgres psql << 'EOF'
CREATE DATABASE polymarket_trading;
CREATE USER polymarket WITH PASSWORD 'ChangeThisPassword123!';
GRANT ALL PRIVILEGES ON DATABASE polymarket_trading TO polymarket;
\c polymarket_trading
GRANT ALL ON SCHEMA public TO polymarket;
EOF
```

**⚠️ Change the password!**

---

## 📦 Step 4: Deploy Application Code

### 4.1 Upload Code to VPS

**Option A: Using Git (Recommended)**

If your code is on GitHub:
```bash
cd /opt
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git polymarket-bot
cd polymarket-bot
```

**Option B: Upload from Local Mac**

On your Mac:
```bash
# Zip your folder
cd ~/Desktop/YOUR-FOLDER-NAME
tar -czf polymarket-bot.tar.gz .

# Upload to VPS
scp polymarket-bot.tar.gz root@YOUR_VPS_IP:/opt/

# Extract on VPS
ssh root@YOUR_VPS_IP
cd /opt
tar -xzf polymarket-bot.tar.gz -C polymarket-bot
cd polymarket-bot
```

### 4.2 Install Dependencies

**Node.js:**
```bash
npm install
```

**Python:**
```bash
cd trading_bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cd ..
```

---

## 🔐 Step 5: Configure Environment Variables

### 5.1 Create .env File

```bash
nano /opt/polymarket-bot/.env
```

### 5.2 Add Configuration

**Paste this and fill in your values:**

```bash
# Database
DATABASE_URL=postgresql://polymarket:ChangeThisPassword123!@localhost:5432/polymarket_trading
PGHOST=localhost
PGPORT=5432
PGUSER=polymarket
PGPASSWORD=ChangeThisPassword123!
PGDATABASE=polymarket_trading

# Polymarket Trading (copy from Replit)
POLYMARKET_PRIVATE_KEY=your_private_key_from_replit
POLYMARKET_PROXY_ADDRESS=your_proxy_address_from_replit
POLYMARKET_API_KEY=your_api_key_from_replit
POLYMARKET_API_SECRET=your_api_secret_from_replit

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_from_replit

# Twitter (optional)
TWITTER_API_KEY=your_twitter_key
TWITTER_API_SECRET=your_twitter_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_SECRET=your_access_secret

# Session Secret
SESSION_SECRET=generate_random_string_here

# ⭐ RESIDENTIAL PROXY (IPRoyal)
HTTP_PROXY=http://your_username:your_password@geo.iproyal.com:12321
HTTPS_PROXY=http://your_username:your_password@geo.iproyal.com:12321
```

**Save:** Press `Ctrl+X`, then `Y`, then `Enter`

**Secure the file:**
```bash
chmod 600 /opt/polymarket-bot/.env
```

---

## 🗄️ Step 6: Initialize Database

### 6.1 Run Migration Script

```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate
python3 add_market_dates.py
```

Should output: `✅ Migration successful!`

### 6.2 Push Drizzle Schema

```bash
cd /opt/polymarket-bot
npm run db:push
```

Or if it asks about destructive changes:
```bash
npm run db:push --force
```

---

## 🧪 Step 7: Test Proxy Connection

### 7.1 Test Basic Connectivity

```bash
export HTTP_PROXY=http://your_username:your_password@geo.iproyal.com:12321
curl https://ipinfo.io
```

**Should show:**
- Different IP than your VPS
- `org` field showing residential ISP (not DigitalOcean)

### 7.2 Test Polymarket API

```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate

python3 << 'EOF'
import os
os.environ['HTTP_PROXY'] = 'http://YOUR_USERNAME:YOUR_PASSWORD@geo.iproyal.com:12321'
os.environ['HTTPS_PROXY'] = 'http://YOUR_USERNAME:YOUR_PASSWORD@geo.iproyal.com:12321'

import requests
response = requests.get('https://clob.polymarket.com/markets')
print(f"Status: {response.status_code}")
if response.status_code == 200:
    print("✅ Proxy working with Polymarket!")
else:
    print("❌ Proxy failed - check credentials")
EOF
```

---

## ⚙️ Step 8: Install systemd Services

### 8.1 Copy Service Files

```bash
cp /opt/polymarket-bot/deployment/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
```

### 8.2 Enable Services (Auto-Start on Boot)

```bash
systemctl enable mastra.service
systemctl enable inngest.service
systemctl enable polymarket-worker.service
systemctl enable polymarket-monitor.service
```

---

## 🚀 Step 9: Start Everything

### 9.1 Start Services in Order

```bash
# Start Inngest first (required by Mastra)
systemctl start inngest.service
sleep 5

# Start Mastra
systemctl start mastra.service
sleep 3

# Start worker and monitor
systemctl start polymarket-worker.service
systemctl start polymarket-monitor.service
```

### 9.2 Check Status

```bash
systemctl status mastra.service
systemctl status inngest.service
systemctl status polymarket-worker.service
systemctl status polymarket-monitor.service
```

All should show **active (running)** in green.

---

## 📊 Step 10: Monitor Your Bot

### View Live Logs

**Mastra (market detection):**
```bash
journalctl -u mastra.service -f
```
Should see markets detected every minute.

**Worker (order placement):**
```bash
journalctl -u polymarket-worker.service -f
```
Should see "Processing job..." when markets are queued.

**Monitor (fill detection):**
```bash
journalctl -u polymarket-monitor.service -f
```
Will show activity when orders fill.

**All logs together:**
```bash
journalctl -u mastra.service -u polymarket-worker.service -u polymarket-monitor.service -f
```

### Check Telegram

Open @ponnymarket channel - should see new markets being posted!

---

## ✅ Verification Checklist

- [ ] VPS created and accessible via SSH
- [ ] IPRoyal proxy purchased and credentials obtained
- [ ] All system packages installed (Node, Python, PostgreSQL)
- [ ] Code deployed to `/opt/polymarket-bot`
- [ ] `.env` file configured with all secrets
- [ ] Database created and migrated
- [ ] Proxy test shows residential IP
- [ ] All 4 systemd services running
- [ ] Logs show market detection every minute
- [ ] Telegram notifications appearing
- [ ] No 403 errors in worker logs

---

## 🛠️ Common Issues & Fixes

### Issue: Services won't start

```bash
# Check detailed logs
journalctl -xe -u mastra.service

# Test manually
cd /opt/polymarket-bot
npm run dev
```

### Issue: 403 Forbidden errors

**Cause:** Proxy not working

**Fix:**
```bash
# Verify proxy in .env
cat /opt/polymarket-bot/.env | grep PROXY

# Test proxy again
export $(cat /opt/polymarket-bot/.env | grep PROXY)
curl --proxy $HTTP_PROXY https://ipinfo.io
```

### Issue: Database connection errors

```bash
# Test connection
sudo -u postgres psql polymarket_trading -c "SELECT version();"

# Check DATABASE_URL
cat /opt/polymarket-bot/.env | grep DATABASE_URL
```

### Issue: Worker not placing orders

**Possible causes:**
1. No pending jobs (all markets filtered out)
2. Cloudflare blocking (proxy issue)
3. Insufficient USDC balance

**Check:**
```bash
# Check pending jobs
sudo -u postgres psql polymarket_trading -c "SELECT COUNT(*) FROM trading_jobs WHERE status='PENDING';"

# Check worker logs
journalctl -u polymarket-worker.service -n 100
```

---

## 🔄 Maintenance Commands

### Restart Everything

```bash
systemctl restart inngest.service
systemctl restart mastra.service
systemctl restart polymarket-worker.service
systemctl restart polymarket-monitor.service
```

### Update Code

```bash
cd /opt/polymarket-bot
git pull  # If using git
npm install
cd trading_bot && source venv/bin/activate && pip install -r requirements.txt
systemctl restart mastra.service polymarket-worker.service polymarket-monitor.service
```

### View Resource Usage

```bash
htop
```

### Backup Database

```bash
sudo -u postgres pg_dump polymarket_trading > /opt/backup_$(date +%Y%m%d).sql
```

---

## 💰 Cost Breakdown

| Item | Cost | Notes |
|------|------|-------|
| **DigitalOcean VPS** | $6/month | First 60 days free with credit |
| **IPRoyal 1GB** | $7/month | Good for testing |
| **IPRoyal Unlimited** | $80/month | For production |
| **Total (testing)** | **$13/month** | VPS + 1GB proxy |
| **Total (production)** | **$86/month** | VPS + unlimited proxy |

---

## 🎯 What Happens Now

Your bot is now running 24/7!

**Every minute:**
1. Mastra workflow detects new Polymarket markets
2. Posts ALL markets to @ponnymarket Telegram
3. Filters out up/down and short-duration markets
4. Queues eligible markets in database

**Every 30 seconds:**
5. Worker checks queue for pending jobs
6. Places ladder buy orders on both YES/NO
7. Monitor checks for filled orders
8. When sells fill, posts to Telegram

**Your bot operates completely autonomously!** 🎉

---

## 📞 Next Steps

1. Monitor logs for first few hours
2. Check Telegram for market notifications
3. Verify orders are being placed (check worker logs)
4. Adjust proxy plan if needed (upgrade to unlimited)
5. Set up automated backups (optional)

---

**Questions?** Check the full deployment guide in `VPS_DEPLOYMENT.md` or proxy guide in `RESIDENTIAL_PROXY_GUIDE.md`.

**Your 24/7 Polymarket sniper is live! 🚀**
