# Introduction to PMFI

## What is PMFI?

PMFI (PredictFi) is a protocol that lets you earn yield from prediction market trading without doing any work. You deposit USDC, receive vault shares, and our automated systems trade on Polymarket to generate returns.

Think of it like a mutual fund, but for prediction markets.

## The Problem We Solve

### Trading Prediction Markets is Hard

Polymarket offers incredible opportunities, but participating effectively requires:

- **24/7 Monitoring** - Markets move around the clock
- **Technical Expertise** - Order management, position sizing, risk control
- **Cross-Chain Operations** - Moving money between Base and Polygon
- **Capital Efficiency** - Managing liquidity across positions
- **Speed** - New markets need to be sniped in seconds

Most people don't have the time, skills, or infrastructure to do this well.

### Our Solution

PMFI handles all of this for you:

1. **You deposit USDC** on Base chain
2. **You receive shares** representing your portion of the vault
3. **We trade** on Polymarket automatically using proven strategies
4. **You withdraw** anytime with your share of the profits

## How Vaults Work

### Share-Based System

When you deposit, you receive vault shares (like pSNIPER tokens). These shares represent your ownership of the vault's total assets.

**Example:**
- Vault has $100,000 in assets
- 100,000 shares exist
- Each share = $1.00
- You deposit $1,000 → receive 1,000 shares
- After trading profits, vault grows to $110,000
- Your 1,000 shares now = $1,100

### Fair Pricing (NAV)

We use Net Asset Value (NAV) to ensure everyone gets a fair price:

```
NAV = Total Vault Assets ÷ Total Shares
```

This means:
- Early depositors don't get diluted by later ones
- New depositors don't buy in at inflated prices
- Everyone's share reflects actual value

## What Makes PMFI Different

### 1. Real Value, Not Guesses

We don't use theoretical prices. We calculate what positions are actually worth by checking real market depth. If something can't be sold, we value it at $0.

### 2. Protected Withdrawals

When you request a withdrawal, your price is locked immediately. You know exactly what you'll receive, regardless of market movements.

### 3. Complete Transparency

We track every dollar across:
- Your initial deposit
- Funds being bridged between chains
- Cash in trading accounts
- Active positions

Nothing gets lost or miscounted.

### 4. Automated Operations

From market monitoring to order execution to fund bridging - operations are automated via off-chain bots. While designed to run 24/7, these systems may require occasional manual intervention for edge cases.

## Who Should Use PMFI

**Good for:**
- Passive investors wanting prediction market exposure
- People who believe in Polymarket's growth but lack trading time
- Those seeking yield beyond traditional DeFi

**Not for:**
- Active traders who want manual control
- Those uncomfortable with prediction market volatility
- Anyone needing instant liquidity (withdrawals take time)

## Getting Started

1. Read about our [Products](./02-products-overview.md)
2. Understand [How Pricing Works](./04-nav-oracle.md)
3. Learn the [Deposit Process](./05-deposit-flow.md)
4. Review [Security](./08-security.md) measures
