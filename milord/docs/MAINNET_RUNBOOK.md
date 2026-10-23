# Mainnet deployment runbook

**Do not execute without written approval.** Contracts are unaudited. This runbook spends no funds by itself;
every step that does is marked 💸 and needs explicit sign-off. Secrets are never stored in the repo: use a
hardware wallet or Safe for every owner action and pass deployer keys via `--ledger`/`--account`, never env files in git.

## 0. Prerequisites

All of `docs/launch/OPERATIONS_PLAN.md` §7 (go/no-go) and `docs/launch/READINESS_MATRIX.md` "Blocked by" cleared,
including: external audit; fork suite and Hookr bytecode verification green at the release SHA; USDG and the
WETH/USDG pool checked (`node offchain/check-funding-pool.mjs`); venue routes confirmed with approved small amounts;
Safes deployed with confirmed signers; hosts provisioned; testnet week completed.

## 1. Launch and protocol deployment (Robinhood Chain 4663) — every step needs written approval

1. Deployer (hardware wallet): `script/DeployAtomicLaunch.s.sol` with `MILORD_TEAM_SAFE` → `MilordAtomicLaunch`
   (flat launch; Anti-Snipe refused). Record `predictedToken()`. Today the script runs only on a local fork; lifting
   its guard for mainnet is part of the approval.
2. Deployer: `script/DeployRobinhood.s.sol` with `MILORD_PREDICTED_TOKEN=<predicted>` (guard lifted only for this
   run) → splitters, NFT, converter, BuybackBurner, registry, governor, V3 vault (USDG), ShareDistributor (publisher
   key ≠ keeper), ParbFundingAdapter, UniV3SwapVenue, UniV3TwapReference, **ArbitrageFundingRouter as arbitrage payee
   of all three splitters** (the script asserts it). Output: `deployments/robinhood-4663.json`.
3. Verify every source on the explorer.
4. Team Safe and Arbitrage Safe: `safe-team-setup.json`, `safe-arbitrage-setup.json` (accept ownership).
5. 💸 Team Safe: `safe-1-launch.json` → `MilordAtomicLaunch.launch{value: Hookr fee}` creates MILORD and the pool in
   one transaction and reverts on any mismatch (ATOMIC_LAUNCH.md).
6. Read-only: `node offchain/record-launch.mjs deployments/robinhood-4663.json <helper> <StateView>`; regenerate the
   Safe batches with the familyId; Team Safe `safe-2-accept.json` (accept family, sweep, `setInitialAdapter`).
7. Collect the lock signatures at once; execute `safe-3-lock-DO-NOT-EXECUTE.json` only with Bobo's go-ahead and
   before `principalLockedUntil` (LOCK_PROCEDURE.md; `keeper:lock-watch` alerts).

## 2. Services and engine

1. milord-auto: keeper + epoch publisher + quote server; milord-watch: root checker + monitor; milord-engine:
   robinhood-mode engine + bridge status (OPERATIONS_PLAN.md §1, §4, §5).
2. Check: all `/healthz` 200, one test alert delivered, `monitor` shows no finding.
3. 💸 First conversion and first exit by a team wallet; check escrow, fees, NFT, votes.

## 3. Weekly loop (automatic; people only watch and veto)

1. Keeper, every 5 min: Hookr tax queues `payCreator` → splitters `distribute` → LP fee collection → bounded
   buyback-and-burn → `router.execute` (ETH → USDG within caps and the TWAP bound → vault deposit request) →
   `adapter.settle` after a report → `activateRoot` after the delay.
2. Engine: trades with deployable capital; signs NAV reports (gross USDG: vault + servicer + venues + positions at
   cost + bridges and recalls in transit, each once); returns liquidity when redemptions exceed the idle buffer.
3. Publisher after Monday 00:00 UTC + grace: deterministic root, `proposeRoot`; root checker recomputes from its own
   RPC; Team Safe vetoes a mismatch within the 1-day delay.
4. Monitor alerts on anything stuck (services README, "Monitor alerts").

## 4. Weekly human checks
- Publish `epoch-N.json` to IPFS; a second person reads the root checker's result.
- Review the monitor's resolved alerts, gas balances and the PPS series.

## 5. Emergency

| Situation | Action |
|---|---|
| Bad quote signer | `setQuoteSigner(addr, false)`; `setEntryPaused(true)` if needed. Exits keep working. |
| Fee too high | `reduceFeeParams` (immediate) |
| Bad report | vault `setPaused(true)`; investigate; reports moving PPS > 10% already revert |
| Bad root | `vetoPending()` during the delay |
| USDG paused or vault frozen by the issuer | claims of pARB keep working; deposits, `tend` and redemptions wait. Pause entry funding; tell holders. |
| Vault wind-down | `setShutdown(true)`; keep reporting so redemptions process; depositors can cancel pending deposits |

## 6. Rollback

Contracts are not upgradeable. A replacement is a new deployment plus migration by users (exits always work).
