# Farcaster Mini App - Testing Guide

## Overview

The pSNIPER Farcaster Mini App provides a mobile-optimized version of the vault interface that can be embedded in Farcaster clients (Warpcast) and Base App.

## Setup Requirements

### 1. Account Association (Required for Production)

The `/.well-known/farcaster.json` manifest requires a valid account association to verify domain ownership. To set this up:

1. Register a Farcaster FID for your app at https://warpcast.com
2. Generate the domain signature using the Farcaster Developer Hub
3. Update the `accountAssociation` fields in `frontend/.well-known/farcaster.json`

The current manifest has placeholder values that will work for development but will fail verification in production.

### 2. Optional: Neynar API Key (Recommended)

For enhanced webhook verification, set `NEYNAR_API_KEY` in your environment:

```bash
# In .env on VPS
NEYNAR_API_KEY=your_neynar_api_key_here
```

Without this, verification falls back to Pinata hub which is less reliable.

## Routes

| Route | Description |
|-------|-------------|
| `/` | Full web app (desktop-focused) |
| `/mini` | Mini app optimized for Farcaster/Base App |
| `/.well-known/farcaster.json` | Farcaster manifest |
| `/api/farcaster/webhook` | Webhook handler for frame actions |
| `/api/farcaster/verify` | Message verification endpoint |

## Testing Checklist

### 1. Verify Manifest is Served Correctly

```bash
curl -s https://pmfi.cc/.well-known/farcaster.json | jq .
```

Expected response:
```json
{
  "name": "pSNIPER Vault",
  "description": "Automated Polymarket sniping vault...",
  "homeUrl": "https://pmfi.cc/mini",
  "iconUrl": "https://pmfi.cc/psniper-logo.png",
  ...
}
```

### 2. Test Mini App Page

```bash
curl -I https://pmfi.cc/mini
```

Should return `200 OK` with HTML content.

### 3. Test Environment Detection

The app auto-detects Farcaster clients and redirects:

```bash
# Simulating Warpcast user agent
curl -H "User-Agent: Warpcast/1.0" https://pmfi.cc/
```

Should return the mini app HTML instead of full app.

### 4. Test Webhook Endpoint

```bash
curl -X POST https://pmfi.cc/api/farcaster/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "untrustedData": {
      "fid": 12345,
      "buttonIndex": 1,
      "inputText": ""
    },
    "trustedData": {
      "messageBytes": "0x..."
    }
  }'
```

### 5. Test in Warpcast

1. Go to Warpcast Frame Validator: https://warpcast.com/~/developers/frames
2. Enter URL: `https://pmfi.cc/mini`
3. Verify the frame preview renders correctly
4. Test the "Open pSNIPER" button

### 6. Test in Base App

1. Open Base App on mobile
2. Navigate to Mini Apps section
3. Search for "pSNIPER" or enter URL directly
4. Verify wallet connection works
5. Test deposit/withdraw flows

## Local Development

For local testing with HTTPS (required by Farcaster):

```bash
# Using ngrok
ngrok http 8080

# Update manifest URLs to ngrok URL for testing
# Then test with Warpcast Frame Validator
```

## Farcaster Frame Meta Tags

The `/mini` page includes required meta tags:

```html
<meta property="fc:frame" content="vNext" />
<meta property="fc:frame:image" content="https://pmfi.cc/psniper-logo.png" />
<meta property="fc:frame:button:1" content="Open pSNIPER" />
<meta property="fc:frame:button:1:action" content="launch_frame" />
<meta property="fc:frame:button:1:target" content="https://pmfi.cc/mini" />
```

## Troubleshooting

### Mini App Not Loading in Warpcast

1. Check manifest is accessible at `/.well-known/farcaster.json`
2. Verify HTTPS is working (no mixed content)
3. Confirm image URLs are absolute and accessible

### Wallet Not Connecting

1. Ensure user is on Base network (chainId 8453)
2. Check for any CORS errors in console
3. Verify the Coinbase Wallet or other injected provider is available

### Action Verification Failing

1. Check server logs for verification errors
2. Ensure `trustedData.messageBytes` is being sent
3. For production, implement proper hub verification

## Production Deployment

When deploying to VPS:

1. Git pull latest changes:
   ```bash
   cd ~/PolyNotifyBot
   git pull origin main
   ```

2. Restart the bot service:
   ```bash
   sudo systemctl restart psniper-bot
   ```

3. Verify routes are accessible:
   ```bash
   curl https://pmfi.cc/mini
   curl https://pmfi.cc/.well-known/farcaster.json
   ```
