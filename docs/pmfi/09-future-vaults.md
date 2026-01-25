# Roadmap & Future Vaults

PMFI is building a suite of prediction market vaults. Here's what's coming.

## Roadmap Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                       PMFI ROADMAP                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  2025 Q4    ████████████████████████████                        │
│             pSNIPER Vault LIVE ✅                                │
│                                                                  │
│  2026 Q1    ████████████████░░░░░░░░░░░░                        │
│             Infrastructure improvements                          │
│             Enhanced monitoring                                  │
│                                                                  │
│  2026 Q2    ████████░░░░░░░░░░░░░░░░░░░░                        │
│             pARBITRAGE Vault                                     │
│             pENDSPIEL Vault                                      │
│                                                                  │
│  2026 Q3    ████░░░░░░░░░░░░░░░░░░░░░░░░                        │
│             pVOLATILITY Vault                                    │
│             pCOPY Vault                                          │
│                                                                  │
│  2026 Q4    ██░░░░░░░░░░░░░░░░░░░░░░░░░░                        │
│             pLP Vault                                            │
│                                                                  │
│  2027+      ░░░░░░░░░░░░░░░░░░░░░░░░░░░░                        │
│             pINSIDER Vault                                       │
│             Additional strategies                                │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Planned Vaults

### pARBITRAGE Vault (Q2 2026)

**Strategy:** Cross-market arbitrage

Finds pricing mistakes between logically related markets and profits from their correction.

**How it works:**

Imagine two markets:
- "Bitcoin above $150k by December" at 25%
- "Bitcoin above $100k by December" at 20%

This is illogical! If BTC hits $150k, it definitely hit $100k first. The second market should be at least 25%.

The vault buys these mispricings and profits when they correct.

**Risk/Reward:**
- Risk: Low (based on logic, not prediction)
- Reward: Moderate (spreads are typically small)
- Best for: Risk-averse investors wanting steady returns

---

### pENDSPIEL Vault (Q2 2026)

**Strategy:** End-game timing

Specializes in markets approaching resolution where outcomes are nearly certain.

**How it works:**

When a market is 24-72 hours from resolving and the outcome is ~95% clear, but prices lag at 85%, the vault captures that 15% gap.

**Example scenarios:**
- Election night with exit polls showing clear winner
- Sports events with insurmountable leads
- Regulatory decisions with leaked outcomes

**Risk/Reward:**
- Risk: Low (high-confidence situations only)
- Reward: High (quick 15-20% returns)
- Best for: Those wanting occasional high-conviction plays

---

### pVOLATILITY Vault (Q3 2026)

**Strategy:** Volatility capture

Profits from price swings regardless of direction during uncertain events.

**How it works:**

During high-uncertainty events (contested elections, major trials, regulatory decisions), prices swing wildly between 30-70%.

The vault:
1. Buys when prices hit extremes (under 30% or over 70%)
2. Sells when prices revert toward 50%
3. Repeats throughout volatile periods

**Risk/Reward:**
- Risk: Medium (requires volatility to profit)
- Reward: Variable (depends on event volatility)
- Best for: Those comfortable with active market conditions

---

### pCOPY Vault (Q3 2026)

**Strategy:** Copy trading

Automatically mirrors trades of consistently profitable Polymarket traders.

**How it works:**

We analyze on-chain trading history to identify wallets with:
- High win rates over 50+ trades
- Consistent returns across different markets
- Clear areas of expertise

When these "smart money" traders make moves, we copy them (scaled to vault size).

**Risk/Reward:**
- Risk: Medium (depends on trader selection)
- Reward: Matches top trader performance
- Best for: Those who believe in "smart money" edges

---

### pLP Vault (Q4 2026)

**Strategy:** Liquidity provision

Acts as a market maker, earning the spread between buy and sell prices.

**How it works:**

The vault quotes both sides of markets:
- Willing to buy at 48¢
- Willing to sell at 52¢
- Spread: 4¢ per round-trip

When both sides trade, the vault keeps the spread as profit.

**Risk/Reward:**
- Risk: Low-Medium (inventory risk during trends)
- Reward: Steady (consistent small gains)
- Best for: Those wanting yield from trading fees, not predictions

---

### pINSIDER Vault (TBD)

**Strategy:** Information edge

Uses alternative data sources to identify informational advantages before prices adjust.

**How it works:**

Aggregates signals from:
- Social media sentiment
- News feeds and breaking stories
- On-chain activity patterns
- Expert prediction networks

Machine learning identifies when these signals predict price movements before the market catches up.

**Risk/Reward:**
- Risk: High (signals can be wrong)
- Reward: High (true edges are very profitable)
- Best for: Aggressive investors comfortable with higher risk

---

## Vault Comparison

| Vault | Strategy | Risk | Target Returns | Status |
|-------|----------|------|----------------|--------|
| pSNIPER | First-mover | Medium-High | Variable | ✅ Live |
| pARBITRAGE | Arbitrage | Low | 10-20% APY | Q2 2026 |
| pENDSPIEL | End-game | Low | 15-25% per play | Q2 2026 |
| pVOLATILITY | Vol capture | Medium | Variable | Q3 2026 |
| pCOPY | Copy trading | Medium | Matches leaders | Q3 2026 |
| pLP | Market making | Low-Medium | 8-15% APY | Q4 2026 |
| pINSIDER | Info edge | High | Variable | TBD |

## Tokenomics Considerations

Future vaults may introduce:

### Protocol Revenue

- Withdrawal fees (1% standard)
- Performance fees (potential for high-performing vaults)
- LP incentives

### Governance

- Token holder voting on new vaults
- Fee parameter adjustments
- Strategy modifications

### Staking

- Stake tokens to boost returns
- Priority access to new vaults
- Revenue sharing

*Specific tokenomics will be announced with each vault launch.*

## Infrastructure Roadmap

### Q1 2026

- Enhanced monitoring dashboard
- Improved bridging reliability
- Multi-oracle redundancy

### Q2 2026

- Cross-vault capital efficiency
- Unified position management
- Advanced risk controls

### Q3+ 2026

- Multi-strategy coordination
- Dynamic rebalancing
- Institutional integrations

## How to Stay Updated

- Follow our announcements for vault launches
- Join the community for early access
- Provide feedback on strategy preferences

## Contributing

We welcome community input on:
- Strategy suggestions
- Risk analysis
- Feature requests
- Bug reports

The best ideas may become real products.
