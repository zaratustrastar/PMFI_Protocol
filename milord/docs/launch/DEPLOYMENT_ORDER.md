# Production deployment order, rollback and incident plan

Nothing here has been executed on a public network. Every 💸 step spends funds and every step on 4663 needs
the owner's explicit approval. Scripts refuse chain 4663 today (`DeployRobinhood.s.sol:26`,
`HookrPostLaunch.s.sol`). No secrets are stored in the repo; keys are supplied by the signer at run time.

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
| 1 | Deploy MILORD token (6,767,000,000 to the allocation Safe), splitters, NFT, converter, registry, governor, BuybackBurner (adapter unset) | `DeployRobinhood.s.sol` (mainnet guard lifted only for this run) | deployer EOA, no admin rights kept |
| 2 | Deploy V3 vault (USDG, 0% fee), ShareDistributor, ParbFundingAdapter; restrict depositors to the adapter | same script | deployer |
| 3 | Verify all sources; record `deployments/robinhood-mainnet.json` | explorer | — |
| 4 | Safe: `nft.setConverter`, registry 8 placeholders + pArbitrage, operators, quote signer | Safe batch | Team Safe |
| 5 | 💸 Hookr launch: existing-token launch of MILORD, one ETH pool, tax 1.5/1.5, recipients TradingTaxSplitter, commission 20%, core blocks off, decay per R-01 | Hookr UI/launcher | allocation Safe |
| 5b | (path B) 💸 Hookr launch with all 6,767,000,000 MILORD, then `transferFamily` + `HookrFamilyLock.lock(familyId, HookrLockFeeCollector, forever)`, **only after the owner's go-ahead to execute the lock** | `offchain/safe-launch-batch.mjs` → Safe Transaction Builder | allocation Safe |
| 6 | Read back the pool: hook, blocks, tax, recipients, commission, tick, liquidity, lock | script / explorer | — |
| 7 | Deploy V4SwapAdapter for the Hookr key and `burner.setInitialAdapter` | `HookrPostLaunch.s.sol` + Safe | Team Safe |
| 8 | Start the quote service on the pool TWAP (entries refused for the first 30 minutes by design) | `offchain/quote-server.mjs` | quote signer (KMS) |
| 9 | 💸 First conversion and first redemption by a team wallet; check escrow, fees, NFT, votes | website | team wallet |
| 10 | Turn on the website with the official pool link (`?hookr=`) and marketplace link (`?market=`) | frontend | — |
| 11 | First weekly close (runbook §4) with a second person recomputing the root | ops | Arbitrage Safe, ops |

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
