"""
Telegram notifications for executed trades
"""

import os
import requests
from typing import Dict

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = "@ponnymarket"


def notify_sell_executed(market_slug: str, trade_data: Dict):
    """
    Send Telegram notification when a sell order executes
    
    Args:
        market_slug: Market identifier
        trade_data: Dict with keys: side, buy_price, sell_price, size, profit_usd, profit_pct
    """
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  TELEGRAM_BOT_TOKEN not set, skipping notification")
        return
    
    # Format the message
    side = trade_data.get("side", "")
    buy_price = trade_data.get("buy_price", 0)
    sell_price = trade_data.get("sell_price", 0)
    size = trade_data.get("size", 0)
    profit_usd = trade_data.get("profit_usd", 0)
    profit_pct = trade_data.get("profit_pct", 0)
    
    message = f"""🎯 **SELL EXECUTED**

Market: {market_slug}
Side: {side}
Tokens: {size:.2f}

Buy: ${buy_price:.4f}
Sell: ${sell_price:.4f}

💰 Profit: ${profit_usd:.2f} (+{profit_pct:.0f}%)
"""
    
    # Send to Telegram
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"✅ Telegram notification sent for {side} sell")
        else:
            print(f"⚠️  Telegram notification failed: {response.text}")
    except Exception as e:
        print(f"⚠️  Error sending Telegram notification: {e}")


def notify_multiple_sells(market_slug: str, sells: list):
    """
    Send a single notification for multiple sell executions
    
    Args:
        market_slug: Market identifier
        sells: List of trade_data dicts
    """
    if not TELEGRAM_BOT_TOKEN or not sells:
        return
    
    total_profit = sum(s.get("profit_usd", 0) for s in sells)
    
    lines = [f"🎯 **{len(sells)} SELLS EXECUTED**\n", f"Market: {market_slug}\n"]
    
    for sell in sells:
        side = sell.get("side", "")
        sell_price = sell.get("sell_price", 0)
        profit_usd = sell.get("profit_usd", 0)
        profit_pct = sell.get("profit_pct", 0)
        
        lines.append(f"• {side} @ ${sell_price:.4f} → +${profit_usd:.2f} ({profit_pct:.0f}%)")
    
    lines.append(f"\n💰 Total Profit: ${total_profit:.2f}")
    
    message = "\n".join(lines)
    
    # Send to Telegram
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"✅ Telegram notification sent for {len(sells)} sells")
        else:
            print(f"⚠️  Telegram notification failed: {response.text}")
    except Exception as e:
        print(f"⚠️  Error sending Telegram notification: {e}")
