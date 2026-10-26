# Mainnet deployment runbook

**Do not execute without written approval.** Contracts are unaudited. This runbook spends no funds by itself;
every step that does is marked 💸 and needs explicit sign-off. Secrets are never stored in the repo: use a
hardware wallet or Safe for every owner action and pass deployer keys via `--ledger`/`--account`, never env files in git.

## 0. Prerequisites (gate G0: owner go/no-go)

All of `docs/launch/OPERATIONS_PLAN.md` §7 (go/no-go) and `docs/launch/READINESS_MATRIX.md` "Blocked by" cleared,
including: external audit; fork suite, Hookr bytecode verification and **the integrated rehearsal
(`./fork/rehearse-full-launch.sh`) green at the release SHA**; USDG and the WETH/USDG pool checked
(`node offchain/check-funding-pool.mjs`); venue routes confirmed with approved small amounts
(`docs/launch/B9_VENUE_ROUTES.md`); Safes deployed with confirmed signers; hosts provisioned; testnet week completed.

## 1. How a mainnet script is approved (configuration hash)

`DeployAtomicLaunch`, `DeployRobinhood` and `HookrPostLaunch` refuse chain 4663 unless one of these holds:

- **Private fork**: `MILORD_LOCAL_FORK=yes` and the broadcaster is the public Anvil account #0 (a key that can never
  hold mainnet funds). This is what `fork/rehearse-full-launch.sh` uses.
- **Mainnet**: `MILORD_MAINNET_APPROVAL` equals the configuration hash of exactly this run, and the broadcaster is
  not the Anvil key. `DeployRobinhood` also requires deployed Safes, USDG as the vault asset and a predicted MILORD
  address with no code; `HookrPostLaunch` requires `MILORD_POOL_ID` from `record-launch.mjs`.

The hash binds every address and parameter of the run, so an approval cannot be reused for another configuration:

1. Operator: dry run **without `--broadcast`** with the final environment:
   `forge script script/<Script>.s.sol --rpc-url $ROBINHOOD_RPC --sender <deployer>` → prints
   `... configuration hash: 0x…` and stops at the approval check.
2. Operator sends the hash, the environment (no keys) and the commit SHA to the owner.
3. Owner approves that hash in writing.
4. Operator re-runs with `MILORD_MAINNET_APPROVAL=<hash> ... --broadcast --ledger` (hardware wallet). Any change to
   the environment changes the hash and the script refuses again.

## 2. Launch and protocol deployment (Robinhood Chain 4663): one owner gate per step

| Gate | Step | Who | Approval |
|---|---|---|---|
| G1 | `script/DeployAtomicLaunch.s.sol` with `MILORD_TEAM_SAFE` → `MilordAtomicLaunch` (flat launch; Anti-Snipe refused). Record `predictedToken()`. | deployer (hardware wallet) | owner approves its configuration hash |
| G2 | `script/DeployRobinhood.s.sol` with `MILORD_PREDICTED_TOKEN=<predicted>` → splitters, NFT, converter, BuybackBurner, registry, governor, V3 vault (USDG), ShareDistributor (publisher ≠ keeper), ParbFundingAdapter, UniV3SwapVenue, UniV3TwapReference, ArbitrageFundingRouter as arbitrage payee of all three splitters. Output `deployments/robinhood-4663.json`. | deployer | owner approves its configuration hash |
| — | Verify every source on the explorer; `node offchain/safe-launch-batch.mjs <config>` | operator | read-only |
| G3 | `safe-team-setup.json`, `safe-arbitrage-setup.json` (accept ownership) | Team Safe, Arbitrage Safe | Safe signers, after the owner confirms G2's addresses |
| G4 💸 | `safe-1-launch.json` → `MilordAtomicLaunch.launch{value: Hookr fee}`: MILORD and the pool in one transaction, reverting on any mismatch (ATOMIC_LAUNCH.md). Re-read `launchFee()` right before signing. | Team Safe | **owner's explicit launch go-ahead** |
| — | `node offchain/record-launch.mjs deployments/robinhood-4663.json <helper> <StateView>` must exit 0 | operator | read-only |
| G5 | `script/HookrPostLaunch.s.sol` with the recorded pool key, `MILORD_POOL_ID`, familyId, BuybackBurner, HookrFamilyLock → V4SwapAdapter, HookrLockFeeCollector | deployer | owner approves its configuration hash |
| G6 | Regenerate the batches with familyId, adapter and collector; `safe-2-accept.json` (accept family, sweep, `setInitialAdapter`) | Team Safe | Safe signers |
| G7 ⚠️ irreversible | `safe-3-lock-DO-NOT-EXECUTE.json` before `principalLockedUntil` (LOCK_PROCEDURE.md; `keeper:lock-watch` alerts) | Team Safe | **owner's separate written go-ahead for the permanent lock** |
| G8 | Services on (section 3) with `ALLOW_MAINNET=yes` | operator | owner approval of the host and key set |
| G9 💸 | First conversion and first exit by a team wallet; first venue funding with a small amount | team | owner approval of the amounts |

The private-fork rehearsal executes G1–G7 in this order with stand-in Safes; a red rehearsal at the release SHA
blocks every gate.

## 3. Services and engine

1. milord-auto: keeper + epoch publisher + quote server; milord-watch: root checker + monitor; milord-engine:
   robinhood-mode engine + bridge status (OPERATIONS_PLAN.md §1, §4, §5).
2. Check: all `/healthz` 200, one test alert delivered, `monitor` shows no finding.
3. 💸 First conversion and first exit by a team wallet; check escrow, fees, NFT, votes.

## 4. Daily loop (automatic; people only watch and veto)

1. Keeper, every 5 min: Hookr tax queues `payCreator` → splitters `distribute` → LP fee collection → bounded
   buyback-and-burn → `router.execute` (ETH → USDG within caps and the TWAP bound → vault deposit request) →
   `adapter.settle` after a report → `activateRoot` after the delay.
2. Engine: trades with deployable capital; signs NAV reports (gross USDG: vault + servicer + venues + positions at
   cost + bridges and recalls in transit, each once); returns liquidity when redemptions exceed the idle buffer.
3. Publisher after each 00:00 UTC + 6 h grace: deterministic cumulative root through the last closed day,
   `proposeRoot` (it waits while the previous root is still in its 1-day review); root checker recomputes from its own
   RPC; Team Safe vetoes a mismatch within the delay. After a veto the publisher halts until the vetoed root is
   listed in `PUBLISHER_ACK_VETOES`.
4. Monitor alerts on anything stuck (services README, "Monitor alerts").

## 5. Human checks
- Daily: publish `epochs/<k>.json` to IPFS; a second person reads the root checker's result before activation.
- Weekly: review the monitor's resolved alerts, gas balances and the PPS series.

## 6. Emergency

| Situation | Action |
|---|---|
| Bad quote signer | `setQuoteSigner(addr, false)`; `setEntryPaused(true)` if needed. Exits keep working. |
| Fee too high | `reduceFeeParams` (immediate) |
| Bad report | vault `setPaused(true)`; investigate; reports moving PPS > 10% already revert |
| Bad root | `vetoPending()` during the delay |
| USDG paused or vault frozen by the issuer | claims of pARB keep working; deposits, `tend` and redemptions wait. Pause entry funding; tell holders. |
| Vault wind-down | `setShutdown(true)`; keep reporting so redemptions process; depositors can cancel pending deposits |

## 7. Rollback

Contracts are not upgradeable. A replacement is a new deployment plus migration by users (exits always work).
