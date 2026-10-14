# NFT entry-fee pricing

Status: implemented and tested locally. Not audited. Nothing deployed or broadcast.

## Formula (unchanged)

`entry fee = max(0.002 ETH, 10% of the ETH value of 1,000,000 MILORD)`, enforced on-chain by
`MilordConverter.entryFeeFor`. At an FDV of F ETH, 1,000,000 MILORD is worth F / 6,767 ETH.

| FDV | value of 1M MILORD | 10% | fee charged |
|---|---|---|---|
| 10 ETH (launch) | 0.001478 ETH | 0.000148 | **0.002** (minimum) |
| 100 ETH | 0.014778 | 0.001478 | **0.002** (minimum) |
| 135.34 ETH | 0.02 | 0.002 | crossover |
| 200 ETH | 0.029555 | 0.0029555 | **≈0.002955** |
| 500 ETH | 0.073888 | 0.0073888 | **≈0.007388** |

## Recommendation: keep the existing design, hardened

No contract change, no observer contract, no keeper, no second pool and no change to Hookr's hook.

The converter already takes the price as an EIP-712 quote `{account, weiPerMillion, issuedAt, expiresAt, nonce}`
signed by an allowlisted quote signer (lifetime ≤ 5 min, single-use nonce, bound to the caller, `maxFee` slippage
cap). The quote service computes `weiPerMillion` from the Hookr MILORD/ETH v4 pool:

1. **Complete price history, not snapshots.** v4 changes a pool's price only in swaps, and every swap emits
   `PoolManager.Swap(..., tick, ...)`. The `Initialize` event plus all `Swap` logs give the exact tick at every
   second, so the 30-minute time-weighted mean tick equals what an on-chain V3-style accumulator would compute.
   Anyone can recompute it from public logs. `getSlot0` is only used to mark "synced up to this block".
2. **Quote = max(TWAP, spot).** Pushing the price down to pay less cannot undercut the TWAP; pushing it up only
   makes entry dearer for everyone (griefing paid in 3% round-trip tax).
3. **Refusals (entry waits, no fallback price):**
   - indexer not synced within 120 s;
   - spot more than 10% from the TWAP;
   - spot outside the founding position's range `[134220, 203280]` (no liquidity there, price moves for free);
     ticks outside the range are also clamped in the mean;
   - in-range liquidity below 1e23 (the founding position has ~2.69e23);
   - less than 30 minutes of history, outside the bootstrap below.
4. **Pool key checks** before any price is read: currency0 must be native ETH (address 0), currency1 must be
   MILORD and have 18 decimals. So `1.0001^tick` = MILORD per ETH and `weiPerMillion = 1e24 / 1.0001^tick`.
5. **Bounded bootstrap.** During the first 30 minutes after `Initialize` only, and only if the pool opened at the
   approved launch tick 203,280, the quote is the **highest** MILORD price seen since open (spot included). It can
   only over-charge, and it ends by itself once the window has full history. Because the fee is the 0.002 ETH
   minimum until ~135 ETH FDV, launch entries pay 0.002 ETH unless the price rises 13x in the first 30 minutes.
   At the exact opening tick (= the range's upper edge) the position is not yet active, so there is no quote
   until the first buy; nobody can hold MILORD before then anyway.

After bootstrap, any stale or unreliable price blocks new conversions. **Redemption never reads a quote or a
price** (`MilordConverter.redeem`), so it works even when the service is down, the signer is revoked or entry is paused.

## Why manipulation between updates does not work

There are no "updates" to sandwich: every price change is a logged swap and counts for exactly as long as it lasted.

- **Same block:** all swaps in a block share its timestamp; only the block's last price persists. A push and
  restore inside one block has weight 0 (tested: a 10x push and restore leaves the quote bit-identical).
- **Push left at the end of a block:** spot is then >10% from the TWAP and the quote is refused.
- **Sustained push:** a price held for k seconds moves the mean by k/1800 of the push. Holding the price 5% low
  for 5 minutes cut the fee by 0.85% in the test (≈0.00006 ETH at 500 ETH FDV) while the attacker paid 1.5% tax
  on each leg, LP fees and arbitrage on the volume moved. Pushes beyond 10% are refused outright.

## What is reused

| Piece | File | Change |
|---|---|---|
| Converter, fee formula, quote verification, redemption | `src/robinhood/MilordConverter.sol` | none |
| Quote signing | `offchain/lib/quote.mjs` | none |
| TWAP maths | `offchain/lib/twap.mjs` | hardened: max(TWAP, spot), range clamp/refusal, liquidity, bootstrap |
| Pool history reader | `offchain/lib/pool-price.mjs` | key/decimals check, records in-range liquidity |
| One config for all services | `offchain/lib/entry-price.mjs` | new (30 min, 10%, 120 s, ticks, 1e23, opening tick) |
| Quote server, keeper | `offchain/quote-server.mjs`, `offchain/services/keeper.mjs` | use `entry-price.mjs` |

The ETH/USDG router protections (`UniV3TwapReference`, `ChainlinkReference`, `DualReference`) are separate and unchanged.

## Observer contract or keeper

None needed. The quote service (already part of the stack) is the indexer. It needs an RPC with `eth_getLogs`
over the pool's history from the launch block.

Not recommended: an on-chain snapshot observer poked by a keeper. Snapshots can be sandwiched between pokes,
need a funded keeper (estimated 30–50k gas per poke, not measured), and Hookr's hook cannot be changed to add an
accumulator. The converter would also need a code change to read it.

## Trust assumptions

1. **Quote signer key.** Whoever holds it can sign any price. The on-chain floor still applies, so the worst
   case per conversion is a fee of 0.002 ETH instead of the correct one; collateral (1M MILORD escrow) and
   redemption are unaffected. The Team Safe can revoke a signer (`setQuoteSigner`) or pause entry instantly.
   Keep the key in a KMS/HSM.
2. **RPC and indexer correctness.** The service trusts the logs and slot0 its RPC returns. Run your own node or
   cross-check two providers in production (not implemented yet).
3. **After-the-fact auditability.** Every `Converted` event carries `weiPerMillion`; anyone can recompute the
   correct price from the pool logs and flag a wrong quote.

## Remaining risks

- Not audited; the float tick→price conversion is accurate to ~12 significant digits (tested within 1 bp).
- A determined attacker who holds a manipulated price for a full 30 minutes moves the TWAP fully; this costs the
  round-trip tax and arbitrage losses on the whole moved volume, far more than the fee difference.
- Thresholds (10%, 120 s, 1e23, tick range) assume the approved launch parameters; re-check them against the
  real Hookr deployment (BLOCKERS.md), especially the tick range.
- If the indexer is down, entries wait. This is by design.

## Effect on Hookr taxes, the lock and pARB distribution

None. No Solidity file changed (`git diff -- src script` is empty). The tax, lock-fee and distribution tests
and the 39 simulation checks still pass.

## Tests

| # | Case | Where | Result |
|---|---|---|---|
| 1 | 10 ETH FDV → 0.002 ETH | `test/robinhood/EntryFeePricing.t.sol`, `offchain/test-twap.mjs`, anvil test (real convert) | pass |
| 2 | 100 ETH FDV → 0.002 ETH | same | pass |
| 3 | 200 ETH FDV → ≈0.002955 ETH | same (anvil: 0.0029557) | pass |
| 4 | 500 ETH FDV → ≈0.007388 ETH | same (anvil: 0.0073898, real convert) | pass |
| 5 | same-block manipulation | `test-twap.mjs`, anvil (10x push+restore in one block; 2x left at block end) | pass |
| 6 | sustained multi-block manipulation | `test-twap.mjs`, anvil (5% low for 5 min: −0.85%; 20% low: refused) | pass |
| 7 | missing or stale observations | `test-twap.mjs`, anvil | pass |
| 8 | low liquidity / inactive pool / out of range | `test-twap.mjs`, anvil (2 h idle OK; below min refused; pushed past range refused) | pass |
| 9 | token ordering and decimals | `test-twap.mjs`, anvil (`verifyPoolKey`) | pass |
| 10 | redemption independent of pricing | Foundry (signer revoked + entry paused), anvil (price refused) | pass |

Run: `node offchain/test-twap.mjs`, `./sim/run-entry-price.sh` (fresh anvil, real v4 swaps),
`forge test --match-contract EntryFeePricingTest -vv`, `./sim/run-local.sh`.

Full suites: forge 169/169, simulation 39/39, services 14/14.

## Gas

| Operation | Gas |
|---|---|
| Price computation, quote signing | 0 (off-chain) |
| Keeper/observer pokes | 0 (none needed) |
| `convert` (first NFT, cold storage) | 384,380 (Foundry); 414,498 incl. tx overhead on anvil |
| `convert` (later NFT) | 260,021 incl. tx overhead on anvil |
| `redeem` | 162,428 (Foundry); 259,938 incl. tx overhead on anvil |
