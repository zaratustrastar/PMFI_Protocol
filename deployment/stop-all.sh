#!/bin/bash
# Stop all Polymarket Trading Bot services

set -e

echo "⏹️  Stopping all services..."

systemctl stop mastra.service
systemctl stop inngest.service
systemctl stop polymarket-worker.service
systemctl stop polymarket-monitor.service

echo "✅ All services stopped!"
