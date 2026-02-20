#!/bin/bash
# ============================================================================
# Local PostgreSQL Setup for pSNIPER VPS
# Run once on your VPS: bash scripts/setup_local_pg.sh
# ============================================================================

set -e

DB_NAME="psniper"
DB_USER="psniper"
DB_PASS=$(openssl rand -hex 16)

echo "=================================================="
echo "  pSNIPER Local PostgreSQL Setup"
echo "=================================================="

# 1. Install PostgreSQL if not present
if ! command -v psql &> /dev/null; then
    echo "📦 Installing PostgreSQL..."
    sudo apt update -qq
    sudo apt install -y -qq postgresql postgresql-contrib
else
    echo "✅ PostgreSQL already installed"
fi

# 2. Ensure PostgreSQL is running
sudo systemctl enable postgresql
sudo systemctl start postgresql
echo "✅ PostgreSQL service running"

# 3. Create user and database
echo "🔧 Creating database and user..."
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"
sudo -u postgres psql -d ${DB_NAME} -c "GRANT ALL ON SCHEMA public TO ${DB_USER};"

echo "✅ Database '${DB_NAME}' ready"

# 4. Build the DATABASE_URL
DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"

echo ""
echo "=================================================="
echo "  DONE! Add this to your nano.env:"
echo "=================================================="
echo ""
echo "DATABASE_URL=${DATABASE_URL}"
echo ""
echo "Then restart bot_v7.py. Tables will be created automatically on first boot."
echo "=================================================="

# 5. Optionally append to nano.env if it exists
NANO_ENV="$(dirname "$0")/../nano.env"
if [ -f "$NANO_ENV" ]; then
    echo ""
    read -p "Update nano.env automatically? (y/N): " CONFIRM
    if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
        # Comment out old DATABASE_URL if present
        sed -i 's/^DATABASE_URL=/#OLD_DATABASE_URL=/' "$NANO_ENV"
        echo "DATABASE_URL=${DATABASE_URL}" >> "$NANO_ENV"
        echo "✅ nano.env updated! Restart bot_v7.py to apply."
    fi
fi
