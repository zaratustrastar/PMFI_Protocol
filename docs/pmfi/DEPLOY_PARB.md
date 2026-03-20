# pARB Vault — Mainnet Deployment Guide

Exact ordered steps to deploy the PMFI pARB Vault (V1) on Base Mainnet
and activate the arb bot on the VPS.

---

## Prerequisites

Have these values ready before you start:

| What | Where to get it |
|---|---|
| Deployer wallet private key | MetaMask / hardware wallet — needs ~$5 ETH for gas |
| ARB_NAV_SIGNER address + private key | Generate a fresh wallet (this signs NAV proofs) |
| ARB_SERVICER_WALLET address | The VPS wallet that will bridge USDC to fund the vault |
| ARB_MAX_TOTAL_USDC | Max vault cap in USDC (e.g. 5000 for $5K) |
| ODDPOOL_API_KEY | From Oddpool dashboard |
| POLY_PRIVATE_KEY | Polymarket trading wallet private key |
| KALSHI_API_KEY | Kalshi API key |

---

## Step 1 — Compile & Deploy the Contract

Run on your local machine (where Hardhat is installed):

```bash
cd ~/your-local-repo

# Set env vars
export DEPLOYER_PRIVATE_KEY="0x..."
export ARB_SERVICER_WALLET="0x..."
export ARB_NAV_SIGNER="0x..."
export ARB_MAX_TOTAL_USDC="5000"   # or 3 for initial $3 test

# Deploy
npx hardhat run scripts/deploy-arb-vault-v1-mainnet.cjs --network base
```

This will:
- Deploy `PMFIArbVaultV1` (token name: "PMFI Arb", symbol: "pARB")
- Save deployment info to `deployments/arb-vault-v1-mainnet.json`
- Print the contract address and a manual verify command

**Record the contract address** — you'll need it in Step 2.

---

## Step 2 — Verify on Basescan (optional but recommended)

Copy the verify command printed by the deploy script, e.g.:

```bash
npx hardhat verify --network base 0xYOUR_CONTRACT_ADDRESS \
  "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" \
  "0xNAV_SIGNER" \
  "0xTAX_COLLECTOR" \
  "0xSERVICER_WALLET" \
  "5000000000"
```

---

## Step 3 — Update .env on VPS

SSH into the VPS:

```bash
ssh user@app.pmfi.cc
nano ~/PolyNotifyBot/.env
```

Add / update these lines:

```bash
ARB_VAULT_V1_ADDRESS=0xYOUR_DEPLOYED_CONTRACT
ARB_NAV_SIGNER_PRIVATE_KEY=0xNAV_SIGNER_PRIVATE_KEY
ARB_SERVICER_WALLET=0xSERVICER_WALLET
ARB_MAX_PAIR_USDC=500
ARB_MAX_DEPLOYED_USDC=5000
ODDPOOL_API_KEY=your_oddpool_key
KALSHI_API_KEY=your_kalshi_key
POLY_PRIVATE_KEY=0xPOLY_TRADING_WALLET_KEY
OPINION_API_KEY=your_opinion_key   # if using Opinion Labs
```

> **Note**: `ARB_MIN_HOURS_TO_EXPIRY` is no longer needed — expiry filtering
> is handled by Oddpool's actionable flag (default is 0, i.e. disabled).

---

## Step 4 — Pull Latest Code on VPS

```bash
cd ~/PolyNotifyBot
git pull origin main
```

---

## Step 5 — Restart the Bot

```bash
sudo systemctl restart psniper-bot
sudo journalctl -u psniper-bot -f    # watch logs live
```

Look for these lines in the logs to confirm pARB started:

```
✅ Arb tables initialized
🔁 [ArbExecLoop] Starting execution loop
🔑 [ArbNav] Signer loaded: 0x...
```

---

## Step 6 — Test with a $2–$3 Deposit

1. Go to `https://app.pmfi.cc/arbitrage`
2. Connect your MetaMask wallet
3. Approve USDC, then deposit $2–$3
4. Confirm the pARB NAV/share shows **~$1.00**
5. Check that pARB shares appear in your wallet

---

## Step 7 — Verify Arb Bot is Trading

After a few minutes (or one Oddpool scan cycle), check:

```bash
sudo journalctl -u psniper-bot --since "5 minutes ago" | grep "ArbExec"
```

Or visit `https://app.pmfi.cc/arbitrage` → Trade History panel should populate as trades execute.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| NAV stuck at 0 | ARB_NAV_SIGNER_PRIVATE_KEY correct? Bot logs for `ArbNav` errors |
| No trades executing | ODDPOOL_API_KEY set? Check `ARB_USE_ODDPOOL_ONLY` env var |
| Deposit fails | Contract address correct in frontend `.env`? |
| Bot not restarting | Check `sudo systemctl status psniper-bot` for errors |

---

## Contract Info

- **Contract class**: `PMFIArbVaultV1`
- **Token name / symbol**: PMFI Arb / pARB
- **Network**: Base Mainnet (chainId: 8453)
- **USDC**: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- **Domain salt** (for NAV signing): `PMFIArbVaultV1.v1`
  *(this string is baked into `DOMAIN_SALT` in the contract and must match `ARB_VAULT_DOMAIN_SALT` in `arb_nav.py`)*
