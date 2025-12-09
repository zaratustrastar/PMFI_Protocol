# Complete Testing Guide for PredictFi Vault V2

This guide will walk you through testing every feature of the vault system step-by-step.

## 🎯 Contract Addresses (Base Sepolia)

| Contract | Address |
|----------|---------|
| **TestUSDC** | `0x743dBb99B51A542aA7b6E859713b4b615445C019` |
| **MockSniperStrategy** | `0x38B00348749CD5194c1fD732226Cd4F5fEEE4695` |
| **PredictFiSniperVaultV2** | `0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3` |
| **Deployer/Keeper** | `0x4E5BddD57b77058eF89C309aD9CCc028a3c42bF0` |

**BaseScan Links:**
- [TestUSDC](https://sepolia.basescan.org/address/0x743dBb99B51A542aA7b6E859713b4b615445C019#code)
- [Strategy](https://sepolia.basescan.org/address/0x38B00348749CD5194c1fD732226Cd4F5fEEE4695#code)
- [Vault](https://sepolia.basescan.org/address/0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3#code)

---

## 🛠 What You Need

1. **MetaMask wallet** with Base Sepolia testnet added
2. **Some Base Sepolia ETH** for gas (get from faucets below)
3. **A web browser** (Chrome recommended)

### Get Free Test ETH

Go to one of these faucets:
- https://www.alchemy.com/faucets/base-sepolia
- https://faucet.quicknode.com/base/sepolia

Paste your wallet address and get free ETH.

---

## 📋 Testing Steps

### STEP 1: Add Base Sepolia Network to MetaMask

1. Open MetaMask
2. Click the network dropdown (top left)
3. Click "Add Network" → "Add a network manually"
4. Enter these details:
   - **Network Name:** Base Sepolia
   - **RPC URL:** `https://sepolia.base.org`
   - **Chain ID:** 84532
   - **Currency Symbol:** ETH
   - **Block Explorer:** `https://sepolia.basescan.org`
5. Click Save

---

### STEP 2: Mint Test USDC (Get Fake Money for Testing)

1. Go to: https://sepolia.basescan.org/address/0x743dBb99B51A542aA7b6E859713b4b615445C019#writeContract
2. Click "Connect to Web3" (connect MetaMask)
3. Find the **`mint`** function (function #2)
4. Enter:
   - **to:** Your wallet address (copy from MetaMask)
   - **amount:** `100000000` (this is 100 USDC - remember USDC has 6 decimals)
5. Click "Write" and confirm in MetaMask
6. Wait for transaction to complete (green checkmark)

**✅ To verify:** Go to the "Read Contract" tab and use `balanceOf` with your address. Should show `100000000` (100 USDC).

---

### STEP 3: Approve Vault to Spend Your USDC

Before depositing, you must give the vault permission to move your USDC:

1. Stay on TestUSDC: https://sepolia.basescan.org/address/0x743dBb99B51A542aA7b6E859713b4b615445C019#writeContract
2. Find the **`approve`** function (function #1)
3. Enter:
   - **spender:** `0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3` (vault address)
   - **amount:** `100000000` (100 USDC)
4. Click "Write" and confirm in MetaMask

**✅ To verify:** Go to "Read Contract" → `allowance` and enter:
- owner: Your wallet address
- spender: `0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3`
- Should show `100000000`

---

### STEP 4: Deposit USDC into the Vault

1. Go to Vault: https://sepolia.basescan.org/address/0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3#writeContract
2. Find the **`deposit`** function
3. Enter:
   - **assets:** `50000000` (50 USDC)
   - **receiver:** Your wallet address
4. Click "Write" and confirm in MetaMask

**✅ To verify:** 
- Go to "Read Contract" → `balanceOf` with your address (should show your vault shares)
- Check `totalAssets()` - should show around 50 USDC

---

### STEP 5: Check the Per-Wallet Deposit Cap

The vault limits each wallet to 100 USDC total deposits:

1. Try depositing more than 50 USDC (since you already deposited 50)
2. Try depositing exactly 50 USDC more - should work
3. Try depositing 51 USDC - should **FAIL** with error "Wallet deposit cap exceeded"

**✅ To verify:** Use `walletDeposited(yourAddress)` in Read Contract - shows your total deposits.

---

### STEP 6: Check Vault Buffer (20% Idle, 80% Invested)

1. Go to "Read Contract" 
2. Check these values:
   - `idleAssets()` - Should be ~20% of total (10 USDC if you deposited 50)
   - `investedAssets()` - Amount sent to strategy

**Note:** Right after deposit, all funds are idle. An owner must call `investIdle()` to send 80% to strategy.

---

### STEP 7: Invest Idle Funds (Owner Only)

If you're the deployer/owner:

1. Go to Vault: https://sepolia.basescan.org/address/0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3#writeContract
2. Find **`investIdle`** function
3. Click "Write" (no parameters needed)
4. Confirm in MetaMask

**✅ To verify:** 
- `idleAssets()` should now be 20% of total
- `investedAssets()` should now be 80% of total
- Check strategy's USDC balance increased

---

### STEP 8: Test NAV Updates (Keeper Bot Simulation)

The keeper bot updates the strategy value (NAV). To test manually:

1. Go to Strategy: https://sepolia.basescan.org/address/0x38B00348749CD5194c1fD732226Cd4F5fEEE4695#writeContract
2. Find **`updateStrategyValue`** function
3. Enter a new NAV (simulating profit/loss):
   - For 10% profit on 40 USDC invested: `44000000` (44 USDC)
   - For 20% loss: `32000000` (32 USDC)
4. Click "Write" and confirm

**✅ To verify:**
- Check `strategyValue()` on the strategy - should show new value
- Check `totalAssets()` on vault - should reflect new NAV

---

### STEP 9: Test Withdrawal

1. Go to Vault: https://sepolia.basescan.org/address/0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3#writeContract
2. Find **`withdraw`** function
3. Enter:
   - **assets:** Amount of USDC to withdraw (e.g., `10000000` for 10 USDC)
   - **receiver:** Your wallet address
   - **owner:** Your wallet address
4. Click "Write" and confirm

**Note:** If withdrawal exceeds idle buffer, it triggers a `LiquidityShortfall` event (the bot watches for these).

**✅ To verify:**
- Check TestUSDC `balanceOf(yourAddress)` - should increase by withdrawn amount
- Check Vault `balanceOf(yourAddress)` - your shares should decrease
- Check `totalAssets()` on vault - should decrease by withdrawn amount

---

### STEP 10: Test Redeem (Withdraw by Shares)

Alternative way to withdraw - specify shares instead of USDC amount:

1. First check your share balance: "Read Contract" → `balanceOf(yourAddress)`
2. Go to "Write Contract" → `redeem`
3. Enter:
   - **shares:** Number of shares to redeem
   - **receiver:** Your wallet address
   - **owner:** Your wallet address
4. Click "Write" and confirm

**✅ To verify:**
- Check TestUSDC `balanceOf(yourAddress)` - should increase (you received USDC)
- Check Vault `balanceOf(yourAddress)` - should decrease by exact shares you redeemed
- Shares-to-USDC ratio depends on current NAV (may not be 1:1)

---

## 🤖 Testing the Bot

### Prerequisites for Bot Testing

1. Create a `.env` file with:
```
RPC_URL=https://sepolia.base.org
VAULT_ADDRESS=0x26BCAe8DEA9A2b04a522cab2679CF9708d3F84E3
USDC_ADDRESS=0x743dBb99B51A542aA7b6E859713b4b615445C019
STRATEGY_ADDRESS=0x38B00348749CD5194c1fD732226Cd4F5fEEE4695
KEEPER_ADDRESS=0x4E5BddD57b77058eF89C309aD9CCc028a3c42bF0
KEEPER_PRIVATE_KEY=your_private_key_here
```

2. Install Python dependencies:
```bash
pip install web3 python-dotenv
```

3. Run the bot:
```bash
python bot/bot.py
```

### What the Bot Does

- **Every 60 seconds:** Calculates NAV and pushes update to strategy contract
- **Every 10 seconds:** Checks for LiquidityShortfall events (when withdrawals exceed buffer)

---

## ✅ Testing Checklist

| Test | Expected Result | Your Result |
|------|-----------------|-------------|
| Mint TestUSDC | Receive 100 USDC | ⬜ |
| Approve vault | Allowance set | ⬜ |
| Deposit 50 USDC | Receive vault shares | ⬜ |
| Check wallet cap | walletDeposited = 50 USDC | ⬜ |
| Deposit 50 more | Success (at cap) | ⬜ |
| Deposit 1 more | FAIL - cap exceeded | ⬜ |
| Check buffer | idleAssets = 20% | ⬜ |
| InvestIdle | 80% sent to strategy | ⬜ |
| Update NAV | strategyValue changes | ⬜ |
| Withdraw | Receive USDC back | ⬜ |
| Run bot | NAV updates every 60s | ⬜ |

---

## 🔍 Troubleshooting

### "Execution reverted" Error
- Make sure you approved enough USDC
- Check you have enough gas (ETH)
- Check you're not exceeding deposit caps

### Transaction Stuck
- Click "Speed Up" in MetaMask
- Wait a few minutes - Base Sepolia can be slow

### Bot Not Working
- Check your private key is correct
- Make sure you have ETH for gas
- Verify RPC URL is correct

### Can't See My Tokens
- Add TestUSDC to MetaMask manually:
  - Token Address: `0x743dBb99B51A542aA7b6E859713b4b615445C019`
  - Symbol: USDC
  - Decimals: 6

---

## 📊 Quick Reference

| Amount | Raw Value (6 decimals) |
|--------|------------------------|
| 1 USDC | 1000000 |
| 10 USDC | 10000000 |
| 50 USDC | 50000000 |
| 100 USDC | 100000000 |
| 1000 USDC | 1000000000 |

---

## Need Help?

If something doesn't work:
1. Check the transaction on BaseScan for error messages
2. Make sure you're connected to Base Sepolia network
3. Verify contract addresses are correct
4. Check your wallet has enough ETH for gas
