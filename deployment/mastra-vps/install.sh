#!/bin/bash

set -e

echo "🚀 Polymarket Bot - Mastra Installation Script"
echo "=============================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "❌ Please run as root (use sudo)"
    exit 1
fi

INSTALL_DIR="/opt/polymarket-bot/mastra"

echo "📦 Step 1: Installing Node.js 20.x..."
if ! command -v node &> /dev/null || [ "$(node -v | cut -d'.' -f1 | tr -d 'v')" -lt "20" ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    echo "✅ Node.js installed: $(node --version)"
else
    echo "✅ Node.js already installed: $(node --version)"
fi

echo ""
echo "📁 Step 2: Creating installation directory..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo ""
echo "📋 Step 3: Copying files..."
# Files should already be in current directory from tar extraction
if [ ! -f "package.json" ]; then
    echo "❌ package.json not found. Make sure you're running this from the extracted directory."
    exit 1
fi

echo ""
echo "📦 Step 4: Installing npm dependencies (this takes 2-3 minutes)..."
npm install --production

echo ""
echo "🔧 Step 5: Setting up environment file..."
if [ ! -f ".env" ]; then
    cp .env.template .env
    echo "⚠️  Please edit /opt/polymarket-bot/mastra/.env and fill in all values!"
    echo "   You'll need to copy them from your Replit secrets."
else
    echo "✅ .env already exists, skipping template copy"
fi

echo ""
echo "🔧 Step 6: Installing systemd services..."
cp systemd/mastra.service /etc/systemd/system/
cp systemd/inngest.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable mastra.service
systemctl enable inngest.service

echo ""
echo "✅ Installation complete!"
echo ""
echo "Next steps:"
echo "1. Edit the .env file: nano /opt/polymarket-bot/mastra/.env"
echo "2. Fill in all environment variables from Replit"
echo "3. Start services: sudo systemctl start mastra inngest"
echo "4. Check logs: journalctl -u mastra.service -f"
echo ""
echo "Full guide: /opt/polymarket-bot/mastra/INSTALLATION_GUIDE.md"
