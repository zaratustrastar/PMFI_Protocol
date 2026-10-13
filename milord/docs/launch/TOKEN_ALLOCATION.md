# Supply and pool allocation (ILLUSTRATIVE, NEEDS APPROVAL)

No production allocation, opening price or FDV has been chosen. The numbers below come from
`offchain/launch-plan.mjs` (concentrated-liquidity math, 1.5% tax and 0.3% LP fee included) and the full table is
in `sim/output/launch-options.json`. The reference row was reproduced on-chain in the local simulation
(open 0.001486 ETH per 1M MILORD; a 0.1 ETH buy moved the price +3.8%, 1 ETH +41.4%, 10 ETH +735.8%).

## Fixed facts

| Item | Value | Status |
|---|---|---|
| Total supply | 6,767,000,000 MILORD, minted once to the allocation Safe, no mint function | TESTED |
| Pool liquidity | MILORD only, 0 ETH | TESTED |
| ETH available to sellers at open | 0 (sellers receive only ETH that buyers paid in) | TESTED |
| NFT minimum entry fee | 0.002 ETH applies while 10% of 1M MILORD's value is below 0.002 ETH, i.e. FDV below about 135.3 ETH | TESTED |

## Options (pool share × opening FDV × range top)

Price impact = spot price change after a single buy of that size at open. "Buy-out" = gross ETH that buys every
MILORD in the position.

| Pool share | Opening FDV (ETH) | ETH per 1M at open | Range top FDV | 0.1 ETH | 1 ETH | 10 ETH | Buy-out ETH |
|---|---|---|---|---|---|---|---|
| 40% | 2.5 | 0.000370 | 250 | +18.5% | +254.8% | +9,574% | 10.2 |
| 40% | 5 | 0.000741 | 5,000 | +9.7% | +117.3% | +3,196% | 64.5 |
| 40% | 10 | 0.001486 | 10,000 | +4.8% | +52.9% | +1,032% | 129.4 |
| 40% | 25 | 0.003700 | 25,000 | +1.9% | +19.9% | +280% | 322.1 |
| 50% | 2.5 | 0.000370 | 250 | +14.6% | +191.3% | +6,410% | 12.7 |
| 50% | 5 | 0.000741 | 5,000 | +7.7% | +90.2% | +2,197% | 80.7 |
| **50% (sim)** | **10** | **0.001486** | **10,000** | **+3.8%** | **+41.4%** | **+736%** | **161.8** |
| 50% | 10 | 0.001486 | 1,000 | +3.5% | +38.2% | +660% | 51.1 |
| 50% | 25 | 0.003700 | 25,000 | +1.5% | +15.8% | +210% | 402.7 |
| 60% | 5 | 0.000741 | 5,000 | +6.4% | +73.2% | +1,631% | 96.8 |
| 60% | 10 | 0.001486 | 10,000 | +3.2% | +34.0% | +564% | 194.1 |
| 60% | 25 | 0.003700 | 25,000 | +1.3% | +13.1% | +167% | 483.2 |

Ticks for every row are in the JSON (ETH = currency0, MILORD = currency1, spacing 60).

## Reading the table

- Lower opening FDV: cheaper NFTs at launch (0.002 ETH minimum fee), but large moves from small buys, and the
  whole position can be bought out cheaply.
- Wider range (top at 1,000x the opening price vs 100x): more MILORD sits at high prices; slightly larger early impact.
- MILORD outside the pool (40–60% of supply) can be sold into the pool. Selling it drains buyers' ETH. If any of
  it is for the team or a treasury, decide lockups/vesting before launch.

## Outside-pool allocation: not proposed

The brief asks for allocations; the spec gives no split for the remainder (team, treasury, community, legacy
PMFI holders). The simulation keeps it all in the allocation Safe. **Owner decision: how much, to whom, and
locked how.** Legacy PMFI holders: see `docs/OPEN_DECISIONS.md` (documented only, no migration contract).

## Values to approve

1. Share of supply in the pool.
2. Opening price (ETH per 1M MILORD), which sets FDV.
3. Range top (and therefore tickLower).
4. Remainder allocation and lockups.
