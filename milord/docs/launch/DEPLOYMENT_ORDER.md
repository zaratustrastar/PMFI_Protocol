# Production deployment order, rollback and incident plan

Nothing here has been executed on a public network. Every 💸 step spends funds and every step on 4663 needs
the owner's explicit approval. Scripts refuse chain 4663 today (`DeployRobinhood.s.sol`, `HookrPostLaunch.s.sol`;
`DeployAtomicLaunch.s.sol` accepts 4663 only on a local fork with the public Anvil key). No secrets are stored in the repo; keys are supplied by the signer at run time.

## Phase A: Robinhood Chain testnet (46630) dry run

1. `forge script script/DeployRobinhood.s.sol --rpc-url $RH_TESTNET` with testnet Safes and a test 6-decimal asset.
2. Open a Hookr pool on testnet if Hookr supports it; otherwise skip to the fork test.
3. `forge script script/HookrPostLaunch.s.sol` with the Hookr pool key → V4SwapAdapter + HookrLockFeeCollector.
4. Run the quote service and the website against testnet; repeat the UI test.

## Phase B: Robinhood Chain fork test (no broadcast)

Fork 4663 at a recent block; run the launch path against the real HookrLauncher, HookrRoot, PoolManager,
PositionManager and USDG: launch, buy, sell, settle, payout to TradingTaxSplitter, buyback through the adapter,
ETH → USDG through the V3 pool, deposit, report, claim. Exit criterion: the same invariants as the local
simulation.

## Phase C: mainnet (after approval of every NEEDS APPROVAL item)

| # | Step | Script / tool | Signer |
|---|---|---|---|
| 1 | Deploy `MilordAtomicLaunch` (owner Team Safe, flat launch); record `predictedToken()` | `DeployAtomicLaunch.s.sol` (guard lifted only for this run) | deployer, no rights kept |
| 2 | Deploy the protocol against the predicted token: splitters, NFT, converter, BuybackBurner, registry, governor, V3 vault (USDG), ShareDistributor, ParbFundingAdapter, swap venue, TWAP reference, ArbitrageFundingRouter (payee of all three splitters) | `DeployRobinhood.s.sol` with `MILORD_PREDICTED_TOKEN` → `deployments/robinhood-4663.json` | deployer |
| 3 | Verify all sources | explorer | — |
| 4 | Accept ownerships | `safe-team-setup.json`, `safe-arbitrage-setup.json` | Team Safe, Arbitrage Safe |
| 5 | 💸 Launch: MILORD created and launched on Hookr in one transaction, every frozen term verified | `safe-1-launch.json` | Team Safe |
| 6 | Record the launch (read-only) | `offchain/record-launch.mjs` | — |
| 7 | Accept the family, sweep, `setInitialAdapter` (adapter from `HookrPostLaunch.s.sol`) | `safe-2-accept.json` | Team Safe |
| 8 | Permanent lock, **only after the owner's go-ahead**, before `principalLockedUntil` | `safe-3-lock-DO-NOT-EXECUTE.json` | Team Safe |
| 9 | Start services, quote server and engine | OPERATIONS_PLAN.md §5 | hot keys on their hosts |
| 10 | 💸 First conversion and exit by a team wallet | website | team wallet |
| 11 | Website on with the official pool and marketplace links | frontend | — |
| 12 | First automatic weekly cycle watched end to end; second person checks the root | MAINNET_RUNBOOK §3 | ops |

## Rollback

Contracts are immutable and the Hookr tax is frozen. "Rollback" means stopping a component, not undoing it:

| Component | Stop | Replace |
|---|---|---|
| Conversions | `setEntryPaused(true)`; exits keep working | none: the NFT accepts its converter once (`MilordsNFT.setConverter`), so the converter cannot be swapped |
| Buybacks | remove operators; burner keeps ETH | new adapter after the 2-day delay |
| Tax recipients | frozen in Hookr | splitter payees changeable after the 2-day delay |
| Vault | `setPaused` / `setShutdown`; redemptions continue | new vault + new adapter, users exit the old one |
| Distribution | `vetoPending()` on a bad root | new distributor; old claims remain claimable |
| Pool | cannot be closed; position locked | — (that is why the lock type is an approval item) |

## Incidents

| Incident | First action | Then |
|---|---|---|
| Wrong pool settings after launch | stop announcing; do not convert | owner decides: accept, or launch a new pool and link only it (old pool stays) |
| Quote signer compromise | `setQuoteSigner(old,false)`, `setEntryPaused(true)` | rotate key, review quotes issued |
| Price manipulation around conversions | quote service refuses on its own (deviation > 10%) | review TWAP parameters |
| Bad vault report | vault `setPaused(true)` | reconcile NAV, corrected report |
| Bad Merkle root | `vetoPending()` | recompute and republish |
| USDG paused/frozen | pause funding; tell holders | wait for issuer; claims of pARB still work |
| Bridge or venue failure with funds in transit | stop funding; record the in-transit amount in NAV | recover through the venue; report a loss only when realised |
| Hookr payout fails | check the splitter received claims instead of ETH | escalate to Hookr; splitter cannot redeem ERC-6909 |
