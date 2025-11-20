#!/bin/bash
# Install systemd services for Polymarket Trading Bot

set -e

echo "🔧 Installing systemd services..."

# Copy service files
cp deployment/systemd/*.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

echo "✅ Services installed!"
echo ""
echo "📋 Available services:"
echo "  - mastra.service           (Mastra dev server)"
echo "  - inngest.service          (Inngest workflow server)"
echo "  - polymarket-worker.service (Trading worker)"
echo "  - polymarket-monitor.service (Order monitor)"
echo ""
echo "🚀 To start all services:"
echo "  systemctl enable --now inngest.service mastra.service polymarket-worker.service polymarket-monitor.service"
echo ""
echo "📊 To check status:"
echo "  systemctl status mastra.service"
echo "  journalctl -u mastra.service -f"
