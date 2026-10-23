# Founding position lock and LP fees

**Status: approved as architecture (Bobo, 2026-10-14). Implemented and TESTED locally. NOT executed: the lock is irreversible and waits for a separate go-ahead.**

## Design
- The Hookr existing-token launch puts 100% of MILORD (6,767,000,000) in one family position held by `HookrLauncher`.
- The family owner hands it to Hookr's own **HookrFamilyLock** and locks it forever with **HookrLockFeeCollector** as beneficiary (`offchain/safe-launch-batch.mjs` → `safe-lock-DO-NOT-EXECUTE.json`).
- `HookrLockFeeCollector` (`src/launch/HookrLockFeeCollector.sol`) is the only new code: no owner, no setters, no withdraw. Anyone may call `collect(member)`: it calls `HookrFamilyLock.collect(familyId, member, 0)`, then sends all ETH to **BuybackBurner** and burns all MILORD through `BuybackBurner.burnHeldTokens()`. If Hookr pays fees as PoolManager claims, `redeemClaims` turns them into tokens first.
- Our earlier PositionManager-based `MilordLiquidityLock` is removed: Hookr positions are not PositionManager NFTs, and two lock implementations would compete. `src/mocks/MockHookrFamilyLock.sol` reproduces Hookr's lock surface over a real Uniswap v4 position for tests only.

## The brief's eight rules
| Rule | How it holds | Test |
|---|---|---|
| 1. Whole position permanently non-withdrawable | Hookr lock with `unlockBlock = 2^64−1`; `release` reverts | `test_onlyBeneficiaryCollects_noRoleWithdraws` |
| 2. No privileged role can withdraw principal | lock has no withdraw; collector has no owner; collect is fees-only | same, plus `testFuzz_collectNeverTakesPrincipal` |
| 3. ETH LP fees → buyback-and-burn | collector forwards every wei to BuybackBurner | `test_ethFeesToBurner_milordFeesBurned_exactly` |
| 4. MILORD LP fees permanently burned | `burnHeldTokens` (ERC-20 burn): supply falls by exactly the fee | same |
| 5. No interference with trading, conversion, other operations | collect between buys, sells, a conversion and a redemption | `test_collectionDoesNotInterfereWithTradingOrConversion` |
| 6. Fees collectable throughout | permissionless `collect` at any time; keeper calls it every pass | keeper tests, simulation |
| 7. No principal collected or burned as a fee | collect with no trades takes 0; fees ≤ 0.3% of volume; only the fee leaves the pool; liquidity unchanged | `test_collectWithNoTradesTakesNothing`, `test_principalStaysInPoolAfterFeeCollection`, fuzz |
| 8. Ticks 134220–203280 validated against Hookr | multiples of spacing 60, inside v4 bounds; Hookr acceptance needs a fork | HOOKR_VERIFICATION.md (B1, B4) |

## What a permanent lock means
Unsold MILORD can only leave the pool by being bought. Buyers' ETH stays in the pool and pays sellers. The range and the 0.3% fee are permanent. Above the range top (~10,037 ETH FDV) the position is all ETH and sells nothing until price returns. If Hookr's hook ever broke, the position could not be rescued: this is the accepted cost of the lock.
