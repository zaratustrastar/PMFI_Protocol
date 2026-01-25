# Withdrawing

PMFI uses a two-phase withdrawal system to ensure fair and reliable payouts.

## Why Two Phases?

Unlike a bank account, PMFI can't give you cash instantly because:
- Funds are actively trading on Polymarket
- Positions may need to be sold
- Money must bridge from Polygon to Base

The two-phase system handles this gracefully while protecting your price.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    WITHDRAWAL FLOW                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  PHASE 1: REQUEST                                                │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                                                              ││
│  │  You submit withdrawal request                               ││
│  │           ↓                                                  ││
│  │  Your shares are locked in the contract                      ││
│  │           ↓                                                  ││
│  │  Your USDC amount is FIXED at current NAV                    ││
│  │  (This protects you from price changes!)                     ││
│  │                                                              ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  BEHIND THE SCENES (Automated)                                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                                                              ││
│  │  Bot detects pending withdrawal                              ││
│  │           ↓                                                  ││
│  │  Sells positions if needed to raise cash                     ││
│  │           ↓                                                  ││
│  │  Bridges USDC from Polygon to Base                           ││
│  │           ↓                                                  ││
│  │  USDC arrives in vault buffer                                ││
│  │                                                              ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  PHASE 2: CLAIM                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                                                              ││
│  │  You submit claim transaction                                ││
│  │           ↓                                                  ││
│  │  1% withdrawal fee deducted                                  ││
│  │           ↓                                                  ││
│  │  USDC sent to your wallet                                    ││
│  │                                                              ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Phase 1: Request Withdrawal

### What You Do

1. Enter how many shares to withdraw
2. See the USDC amount you'll receive (at current NAV)
3. Submit the request transaction

### What Happens

- Your shares are transferred to the vault (locked)
- Your USDC amount is recorded and **fixed forever**
- You receive a withdrawal request ID

### Price Protection

This is the key feature: **your price is locked at request time**.

| Scenario | Without Locking | With PMFI |
|----------|-----------------|-----------|
| Request at NAV $1.00 | Price not fixed | Price = $1.00 ✓ |
| NAV drops to $0.85 | You get $0.85/share | You get $1.00/share |
| NAV rises to $1.20 | You get $1.20/share | You get $1.00/share |

You trade upside potential for downside protection. Most users prefer knowing exactly what they'll receive.

## Between Phases: What Happens (Off-Chain)

Our withdrawal servicer bot handles liquidity preparation:

1. **Detects** your pending withdrawal
2. **Calculates** how much USDC is needed
3. **Sells positions** if vault cash is insufficient
4. **Bridges** USDC from Polygon to Base
5. **Deposits** into vault buffer for your claim

**Important:** This is an off-chain process run by a bot, not guaranteed by the smart contract. While designed to run automatically, it may experience delays due to:
- Bot downtime or errors
- Illiquid positions that can't be sold
- Bridge congestion or failures
- Manual intervention requirements

In most cases, the process completes within 30-60 minutes. Complex situations may take longer.

## Phase 2: Claim

### When You Can Claim

Once USDC is in the vault buffer, you can claim. Typically:
- **5-30 minutes** if cash is available
- **30-60 minutes** if positions need selling
- **Longer** for illiquid positions or large amounts

### What You Do

1. Check if your withdrawal is ready
2. Submit the claim transaction
3. Receive USDC in your wallet

### Fees

| Fee | Amount | Deducted From |
|-----|--------|---------------|
| Withdrawal Tax | 1% | Your locked USDC amount |
| Bridge Fees | ~0.1-0.5% | Absorbed by vault (up to 0.5% tolerance) |
| Gas | ~$0.05-0.10 | Paid by you |

**Example:**
- Locked amount: $1,000
- Withdrawal tax (1%): $10
- You receive: **$990**

## Timing Expectations

| Scenario | Expected Time |
|----------|---------------|
| Cash already available | 5-15 minutes |
| Small position sales needed | 15-30 minutes |
| Large position sales needed | 30-60+ minutes |
| Illiquid positions | May require waiting for market activity |

## Withdrawal Expiry

Withdrawal requests expire after **7 days**. If you don't claim in time:
- Your shares are returned to you
- You can submit a new request
- This prevents indefinitely locked capital

## Edge Cases

### Large Withdrawals

Very large withdrawals may take longer because:
- More positions need liquidating
- Off-chain rate limits in the bot (soft cap, not contract-enforced)
- Bridge transactions take time

### Illiquid Positions

If the vault holds positions that can't be sold:
- These are valued at $0 in NAV
- The servicer skips them
- You still receive your locked amount from available cash

### Multiple Withdrawals

You can have multiple pending withdrawals:
- Each tracked separately
- Claim in any order
- Each has its own 7-day expiry

## Frequently Asked Questions

**Q: Can I cancel a withdrawal request?**
A: Once submitted, you cannot cancel. You must wait for the 7-day expiry or claim.

**Q: What if NAV goes up after I request?**
A: You receive your locked amount. You don't benefit from increases after requesting.

**Q: What if NAV goes down after I request?**
A: You still receive your locked amount. You're protected from decreases.

**Q: How do I know when I can claim?**
A: Check the interface - it shows when USDC is available for your request.

**Q: What if the vault doesn't have enough USDC?**
A: The bot automatically sells positions and bridges funds. Just wait.

**Q: Is there a minimum withdrawal?**
A: The bridge requires minimum $5 transfers. Very small withdrawals may be bundled.

**Q: What's the 0.5% tolerance mentioned?**
A: Bridge fees may reduce the final amount slightly (up to 0.5%). This is rare and absorbed when possible.
