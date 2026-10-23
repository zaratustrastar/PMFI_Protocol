# Atomic MILORD launch (MilordAtomicLaunch)

Status (2026-10-21): implemented in `src/launch/MilordAtomicLaunch.sol`, fork tests in
`test/fork/MilordAtomicLaunchFork.t.sol` (compiled here; they run on the VPS with `./fork/run-fork-tests.sh`).
Nothing is deployed. The tokenomics are the approved ones and are constants in the contract.

## Why
The VPS fork run confirmed B13: once MILORD exists, anyone can open the ETH/MILORD Hookr pool first, and our launch
then reverts. The fix is that MILORD does not exist until the transaction that launches it.

## What one Safe transaction does
`MilordAtomicLaunch.launch{value: Hookr launch fee}(tradingTaxSplitter, deadline)`, callable once, by the Team Safe only:

1. Checks: caller is the Safe, not launched yet, tax recipient is a contract, Hookr's current launch fee is at most
   `maxLaunchFee` (fixed at construction), `msg.value` equals the fee exactly.
2. Creates MILORD with CREATE2 (`predictedToken()`); all 6,767,000,000 MILORD are minted to the helper.
3. `HookrLauncher.launchFamily`: one member, native ETH quote, ticks 134220–203280, opening price at 203280
   (about 10.06 ETH FDV), amount0Max = 0 (no ETH), 0.3% base LP fee, Rules `protocolShareBps` 2000, Tax + Conversion
   1.5% buy and 1.5% sell to the trading-tax splitter (no route, no decay), principal locked for `lockBlocks` parent
   blocks after the launch (or after the guard). Anti-Snipe is off unless the three knobs are set (candidate only).
4. Burns the rounding dust (at most 1e6 wei of MILORD).
5. Verifies, and reverts the whole transaction on any mismatch: pool key and position (2), opening price (3), the
   PoolManager holds the entire supply and the helper none (4), every frozen Rules field (5), no Hookr minimum fee (6),
   tax terms and both queues (7, 8), `principalLockedUntil` (9), family owner (10).
6. Offers the family to the Safe (`transferFamily(familyId, safe, 0)`), emits `Launched`.

The helper keeps no rights: no withdraw, no collect, no admin. `sweep()` (anyone) forwards any ETH or MILORD it holds to
the Safe; that is how fees earned between the launch and the Safe's `acceptFamily` reach the Safe.

## Order of operations
| Step | Who | What |
|---|---|---|
| 1 | deployer key (gas only) | deploy `MilordAtomicLaunch(config)`; owner = Team Safe |
| 2 | deployer key | deploy the protocol with `MILORD_PREDICTED_TOKEN = predictedToken()` (MilordDeployer: the converter, BuybackBurner and NFT only store the address) |
| 3 | Team Safe | `deploy/out/safe-1-launch.json`: `launch{value: fee}` |
| 4 | Team Safe | `safe-2-accept.json`: `acceptFamily`, `sweep`, `BuybackBurner.setInitialAdapter` |
| 5 | Team Safe, after explicit approval | `safe-3-lock-DO-NOT-EXECUTE.json`: permanent lock (LOCK_PROCEDURE.md) |

`node offchain/safe-launch-batch.mjs <config>` encodes steps 3–5 (never signs or sends).

## Pre-emption and front-running
| Attack | Result | Evidence |
|---|---|---|
| Open the Hookr pool for MILORD before our launch | impossible: `HookrLauncher` refuses a token without code (`InvalidFunding`, HookrLauncher.sol:345), and HookrLane refuses it again at pool initialization (`_holdsOwnCode`); MILORD has no code until step 3 | `test_preemption_predictedAddressRefused`, with the positive control `test_preemption_control_existingTokenIsSquattable` (same attack parameters succeed on a deployed token) |
| Initialize the pool without the launcher (`HookrRoot.initializePool` or `PoolManager.initialize` with the root hook) | refused: the root accepts only admitted launchers, and its `beforeInitialize` accepts only its own binding | `test_preemption_directPathsRefused` |
| Code at the predicted address before our launch | cannot happen (only the helper can CREATE2 there); if it did, the pool could be squatted and our launch would revert as a whole, spending nothing | `test_preemption_ifCodeExistedLaunchWouldFailClosed` |
| See the predicted address (public helper, or the launch tx in a mempool) and open other pools for it | possible (a hookless v4 pool accepts a token without code) but harmless: it is not the Hookr pool, and our launch still opens the exact Hookr key | `test_preemption_otherPoolsDoNotBlockLaunch` |
| Deploy something else at the predicted address | impossible: the CREATE2 address is derived from the helper, which only creates MILORD | CREATE2 |
| Call `launch` first | `NotOwner` | `test_onlySafeOnce` |
| Hookr raises its launch fee before the Safe's transaction lands | `LaunchFeeTooHigh` (cap fixed at construction); exact `msg.value` means no overpayment | `test_refusesBadInputs` |
| Hookr changes an admission, caps or terms | the Hookr call or the helper's verification reverts; nothing is created | `test_verificationFailureRevertsEverything` |
| Hookr's registry guardian calls `pauseNewMarkets` (no timelock) | our launch reverts, nothing is created or lost; retry after unpause. An open pool is unaffected | HOOKR_BYTECODE_VERIFICATION.md |
| Grab the family between launch and acceptance | impossible: only the Safe is the pending owner; the helper has no function that changes it | `test_atomicFlatLaunch` |
| Buy in the next transaction after the launch | allowed by design. With the flat launch the first buyer pays the opening price + 1.5%; this is what the Anti-Snipe candidate addresses | `test_atomicAntiSnipeCandidate` |
| A second, different MILORD pool later (other fee or tick spacing, or a plain v4 pool) | possible once MILORD trades; it cannot touch our position. Our price readers use the verified pool key only (`offchain/lib/pool-price.mjs verifyPoolKey`) | operational |

Robinhood Chain's sequencer orders transactions first come, first served without a public mempool; the design does
not rely on that.

## 2026-10-21 VPS failure of the first pre-emption test
`test_cannotPreemptBeforeLaunch` failed with "next call did not revert as expected". Cause: a test bug, not Hookr.
The call was `vm.prank(squatter); vm.expectRevert(...); LAUNCHER.launchFamily{value: 0.1 ether + helper.launchFee()}(p)`.
Solidity evaluates `helper.launchFee()` (an external view call) before `launchFamily`, so the prank and the expected
revert were both consumed by that successful view call, and the launch attempt was never checked (it also ran from the
test contract, not the squatter). Hookr source refuses a codeless token twice (above). The replacement tests compute the
fee first and capture the real result with try/catch; the old test was removed, not loosened.

## Configuration note
`tax` must be HookrDirectionalTax A (0xE73f…1e1F). Candidate B (0x9901…5d3c) is HookrPartnerTax, which binds only pools
launched through HookrAttributionLauncher and has no tax queues.

## Not covered
- The helper's production bytecode is 18.6 KB (via-IR). The unoptimized lowmem build is 34 KB; the lowmem profile
  disables the size limit for tests only.
- A launch-and-lock-in-one-transaction variant is possible (the helper could hand the family to HookrFamilyLock
  itself). It is not implemented because executing the lock needs its own approval.
