# 🗄️ Database Migration Guide - Replit to VPS

This guide shows you how to migrate your PostgreSQL database from Replit to your VPS.

---

## 🎯 Two Migration Options

### Option A: Fresh Database (Recommended for Clean Start)
- Start with empty database on VPS
- Let workflow rebuild market history naturally
- Simplest approach

### Option B: Full Migration (Preserve All Data)
- Export from Replit database
- Import to VPS database
- Keeps all historical trading data

---

## ✅ Option A: Fresh Database Setup (Easiest)

This is the **recommended approach** for most users.

### 1. Create Database on VPS

```bash
# SSH into VPS
ssh root@your-vps-ip

# Create PostgreSQL database
sudo -u postgres psql << EOF
CREATE DATABASE polymarket_trading;
CREATE USER polymarket WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE polymarket_trading TO polymarket;
\c polymarket_trading
GRANT ALL ON SCHEMA public TO polymarket;
EOF
```

### 2. Configure DATABASE_URL

Edit `/opt/polymarket-bot/.env`:
```bash
DATABASE_URL=postgresql://polymarket:your_secure_password@localhost:5432/polymarket_trading
PGHOST=localhost
PGPORT=5432
PGUSER=polymarket
PGPASSWORD=your_secure_password
PGDATABASE=polymarket_trading
```

### 3. Run Migration Script

```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate
python3 add_market_dates.py
```

This creates the `market_created_at` column.

### 4. Push Drizzle Schema (if applicable)

```bash
cd /opt/polymarket-bot
npm run db:push
```

### 5. Start Services

The workflow will automatically start detecting markets and populating the database.

**Pros:**
- Simple, clean start
- No export/import complexity
- Database rebuilds naturally

**Cons:**
- Loses historical trading data from Replit
- No record of past markets/orders

---

## 📦 Option B: Full Database Migration (Advanced)

Migrate all data from Replit to VPS.

### Step 1: Export from Replit Database

**On Replit**, open Shell and run:

```bash
# Export entire database to SQL file
pg_dump $DATABASE_URL > polymarket_backup.sql

# Verify export
ls -lh polymarket_backup.sql
# Should show file size > 0
```

### Step 2: Download Backup to Local Machine

**On your local machine:**

```bash
# Download via Replit's file manager
# 1. Open Files tab in Replit
# 2. Right-click polymarket_backup.sql
# 3. Click "Download"

# OR use curl if you have a way to expose the file
```

### Step 3: Upload to VPS

**On your local machine:**

```bash
# Upload backup to VPS
scp polymarket_backup.sql root@your-vps-ip:/opt/polymarket_backup.sql
```

### Step 4: Create Empty Database on VPS

**On VPS:**

```bash
sudo -u postgres psql << EOF
CREATE DATABASE polymarket_trading;
CREATE USER polymarket WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE polymarket_trading TO polymarket;
\c polymarket_trading
GRANT ALL ON SCHEMA public TO polymarket;
EOF
```

### Step 5: Import Backup

```bash
# Import SQL backup into new database
sudo -u postgres psql polymarket_trading < /opt/polymarket_backup.sql

# Verify data
sudo -u postgres psql polymarket_trading -c "SELECT COUNT(*) FROM trading_jobs;"
sudo -u postgres psql polymarket_trading -c "SELECT COUNT(*) FROM market_orders;"
```

### Step 6: Fix Permissions

```bash
sudo -u postgres psql polymarket_trading << EOF
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO polymarket;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO polymarket;
GRANT USAGE ON SCHEMA public TO polymarket;
EOF
```

### Step 7: Configure .env

Same as Option A - edit `/opt/polymarket-bot/.env`:
```bash
DATABASE_URL=postgresql://polymarket:your_secure_password@localhost:5432/polymarket_trading
```

### Step 8: Start Services

```bash
systemctl start inngest.service mastra.service polymarket-worker.service polymarket-monitor.service
```

**Pros:**
- Preserves all historical data
- Continuous trading record
- Can analyze past performance

**Cons:**
- More complex
- Risk of import errors
- Larger database size

---

## 🔄 Alternative: Cloud PostgreSQL (Neon/Supabase)

Instead of running PostgreSQL on VPS, use managed database.

### Option C: Neon Database (Serverless Postgres)

**Advantages:**
- Automatic backups
- Better performance
- Easier to scale
- Can connect from both Replit and VPS

**Setup:**

1. **Create Neon Database**
   - Go to https://neon.tech
   - Create free account
   - Create new project "polymarket-trading"
   - Copy connection string

2. **Update Both Replit and VPS**
   
   On Replit `.env`:
   ```bash
   DATABASE_URL=postgresql://user:password@ep-xxx.us-east-1.aws.neon.tech/polymarket_trading?sslmode=require
   ```
   
   On VPS `/opt/polymarket-bot/.env`:
   ```bash
   DATABASE_URL=postgresql://user:password@ep-xxx.us-east-1.aws.neon.tech/polymarket_trading?sslmode=require
   ```

3. **Run Migration**
   ```bash
   npm run db:push
   cd trading_bot && python3 add_market_dates.py
   ```

**Pricing:**
- Free tier: 0.5 GB storage, 100 compute hours/month
- Pro tier: $19/month for unlimited

### Option D: Supabase (Managed Postgres)

Similar to Neon but includes additional features.

1. **Create Supabase Project**
   - Go to https://supabase.com
   - Create account
   - New project
   - Copy Postgres connection string (not Supabase URL)

2. **Use Same Process as Neon**
   - Update DATABASE_URL on both Replit and VPS
   - Run migrations

**Pricing:**
- Free tier: 500 MB database, 2 GB bandwidth
- Pro tier: $25/month

---

## 🔍 Verification After Migration

### Check Database Connection

```bash
cd /opt/polymarket-bot/trading_bot
source venv/bin/activate

python3 << EOF
from database import Database
db = Database()
print("✅ Database connection successful!")

# Check tables
import psycopg2
conn = psycopg2.connect(db.database_url)
cursor = conn.cursor()
cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
tables = cursor.fetchall()
print(f"📊 Found {len(tables)} tables:")
for table in tables:
    print(f"  - {table[0]}")
EOF
```

### Check Data (if migrated)

```bash
sudo -u postgres psql polymarket_trading << EOF
-- Count records
SELECT 'trading_jobs' as table, COUNT(*) FROM trading_jobs
UNION ALL
SELECT 'market_orders', COUNT(*) FROM market_orders;

-- Show recent jobs
SELECT * FROM trading_jobs ORDER BY created_at DESC LIMIT 5;
EOF
```

---

## 🚨 Troubleshooting

### Error: Permission Denied

```bash
# Fix ownership
sudo -u postgres psql polymarket_trading << EOF
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO polymarket;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO polymarket;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO polymarket;
EOF
```

### Error: Connection Refused

```bash
# Check PostgreSQL is running
systemctl status postgresql

# Check connection string
echo $DATABASE_URL

# Test connection
psql $DATABASE_URL -c "SELECT version();"
```

### Error: Table Does Not Exist

```bash
# Run migrations
cd /opt/polymarket-bot
npm run db:push --force

cd trading_bot
source venv/bin/activate
python3 add_market_dates.py
```

---

## 📊 Recommended Approach

**For Most Users:**
→ **Option A (Fresh Database)** if you don't need historical data

**For Production/Analysis:**
→ **Option C (Neon Database)** for managed solution with backups

**For Full Control:**
→ **Option B (Full Migration)** to VPS PostgreSQL

---

## 🎯 Quick Decision Matrix

| Priority | Recommended Option |
|----------|-------------------|
| **Simplest setup** | Option A (Fresh DB) |
| **Keep history** | Option B (Full Migration) |
| **Best reliability** | Option C (Neon/Supabase) |
| **Cheapest long-term** | Option A + VPS PostgreSQL |
| **Easiest backups** | Option C (Neon/Supabase) |

---

Choose your option and follow the steps above! 🚀
