# Temporary and permanent lock procedure

Approved architecture (2026-10-14): permanent HookrFamilyLock with HookrLockFeeCollector as beneficiary. **Executing the
lock still needs Bobo's explicit go-ahead.** Nothing here has been executed. All block numbers below are Robinhood
Chain's `block.number`, which is the parent-chain (Ethereum) height, about 12 s a block (CLOCK_AND_ARBSYS.md), not
`eth_blockNumber`.

## Who can withdraw principal, and when
| Period | Family owner | Principal withdrawable? |
|---|---|---|
| launch tx → Safe's `acceptFamily` | MilordAtomicLaunch | **No, never**: the helper has no withdraw path, at any block |
| `acceptFamily` → `principalLockedUntil` | Team Safe | No: `withdraw` reverts `PrincipalLocked` (Hookr) |
| `principalLockedUntil` → lock | Team Safe | **Yes, by the Safe's signer threshold.** This is the only exposure |
| after `lock(collector, 2^64-1)` | HookrFamilyLock | No, forever: `release` reverts `StillLocked`; the collector has no release path |

`principalLockedUntil = launch block + guardBlocks + lockBlocks` (fork-tested to the block in
`test_withdrawableExactlyAtLockEnd`: block `until-1` reverts, block `until` succeeds). `lockBlocks` is a constructor
parameter of MilordAtomicLaunch (at most 100,000, Hookr's own bound; with the guard the total stays within it).
Fees stay collectable by the owner throughout and never touch principal.

## Flat launch (approved terms)
1. Launch (ATOMIC_LAUNCH.md). Proposed `lockBlocks` = 7,200 (about 24 h).
2. Same session: Safe batch `acceptFamily` + `sweep` + `setInitialAdapter`.
3. As soon as the lock is approved, and well before `principalLockedUntil`: Safe batch
   `transferFamily(familyId, HookrFamilyLock, 0)` + `HookrFamilyLock.lock(familyId, HookrLockFeeCollector, 2^64-1)`.
   `lock` performs the acceptance itself and pays the Safe the fees earned so far (forward them to BuybackBurner).
4. From then on anyone calls `HookrLockFeeCollector.collect(0)`: ETH fees to BuybackBurner, MILORD fees burned.

If the go-ahead is already given before the launch, steps 2 and 3 can be one Safe batch right after the launch.

## Option D (Anti-Snipe candidate, not a launch decision)
1. Launch with guard = 300 parent blocks, Snipe 40% → 0 over 3,600 s; `principalLockedUntil` = guard end + `lockBlocks`.
2. Right after the launch: `acceptFamily` + `sweep` (fees before acceptance reach the Safe via the helper).
3. After the guard ends (`block.number >= guardEndBlock`): the lock batch as above.
4. Then `node offchain/option-d-reconcile.mjs <config>`: reads PoolManager `Swap` and HookrLauncher `LPFeesCollected`
   events up to the handover, refuses unless the handover is final and the helper is empty, and writes the Safe split
   batch (team = 10% of the post-commission Snipe LP part, the rest and the MILORD fees to BuybackBurner) plus a report.
   Nothing is executed by the script. `test_optionD_lpFeesCollectedEventsMatchPayouts` checks on the fork that the
   events equal what the helper and the Safe received and equal the Swap-event estimate.

Locking before the guard ends is safe, only less precise: Snipe earned after it goes through the collector (100% burn).

## Missed-handover contingency
The risk: the lock batch is not executed by `principalLockedUntil`, so the Safe's signers could withdraw principal.
1. **Pre-sign.** Once the familyId is known (the `Launched` event), collect the signatures for the lock batch at once.
   A fully signed Safe transaction can be executed by anyone, so the operator (or a second operator) can submit it
   without waiting for signers.
2. **Alert.** The keeper's read-only `lock-watch` step (needs `HookrLauncher`, `HookrFamilyLock`, `MilordFamilyId` in the
   deployments file) reads `familyOwner` and `principalLockedUntil` against the parent block: warning at 1,800 blocks
   left (~6 h), critical at 300 (~1 h), critical "WITHDRAWABLE" on every pass after. Metric `lock_blocks_left`.
3. **Late lock still works.** `lock` has no deadline: the fork test locks 50,000 blocks after the temporary lock ended,
   and afterwards nothing can be withdrawn. A missed deadline opens a window of signer trust; it does not lose the option.
4. **Generous margin.** 7,200 blocks is the proposal; raise it (up to Hookr's 100,000 including the guard) if signer
   availability is uncertain. A longer temporary lock has no economic cost.
5. **Never** sign `withdraw` from the Safe. A Safe transaction calling `HookrLauncher.withdraw` for the MILORD family is
   a red flag for every signer.
