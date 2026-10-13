# MILORD end-to-end trace (SIMULATED)

Run: `./sim/run-local.sh` (fresh anvil → canonical Permit2 → `DeployLocal` → `node sim/simulate.mjs`).
Output: `sim/output/simulation.json`, dashboard `sim/output/dashboard.html`.

**What is real and what is mocked.** Real: every MILORD contract, Uniswap v4 PoolManager, PositionManager,
StateView, Quoter, Permit2 and WETH (official npm artifacts), the V3 vault, pARB, distributor, Merkle claims,
TWAP quote service. Mocked: the Hookr hook (`MockHookrTaxHook`, implements 1.5%/1.5% ETH tax, 20% commission),
USDG (6-decimal ERC-20), the ETH/USDG pool (a local v4 pool instead of the Robinhood V3 pool), and the arbitrage
P&L (+2% week 2, −3% week 3, scenario inputs, **not** performance data). Illustrative launch values only.

## Launch (reference row of TOKEN_ALLOCATION.md)

50% of supply (3,383,500,000 MILORD) in one MILORD-only position, tick 134220–203280 (about 10 ETH opening FDV,
range top at about 1,000x the opening price, i.e. 10,000 ETH FDV), 0.3% LP fee, spacing 60, position minted to `MilordLiquidityLock`.

## Steps and results

| # | Step | Result |
|---|---|---|
| 1 | Deploy and allocate | 6,767,000,000 MILORD minted once; 3,383,500,000 to the pool |
| 2 | Open MILORD/ETH market | pool initialised at tick 203280 with 0 ETH |
| 3 | Mint MILORD-only position, lock | position #1 owned by the lock, no withdraw function |
| 3b | Sell before any buy | static call: 10M MILORD sell receives **0 ETH** |
| 4 | First buy 1 ETH | 555,660,878.6 MILORD out (after tax and LP fee) |
| 4b | Entry quote at minute 5 | **refused**: insufficient TWAP history (no 0.002 ETH fallback) |
| 5 | More buys in the first hour; later two traders sell half | tax charged on both sides in ETH |
| 6–9 | Tax settle, commission, split | Hookr 20%, rest split ⅓ / ⅓ / ⅓ by TradingTaxSplitter |
| 10 | 40 users buy 2–3M MILORD each | — |
| 11 | 100 conversions with TWAP-signed quotes | 0.2 ETH fees, all at the 0.002 ETH minimum (price still low) |
| 12 | 100 Milords issued | escrow = 100 × 1,000,000 MILORD |
| 13 | Weekly close | entry fees 70/30; tax split; buyback burn; ETH → USDG; deposit; report; pARB minted; Merkle root; claims |
| 14–17 | Characteristics | owners without a choice default to pArbitrage; one holder edits their preference to characteristics 2 and 5 (identity and original selection unchanged); pArbitrage is the only live strategy |
| 18 | Secondary sales | one sale pays 6.7% (0.0335 ETH, split 50/50); a private sale pays none |
| 19 | Large buy (15 ETH) in week 2 | price rises; later conversions pay the 10% path |
| 20 | Redemption | 900,000 MILORD returned, 100,000 burned; NFT to inventory |
| 21 | Positive report | PPS 1.000000 → 1.017999 |
| 22 | Negative report | PPS → 0.990513 (not reset to 1) |
| 23 | Fee deposit after the loss | 49.966827 USDG |
| 24 | Shares at the new PPS | 50.445403 pARB minted at 0.990513 |
| 25 | Second strategy registered | inactive; collection and token unchanged |
| 26 | Final reconciliation | 35/35 checks pass |

## Weekly summary

| Week | Supply (MILORD) | Active NFTs | USDG deposited | Scenario P&L | pARB minted | PPS | NAV (USDG) | Eligible wallets | Claimed total |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 6,760,562,411.84 | 100 | 457.933179 | 0 | 457.933179 | 1.000000 | 457.93 | 42 | 220.11 |
| 2 | 6,751,523,602.58 | 109 | 301.864789 | +8.242797 | 296.527589 | 1.017999 | 768.04 | 53 | 364.47 |
| 3 | 6,749,646,057.76 | 114 | 49.966827 | −20.737100 | 50.445403 | 0.990513 | 797.27 | 56 | 388.86 |

Totals: 17,253,942.24 MILORD burned by buybacks, 100,000 on exit; 115 conversions (105 at the 0.002 ETH
minimum, 10 at 10%); 804.906 pARB minted and fully allocated (unallocated dust 5e-17); 388.857 claimed.

## Tracked wallets

- **Alice (seller)**: held 3 Milords in week 1, sold one on day 3.5 of week 2; kept every pARB already earned.
- **Buyer**: earns only from day 3.5 of week 2 (time-weighted).
- **Carol (redeemer)**: keeps earned pARB; earns nothing after redeeming.

All amounts are simulated and illustrate mechanics; they are not a forecast or a promise of return.
