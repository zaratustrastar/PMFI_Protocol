#!/bin/bash
# Start the order monitor
# This continuously monitors all open buy/sell orders

cd "$(dirname "$0")"
echo "👁️  Starting Order Monitor..."
python3 order_monitor.py
