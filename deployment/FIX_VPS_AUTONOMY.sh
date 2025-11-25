#!/bin/bash
# Fix VPS Systemd Services for True Autonomy
# This script updates the mastra and inngest systemd services to run independently from Replit

set -e

echo "🔧 Fixing VPS Systemd Services for Autonomous Operation"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "❌ This script must be run as root (use sudo)" 
   exit 1
fi

# Stop services first
echo "⏸️  Stopping current services..."
systemctl stop mastra.service inngest.service 2>/dev/null || true

# Update mastra.service
echo "📝 Updating mastra.service configuration..."
cat > /etc/systemd/system/mastra.service << 'EOF'
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
EOF

# Update inngest.service
echo "📝 Updating inngest.service configuration..."
cat > /etc/systemd/system/inngest.service << 'EOF'
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
EOF

# Reload systemd
echo "🔄 Reloading systemd configuration..."
systemctl daemon-reload

# Enable services
echo "✅ Enabling services to start on boot..."
systemctl enable mastra.service
systemctl enable inngest.service

# Start services
echo "🚀 Starting services..."
systemctl start mastra.service
sleep 5  # Give mastra time to start before inngest
systemctl start inngest.service

# Wait a moment for services to initialize
sleep 3

# Check status
echo ""
echo "📊 Service Status:"
echo ""
systemctl status mastra.service --no-pager -l || true
echo ""
systemctl status inngest.service --no-pager -l || true

echo ""
echo "✅ DONE! Your VPS should now run autonomously."
echo ""
echo "📝 Next steps:"
echo "   1. Monitor logs: journalctl -u mastra.service -f"
echo "   2. Wait 1 minute for workflow to trigger"
echo "   3. Verify Telegram posts work without Replit"
echo "   4. Stop Replit to confirm full autonomy"
echo ""
