# Quick Start - Test Your Bot Now!

## ✅ Everything Is Ready!

Your bot is fully set up and ready to trade. Here's how to test it:

## 1. Find a Market to Test With

Go to **polymarket.com** and pick ANY active market. Get the **slug** from the URL:

Example URL:
```
https://polymarket.com/event/will-bitcoin-hit-100k
                               ^^^^^^^^^^^^^^^^^^^
                               This is the slug!
```

## 2. Run the Bot

In your Replit Shell, run:

```bash
cd trading_bot
python3 run_trader.py <market-slug>
```

**Real example:**
```bash
python3 run_trader.py will-bitcoin-hit-100k
```

## 3. What Happens

The bot will:
1. ✅ Connect to Polymarket
2. 📊 Place 20 buy orders (10 YES + 10 NO) = **$2 total**
3. 🔍 Monitor every 10 seconds for fills
4. 💸 Auto-place sell orders when buys fill
5. 📱 **Notify Telegram (@ponnymarket) when sells execute**

## 4. Monitor Progress

**In the terminal:** Watch real-time updates
**In your database:** Check positions anytime:
```bash
python3 database.py
```

**On Telegram:** You'll get notifications like:
```
🎯 SELL EXECUTED

Market: will-bitcoin-hit-100k
Side: YES
Tokens: 2.50

Buy: $0.0030
Sell: $0.0090

💰 Profit: $0.15 (+200%)
```

## 5. Stop Monitoring

Press `Ctrl+C` in the terminal

⚠️ **Note:** Your orders stay active on Polymarket even after you stop monitoring!

## What Changed From Before

✅ **Budget:** $2 per market (was $20)
✅ **Sell monitoring:** Now tracks when sells execute  
✅ **Telegram:** Only posts executed sells (not new markets)
✅ **Auto-deployment:** Ready for scheduled deployment

## Next Step: Deploy Automatically

Once you've tested and it works, follow `DEPLOYMENT.md` to set up automatic deployment that:
- Runs every 15 minutes
- Auto-detects new markets
- Trades them automatically
- Notifies you on Telegram

## Troubleshooting

**"Market not found"**
- Check the slug is correct
- Try a different market from polymarket.com

**Orders not placing:**
- Check you have USDC in your Polymarket wallet
- Verify `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_PROXY_ADDRESS` are set

**No Telegram notifications:**
- Verify `TELEGRAM_BOT_TOKEN` is set
- Check bot has permission to post to @ponnymarket

## Test Right Now!

```bash
cd trading_bot
python3 run_trader.py <pick-any-active-market-slug>
```

Watch the magic happen! 🚀
