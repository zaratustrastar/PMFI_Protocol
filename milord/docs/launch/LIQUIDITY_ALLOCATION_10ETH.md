# One-sided liquidity at the approved 10 ETH opening FDV

Owner decisions (Bobo, 2026-10-14): opening FDV 10 ETH; 0 ETH seeded; one MILORD-only Uniswap v4 position through
Hookr; the rest of the supply kept for approved reserves and allocations. **Not deployed.** Numbers come from
`offchain/launch-10eth.mjs` (`sim/output/launch-10eth.json`), with the 1.5% buy tax and 0.3% LP fee included, and are
checked on real Uniswap v4 contracts by `test/launch/HookrAllocation.t.sol` (mock Hookr tax hook).

## Position (same for all three options)

| Item | Value |
|---|---|
| Pool | native ETH (currency0) / MILORD (currency1), tick spacing 60, LP fee 0.3% |
| Opening tick (= tickUpper) | 203280 → **0.001486 ETH per 1M MILORD, FDV 10.06 ETH** (nearest spacing-60 tick to 10 ETH) |
| tickLower | 134220 → range top at about 1,000x the opening price (≈10,000 ETH FDV) |
| ETH seeded | 0 |
| Cost of one Milord's 1M MILORD at open | ≈0.0015 ETH plus tax, plus the 0.002 ETH minimum entry fee |

## The three allocations

| | **25%** | **50%** | **75%** |
|---|---|---|---|
| MILORD in the position | 1,691,750,000 | 3,383,500,000 | 5,075,250,000 |
| MILORD kept for reserves/allocations | 5,075,250,000 | 3,383,500,000 | 1,691,750,000 |
| Max NFTs backable from pool purchases | 1,691 | 3,383 | 5,075 |
| ETH to buy the whole position | ≈81 | ≈162 | ≈243 |

### First buy at open (one buyer, single swap)

| First buy | 25%: price move / MILORD out (% of supply) | 50% | 75% |
|---|---|---|---|
| 0.01 ETH | +0.76% / 6.58M (0.10%) | +0.38% / 6.59M (0.10%) | +0.25% / 6.60M (0.10%) |
| 0.1 ETH | +7.7% / 63.7M (0.94%) | +3.8% / 64.8M (0.96%) | +2.5% / 65.3M (0.96%) |
| 0.5 ETH | +41.4% / 278M (4.1%) | +19.8% / 302M (4.5%) | +13.0% / 311M (4.6%) |
| **1 ETH** | **+89.9% / 479M (7.1%)** | **+41.4% / 556M (8.2%)** | **+26.8% / 587M (8.7%)** |
| 2 ETH | +208% / 752M (11.1%) | +89.9% / 959M (14.2%) | +56.8% / 1,055M (15.6%) |
| 5 ETH | +736% / 1,143M (16.9%) | +279% / 1,698M (25.1%) | +166% / 2,026M (29.9%) |
| 10 ETH | +2,187% / 1,382M (20.4%) | +736% / 2,285M (33.8%) | +411% / 2,923M (43.2%) |

FDV after a 1 ETH first buy: 19.1 / 14.2 / 12.8 ETH.

### Sells

With 0 ETH seeded, a sell before any buy receives 0 ETH in every option (tested). After the first buy, the pool holds the
buyer's ETH net of tax and LP fee (0.982 ETH after a 1 ETH buy), and sellers can take out only that. Selling 1M MILORD
right after a 1 ETH first buy returns ≈0.0028 / 0.0021 / 0.0019 ETH net (25 / 50 / 75%).

## Trade-off

- **More in the pool** (75%): smaller price moves and a deeper market for the community, but one early large buyer
  gets a bigger slice of total supply (10 ETH buys 43% of all MILORD), and only 1.69B is left for reserves.
- **Less in the pool** (25%): a large reserve, but the market is thin: a single 1 ETH buy nearly doubles the price,
  and 81 ETH buys out the whole position.
- **Reserves outside the pool** can be sold into the pool and take buyers' ETH. Whatever is kept should be locked or
  vested as approved.

## Recommendation: 50% (3,383,500,000 MILORD)

- A 1 ETH first buy moves the price by 41% rather than 90%, and 0.1 ETH (about 65 Milords' worth) moves it by 4%.
- A 10 ETH buyer gets about a third of supply, not 43%, which limits early concentration.
- It keeps 3.38B MILORD (3,383 Milords' worth) for approved reserves and allocations.
- It is the setting the full end-to-end simulation already ran with (35/35 checks).

Pick 75% if most reserves will stay unused and a deeper market matters more than reserves. Avoid 25%.
