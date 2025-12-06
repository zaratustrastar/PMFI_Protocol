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
| `PredictFiSniperVault.sol` | Main ERC4626 vault contract |

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

## Liquidity Management Bot

The `bot/` folder contains a Python skeleton for monitoring and managing vault liquidity.

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
VAULT_ADDRESS=0x...
USDC_ADDRESS=0x...

# Strategy wallet for sending USDC to vault
STRATEGY_ADDRESS=0x...
STRATEGY_PRIVATE_KEY=your_private_key_here
```

3. Make sure contracts are compiled (the bot loads ABIs from artifacts):
```bash
npx hardhat compile
```

### Run the Bot

```bash
python bot/bot.py
```

The bot will:
- Connect to the blockchain via RPC
- Load the vault and USDC contracts
- Listen for `LiquidityShortfall` events
- Log shortfall details when detected

### What the Bot Does (Skeleton)

Currently the bot only **monitors** for events. The following functionality needs to be implemented:

1. **Polymarket Withdrawal**: When a shortfall is detected, withdraw USDC from Polymarket positions
2. **Vault Transfer**: Send recovered USDC from the strategy wallet to the vault

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
