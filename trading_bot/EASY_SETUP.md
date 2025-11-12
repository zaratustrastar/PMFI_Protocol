# 🏠 How to Run Trading Bot from Your Home Computer

**Super Simple Guide** - Follow these steps exactly!

---

## 🤔 Why Your Market Notifier Works But Trading Doesn't

**Simple Answer:**
- **Market Notifier** = Just reads data (Polymarket allows this)
- **Trading Bot** = Places real orders (Polymarket blocks this from Replit)

**Technical Reason:**
Polymarket uses Cloudflare to block "data center" computers (like Replit). Your home computer has a "residential" internet connection that Cloudflare allows.

Think of it like:
- Replit = Office building (blocked at the door)
- Your home = Your house (allowed to enter)

---

## 📋 What You'll Do (5 Steps)

1. Get your passwords from Replit
2. Download the code to your computer
3. Install Python stuff
4. Run 2 programs (worker + monitor)
5. Done! 🎉

---

## Step 1: Get Your Passwords from Replit

**On Replit, open the Shell and run this ONE command:**

```bash
cd trading_bot && ./export_env.sh
```

You'll see something like:
```
==================================================
🔑 COPY THESE LINES TO YOUR HOME COMPUTER
==================================================

DATABASE_URL=postgresql://...
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_PROXY_ADDRESS=0x...
TELEGRAM_BOT_TOKEN=...

==================================================
✅ Copy everything above this line!
==================================================

NOTE: Builder API credentials are NOT needed for trading.
Trading credentials are automatically derived from your private key.
```

📝 **Copy ALL those lines!** You'll paste them in Step 4.

---

## Step 2: Download Code to Your Computer

**On your home computer:**

### Option A: If you have Git installed
1. Open Terminal (Mac) or Command Prompt (Windows)
2. Run:
```bash
cd Desktop
git clone https://github.com/YOUR-USERNAME/YOUR-REPO-NAME.git
cd YOUR-REPO-NAME/trading_bot
```

### Option B: If you don't have Git
1. Go to your Replit project
2. Click the 3 dots menu (top left)
3. Click "Download as zip"
4. Unzip the file on your Desktop
5. Open Terminal/Command Prompt
6. Run:
```bash
cd Desktop/YOUR-FOLDER-NAME/trading_bot
```

---

## Step 3: Install Python Stuff

**Still in Terminal/Command Prompt, run:**

```bash
# Install all requirements (easiest way)
pip install -r requirements.txt
```

**If that doesn't work, try:**
```bash
pip3 install -r requirements.txt
```

**If that STILL doesn't work, install manually:**
```bash
pip install py-clob-client psycopg2-binary requests curl-cffi python-dotenv web3
```

---

## Step 4: Set Your Passwords

### Option A: Quick Way (Mac/Linux)
1. Create a new file:
```bash
nano .env
```

2. Paste the lines you copied from Step 1
3. Press `Ctrl+X`, then `Y`, then `Enter` to save

### Option B: Manual Way (Windows/Everyone)
1. Create a new file called `.env` (yes, it starts with a dot)
2. Open it with Notepad
3. Paste the lines you copied from Step 1 (should look like this):
```
DATABASE_URL=postgresql://...
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_PROXY_ADDRESS=0x...
TELEGRAM_BOT_TOKEN=...
```

4. Save the file as `.env` in the `trading_bot` folder

**Note:** You only need 4 credentials! Trading API credentials are automatically derived from your private key. Builder API credentials are NOT used for trading.

**✅ The file should be named exactly `.env` with no extension!**

---

## Step 5: Run the Trading Workers! 🚀

**Open 2 Terminal/Command Prompt windows**

### Window 1 - Trading Worker
```bash
cd Desktop/YOUR-FOLDER-NAME/trading_bot
python3 auto_trader.py
```

You should see:
```
🤖 Initializing Polymarket Trading Bot...
✅ Trading bot initialized!
🔍 Polling for pending jobs...
```

### Window 2 - Order Monitor
```bash
cd Desktop/YOUR-FOLDER-NAME/trading_bot
python3 order_monitor.py
```

You should see:
```
👀 Starting Order Monitor...
🔍 Checking for filled orders...
```

---

## ✅ How to Know It's Working

**You'll see:**
1. Worker picks up jobs from database ✅
2. Orders get placed on Polymarket ✅
3. Monitor detects fills ✅
4. Telegram notifications for sells ✅

**If you see "Cloudflare blocked":**
- Something is wrong with your internet connection
- Make sure you're on your HOME wifi (not work/school/public wifi)
- Some home routers use VPNs - turn that off

---

## 🛑 How to Stop

Just close both Terminal windows (Ctrl+C or close the window)

The orders will stay active on Polymarket - you can check them at polymarket.com

---

## 📊 Check Your Orders

Go to: https://polymarket.com/account

You'll see all your active orders there!

---

## ❓ What If It Doesn't Work?

**"Can't find python3"**
- Try `python` instead of `python3`
- Or install Python from python.org

**"Can't connect to database"**
- Double-check your DATABASE_URL from Step 1
- Make sure you copied it EXACTLY (no extra spaces)

**"Module not found"**
- Run the pip install command from Step 3 again

**Still stuck?**
- Check the file `CLOUDFLARE_ISSUE.md` for more details
- Or ask for help with the exact error message you see

---

## 🎉 That's It!

Your trading bot is now running from your home computer!

- Replit monitors markets → Posts Telegram → Queues jobs
- Your computer → Places orders → Monitors fills → Posts Telegram

**Keep both Terminal windows open** for the bot to keep working!
