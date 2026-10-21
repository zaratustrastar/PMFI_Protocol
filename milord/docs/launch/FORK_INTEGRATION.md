# Robinhood mainnet-fork integration suite (2026-10-21)

Tests the MILORD launch, Anti-Snipe, the permanent lock and buybacks against the **deployed Hookr-1 contracts** on a
private local Anvil fork of Robinhood Chain (4663). No mocks of Hookr. Status: **written and compiled; not yet run
against a fork** (this development environment cannot reach the Robinhood RPC). The first real run is the VPS run below.

## One command (VPS)

```
cd /opt/milord-staging/milord && git fetch origin milord/v1-protocol && git checkout --detach origin/milord/v1-protocol && npm ci && ./fork/run-fork-tests.sh
```

What the runner does, in order:
1. Gets the CI production artifacts (`milord-production-out`) for the checked-out commit with `gh` (once per commit,
   stamped in `out/.ci-commit`) and replaces solc with `sim/no-compile-solc.sh`, so nothing compiles. No `gh`, or CI not
   finished for that commit: it stops and says so. `FORK_BUILD=lowmem` compiles the non-IR lowmem profile instead.
2. Starts its own Anvil fork on `127.0.0.1:8548` (`FORK_PORT`), refusing if the port is taken, at low CPU and IO
   priority, and stops only that process on exit. To reuse your running fork instead: `FORK_URL=http://127.0.0.1:8547`.
3. Proves the endpoint is a local Anvil fork before anything else: the URL must be `http://127.0.0.1:` or
   `http://localhost:`; `web3_clientVersion` must start with `anvil`; `anvil_nodeInfo` must report a fork block; the
   chain id must be 4663; HookrLauncher must have code. Chain id alone is never accepted.
4. Prints read-only facts from the fork: code sizes, registry admissions and caps of both Rules and both Tax +
   Conversion modules, commission floors, treasury fee terms and the launch fee.
5. Runs `forge test --match-path 'test/fork/*' --fork-url <local fork> --threads 2 -vv` and writes everything to
   `fork/logs/fork-<UTC time>.log`, with measured peak memory and wall time at the end.

Safety: no private key is read (the runner unsets the usual key variables) and nothing is signed. `forge test`
executes every transaction inside its own in-memory EVM and only reads state from the local Anvil. Nothing is sent
to Robinhood mainnet; the trading services and any other Anvil on the machine are not touched.

Knobs for the Anti-Snipe exploration: `SNIPE_PIPS` (400000), `SNIPE_SECONDS` (3600), `GUARD_BLOCKS` (300),
`LOCK_MARGIN_BLOCKS` (7200). `MATCH=<regex>` runs a subset. `FORK_BLOCK=94256940` pins the fork block.

## Expected resources (estimates until the first VPS run reports measured values)

| | Memory | CPU / time | Network |
|---|---|---|---|
| Anvil fork | 100–300 MB, grows with fetched state | low; waits on the RPC | a few thousand `eth_getStorageAt` / `eth_getCode` reads from the public RPC, cached after the first test |
| `forge test` (2 threads, prebuilt) | 200–600 MB | 1–5 min wall, dominated by RPC latency | local only |
| Build | none with CI artifacts; ~0.7 GB with `FORK_BUILD=lowmem` | none / ~1 min | `gh` download once per commit (~30 MB) |

The runner prints the measured forge and Anvil peak memory and wall time; on a 3.8 GiB VPS the whole run should stay
well under 1 GB. Smoke-tested here against stand-in local chains: the refusal paths (remote URL, plain Anvil, wrong
chain id, busy port, no Hookr code) and the clean stop of its own Anvil all work; the Robinhood run itself is not.

## What it tests

`test/fork/` (skipped in plain `forge test` and CI; 20 tests). Our real contracts are deployed on the fork: MILORDToken,
BuybackBurner, the trading-tax RevenueSplitter (thirds), V4SwapAdapter and HookrLockFeeCollector. A stand-in address
plays the Team Safe. Hookr interfaces are vendored verbatim in `test/fork/hookr/` (MIT, hookr-contracts@7e771b3).

**Priority 1, `MilordLaunchFork`** (the approved launch: no Anti-Snipe)
- `test_launch_fullSupply_zeroEth`: existing MILORD opens through `launchFamily`; the Safe pays only the launch fee in
  ETH; 100% of 6.767B goes into one MILORD-only position at tick 203280 (range 134220–203280); the integer-rounding
  remainder (< 1e6 wei) is refunded and burned, so the Safe keeps 0 MILORD.
- `test_openingFdv`: FDV from the pool price is 9.9–10.2 ETH.
- `test_frozenTerms`: Hookr froze exactly our terms: no guard, Snipe, dynamic fee, Auto Burn, LP Rewards, pot or
  royalty; commission 20%; no Hookr minimum fee; Tax + Conversion flat 1.5%/1.5% from the launch block (no decay, no
  session tiers), no conversion route, creator share to our splitter.
- `test_firstBuy_flatTaxNoSurcharge`: the first 1 ETH buy, in the launch block, pays exactly 1.5% tax + 0.3% LP fee,
  with no Hookr take beyond the tax. Logs the first-buy price move.
- `test_sell_flatTax`, `test_exactOutputBuy_flatTax`: 1.5% on sells (of the gross ETH out) and on exact-output buys.
- `test_taxPayout_recipients`: the tax queues pay 80% to our splitter and 20% to Hookr's protocol destination; the
  splitter pays 1/3 BuybackBurner, 1/3 Team Safe, 1/3 Arbitrage Funding Safe.
- `test_poolSquattingRisk`: whether an outsider can open the ETH/MILORD pool first with 0 MILORD (see changes below).

**Priority 2, `MilordAntiSnipeFork`** (exploration; not an approved launch setting)
- `test_existingTokenAntiSnipeAccepted`: native Anti-Snipe on an existing-token family.
- `test_decayFormulaAndTakes`: at t = 0, 1, 600 … 3700 s, LP fee, Hookr take, tax and pool input per 0.1 ETH buy
  against the formula below.
- `test_maxSurchargeBounds`: 40% accepted; above 59.7% refused; logs whether 59.7% fits the live admission caps.
- `test_guardEndStopsSnipe`, `test_guardRules`, `test_buybackRouteWorksInGuard`, `test_snipeRevenueDestination`,
  `test_taxDecayCappedAtTenPercent`.

**Priority 3, `MilordLockBuybackFork`**
- `test_permanentLock`: `transferFamily(lock)` + `lock(collector, 2^64-1)`; nobody can withdraw, release, redirect.
- `test_feeCollectionToBuybackBurner`: anyone triggers the collector; ETH fees reach BuybackBurner, MILORD fees burn.
- `test_buybackAndBurnThroughHookr`: BuybackBurner buys through V4SwapAdapter on the Hookr pool and burns.
- `test_optionD_delayedHandover_90_10`: the 90/10 split of post-commission Anti-Snipe revenue (below).

## Findings from the Hookr-1 source (hookr-contracts@7e771b3), to be confirmed by the run

- **Tax + Conversion encoding.** `advisoryConfigs[0] = abi.encode(Config{buy: Leg, sell: Leg})`, 320 bytes, Leg =
  (taxPips, routeId, assetRecipient, recoveryRecipient, staleRecoveryDelay). The "launch tax above the base tax" only
  exists if a 192-byte decay `Schedule` is appended; without it the tax is flat from the launch block. The tax is taken
  on the gross ETH of buys and on the gross ETH out of sells.
- **Commission.** The 20% is `RulesConfig.protocolShareBps`. On the tax it is applied by the tax queue (Hookr gets 20%
  of the 1.5%, i.e. 0.3% of volume); on the Snipe it is a quote take credited to Hookr's protocol recipient. The 0.3%
  LP fee is 100% ours. The floor is the larger of the modules' `minProtocolShareBps` and the treasury's governed
  floor; the treasury can also set a per-pool minimum fee (the suite asserts it is 0 for our pool).
- **Tax payout is pull, not push.** The tax sits as a HookrRules claim of two queue contracts (buy and sell) until
  someone calls `payCreator()` (80% to our splitter) and `sweepProtocol()` (20% to Hookr). Both are permissionless.
- **Anti-Snipe formula.** `Snipe(t) = floor + (start − floor) × (D − t) / D` for `t < D`, rounded down, where `t` is
  `block.timestamp` minus the launch timestamp and `D = snipeDecaySeconds`; then the floor (0). Charged only on buys and
  only while `block.number < guardEndBlock`. On Robinhood mainnet `block.number` is the parent-chain (L1) height,
  ~12 s per block, so a 60-minute Snipe needs a guard of at least 300 parent blocks; a shorter guard cuts the Snipe off.
  Of each Snipe pip, 80% is an LP-fee surcharge (to the family owner) and 20% Hookr's quote take. Maximum:
  base + Snipe ≤ 60% (59.7% at a 0.3% base), and the LP part and the Hookr take must fit the admission caps minus the
  tax module's reserved 10% quote take. Optional curves: linear (default), front-loaded, step (`MemberKnobs.snipeCurve`).
  During the guard: exact-output buys and outside liquidity are refused; sells pay no Snipe; the tax is unchanged.
  Tax + Conversion's own decay cannot do this: it is capped at 10%.
- **Lock.** `lock()` itself calls `acceptFamily`, after the owner's `transferFamily(familyId, lock, claims)`; accepting
  pays the previous owner every fee earned so far. `unlockBlock = type(uint64).max` can never be released. `collect`
  pays the beneficiary directly (as tokens with `claims = 0`), so HookrLockFeeCollector works unchanged.
- **Option D (exact 90/10, no new contract).** The Safe owns the family through the window (principal stays locked
  by `lockEndBlock`), then locks it; the handover pays the Safe the window's LP fees. From the window's PoolManager
  Swap events: surcharge = Σ fee × (lpFee − 3000) / lpFee on buys. The Safe sends base fee + 90% of surcharge (+ rounding)
  to BuybackBurner, 10% of surcharge to the Team Safe, and the window's MILORD fees to BuybackBurner to burn.

## Changes still required before mainnet

1. **Atomic token deployment and launch** (if `test_poolSquattingRisk` confirms the risk): anyone can open the
   ETH/MILORD pool on HookrRoot first with an ETH-only position, after which our launch reverts `PoolExists`. Deploy
   MILORD and call `launchFamily` in one Safe MultiSend transaction (CREATE2 deployment, approve, launch).
2. **Encode the launch** in `offchain/safe-launch-batch.mjs` from the fork-tested parameters (`MilordForkBase._launch`):
   it is still a blocked draft. The lock is a second Safe transaction because `familyId` uses the launcher's global
   nonce; HookrPostLaunch deploys the collector and adapter in between.
3. **Keeper:** call `payCreator()` on both tax queues before `splitter.distribute`, otherwise the 1.5% tax never
   reaches the splitter. Queue addresses come from `HookrDirectionalTax.terms(root, poolId)`.
4. **Bytecode check** of the deployed Hookr-1 contracts against the published sources (solc 0.8.37,
   `verify-onchain.sh` step 1), unless already done on the VPS.
5. **Only if Anti-Snipe is approved:** guard in parent blocks ≥ 300 (plus margin), `lockEndBlock` ≥ guard end, a
   Safe procedure and script for the Option D split, and the lock executed before `principalLockedUntil`.
6. **Independent security review** of all new contracts (unchanged, B6).
