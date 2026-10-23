# MILORD end-to-end trace (SIMULATED)

Run: `./sim/run-local.sh` (fresh anvil → canonical Permit2 → `DeployLocal` → `node sim/simulate.mjs`).
Output: `sim/output/simulation.json`, dashboard `sim/output/dashboard.html`.

**What is real and what is mocked.** Real: every MILORD contract, Uniswap v4 PoolManager, PositionManager,
StateView, Quoter, Permit2 and WETH (official npm artifacts), the V3 vault, pARB, distributor, Merkle claims,
TWAP quote service. Mocked: the Hookr hook (`MockHookrTaxHook`, implements 1.5%/1.5% ETH tax, 20% commission),
USDG (6-decimal ERC-20), the ETH/USDG pool (a local v4 pool instead of the Robinhood V3 pool), and the arbitrage
P&L (+2% week 2, −3% week 3, scenario inputs, **not** performance data). Illustrative launch values only.

## Launch (final configuration, LAUNCH_LIQUIDITY.md)

100% of supply (6,767,000,000 MILORD) in one MILORD-only position, tick 134220–203280 (≈10.06 ETH opening FDV, range
top ≈1,000× the opening price), 0 ETH, 0.3% LP fee, spacing 60, position held by `MockHookrFamilyLock` and locked forever with `HookrLockFeeCollector` as beneficiary (local only;
the mainnet lock awaits approval). The 24,920-wei rounding remainder is burned; the deployer ends with 0 MILORD.

## Steps and results

| # | Step | Result |
|---|---|---|
| 1 | Deploy and allocate | 6,767,000,000 MILORD minted once; all of it offered to the pool; 24,920-wei remainder burned; deployer, team, arbitrage and operator hold 0 |
| 2 | Open MILORD/ETH market | pool initialised at tick 203280 with 0 ETH |
| 3 | Mint MILORD-only position, lock | position #1 owned by the lock, no withdraw function |
| 3b | Nobody holds MILORD outside the pool | the first trade can only be a buy |
| 4 | First buy 1 ETH | 603,660,290.5 MILORD out (after tax and LP fee), +19.8% price |
| 4b | Entry quote at minute 5 | bounded bootstrap: highest price since open, fee 0.002 ETH (minimum) |
| 5 | More buys in the first hour; later two traders sell half | tax charged on both sides in ETH |
| 6–9 | Tax settle, commission, split | Hookr 20%, rest split ⅓ / ⅓ / ⅓ by TradingTaxSplitter |
| 10 | 40 users buy 2–3M MILORD each | — |
| 11 | 100 conversions with TWAP-signed quotes | 0.2 ETH fees, all at the 0.002 ETH minimum (price still low) |
| 12 | 100 Milords issued | escrow = 100 × 1,000,000 MILORD |
| 13 | Weekly close | entry fees 70/30; tax split; buyback burn; ETH → USDG; deposit; report; pARB minted; Merkle root; claims |
| 14–17 | Characteristics | owners without a choice default to pArbitrage; one holder edits their preference to characteristics 2 and 5 (identity and original selection unchanged); pArbitrage is the only live strategy |
| 18 | Secondary sales | one sale pays 6.7% (0.0335 ETH, split 50/50); a private sale pays none |
| 19 | Large buy (30 ETH, SCENARIO) in week 2 | FDV passes ≈135 ETH; later conversions pay the 10% path |
| 20 | Redemption | 900,000 MILORD returned, 100,000 burned; NFT to inventory |
| 21 | Positive report | PPS 1.000000 → 1.017999 |
| 22 | Negative report | PPS → 0.990513 (not reset to 1) |
| 23 | Fee deposit after the loss | 49.864139 USDG |
| 24 | Shares at the new PPS | 50.341731 pARB minted at 0.990513 |
| 25 | Second strategy registered | inactive; collection and token unchanged |
| 26 | Final reconciliation | 36/36 checks pass |

## Weekly summary

| Week | Supply (MILORD) | Active NFTs | USDG deposited | Scenario P&L | pARB minted | PPS | NAV (USDG) | Eligible wallets | Claimed total |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 6,757,908,336.09 | 100 | 452.627601 | 0 | 452.627601 | 1.000000 | 452.63 | 42 | 217.56 |
| 2 | 6,734,296,642.78 | 109 | 539.652507 | +8.147296 | 530.111038 | 1.017999 | 1,000.43 | 53 | 475.63 |
| 3 | 6,732,323,095.46 | 114 | 49.864139 | −27.011539 | 50.341731 | 0.990513 | 1,023.28 | 56 | 499.97 |

Totals: 34,576,904.54 MILORD burned by buybacks, 100,000 on exit, 24,920 wei at launch; 115 conversions (105 at the
0.002 ETH minimum, 10 at 10%); 1,033.080 pARB minted and fully allocated (unallocated dust 2e-17); 499.967 claimed.

## Tracked wallets

- **Alice (seller)**: held 3 Milords in week 1, sold one on day 3.5 of week 2; kept every pARB already earned.
- **Buyer**: earns only from day 3.5 of week 2 (time-weighted).
- **Carol (redeemer)**: keeps earned pARB; earns nothing after redeeming.

All amounts are simulated and illustrate mechanics; they are not a forecast or a promise of return.
