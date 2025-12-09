# PredictFi Sniper Vault

An ERC4626 tokenized vault for managing USDC deposits and investing in prediction market sniping strategies.

## Features

- **ERC4626 Standard**: Fully compliant tokenized vault
- **Buffer Management**: Keeps configurable % of funds idle for withdrawals
- **Performance Fees**: Charges fees only on realized profits
- **Deposit Caps**: Limits total deposits to manage risk
- **Strategy Integration**: Modular strategy interface for trading logic

## Contracts

| Contract | Description |
|----------|-------------|
| `TestUSDC.sol` | Mock USDC token with 6 decimals for testing |
| `ISniperStrategy.sol` | Interface for trading strategies |
| `MockSniperStrategy.sol` | Mock strategy for testing |
| `PredictFiSniperVault.sol` | Main ERC4626 vault contract (V1) |
| `PredictFiSniperVaultV2.sol` | V2 vault with per-wallet deposit caps |

## V2 Features: Per-Wallet Deposit Caps

V2 adds individual wallet deposit limits on top of the global cap:

**New State Variables:**
- `walletDepositCap` - Maximum USDC each wallet can deposit
- `walletDeposited[address]` - Tracks total deposited per wallet

**New Functions:**
- `setWalletDepositCap(uint256 _cap)` - Owner can update the per-wallet limit

**Behavior:**
- Deposit/mint checks both global cap AND per-wallet cap
- Once a wallet hits their cap, they cannot deposit more
- Withdrawals do NOT reduce `walletDeposited` (conservative MVP approach)
- Different wallets have independent deposit limits

## Installation

```bash
npm install
```

## Compile Contracts

```bash
npx hardhat compile
```

## Run Tests

```bash
npx hardhat test
```

Run with gas reporting:
```bash
REPORT_GAS=true npx hardhat test
```

## Deploy to Local Network

Start a local Hardhat node:
```bash
npx hardhat node
```

In a new terminal, deploy:
```bash
npx hardhat run scripts/deploy-sniper-vault.js --network localhost
```

## Deploy to Testnet

1. Create a `.env` file with your credentials:
```bash
RPC_URL=https://sepolia.base.org
PRIVATE_KEY=your_private_key_here
```

2. Fund your deployer wallet with testnet ETH

3. Deploy to your chosen network:

**Base Sepolia:**
```bash
npx hardhat run scripts/deploy-sniper-vault.js --network baseSepolia
```

**Polygon Amoy:**
```bash
npx hardhat run scripts/deploy-sniper-vault.js --network polygonAmoy
```

**Ethereum Sepolia:**
```bash
npx hardhat run scripts/deploy-sniper-vault.js --network sepolia
```

## Vault Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `performanceFeeBps` | 500 (5%) | Fee charged on profits |
| `bufferBps` | 2000 (20%) | % of assets kept idle |
| `maxTotalDeposits` | 10,000 USDC | Maximum vault capacity |

## Architecture

```
User Deposits USDC
        ↓
┌──────────────────────┐
│ PredictFiSniperVault │
│   (ERC4626)          │
│                      │
│  Buffer: 20% idle    │
│  Invested: 80%       │
└──────────┬───────────┘
           ↓
┌──────────────────────┐
│   SniperStrategy     │
│                      │
│  Polymarket trading  │
│  Buy low, sell high  │
└──────────────────────┘
```

## Security Considerations

- Owner-only functions for configuration changes
- ReentrancyGuard on all state-changing functions
- Max 50% performance fee cap
- Strategy restricted to vault-only interactions

---

## NAV Updater & Liquidity Management Bot

The `bot/` folder contains a Python bot that:
1. **Periodically updates the on-chain NAV** of the strategy (keeper role)
2. **Monitors for LiquidityShortfall events** from the vault
3. (Future) Handles withdrawing funds from Polymarket to cover shortfalls

### Bot Setup

1. Install Python dependencies:
```bash
cd bot
pip install -r requirements.txt
```

2. Create a `.env` file in the project root:
```bash
# RPC connection
RPC_URL=https://sepolia.base.org

# Deployed contract addresses (from deployment output)
VAULT_ADDRESS=0xDFde5410FF65D0fb31D82a901400eeb48c40b272
USDC_ADDRESS=0x7FF3F11bbE48a6573F7CeEA46993d8166bf057C5
STRATEGY_ADDRESS=0x6a944Badac3a3C42bAf0347631BB219961Eb60Bc

# Keeper credentials (the EOA that has been set as keeper on the strategy)
KEEPER_ADDRESS=0xYourKeeperAddress
KEEPER_PRIVATE_KEY=your_keeper_private_key_here
```

3. Make sure contracts are compiled (the bot loads ABIs from artifacts):
```bash
npx hardhat compile
```

4. Set the keeper on-chain (owner must do this once):
   - Go to the MockSniperStrategy on BaseScan
   - Call `setKeeper(KEEPER_ADDRESS)` using the owner wallet

### Run the Bot

```bash
python bot/bot.py
```

The bot will:
- Connect to the blockchain via RPC
- Load the vault, USDC, and strategy contracts
- Every 60 seconds: Calculate NAV and push it on-chain via `updateStrategyValue()`
- Every 10 seconds: Check for `LiquidityShortfall` events and log them

### NAV Calculation (Current Implementation)

The current implementation is a **dummy prototype**:
- Reads USDC balance held by strategy contract
- Adds a simulated 5% profit
- Pushes this value on-chain

**TODO**: Replace with real Polymarket NAV calculation using orderbook mid prices.

### LiquidityShortfall Handling (TODO)

When a shortfall is detected, the bot should:
1. Withdraw USDC from Polymarket positions
2. Send recovered USDC to the vault

See the `TODO` comments in `bot/bot.py` for implementation placeholders.

---

## Frontend

The `frontend/` folder contains a simple HTML/JavaScript interface to interact with the vault using MetaMask.

### Frontend Setup

1. Update contract addresses in `frontend/main.js`:
```javascript
const VAULT_ADDRESS = "0x...";  // Your deployed vault address
const USDC_ADDRESS = "0x...";   // Your deployed USDC address
```

2. Serve the frontend locally:
```bash
# Option 1: Python simple server
cd frontend && python -m http.server 8080

# Option 2: Node.js with npx
npx serve frontend

# Option 3: Just open index.html in browser
open frontend/index.html
```

### Using the Frontend

1. **Connect Wallet**: Click "Connect MetaMask" and approve the connection
2. **Switch Network**: Make sure MetaMask is on the correct testnet (Base Sepolia, etc.)
3. **Get Test USDC**: Use the TestUSDC contract's `mint()` function to get tokens
4. **Deposit**: Enter amount, click Deposit (approves USDC first, then deposits)
5. **Withdraw**: Enter amount, click Withdraw

### Minting Test USDC

Use Hardhat console to mint test tokens:
```bash
npx hardhat console --network baseSepolia
```

```javascript
const usdc = await ethers.getContractAt("TestUSDC", "USDC_ADDRESS");
await usdc.mint("YOUR_WALLET_ADDRESS", ethers.parseUnits("1000", 6));
```

### Features

- Displays wallet connection status
- Shows USDC balance and vault share balance
- Shows total vault assets and your redeemable amount
- Deposit with automatic USDC approval
- Withdraw to receive USDC back

---

## License

MIT
