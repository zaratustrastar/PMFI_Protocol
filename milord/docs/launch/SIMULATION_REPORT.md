# Simulation and test report

Run on 2026-10-13 in a local container. Nothing broadcast to a public network.

## Foundry (`forge test`): 134 passed, 0 failed, 0 skipped

| Suite | Tests | Covers |
|---|---|---|
| HookrLaunchTest | 15 | token-only open, sell before buy gets 0, 1.5% tax both sides in ETH, 20% commission and equal thirds, LP fees separate, round-trip bounded, price impact, buyback true burn with price limit and threshold, lock has no withdraw, ETH→USDG, adapter errors, adapter set once, commission fuzz 20–50% |
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

## End-to-end simulation (`./sim/run-local.sh`): 35 / 35 checks pass

See [MILORD_END_TO_END.md](MILORD_END_TO_END.md) for the trace. Invariants checked:

- Supply: exact supply after burns; no mint after deployment; buyback burns recorded == supply reduction.
- Collateral: escrow == backed NFTs × 1,000,000; active NFTs == backed count; redemption 900,000 / 100,000;
  reissued ID came from inventory.
- pARB: distributor balance + claimed == minted from fee deposits; allocated ≤ minted; every batch minted at
  USDG × 1e18 / PPS; week-3 deposit at the post-loss PPS; same batch cannot deposit twice; second claim pays
  nothing; no performance-fee shares; deposits leave PPS unchanged; PPS up and down with reports, never reset;
  PPS × outstanding pARB == NAV after settlement; USDG 6 decimals.
- Fees: tax 1.5% of volume; commission 20%; splitter receives all project tax; equal thirds within 1 bp; entry
  70/30; royalty only when paid, 50/50; all arbitrage ETH swapped and deposited; LP fees separate.
- Eligibility: seller keeps earned pARB; buyer earns from purchase time; redeemer earns nothing after redeeming.
- Launch: token-only liquidity pays 0 ETH to an early seller; entry refused for the first 30 minutes (no
  fallback); minimum fee used only with a valid TWAP; 10% path used after the price rose; position still locked;
  second strategy inactive.

## Website

`scratchpad/ui/ui-test.cjs` (Playwright, Chromium) against anvil + quote service: connect, buy in the local pool,
delegate votes, wake (convert), choose characteristics, redeem: passed.

## Offchain

`node offchain/test-twap.mjs`: passed (window, minimum history, staleness, deviation refusal).

## Not covered

- Real Hookr contracts (no ABI/fork access). The hook is a mock that follows the documented behaviour.
- Robinhood Chain fork (RPC blocked from this environment).
- Funding bot on 4663 (not changed yet; see ARBITRAGE_FUNDING_READINESS.md).
- Real marketplaces (royalty honouring is modelled, not tested).
- No audit.
