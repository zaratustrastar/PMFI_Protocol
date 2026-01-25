# Depositing

This guide explains how to deposit USDC into PMFI vaults and receive shares.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    DEPOSIT FLOW                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  YOU                     VAULT                    POLYMARKET     │
│   │                        │                          │          │
│   │  1. Approve USDC       │                          │          │
│   │───────────────────────▶│                          │          │
│   │                        │                          │          │
│   │  2. Deposit            │                          │          │
│   │───────────────────────▶│                          │          │
│   │                        │  3. Forward 100%         │          │
│   │                        │─────────────────────────▶│          │
│   │                        │                          │          │
│   │  4. Receive shares     │                          │          │
│   │◀───────────────────────│                          │          │
│   │                        │                          │          │
│   │  Done! Your shares     │  Trading begins          │          │
│   │  represent ownership   │  automatically           │          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Step-by-Step Process

### Step 1: Approve USDC

Before depositing, you must approve the vault to spend your USDC. This is a one-time transaction per wallet.

### Step 2: Enter Amount

Choose how much USDC to deposit. The interface shows:
- Current NAV (price per share)
- Shares you'll receive
- Any applicable limits

### Step 3: Confirm Transaction

Submit the deposit transaction. Behind the scenes:
1. Your USDC transfers to the vault
2. Vault forwards it to Polymarket's deposit address
3. New shares are minted to your wallet

**Note:** The forwarding to Polymarket is handled by the smart contract automatically. However, the subsequent bridging (Base → Polygon) depends on Polymarket's infrastructure.

### Step 4: Receive Shares

You now own vault shares (e.g., pSNIPER tokens). These:
- Represent your portion of vault assets
- Can be transferred like any token
- Increase in value as the vault profits

## Share Calculation

Shares received = Deposit Amount ÷ Current NAV

**Example:**
- You deposit: $5,000 USDC
- Current NAV: $1.05 per share
- Shares received: $5,000 ÷ $1.05 = **4,762 shares**

## Where Does Your Money Go?

After deposit, your USDC flows through several states:

| State | Duration | Location |
|-------|----------|----------|
| Deposit Address | 0-5 min | Waiting to be swept to Polygon |
| Bridging | 5-20 min | Crossing from Base to Polygon |
| Polymarket Account | Ongoing | Available for trading |

Throughout this journey, your shares track the full value. Nothing is lost during transit.

## Deposit Limits

### Per-Wallet Cap

Each wallet has a maximum deposit limit. This:
- Prevents concentration of ownership
- Distributes risk across depositors
- Can be checked in the interface

### Total Vault Cap

The vault has a maximum total size. When reached:
- New deposits are paused
- Existing depositors can still withdraw

### Limbo Mode

If too much money is in transit (>10% of vault), single deposits are capped at $1,000. This prevents concentration during bridging delays.

## Timing Considerations

### When to Deposit

- **No urgency**: Your shares are calculated at current NAV
- **Market conditions**: High activity = more opportunities for the vault
- **Gas fees**: Base chain has low fees, typically <$0.10

### After Depositing

- Funds begin trading immediately after bridging (~20 min)
- No action needed from you
- Check NAV periodically to track performance

## Fees

| Fee | Amount | When |
|-----|--------|------|
| Deposit Fee | None | On deposit |
| Gas | ~$0.05-0.10 | Transaction cost |
| Management Fee | None | Ongoing |
| Withdrawal Fee | 1% | On withdrawal only |

## Safety Checks

Before your deposit executes, the contract verifies:

1. **NAV is fresh** - Calculated within last 5 minutes
2. **NAV is valid** - Signed by authorized oracle
3. **Limits respected** - Within per-wallet and total caps
4. **Conservation bound** - NAV hasn't dropped suspiciously

If any check fails, the transaction reverts and you keep your USDC.

## What You Receive

After depositing, you have:

| Item | Description |
|------|-------------|
| Vault Shares | ERC-20 tokens in your wallet (e.g., pSNIPER) |
| Ownership | Proportional claim on all vault assets |
| Upside | Share in trading profits |
| Downside | Share in trading losses |
| Exit Right | Can withdraw anytime (subject to process) |

## Frequently Asked Questions

**Q: How long until my deposit is working?**
A: About 20-30 minutes for bridging, then immediately trading.

**Q: Can I deposit any amount?**
A: Minimum and maximum limits apply. Check the interface.

**Q: What if I deposit and NAV drops immediately?**
A: Your shares reflect the NAV at deposit time. Subsequent changes affect your value.

**Q: Are my shares transferable?**
A: Yes, they're standard ERC-20 tokens. You can send them to other wallets.

**Q: What's the minimum deposit?**
A: Set by the contract. Check the interface for current limits.
