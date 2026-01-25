# PMFI Protocol Documentation

Welcome to PMFI (PredictFi) - automated prediction market vaults that generate yield from Polymarket trading.

## Quick Navigation

| Section | Description |
|---------|-------------|
| [Introduction](./01-introduction.md) | What is PMFI and why it exists |
| [Products](./02-products-overview.md) | All vault types and strategies |
| [pSNIPER Vault](./03-psniper-vault.md) | Our flagship first-mover vault |
| [How Pricing Works](./04-nav-oracle.md) | NAV and share pricing explained |
| [Depositing](./05-deposit-flow.md) | How to put money in |
| [Withdrawing](./06-withdrawal-flow.md) | How to get money out |
| [Asset Tracking](./07-asset-calculation.md) | How we track your funds |
| [Security](./08-security.md) | Safety mechanisms |
| [Roadmap](./09-future-vaults.md) | What's coming next |

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                         PMFI Protocol                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│     ┌─────────┐         ┌─────────┐         ┌─────────┐         │
│     │  User   │ ──────▶ │  Vault  │ ──────▶ │Polymarket│        │
│     │ (USDC)  │         │(Shares) │         │(Trading) │        │
│     └─────────┘         └─────────┘         └─────────┘         │
│                                                                  │
│     1. Deposit USDC    2. Receive shares   3. Vault trades      │
│                                             automatically        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Key Features

| Feature | Description |
|---------|-------------|
| **Passive Yield** | Deposit and earn - no active management needed |
| **Fair Pricing** | Real-time NAV ensures you get fair value |
| **Protected Withdrawals** | Price locked when you request - no slippage |
| **Full Transparency** | All assets tracked across chains |
| **Automated Trading** | Off-chain bots execute strategies |

## Contract Information

| Item | Details |
|------|---------|
| Network | Base (Ethereum L2) |
| Token | pSNIPER |
| Contract | `0x960eC492C1c9245dAe05bA4027d6e15ce0AD9d3D` |
| Deposit Currency | USDC |

## Version History

| Version | Key Changes |
|---------|-------------|
| V7.3.4 | Conservative position valuation |
| V7.3.3 | Improved asset tracking accuracy |
| V7.3.2 | Enhanced withdrawal reliability |
| V7.3.0 | Locked withdrawal pricing |
| V7.0.0 | Initial 3-state asset tracking |
