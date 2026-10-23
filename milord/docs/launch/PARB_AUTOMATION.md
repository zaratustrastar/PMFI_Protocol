# Protocol ETH → USDG → pArbitrage V3 → weekly pARB (automated)

**Status: TESTED locally (Foundry + anvil simulation, 46/46 incl. redemptions with liquidity return and restarts). Not deployed.** Deployment wiring: `DeployRobinhood.s.sol` makes the router the arbitrage payee of all three splitters. Monitoring: `offchain/services/monitor.mjs`. No Safe signature per run; the Arbitrage Funding Safe only sets bounds, pauses and owns the router.

| # | Step | Code | Trigger | Bounds |
|---|---|---|---|---|
| 1 | Hookr tax settles to TradingTaxSplitter | Hookr | keeper / anyone | recipient frozen at launch |
| 2 | Splitters pay the arbitrage share (tax ⅓, entry 70%, royalty 50%) to **ArbitrageFundingRouter** | `RevenueSplitter.distribute` | keeper / anyone | fixed payees, timelocked changes |
| 3 | ETH → USDG | `ArbitrageFundingRouter.execute` → `UniV3SwapVenue` (Robinhood WETH/USDG V3 pool) | keeper / anyone (`openExecution`) | min 0.05 ETH, max per tx, max per day, min interval; minimum output = 30-min V3 TWAP − maxSlippage (≤ 5% hard cap); full fill required |
| 4 | USDG → vault deposit request for the distributor | router → `ParbFundingAdapter.fund` | router (only automated operator) | unique `sourceRef` per run; only the adapter can deposit |
| 5 | Vault processes the request at the reported PPS | existing reporter → `PMFIArbVaultV3.report` | reporter bot | signed report, max PPS move |
| 6 | pARB minted to the distributor | `ParbFundingAdapter.settle` | keeper / anyone | only what the vault minted |
| 7 | Weekly eligibility, Merkle tree, `proposeRoot` | `offchain/services/epoch-publisher.mjs` | publisher key | deterministic from finalized data; cannot exceed pARB held |
| 8 | Independent recomputation | `offchain/services/root-checker.mjs` | no key | critical alert → Team Safe `vetoPending` within 1 day |
| 9 | Activation and claims | `activateRoot`, `claim` | keeper / anyone | 1-day delay; pARB goes only to the leaf account |

## Price reference and manipulation protection
Uniswap v4 has **no built-in oracle**, so the ETH price comes from the Uniswap V3 WETH/USDG pool's `observe()` oracle (`UniV3TwapReference`):
- 30-minute time-weighted mean tick; moving it means holding a skewed price for 30 minutes against arbitrage;
- reverts if the pool's history is shorter than the window (no fallback price);
- reverts if spot differs from the mean by more than 100 ticks (~1%), so a same-block push is refused (`test_sameBlockManipulationRefused`);
- optional `DualReference(TWAP, ChainlinkReference)`: both must agree within a bound and the lower price is used; Chainlink staleness and incomplete rounds revert. No Chainlink feed is confirmed on 4663 yet (BLOCKERS B8).
- Even a wrong reference cannot force a bad trade below the venue's realised price: the swap reverts if output < minimum (`test_badReferenceCannotForceABadTrade`).

## Failure behaviour
| Failure | Effect | Test |
|---|---|---|
| Router paused | ETH accumulates in the router; nothing lost; funds after unpause within bounds | `test_routerStoppedRevenueAccumulatesSafely`, services test |
| Keeper down | nothing moves; one pass catches up every step; all steps are permissionless | services test "keeper down for days" |
| Reporter down | requests stay pending, no pARB minted, nothing allocated; settles after the next report | services test "reporter down" |
| Indexer down | the next run proposes one cumulative root covering missed weeks | services test "indexer down for two weeks" |
| No eligible NFTs | nothing allocated, shares roll to the next epoch | services test "zero eligible" |
| Wrong root | root checker alerts; Team Safe vetoes | services test "root checker" |

Weekly rule: epoch k covers one UTC week from Monday; its pARB is everything settled before `end + 6 h` and not yet allocated, split by NFT-seconds held, converter inventory excluded. Recipients keep claimed pARB after selling or redeeming their NFT.
