# LP custody and locking proposal (NEEDS APPROVAL, nothing locked)

The founding position holds 100% of MILORD. Whoever controls it controls the entire unsold supply and every ETH buyers
have paid in. **No permanent lock will be executed until you approve the exact parameters, position owner and fee
beneficiary.**

## What a permanent lock means in practice

| Question | Answer |
|---|---|
| Can unsold MILORD ever be recovered? | **No.** Unsold MILORD stays in the pool and can only leave by being bought. Nobody can withdraw it, including the team. |
| Can buyers' ETH be taken out? | No. It stays in the pool and pays sellers. |
| Can the liquidity be repositioned (new range, new pool, new fee)? | **No.** The range 134220–203280 and the 0.3% fee are permanent. A new pool could be opened later, but only with MILORD someone buys first; the locked position keeps trading forever. |
| What if the price goes above the range top (~10,037 ETH FDV)? | The position becomes 100% ETH and sells no more MILORD until the price comes back into range. That ETH also stays locked. |
| What if the pool or Hookr's hook breaks? | The position cannot be rescued. This is the main cost of a permanent lock. |
| How are LP fees collected? | Fees accrue in the position in ETH (buys) and MILORD (sells). `MilordLiquidityLock.collectFees(tokenId)` is permissionless and sends both to one recipient fixed at deployment. It removes no liquidity. |
| Does locking change the tax? | No. Hookr's tax is separate and goes to the tax recipient regardless. |

## Options

| Option | Who controls the position | Risk |
|---|---|---|
| A. **Unlocked, held by the allocation Safe** | Safe signers | Signers could withdraw all MILORD and ETH at any time. Buyers must trust them. |
| B. Hookr launch lock "until block N" | Safe after block N | Same as A after N; repositioning possible later. |
| C. **Permanent lock in `MilordLiquidityLock`** (or Hookr's "forever" lock) | nobody | No rug path, no recovery, no repositioning. |

## Recommendation

**C, executed in the same Safe batch as the mint**, so the position is never held unlocked:

1. Mint the MILORD-only position directly to `MilordLiquidityLock` (`owner` = the lock contract; the batch from
   `offchain/safe-launch-batch.mjs` already does this; burn the 24,920-wei remainder in the same batch).
2. Lock contract: no owner, no withdraw or decrease-liquidity path except a zero-liquidity fee collection; Foundry
   test `test_lockHasNoWithdrawPath`.
3. **Fee beneficiary: `BuybackBurner`.** ETH fees become buyback-and-burn budget, and MILORD fees are burned by the
   permissionless `burnHeldTokens()`. Nobody receives MILORD, which keeps "the team holds no MILORD" true. No code change.
   Alternatives: Team Safe (team would receive MILORD from sell fees) or TradingTaxSplitter (would pass MILORD to the
   team and arbitrage Safes).
4. If Hookr's launcher seeds the position itself, use its "forever" lock only if fee collection can be pointed at the
   same beneficiary; otherwise use path B (empty market + PositionManager) with our lock.

Simulation reference: over three simulated weeks the position earned 0.1044 ETH and 6.52M MILORD in LP fees.

## Parameters to approve

1. Lock type: permanent (recommended) or until a block.
2. Position owner: `MilordLiquidityLock` (address after deployment) or Hookr's launch lock.
3. Fee beneficiary: `BuybackBurner` (recommended), Team Safe or TradingTaxSplitter.
4. Range 134220–203280 and LP fee 0.3% (both become permanent with the lock).
