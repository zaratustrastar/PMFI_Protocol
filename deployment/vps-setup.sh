#!/bin/bash
# VPS Setup Script - Polymarket Trading Bot
# Run this script on a fresh Ubuntu 22.04 VPS

set -e

echo "🚀 Starting Polymarket Trading Bot VPS Setup..."

# Update system
echo "📦 Updating system packages..."
apt update && apt upgrade -y

# Install Node.js 20.x
echo "📦 Installing Node.js 20.x..."
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs

# Install Python 3
echo "📦 Installing Python 3..."
apt install -y python3 python3-pip python3-venv

# Install PostgreSQL
echo "📦 Installing PostgreSQL..."
apt install -y postgresql postgresql-contrib

# Install essential tools
echo "📦 Installing Git and tools..."
apt install -y git curl wget ufw

# Start PostgreSQL
echo "🔧 Starting PostgreSQL..."
systemctl start postgresql
systemctl enable postgresql

# Configure firewall
echo "🔧 Configuring firewall..."
ufw allow ssh
ufw allow 3000/tcp
ufw allow 8288/tcp
echo "y" | ufw enable

echo "✅ System setup complete!"
echo ""
echo "📋 Next Steps:"
echo "1. Create PostgreSQL database and user (see VPS_DEPLOYMENT.md)"
echo "2. Clone your repository to /opt/polymarket-bot"
echo "3. Run: cd /opt/polymarket-bot && npm install"
echo "4. Setup Python venv: cd trading_bot && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
echo "5. Configure .env file"
echo "6. Install systemd services"
echo "7. Start services!"
