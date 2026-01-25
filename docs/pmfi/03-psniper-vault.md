# pSNIPER Vault

The pSNIPER vault is PMFI's flagship product - an automated first-mover strategy for new Polymarket listings.

## Strategy Overview

When new prediction markets launch on Polymarket, there's a window of opportunity. The first few minutes often have:

- **No initial liquidity** - Few people watching
- **Wide spreads** - Big gap between buy and sell prices
- **Mispriced odds** - Probabilities that don't reflect reality

The pSNIPER vault exploits this by being first.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                    pSNIPER STRATEGY                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  STEP 1: DETECT                                                  │
│  ┌─────────────────┐                                            │
│  │ Monitor every   │ → New market found!                        │
│  │ minute for new  │   "Will X happen by Y?"                    │
│  │ markets         │                                            │
│  └─────────────────┘                                            │
│                                                                  │
│  STEP 2: FILTER                                                  │
│  ┌─────────────────┐                                            │
│  │ Skip low-value  │ → Keep high-potential markets              │
│  │ markets (hourly │   (elections, crypto, major events)        │
│  │ price bets)     │                                            │
│  └─────────────────┘                                            │
│                                                                  │
│  STEP 3: SNIPE                                                   │
│  ┌─────────────────┐                                            │
│  │ Place orders at │ → Buy YES tokens at 1-3¢                   │
│  │ extreme prices  │ → Buy NO tokens at 1-3¢                    │
│  │ on both sides   │                                            │
│  └─────────────────┘                                            │
│                                                                  │
│  STEP 4: PROFIT                                                  │
│  ┌─────────────────┐                                            │
│  │ When orders     │ → Bought at 2¢                             │
│  │ fill, sell at   │ → Sell at 6-20¢                            │
│  │ 3-10x profit    │ → 3-10x return                             │
│  └─────────────────┘                                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Example Trade

1. **New market launches:** "Will Company X announce earnings above $5B?"
2. **Bot detects it** within 60 seconds
3. **Places orders:**
   - 100 YES tokens at $0.01
   - 100 YES tokens at $0.02
   - 100 YES tokens at $0.03
   - Same for NO tokens
4. **Orders fill** at $0.02 average (200 tokens for $4)
5. **Price discovery happens** - market settles at $0.40 YES
6. **Bot sells** YES tokens at $0.20 (10x target)
7. **Profit:** Bought for $4, sold for $40 = $36 profit

Not every trade wins, but the winners significantly outweigh the losses.

## Risk Management

### What We Filter Out

- **Hourly price markets** - "Bitcoin up/down" bets resolve too fast
- **Duplicate markets** - Already have exposure
- **Illiquid categories** - Markets with no trading activity

### Position Limits

- Maximum per-market exposure
- Portfolio-level position caps
- Automatic rebalancing when overweight

### Order Management

- **Stale order cancellation** - Orders open >12 hours get cancelled
- **Capital recycling** - Freed capital goes to new opportunities
- **Loss limits** - Stop trading if losses exceed thresholds

## Vault Details

| Parameter | Value |
|-----------|-------|
| Network | Base |
| Token Symbol | pSNIPER |
| Deposit Currency | USDC |
| Withdrawal Fee | 1% |
| Min Deposit | Set by contract |
| Max per Wallet | Set by contract |

## Performance Factors

Returns depend on:

1. **New market volume** - More new markets = more opportunities
2. **Market quality** - Better markets have more price discovery
3. **Competition** - Other snipers reduce edge
4. **Resolution outcomes** - Winning vs losing positions

## Risks

| Risk | Description | Mitigation |
|------|-------------|------------|
| Market Risk | Positions can lose value | Diversification across many markets |
| Liquidity Risk | May be hard to exit positions | Conservative valuation (no bids = $0) |
| Execution Risk | Orders may not fill | Laddered order placement |
| Bridge Risk | Cross-chain delays | 3-state asset tracking |

## Who Is This For?

**Good fit:**
- Believers in Polymarket's continued growth
- Those comfortable with prediction market volatility
- Medium-to-long term holders

**Not ideal for:**
- Those needing daily liquidity
- Risk-averse investors
- Those who want to pick specific markets
