# Reward distribution

pARB is allocated **daily**. Epoch *k* is the UTC day `[genesis + (k-1)·86400, genesis + k·86400)`, and
`EPOCH_GENESIS` must be 00:00 UTC. Holders claim it; nothing is sent to wallets automatically.

## Rules

- Only circulating Milords earn. Converter inventory and unminted IDs earn nothing.
- Eligibility is time-weighted: NFT-seconds held during the UTC day, not ownership at a snapshot.
- A day closes at its end (00:00 UTC) plus the 6 h settlement grace. Only finalized Robinhood Chain blocks are used
  (L1 finality ≈ 13 min).
- A day's amount = all pARB settled into the distributor before the day's cutoff (end + grace), minus everything
  allocated to earlier days. A batch settled after the cutoff belongs to the next day (late funding).
- Allocation = floor(amount × holderSeconds / totalSeconds). Rounding dust carries to the next day.
- A day with no settled pARB allocates nothing; a day with no circulating NFT carries its whole amount forward.
- Allocations are keyed by the holder address on Robinhood Chain, the same chain as the vault and the
  distributor, so smart-contract wallets receive pARB at their own address. Published allocations never move.
- Previous owners keep everything earned while holding. Buyers earn from the second they receive the NFT.
- Leaves are cumulative (`[address, totalEverAllocated]`), so claiming late or skipping days loses nothing.
  Unclaimed pARB never expires.

## Roots

- A root is cumulative from genesis; its on-chain `epoch` is the last day it covers. If the publisher is down or a
  day is not ready, the next root covers every missed day at once (each day still uses its own weights and amount).
- One root is pending at a time. A root in its 24 h review cannot be replaced: `proposeRoot` reverts
  `PendingRootNotActivated(epoch, activatesAt)`. Once the delay has passed, `proposeRoot` first activates the pending
  root, then accepts the next. So with the 24 h delay a root is proposed roughly once per day; when the previous root
  activates a little later than the day closes, the next root simply covers one more day. No day is lost.
- After a veto the publisher stops (`blocked`, critical alert) instead of re-proposing the disputed root, also
  after a restart, until the Safe lists the vetoed root in `PUBLISHER_ACK_VETOES`. The next root then covers the
  vetoed days again with the corrected data.
- The publisher keeps no state that matters: every tick recomputes all days from genesis from finalized chain
  data, so a restart neither duplicates nor skips an allocation.

## On-chain (ShareDistributor, Robinhood Chain)

| Step | Check |
|---|---|
| `proposeRoot(root, totalCumulative, epoch, dataHash)` | publisher only; activates a ready pending root first; reverts while a pending root is in review; epoch increases; total ≥ previous; **pARB held + claimed ≥ total** |
| activation | after `activationDelay` (1 day, max 7); `activateRoot()` is permissionless and the keeper calls it |
| `vetoPending()` | owner, during the delay |
| `claim(account, cumulative, proof)` | anyone may call; pays `cumulative − claimed[account]` to `account` |

pARB cannot be withdrawn from the distributor except by claims (`rescue` refuses pARB).

## Publication

The publisher writes `STATE_DIR/epochs/<k>.json` for each root (day bounds, finalized block range, per-day NFT-seconds
per holder, allocations, cumulative leaves, root and total; `dataHash` on-chain commits to it) and `rewards.json` on
every tick. Anyone can recompute both from public Transfer events with `offchain/lib/eligibility.mjs`.

## What the app shows

| Figure | Meaning |
|---|---|
| Earned | allocated to the holder for every closed day, whether or not a root includes it yet |
| Allocated | in the latest proposed root |
| Awaiting activation | in the pending root, claimable after its review delay |
| Claimable | active root's cumulative minus already claimed |
| Claimed | `claimed(address)` on chain |
| Accruing | NFT-seconds held in the current, not yet closed day (amount unknown until it closes) |

## Worked example (from `offchain/test-eligibility.mjs`)

A holds #1 from before the day and sells it to B at 12:00; A mints #2 at 03:00 and redeems it at 18:00.
A: 12 h + 15 h = 27 h; B: 12 h. Amount 1000 → A 692, B 307, 1 dust carried. The next day B holds all day and gets
the whole amount; A's cumulative stays at 692.

## Not promised

Rewards depend on fees actually collected, the ETH→USDG swap price, the vault's results and pARB redemption liquidity.
They can be zero, and pARB can be worth less than the USDG that bought it.
