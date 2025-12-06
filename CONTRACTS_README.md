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

## License

MIT
