# How to Get Environment Variables from Replit

Before deploying to your VPS, you need to collect all environment variables from Replit.

## Step 1: View All Secrets in Replit

In your Replit Shell, run:

```bash
env | grep -E "(DATABASE_URL|POLYMARKET|TELEGRAM|TWITTER|AI_INTEGRATIONS|SESSION)" | sort
```

This will show all the values you need.

## Step 2: Copy Values to .env File

When you edit `/opt/polymarket-bot/mastra/.env` on your VPS, fill in these values:

### Database (Already Filled)
```bash
DATABASE_URL=postgresql://neondb_owner:npg_sPnNQtm3xf8h@ep-noisy-lab-ahf3wcvu.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require
```

### Polymarket (Copy from Replit)
```bash
POLYMARKET_PRIVATE_KEY=<value from Replit>
POLYMARKET_PROXY_ADDRESS=<value from Replit>
POLYMARKET_API_KEY=<value from Replit>
POLYMARKET_API_SECRET=<value from Replit>
```

### Telegram (Copy from Replit)
```bash
TELEGRAM_BOT_TOKEN=<value from Replit>
```

### Twitter (Copy from Replit)
```bash
TWITTER_API_KEY=<value from Replit>
TWITTER_API_SECRET=<value from Replit>
TWITTER_ACCESS_TOKEN=<value from Replit>
TWITTER_ACCESS_SECRET=<value from Replit>
```

### AI Integrations (Copy from Replit or leave blank)
```bash
AI_INTEGRATIONS_OPENAI_BASE_URL=https://ai-integrations.replit.com/v1
AI_INTEGRATIONS_OPENAI_API_KEY=<value from Replit - or leave blank if using your own OpenAI key>
```

### Session Secret (Generate new one)
```bash
# On your VPS, run this to generate a random secret:
openssl rand -hex 32

# Then paste the output into .env:
SESSION_SECRET=<paste generated value here>
```

## Alternative: Use Replit Secrets Tool

In your Replit workspace:

1. Click on "Tools" in left sidebar
2. Click on "Secrets"
3. You'll see all your environment variables listed
4. Copy each value to your VPS .env file

## IMPORTANT

- Make sure there are NO comments in the .env file (no lines starting with #)
- Each line should be: `KEY=value` with no spaces around the =
- No quotes needed around values unless they contain spaces or special characters
