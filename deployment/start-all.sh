#!/bin/bash
# Start all Polymarket Trading Bot services

set -e

echo "🚀 Starting all services..."

# Start Inngest first (required by Mastra)
echo "▶️  Starting Inngest server..."
systemctl start inngest.service
sleep 3

# Start Mastra
echo "▶️  Starting Mastra server..."
systemctl start mastra.service
sleep 2

# Start worker
echo "▶️  Starting trading worker..."
systemctl start polymarket-worker.service

# Start monitor
echo "▶️  Starting order monitor..."
systemctl start polymarket-monitor.service

echo ""
echo "✅ All services started!"
echo ""
echo "📊 Check status:"
systemctl status mastra.service inngest.service polymarket-worker.service polymarket-monitor.service

echo ""
echo "📋 View logs:"
echo "  journalctl -u mastra.service -f"
echo "  journalctl -u polymarket-worker.service -f"
echo "  journalctl -u polymarket-monitor.service -f"
