#!/bin/bash
# Setup script for standalone Polymarket market monitor
# Run this on VPS after git pull

echo "=== Polymarket Market Monitor Setup ==="

# 1. Stop existing services
echo "Stopping mastra and inngest services..."
sudo systemctl stop mastra.service 2>/dev/null || true
sudo systemctl stop inngest.service 2>/dev/null || true
sudo systemctl disable mastra.service 2>/dev/null || true
sudo systemctl disable inngest.service 2>/dev/null || true

# 2. Install Python dependencies (if not already installed)
echo "Checking Python dependencies..."
pip3 install --quiet requests psycopg2-binary 2>/dev/null || {
    echo "Installing Python dependencies..."
    pip3 install requests psycopg2-binary
}

# 3. Create log directory
echo "Creating log directory..."
sudo mkdir -p /var/log
sudo touch /var/log/market_monitor.log
sudo chmod 666 /var/log/market_monitor.log

# 4. Test the script works
echo "Testing market monitor script..."
cd /opt/polymarket-bot
source .env 2>/dev/null || export $(cat .env | grep -v '^#' | xargs)
python3 trading_bot/market_monitor.py

if [ $? -eq 0 ]; then
    echo "✅ Market monitor script works!"
else
    echo "❌ Script failed - check your .env file has DATABASE_URL and TELEGRAM_BOT_TOKEN"
    exit 1
fi

# 5. Setup cron job
echo "Setting up cron job..."
CRON_CMD="* * * * * cd /opt/polymarket-bot && /usr/bin/env bash -c 'source .env && /usr/bin/python3 trading_bot/market_monitor.py' >> /var/log/market_monitor.log 2>&1"

# Check if cron job already exists
(crontab -l 2>/dev/null | grep -v "market_monitor.py") | crontab -
(crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -

echo "✅ Cron job installed!"

# 6. Show cron status
echo ""
echo "=== Current crontab ==="
crontab -l

echo ""
echo "=== Setup Complete ==="
echo "The market monitor will run every minute via cron."
echo ""
echo "To view logs:  tail -f /var/log/market_monitor.log"
echo "To check cron: crontab -l"
echo ""
echo "Your VPS is now running independently of Replit! 🎉"
