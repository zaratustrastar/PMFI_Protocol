# Bridge Runbook: Polygon → Base

This document explains how USDC is bridged from Polygon (Polymarket) to Base (Treasury) to refill the vault.

## Automatic Bridging (Default)

The Liquidity Keeper now uses **Relay.link** for automatic bridging:

1. **Liquidation** → USDC lands in PM wallet on Polygon
2. **Auto-Bridge** → Relay.link transfers Polygon USDC → Base treasury (~3 seconds)
3. **Treasury refill** → Next keeper iteration sends treasury → vault

### Requirements for Auto-Bridge
- POL (Polygon native token) in wallet for gas (~$0.01 per tx)
- `POLYMARKET_PRIVATE_KEY` configured
- `TREASURY_ADDRESS` set (or defaults to same wallet)
- **USDC in wallet** (not in PM custody) - withdraw from PM first if needed

### Known Limitation
After selling PM positions, funds remain in PM custody. You must withdraw from PM to your Polygon wallet before auto-bridge can access them. The keeper will alert you when this is needed.

### Relay.link Fees
- Very low: ~$0.0001 per bridge (0.01% fee)
- Fast: ~3 seconds
- No API key required

## Manual Bridging (Fallback)

If auto-bridge fails, the keeper will alert via Telegram. Use these steps:

### 1. Withdraw from Polymarket to Polygon Wallet

If funds are still in Polymarket:
1. Go to https://polymarket.com
2. Click "Funds" → "Withdraw"
3. Enter your Polygon wallet address
4. Confirm withdrawal (instant on Polygon)

### 2. Bridge from Polygon to Base

**Option A: Relay.link (Recommended)**
- URL: https://relay.link
- Fast: ~3 seconds
- Lowest fees

**Option B: Stargate Finance**
- URL: https://stargate.finance/transfer
- Fast: ~15 minutes
- Low fees

**Option C: Across Protocol**
- URL: https://across.to
- Fast: ~5-10 minutes
- Competitive fees

### 3. Verify Funds on Base

After bridging:
1. Check treasury wallet balance on Base: https://basescan.org/address/YOUR_TREASURY_ADDRESS
2. The Liquidity Keeper will automatically detect new funds
3. Next iteration will use treasury to refill vault

## Addresses

- **USDC on Polygon**: `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` (native USDC)
- **USDC on Base**: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- **Vault Address**: Check `VAULT_V5_ADDRESS` in .env
- **Treasury Address**: Check `TREASURY_ADDRESS` in .env

## Gas Requirements

- **Polygon**: Keep ~1 POL for gas (~$0.50)
- **Base**: Keep ~0.0001 ETH for gas (~$0.30)

## Troubleshooting

**Bridge stuck?**
- Check transaction on Polygon explorer: https://polygonscan.com
- Relay.link bridges are usually instant (<5s)
- If using other bridges, wait up to 30 minutes before retrying

**Auto-bridge not working?**
- Check POL balance on Polygon wallet
- Verify `POLYMARKET_PRIVATE_KEY` is correct
- Check keeper logs for error messages

**Wrong network?**
- Ensure wallet is on correct network before sending
- Base Chain ID: 8453
- Polygon Chain ID: 137
