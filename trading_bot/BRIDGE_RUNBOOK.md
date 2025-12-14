# Bridge Runbook: Polygon → Base

This document explains how to manually bridge USDC from Polygon (Polymarket) to Base (Treasury) to refill the vault.

## When to Bridge

The Liquidity Keeper will alert you via Telegram when:
- Treasury balance falls below $500 (LOW warning)
- Treasury balance falls below $100 (CRITICAL alert)
- Positions are liquidated on Polygon

## Step-by-Step Bridge Process

### 1. Withdraw from Polymarket to Polygon Wallet

If funds are still in Polymarket:
1. Go to https://polymarket.com
2. Click "Funds" → "Withdraw"
3. Enter your Polygon wallet address
4. Confirm withdrawal (instant on Polygon)

### 2. Bridge from Polygon to Base

**Option A: Stargate Finance (Recommended)**
- URL: https://stargate.finance/transfer
- Fast: ~15 minutes
- Low fees

Steps:
1. Connect wallet (Polygon network)
2. Select: From Polygon → To Base
3. Token: USDC
4. Enter amount
5. Confirm transaction
6. Wait for confirmation on Base

**Option B: Hop Protocol**
- URL: https://app.hop.exchange
- Fast: ~10-15 minutes
- Competitive fees

**Option C: Across Protocol**
- URL: https://across.to
- Fast: ~5-10 minutes
- Lowest fees for larger amounts

### 3. Verify Funds on Base

After bridging:
1. Check treasury wallet balance on Base: https://basescan.org/address/YOUR_TREASURY_ADDRESS
2. The Liquidity Keeper will automatically detect new funds
3. Next iteration will use treasury to refill vault

## Addresses

- **USDC on Polygon**: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
- **USDC on Base**: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- **Vault Address**: Check `VAULT_V5_ADDRESS` in .env
- **Treasury Address**: Check `TREASURY_ADDRESS` in .env (or defaults to PM wallet)

## Gas Requirements

- **Polygon**: Keep ~1 MATIC for gas (~$0.50)
- **Base**: Keep ~0.0001 ETH for gas (~$0.30)

## Automation (Future)

This bridge process can be automated using:
- Across Protocol API
- Stargate LayerZero SDK
- Socket API (aggregates bridges)

For now, manual bridging provides safety and control.

## Troubleshooting

**Bridge stuck?**
- Check transaction on source chain explorer
- Most bridges have status pages (e.g., stargate.finance/activity)
- Wait up to 30 minutes before retrying

**Wrong network?**
- Ensure wallet is on correct network before sending
- Base Chain ID: 8453
- Polygon Chain ID: 137

**Need help?**
- Stargate Discord: discord.gg/stargate
- Across Discord: discord.gg/across-protocol
