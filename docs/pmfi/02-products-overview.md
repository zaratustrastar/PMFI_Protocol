# Products Overview

PMFI offers specialized vaults, each targeting different prediction market opportunities.

## Active Vaults

### pSNIPER Vault ✅ Live

**Strategy:** First-mover market sniping

The pSNIPER vault captures value when new markets launch on Polymarket. New markets often have:
- Wide spreads between buy and sell prices
- Mispriced probabilities
- Low competition for early fills

Our bot monitors for new markets every minute and places orders at favorable prices before the crowd arrives.

| Metric | Details |
|--------|---------|
| Status | Active |
| Token | pSNIPER |
| Risk Level | Medium-High |
| Target | New market inefficiencies |
| Withdrawal Fee | 1% |

[Learn more about pSNIPER →](./03-psniper-vault.md)

---

## Planned Vaults

### pARBITRAGE Vault

**Strategy:** Cross-market arbitrage

Finds pricing mistakes between related markets. For example, if "Bitcoin > $150k" trades higher than "Bitcoin > $100k" (which makes no sense), the vault exploits this.

| Metric | Details |
|--------|---------|
| Status | Planned (Q2 2026) |
| Risk Level | Low |
| Target | Logical pricing errors |

---

### pENDSPIEL Vault

**Strategy:** End-game timing

Specializes in markets approaching resolution. When an outcome is 95% certain but the market trades at 85%, the vault captures that final 15% gap.

| Metric | Details |
|--------|---------|
| Status | Planned (Q2 2026) |
| Risk Level | Low |
| Target | Near-resolution opportunities |

---

### pVOLATILITY Vault

**Strategy:** Volatility capture

Profits from price swings regardless of direction. During high-uncertainty events (elections, trials), prices oscillate wildly - this vault trades both sides.

| Metric | Details |
|--------|---------|
| Status | Planned (Q3 2026) |
| Risk Level | Medium |
| Target | High-volatility events |

---

### pCOPY Vault

**Strategy:** Copy trading

Identifies consistently profitable Polymarket traders and mirrors their positions automatically. Access "smart money" strategies without research.

| Metric | Details |
|--------|---------|
| Status | Planned (Q3 2026) |
| Risk Level | Medium |
| Target | Top trader replication |

---

### pLP Vault

**Strategy:** Liquidity provision

Provides two-sided liquidity to prediction markets, earning the spread between buy and sell prices. Similar to being a market maker.

| Metric | Details |
|--------|---------|
| Status | Planned (Q4 2026) |
| Risk Level | Low-Medium |
| Target | Bid-ask spreads |

---

### pINSIDER Vault

**Strategy:** Information edge

Aggregates alternative data (social media, news, on-chain signals) to identify informational edges before market prices adjust.

| Metric | Details |
|--------|---------|
| Status | Planned (TBD) |
| Risk Level | High |
| Target | Information advantages |

---

## Vault Comparison

| Vault | Strategy | Risk | Status | Key Advantage |
|-------|----------|------|--------|---------------|
| pSNIPER | First-mover | Medium-High | ✅ Live | Speed on new markets |
| pARBITRAGE | Arbitrage | Low | Planned | Risk-free logic trades |
| pENDSPIEL | End-game | Low | Planned | High certainty plays |
| pVOLATILITY | Vol trading | Medium | Planned | Direction-agnostic |
| pCOPY | Copy trading | Medium | Planned | Access to smart money |
| pLP | Market making | Low-Medium | Planned | Consistent spreads |
| pINSIDER | Info edge | High | Planned | Alternative data |

## Common Features

All vaults share:

- **NAV-based pricing** - Fair value at all times
- **Locked withdrawals** - Price fixed when you request
- **Cross-chain automation** - Base ↔ Polygon handled automatically
- **Transparent tracking** - See exactly where funds are
- **Safety mechanisms** - Rate limits, kill switches, conservation bounds
