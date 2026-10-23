# Simulation and test report

Run on 2026-10-14 in a local container. Nothing broadcast to a public network.

**Totals: Foundry 162/162, simulation 39/39, services failure tests 14/14, funding bot 19/19, website flow test passes.**

## Foundry (`forge test`): 162 passed, 0 failed, 0 skipped

New in this round: ArbitrageFundingRouterTest (16: wiring, revenue → USDG → vault → pARB at reported PPS, per-tx/daily caps and interval, threshold, parameter bounds, pause rules, keeper mode, sweep only to owner, keeper cannot reach adapter, same-block manipulation refused, small move bounded by slippage, no-history revert, bad reference cannot force a trade, venue checks, Chainlink + DualReference, paused router accumulates safely) and HookrLockFeesTest (9: exact ETH-to-burner and MILORD burn, no-trade collect takes 0, second collect empty, only fees leave the pool, only beneficiary collects / no role withdraws / release refused, ERC-6909 claims redeemed and burned, PoolManager-only callback, no interference with trading or conversion, fuzz: collect never takes principal). HookrLaunchTest now checks LP fees reach buyback-and-burn.

| Suite | Tests | Covers |
|---|---|---|
| HookrFullSupplyTest | 5 | final config: all supply in the position, every other wallet 0, remainder burned, ~10 ETH FDV with 0 ETH, first buy matches the model, buys then sells, whole position purchasable |
| HookrLaunchTest | 15 | token-only open, first trade must be a buy, 1.5% tax both sides in ETH, 20% commission and equal thirds, LP fees separate, round-trip bounded, price impact, buyback true burn with price limit and threshold, lock has no withdraw, ETH→USDG, adapter errors, adapter set once, commission fuzz 20–50% |
| ConverterTest | 28 | entry fee max(10%, 0.002 ETH), signed TWAP quotes, staleness, collateral, exit 900k/100k |
| ConverterInvariantTest | 5 | escrow == active NFTs × 1M; no unbacked NFT |
| VaultV3Test | 16 | V3 vault on USDG, 0% fee, PPS from reports only |
| FeeToParbFlowTest | 6 | fee batch → USDG → deposit → pARB at current PPS, no double deposit |
| DistributionTest | 14 | daily accounting, weekly time-weighted roots, funded roots, claims |
| DistributionInvariantTest | 6 | allocated ≤ funded; no double claim |
| SplitterTest | 10 | tax, entry, royalty splitters; bounded payee changes |
| NFTTest | 10 | 6,767 cap, ERC-2981 6.7%, inventory reuse |
| RegistryTest | 7 | 8 placeholders, identity / original / preference kept separate |
| RobinhoodDeploymentTest | 8 | deployment script wiring on a 4663-shaped config |
| TokenTest | 4 | fixed supply, burnable, no mint |
| LegacyV2BugsTest | 4 | known V2 issues absent in V3 |
| ReporterCompatTest | 1 | bot NAV signature accepted by V3 |

## End-to-end simulation (`./sim/run-local.sh`): 39 / 39 checks pass

The weekly close now runs the packaged services' own code (`offchain/services/lib/jobs.mjs`): keeper passes (tax settle, splits, LP fee collection into buyback-and-burn, buyback, router ETH → USDG on an official Uniswap V3 pool with a 30-min TWAP, settle, root activation), then the epoch publisher (deterministic root after a 6 h settlement grace) and the root checker. New checks: router swaps within cap and slippage of the TWAP; router swapped + held == allocated; every proposed root recomputed identically; keeper passes had no failed step; LP fees ETH → burner and MILORD burned; position locked forever with release refused.

See [MILORD_END_TO_END.md](MILORD_END_TO_END.md) for the trace. Invariants checked:

- Supply: exact supply after burns (launch remainder, exits, buybacks); no mint after deployment; buyback burns recorded == supply reduction.
- Collateral: escrow == backed NFTs × 1,000,000; active NFTs == backed count; redemption 900,000 / 100,000;
  reissued ID came from inventory.
- pARB: distributor balance + claimed == minted from fee deposits; allocated ≤ minted; every batch minted at
  USDG × 1e18 / PPS; week-3 deposit at the post-loss PPS; same batch cannot deposit twice; second claim pays
  nothing; no performance-fee shares; deposits leave PPS unchanged; PPS up and down with reports, never reset;
  PPS × outstanding pARB == NAV after settlement; USDG 6 decimals.
- Fees: tax 1.5% of volume; commission 20%; splitter receives all project tax; equal thirds within 1 bp; entry
  70/30; royalty only when paid, 50/50; all arbitrage ETH swapped and deposited; LP fees separate.
- Eligibility: seller keeps earned pARB; buyer earns from purchase time; redeemer earns nothing after redeeming.
- Launch: 100% of supply entered the position (deployer, team, arbitrage, operator at 0; remainder ≤1e6 wei burned);
  pool started with 0 ETH and the first trade was a buy; entry refused for the first 30 minutes (no fallback);
  every entry fee == max(10% of TWAP value, 0.002 ETH) with both paths used; position still locked; second strategy
  inactive.

## Website

`scratchpad/ui/ui-test.cjs` (Playwright, Chromium) against anvil + quote service: connect, buy in the local pool,
delegate votes, wake (convert), choose characteristics, redeem: passed.

## Offchain

`node offchain/test-twap.mjs`: passed (fees at 10/100/200/500 ETH FDV, bootstrap, same-block and sustained manipulation, staleness, liquidity, range, ordering). `./sim/run-entry-price.sh`: 17 checks on real v4 swaps passed (ENTRY_FEE_PRICING.md).

## Not covered

- Real Hookr contracts (no ABI/fork access). The hook is a mock that follows the documented behaviour.
- Robinhood Chain fork (RPC blocked from this environment).
- Funding bot on 4663 (not changed yet; see ARBITRAGE_FUNDING_READINESS.md).
- Real marketplaces (royalty honouring is modelled, not tested).
- No audit.
