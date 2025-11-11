# Polymarket Automated Trading Bot

Automated ladder trading strategy for Polymarket prediction markets.

## Strategy

1. **Buy Ladder**: Places limit buy orders from $0.001 to $0.01 (0.1¢ to 1¢) on both YES and NO tokens
   - Each order: $1 worth of tokens
   - Total investment: ~$20 per market ($10 YES + $10 NO)

2. **Monitor Fills**: Continuously checks for filled buy orders

3. **Sell Ladder**: When a buy fills, automatically places sell orders at 200%-1000% profit
   - Divides position across 8 sell orders
   - Prices: 3x, 4x, 5x, 6x, 7x, 8x, 9x, 10x the buy price

## Setup

### Prerequisites
- Python 3.11+
- PostgreSQL database
- Polymarket wallet with USDC balance

### Environment Variables
Set these in Replit Secrets:
- `POLYMARKET_PRIVATE_KEY`: Your wallet private key
- `POLYMARKET_PROXY_ADDRESS`: Your Polymarket funding address
- `DATABASE_URL`: PostgreSQL connection string

### Installation
Already installed via Replit package manager:
- `py-clob-client`
- `web3`
- `requests`

## Usage

### Run the bot on a market:
```bash
cd trading_bot
python3 run_trader.py <market-slug>
```

### Example:
```bash
python3 run_trader.py eth-updown-15m-1762961400
```

### Find market slugs:
1. Go to polymarket.com
2. Browse markets
3. The slug is in the URL: `polymarket.com/event/SLUG`

Or use your existing Telegram monitoring to get new market slugs automatically.

## How It Works

1. **Initialization**
   - Connects to Polymarket CLOB API
   - Creates/derives API credentials
   - Initializes database tables

2. **Market Analysis**
   - Fetches market data by slug
   - Extracts YES and NO token IDs
   - Validates market is active

3. **Buy Phase**
   - Places 10 buy orders on YES (0.1¢ to 1¢)
   - Places 10 buy orders on NO (0.1¢ to 1¢)
   - Logs all order IDs

4. **Monitor Phase**
   - Polls CLOB API every 10 seconds
   - Checks status of all open buy orders
   - Identifies filled orders

5. **Sell Phase** (triggers when buy fills)
   - Calculates profit prices (3x to 10x)
   - Divides position across 8 sell orders
   - Places all sell orders
   - Tracks P&L in database

## Database Schema

### `trading_positions`
Tracks all orders (buy and sell):
- `order_id`: Unique Polymarket order ID
- `market_slug`: Market identifier
- `token_id`: YES or NO token ID
- `side`: "YES" or "NO"
- `order_type`: "BUY" or "SELL"
- `price`: Order price
- `size`: Number of tokens
- `status`: "OPEN" or "FILLED"
- `buy_price`: Original buy price (for sells)
- `profit_multiple`: Profit multiplier (for sells)

### `trading_summary`
Market-level statistics:
- `total_buys`, `filled_buys`
- `total_sells`, `filled_sells`
- `total_invested`, `total_returned`
- `realized_pnl`: Profit/loss

## Files

- `run_trader.py`: Main launcher script
- `polymarket_trader.py`: Core trading bot logic
- `market_utils.py`: Market data fetching
- `database.py`: Database operations
- `config.py`: Configuration settings

## Current Limitations

⚠️ **This is a basic MVP with known limitations:**

1. **No sell order monitoring** - Bot places sell orders but doesn't track when they fill
   - You must manually check Polymarket to see if sells executed
   - P&L tracking only updates for buy fills, not sell fills
   - Consider this for future improvement

2. **Partial fills** - Bot logs partial fills but doesn't hedge them
   - If a buy partially fills, sell ladder waits for full fill
   - Unhedged exposure if market moves before full fill

3. **No stop-loss** - Bot never cancels losing positions
   - Sell orders stay open indefinitely at high prices
   - You may hold positions long-term if prices don't reach targets

4. **Single market only** - Bot runs on one market at a time
   - To trade multiple markets, run multiple instances
   - No portfolio-level risk management

5. **No balance checks** - Bot doesn't verify sufficient USDC before placing orders
   - Orders will fail if wallet balance too low

## Safety Notes

⚠️ **This bot trades real money**
- Start with small amounts ($20 per market)
- Markets can move against you
- Sell prices may never be reached
- Always monitor your positions on polymarket.com
- Check database regularly for position status

## Integration with Telegram Monitoring

Your existing Polymarket monitoring workflow detects new markets and posts to Telegram. You can integrate by:

1. Extract market slug from new market notifications
2. Automatically trigger this bot with the slug
3. Bot places orders and monitors fills

## Support

For issues or questions about:
- Polymarket API: https://docs.polymarket.com
- CLOB Client: https://github.com/Polymarket/py-clob-client
