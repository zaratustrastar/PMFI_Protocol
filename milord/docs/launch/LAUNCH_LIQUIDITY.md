# Final launch liquidity: 100% of supply, 0 ETH

**Owner decision (Bobo, 2026-10-14), final:** all 6,767,000,000 MILORD go into one MILORD-only Uniswap v4 position
through Hookr's existing-token launch. 0 MILORD stays with the deployer, team or treasury. 0 ETH is added. Opening FDV
about 10 ETH. **Implemented locally; not deployed; nothing locked on mainnet.**

Sources: `offchain/launch-final.mjs` → `sim/output/launch-final.json` (model), `test/launch/HookrFullSupply.t.sol`
(real Uniswap v4 contracts, mock Hookr tax hook), `./sim/run-local.sh` (end-to-end).

## Configuration

| Item | Value | Status |
|---|---|---|
| Chain / launchpad | Robinhood Chain 4663 / Hookr existing-token launch | EXTERNAL |
| Pool | native ETH (currency0) / MILORD (currency1), tick spacing 60 | TESTED locally |
| Opening tick (= tickUpper) | 203280 → 0.0014863 ETH per 1M MILORD → **FDV 10.058 ETH** (nearest spacing-60 tick to 10 ETH) | TESTED |
| tickLower | 134220 → range top 1.483 ETH per 1M ≈ 10,037 ETH FDV (≈1,000× the opening price) | TESTED |
| MILORD offered to the position | 6,767,000,000.000000000000000000 | TESTED |
| Liquidity | 269,412,717,938,970,703,383,501 | computed with exact v4 math |
| ETH in the position at open | 0 | TESTED |
| Buy / sell tax | 1.5% / 1.5% in ETH, no transfer tax | SIMULATED WITH MOCKS |
| LP fee | 0.3% (pending approval) | NEEDS APPROVAL |
| Hookr commission | 20% target (verify live floor) | EXTERNAL |

## Exact balances after initialization

v4 positions take MILORD in whole units of liquidity, so the position can absorb all but a rounding remainder.

| Account | MILORD after initialization |
|---|---|
| Founding position (PoolManager) | **6,766,999,999.999999999999975080** (6,766,999,999,999,999,999,999,975,080 wei) |
| Rounding remainder | 24,920 wei (0.00000000000002492 MILORD), **burned in the same batch** with `MILORD.burn` |
| Allocation Safe / deployer | 0 |
| Team Safe | 0 |
| Arbitrage Funding Safe | 0 |
| Converter / any other wallet | 0 |
| Total supply after initialization | 6,766,999,999.999999999999975080 |

The local deployment, the Foundry fixture and the Safe batch generator all produce the same 24,920-wei remainder,
and the deployment script reverts if the deployer holds any MILORD afterwards. If Hookr's launcher seeds the
position itself, its own rounding decides the remainder; it must be checked on the real contracts (checklist).

## First-buy price impact (opening price, single swap, 1.5% tax and 0.3% LP fee included)

| First buy (ETH) | MILORD received | % of supply | Milords' worth | Avg ETH per 1M | Price after | FDV after (ETH) |
|---|---|---|---|---|---|---|
| 0.002 | 1,321,224 | 0.02% | 1 | 0.001514 | +0.04% | 10.06 |
| 0.01 | 6,601,123 | 0.10% | 6 | 0.001515 | +0.19% | 10.08 |
| 0.05 | 32,881,373 | 0.49% | 32 | 0.001521 | +0.95% | 10.15 |
| 0.1 | 65,454,764 | 0.97% | 65 | 0.001528 | +1.9% | 10.25 |
| 0.25 | 161,369,720 | 2.39% | 161 | 0.001549 | +4.8% | 10.54 |
| 0.5 | 315,455,054 | 4.66% | 315 | 0.001585 | +9.7% | 11.03 |
| **1** | **603,660,290** | **8.92%** | 603 | 0.001657 | **+19.8%** | 12.05 |
| 2 | 1,111,321,757 | 16.4% | 1,111 | 0.001800 | +41.4% | 14.22 |
| 5 | 2,243,205,927 | 33.1% | 2,243 | 0.002229 | +117% | 21.82 |
| 10 | 3,396,228,527 | 50.2% | 3,396 | 0.002944 | +279% | 38.07 |
| 25 | 4,910,711,257 | 72.6% | 4,910 | 0.005091 | +1,031% | 113.8 |
| 50 | 5,768,102,711 | 85.2% | 5,768 | 0.008668 | +3,180% | 329.9 |
| 100 | 6,319,810,077 | 93.4% | 6,319 | 0.01582 | +10,831% | 1,099 |

On-chain check: the simulation's 1 ETH first buy received 603,660,290.48 MILORD, matching the model.

## Maximum purchasable

- **Everything.** There are no wallet or transaction limits (none added, per your instruction). One swap of about
  **323.5 ETH** buys the entire position (a 400 ETH order in the test bought all of it). Above the range top the pool
  holds only ETH, and no MILORD is for sale until someone sells.
- One Milord at open: 1,000,000 MILORD costs ≈0.001514 ETH, plus the 0.002 ETH minimum entry fee.
- The 10% entry fee overtakes the 0.002 ETH minimum at FDV ≈135.3 ETH, reached after ≈28.2 ETH of net buys.
- **Concentration:** a single 10 ETH first buyer would get about half of all MILORD. That is a consequence of a low
  opening price with no limits; it is disclosed, not prevented.

## Sell side

At open no wallet holds MILORD, so the first trade can only be a buy (tested). After that, sellers are paid only from
ETH that buyers put in, net of tax and LP fee (tested: two buyers selling everything back receive less than their
combined 1.5 ETH minus tax and fees, and the price returns to the opening level).

## NFT conversion with no treasury allocation

The converter never uses a treasury: each holder brings 1,000,000 MILORD bought in the pool. Because every buyback and
exit burn reduces supply below 6,767,000,000, all 6,767 Milords can never be awake at the same time once any MILORD has
been burned; the practical ceiling is `floor(totalSupply / 1,000,000)` minus MILORD held elsewhere.

## Legacy PMFI holders (process only, no contract, no minting)

1. Holders voluntarily send PMFI; the team sells it under a disclosed process and records ETH proceeds `P`.
2. Buy MILORD in the pool with `P` in tranches (for example ≤0.5 ETH each, spaced apart) with `minOut` from the TWAP.
3. Distribute exactly the MILORD bought, pro rata to PMFI contributed.

What `P` buys is not fixed. At the opening price, 1 ETH buys 603.7M MILORD instead of the 672.8M a zero-cost quote
suggests: 1.8% goes to tax and LP fee and 8.5% to price impact. Later the price may be higher or lower. Gas (Robinhood
Chain, small) and the PMFI sale's own slippage also reduce `P`. The migration's buys pay the 1.5% tax like any buy.
Publish the PMFI received, ETH raised, each buy transaction, and MILORD distributed.
