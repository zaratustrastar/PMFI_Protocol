#!/bin/bash
# Start the trading job worker
# This polls the trading_jobs queue and processes pending markets

cd "$(dirname "$0")"
echo "🤖 Starting Trading Job Worker..."
python3 auto_trader.py
