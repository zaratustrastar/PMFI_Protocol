# pARB V2 Deployment Guide

## Overview
pARB V2 is an async Yearn-style vault. No live NAV is required for deposit/redeem — users submit requests and shares/USDC are issued during the next `report()` call.

## Contract: PMFIArbVaultV2.sol

Key concepts:
- `requestDeposit(assets, receiver)` — queue USDC, get requestId
- `claimDeposit(requestId, receiver)` — claim shares after report()
- `requestRedeem(shares, receiver)` — queue shares for USDC, get requestId
- `claimRedeem(requestId, receiver)` — claim USDC after report() + liquidity
- `tend()` — anyone can call; refills 10% idle buffer from strategy
- `report(reportData, sig)` — keeper-only; updates officialPPS, processes queues, charges 20% perf fee on realized profits; requires ARB_NAV_SIGNER_PRIVATE_KEY

## Deploy Steps

1. Fund deployer wallet with ~0.005 ETH on Base Mainnet

2. Set env vars:
   ```
   ARB_NAV_SIGNER_ADDRESS=<signer wallet address>
   ARB_SERVICER_ADDRESS=<servicer/keeper wallet address>
   ARB_FEE_RECIPIENT=<fee recipient address>
   ```

3. Run deploy script:
   ```bash
   node scripts/deploy-arb-vault-v2-mainnet.cjs
   ```

4. Set `ARB_VAULT_V2_ADDRESS` in `.env` on VPS and in Replit secrets

5. Set `ARB_VAULT_V2_ADDRESS` in `frontend/config.js` (or equivalent config injection) so frontend switches to V2

6. Restart VPS bot:
   ```bash
   cd ~/PolyNotifyBot && git pull && sudo systemctl restart psniper-bot
   ```

## V1 → V2 Migration

- V1 contract (`0x10f67BA7aB746a0DC8A48f0D74aA3a962328E689`) stays live until all V1 holders redeem
- V2 is additive: set `ARB_VAULT_V2_ADDRESS` to enable; leave unset to silently skip
- Frontend auto-routes to V2 when `ARB_VAULT_V2_ADDRESS` is set; V1 functions remain as fallback

## Auto-Claim (reporter post-report sweep)

After each successful `report()` call the reporter automatically sweeps all CLAIMABLE requests using two new permissionless contract functions:

- `autoClaimDeposits(uint256[] requestIds)` — mints pARB shares → stored receiver
- `autoClaimRedeems(uint256[] requestIds)` — transfers USDC → stored receiver

Users never need to click "Claim Shares" manually. The reporter:
1. Waits for the report() tx to confirm on-chain (polls up to 90s)
2. Reads `depositRequestCount()` / `redeemRequestCount()` to get total request counts
3. Reads each request struct to find CLAIMABLE (status=1) ones
4. Broadcasts `autoClaimDeposits` and/or `autoClaimRedeems` in a single batch tx each

> **Requires contract redeploy**: The live V2 contract must be replaced with the updated `PMFIArbVaultV2.sol` that includes `autoClaimDeposits`, `autoClaimRedeems`, `depositRequestCount`, and `redeemRequestCount`. See Deploy Steps below.

## Reporter (arb_reporter.py)

The reporter signs `ReportDataV2` and calls `report()`. It runs on each execution loop tick if `ARB_VAULT_V2_ADDRESS` is set.

Required env vars on VPS:
- `ARB_VAULT_V2_ADDRESS`
- `ARB_NAV_SIGNER_PRIVATE_KEY`
- `POLY_API_KEY`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `OPINION_API_KEY`
- `ARB_SERVICER_WALLET_ADDRESS` (= `0xba32aa4cF8800b0e57c79900C11B9c839C6bAeAF`)

Report frequency: controlled by `ARB_REPORT_INTERVAL_HOURS` (default: 1h)

## Frontend HTML Requirements

Add a container for pending requests in the pARB section of `index.html`:
```html
<div id="arbPendingRequests" class="hidden" style="margin-top:12px;"></div>
```
This is populated automatically by `loadArbPendingRequests()` when the user has active deposit/redeem requests.

## Key Addresses (Base Mainnet)

- USDC: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- V1 vault: `0x10f67BA7aB746a0DC8A48f0D74aA3a962328E689`
- V2 vault: set after deploy

## Domain Salt

`keccak256("PMFIArbVaultV2.v1")` — isolated from V1 and pSNIPER oracles
