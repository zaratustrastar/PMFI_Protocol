# pSNIPER V5.1 Operator Runbook

## Overview

This document provides operational guidance for running the pSNIPER V5.1 autonomous vault system.

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER FLOW                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. DEPOSIT                                                     │
│     User → approve USDC → deposit(amount, signedNAV)            │
│     → USDC stays in vault buffer                                │
│     → User receives pSNIPE shares                               │
│                                                                 │
│  2. INVEST IDLE (permissionless, anyone can call)               │
│     Anyone → investIdle()                                       │
│     → Funds above targetBuffer sent to Polymarket wallet        │
│     → Blocked when vault is paused                              │
│                                                                 │
│  3. REQUEST WITHDRAW                                            │
│     User → requestWithdraw(shares, signedNAV)                   │
│     → Shares locked in vault                                    │
│     → Added to withdrawal queue                                 │
│                                                                 │
│  4. CLAIM (works even when paused)                              │
│     User → claim(requestId, signedNAV)                          │
│     → Payout = shares × current NAV (claim-time pricing)        │
│     → 1% tax sent to deployer                                   │
│     → Requires: buffer ≥ payout amount                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Bot Components

### 1. NAV Oracle (every 30 seconds)
- Fetches Polymarket positions and orderbook
- Calculates depth-weighted liquidation value
- Applies 0.5% safety haircut
- Signs NAV data for user transactions

### 2. Withdrawal Watchdog (every 30 seconds)
- Monitors pending withdrawal queue
- Calculates buffer shortfall
- Sends Telegram alerts when refill needed
- Checks circuit breaker status

### 3. Safety Systems
- **Circuit Breaker**: Triggers at 15% NAV drop from baseline
- **Hourly Limit**: Max $5,000 liquidation per hour
- **Telegram Alerts**: Instant notifications for critical events

---

## Daily Monitoring Checklist

### Every Hour
- [ ] Check `/health` endpoint - status should be "ok"
- [ ] Verify NAV is updating (check timestamp)
- [ ] Review hourly liquidation usage

### Every 4 Hours
- [ ] Check `/withdrawals` for pending requests
- [ ] Verify buffer balance covers pending withdrawals
- [ ] Review Polymarket positions and cash

### Daily
- [ ] Check circuit breaker status
- [ ] Review Telegram alert history
- [ ] Verify no expired withdrawal requests

---

## Alert Response Procedures

### Alert: "Buffer shortfall detected"

**Severity**: Medium

**Actions**:
1. Check `/withdrawals` endpoint for shortfall amount
2. Review refill plan in response
3. If action is `PULL_PM_CASH`:
   - Log into Polymarket wallet
   - Withdraw cash to vault address on Base
4. If action is `PULL_CASH_THEN_LIQUIDATE`:
   - Withdraw available PM cash first
   - Sell positions as needed for remaining shortfall
   - Send proceeds to vault address

### Alert: "NAV dropped X% from baseline!"

**Severity**: Critical (Circuit Breaker)

**Actions**:
1. **DO NOT PANIC** - Circuit breaker has activated
2. Investigate cause of NAV drop:
   - Market event?
   - Polymarket API issue?
   - Position liquidation?
3. If legitimate drop:
   - Consider pausing vault: `vault.pause()`
   - Communicate with users
4. If false alarm (API glitch):
   - Restart bot to reset baseline NAV
   - Monitor for stabilization

### Alert: "Hourly limit exceeded"

**Severity**: Low-Medium

**Actions**:
1. Check if legitimate withdrawal demand
2. Wait for next hour for limit reset
3. If urgent, consider manually transferring USDC to vault

---

## When to Pause the Vault

Call `vault.pause()` when:

1. **Security breach suspected**
2. **Oracle compromise detected**
3. **Polymarket API unavailable >1 hour**
4. **Circuit breaker triggered + confirmed real issue**
5. **Smart contract bug discovered**

**Note**: When paused:
- ❌ `deposit()` blocked
- ❌ `requestWithdraw()` blocked
- ❌ `investIdle()` blocked
- ✅ `claim()` still works (users can exit)
- ✅ `cancelExpiredWithdrawal()` still works

---

## Manual Buffer Refill Procedure

When watchdog alerts for buffer shortfall:

### Option A: Pull Polymarket Cash
```bash
# 1. Login to Polymarket wallet
# 2. Navigate to Wallet → Withdraw
# 3. Enter vault address as destination
# 4. Withdraw required USDC amount
# 5. Verify on Base Explorer
```

### Option B: Liquidate Positions
```bash
# 1. Identify positions to sell (check /nav for values)
# 2. Place market sell orders on Polymarket
# 3. Wait for fills
# 4. Withdraw proceeds to vault
```

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `ORACLE_PRIVATE_KEY` | Oracle signer key (same as deployer) | Yes |
| `VAULT_V5_ADDRESS` | Deployed vault contract | Yes |
| `POLYMARKET_PROXY_ADDRESS` | Polymarket wallet with positions | Yes |
| `BASE_RPC_URL` | Base Mainnet RPC endpoint | Yes |
| `TELEGRAM_BOT_TOKEN` | Telegram bot for alerts | Recommended |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID | Recommended |

---

## Gelato/Chainlink Automation (Optional)

For automatic `investIdle()` calls:

1. **Condition**: `vault.getIdleBalance() > margin` (e.g., $100)
2. **Action**: Call `vault.investIdle()`
3. **Frequency**: Check every 5 minutes
4. **Gas**: Funded by automation platform

This ensures idle funds are automatically rebalanced without manual intervention.

---

## Recovery Procedures

### Bot Crashed
1. Check logs for error
2. Verify RPC connection
3. Restart bot: `python bot/bot_v5.py`
4. Monitor first few NAV updates

### Circuit Breaker Triggered (False Positive)
1. Stop bot
2. Delete baseline_nav from memory (restart clears it)
3. Restart bot
4. Verify NAV stabilized

### Vault Paused Accidentally
1. Call `vault.unpause()` from owner
2. Verify deposits working
3. Monitor for issues

---

## Key Invariants (Safety Checks)

The contract enforces these invariants:

1. **claim() buffer check**: `usdc.balanceOf(vault) >= grossUsdc`
2. **Fresh NAV required**: Signature valid for 30 seconds
3. **Shares burned once**: `request.claimed` flag prevents double-claim
4. **Monotonic roundId**: Prevents replay of old NAV signatures
5. **Max 5% NAV change**: Between consecutive updates

---

## Contact & Escalation

- **Telegram Alerts**: Automated via bot
- **Manual Pause**: Call `vault.pause()` from owner wallet
- **Emergency**: Drain all PM positions, refill buffer, communicate with users

---

## Version History

- **V5.1** (Current): Autonomous with safety limits, circuit breaker, Telegram alerts
- **V5.0**: Permissionless investIdle, claim-time NAV, 0.5% haircut
- **V4.0**: Signed NAV oracle, basic withdrawal queue
