# Arbitrage Funding Safe → pArbitrage V3 → weekly pARB: what runs on its own today

Read from the code on 2026-10-14. Line references are to `milord/`. Nothing deployed, no funds moved.

## The flow as implemented

| # | Step | Contract / code | Who triggers it | Bounded how |
|---|---|---|---|---|
| 1 | Hookr tax settles and pays the TradingTaxSplitter | Hookr (external) | anyone (Hookr docs) | recipient frozen at launch |
| 2 | Splitters push ETH: tax ⅓, entry fees 70%, royalties 50% to the Arbitrage Funding Safe | `RevenueSplitter.distribute` `src/robinhood/RevenueSplitter.sol:92` | **anyone** | fixed payees and shares; payee changes delayed 2 days |
| 3 | ETH → USDG with slippage protection | Arbitrage Funding Safe | **Safe signers (manual)** | — |
| 4 | USDG → vault deposit request, receiver = distributor | `ParbFundingAdapter.fund` `src/parb/ParbFundingAdapter.sol:81` | **operator key** | can only deposit USDG the adapter holds, only into the vault, only for the distributor; each `sourceRef` once |
| 5 | Vault prices the request at the reported PPS | `PMFIArbVaultV3.report` `src/parb/PMFIArbVaultV3.sol:378` | existing reporter bot (`bot/arb_monitor/core/arb_reporter.py`) | EIP-712 report signer; >10% PPS move reverts; vault accepts deposits only from the adapter |
| 6 | Real pARB minted to the distributor, amount recorded | `ParbFundingAdapter.settle` `:98` | **anyone** | shares are whatever the vault mints; no other mint path |
| 7 | Weekly eligibility and Merkle root | `offchain/lib/eligibility.mjs` (used by the simulation; **no production job script yet**) | publisher key | see Q3, Q4 |
| 8 | Root proposed, activates after delay | `ShareDistributor.proposeRoot` `src/parb/ShareDistributor.sol:67` | publisher key | must be funded; 1-day delay; Team Safe can veto (`:80`) |
| 9 | Claims | `ShareDistributor.claim` `:100` | holder, or anyone on the holder's behalf | pARB only goes to the account in the leaf |

After claiming, pARB is an ordinary ERC-20 in the holder's wallet and stays there after the NFT is sold or redeemed
(simulation checks "seller keeps week-1 pARB", "redeemer keeps earned pARB"). No USDC/USDG profit is paid out
separately, and the vault charges 0% performance fee (check "no performance-fee shares").

## 1. What is automatic and what needs a person or a keeper

- **Fully permissionless (any keeper can run them):** Hookr settle/payout, splitter `distribute`, adapter `settle`,
  vault `tend`, root `activateRoot`, `claim` on behalf of holders, `BuybackBurner.burnHeldTokens`, lock `collectFees`.
- **Bot keys with narrow roles:** adapter `fund` (operator), `proposeRoot` (publisher), vault `report` (report signer),
  `buybackAndBurn` (burner operator, per-call min/max).
- **Still a human:** step 3. The ETH sits in a Safe, and swapping it needs Safe signatures every week. This is the only
  manual step in the weekly loop.
- **Missing software:** a production epoch job (reads finalized Transfer events, computes weights, builds the tree,
  publishes data, calls `proposeRoot`) and a keeper loop that calls the permissionless steps. The logic exists and
  is tested; it runs inside `sim/simulate.mjs` today, not as a service.

## 2. Can it run without giving a bot control of the Safe?

Yes, with one small contract. **Recommended (smallest change): `ArbitrageFundingRouter`** (~120 lines, not built yet):

- Set as the arbitrage payee in the three splitters at deployment (instead of the Safe). Owner = Arbitrage Funding Safe.
- `swapAndFund(ethIn)` callable by a keeper: wraps ETH, swaps on the Robinhood WETH/USDG Uniswap V3 pool, computes
  `minOut` **itself** from the pool's 30-minute TWAP minus a fixed max slippage (for example 1%), sends USDG to
  `ParbFundingAdapter` and calls `fund` with a unique `sourceRef`. The keeper chooses only when and how much.
- Bounds: max ETH per call and per day, minimum interval, the vault's $10 minimum, deadline.
- Safe-only: `pause`, change caps (with a delay), and `sweep` ETH/USDG **only to the Arbitrage Funding Safe**.
- The router becomes the adapter's operator; the separate operator key is no longer needed.

If you want fee ETH to keep landing in the Safe first, the alternative is a Safe module (Safe Allowance Module or
Zodiac Roles) that lets a keeper send at most X ETH per day from the Safe to the router. It works but adds module
configuration to review; the payee change is simpler.

Nothing else needs a change: steps 2, 6, 8 and 9 already run without the Safe, and the Safe only acts to pause, veto or
change parameters.

## 3. How ownership records are verified and roots published

- The job replays the Milords NFT `Transfer` events from **finalized** blocks only into ownership intervals
  (`eligibility.mjs:15`), and scores NFT-seconds per owner per UTC day for the week (`:36`). Converter inventory and the
  zero address earn nothing, so redeemed NFTs stop earning immediately; each NFT counts equally.
- Weekly pARB = everything unallocated in the distributor (new batches plus carried dust), split by NFT-seconds
  (`:63`), added to cumulative totals (`:78`), and committed as an OpenZeppelin Merkle tree of
  `(address, cumulative)` (`:85`).
- The job publishes the full data (daily breakdown, cumulative table, tree) to IPFS and passes its hash as `dataHash`
  to `proposeRoot`. During the 1-day activation delay anyone can recompute the root from public chain data; the Team
  Safe can `vetoPending`. Recommended: run a second, independent watcher that recomputes each root and alerts the Safe
  signers on any mismatch.

## 4. How over-distribution is prevented

On-chain, not by trust in the job: `proposeRoot` reverts with `Underfunded` unless the root's total cumulative
allocation ≤ pARB the distributor holds + pARB already claimed (`ShareDistributor.sol:72-73`). Cumulative totals can
only grow, and `claim` pays `cumulative − already claimed`, so the same pARB cannot be claimed twice. The distributor's
pARB comes only from `settle`, i.e. shares the vault actually minted; `rescue` cannot move pARB (`:139`). Simulation
checks: allocated ≤ minted, distributor balance + claimed == minted, a second claim pays nothing.

## 5. Zero eligible NFTs in a period

`allocate` returns everything as carried dust when total weight is 0 (`eligibility.mjs:67`). The pARB stays in the
distributor as unallocated and is allocated in the next week that has eligible holders. No root is needed for that
week (the previous root stays active); nothing is lost or burned. The vault deposit itself is unaffected.

## Smallest set of changes to make it fully operational

1. `ArbitrageFundingRouter` + tests; arbitrage payee in the three splitters = router (deployment config).
2. `offchain/epoch-job.mjs`: the production wrapper around `eligibility.mjs` (finalized-block reader, IPFS publish,
   `proposeRoot`), plus the independent watcher.
3. `offchain/keeper.mjs`: Hookr settle/payout, `distribute` ×3, router `swapAndFund`, adapter `settle`, `activateRoot`,
   optional claim relaying, `burnHeldTokens`, `collectFees`.
4. Keys: keeper (no Safe rights), publisher, report signer: separate keys in a KMS. Pause controls: vault `setPaused`,
   router `pause`, converter `setEntryPaused`, distributor `vetoPending`, burner operator removal.

No change to pArbitrage V3, ParbFundingAdapter, ShareDistributor, the splitters or the converter.
