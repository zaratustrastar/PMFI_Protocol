# Reward distribution

## Rules

- Only circulating Milords earn. Converter inventory and unminted IDs earn nothing.
- Eligibility is time-weighted: NFT-seconds held, scored per UTC day, summed over the UTC week (Mon 00:00 – Mon 00:00).
- Only finalized Robinhood Chain blocks are used (L1 finality ≈ 13 min). The job runs after the week closes
  plus a finality margin.
- Each week's budget = all pARB in the distributor not yet allocated (new shares + carried dust).
- Allocation = floor(budget × holderSeconds / totalSeconds). Rounding dust carries to the next week.
- If no NFT circulated during a week, the whole budget carries forward.
- Allocations are keyed by the holder address on Robinhood Chain, the same chain as the vault and the
  distributor, so smart-contract wallets receive pARB at their own address. Published allocations never move.
- Previous owners keep what they earned while holding. Buyers earn from the moment they receive the NFT.
- Leaves are cumulative (`[address, totalEverAllocated]`), so claiming late or skipping weeks loses nothing.
  There is no expiry.

## On-chain (ShareDistributor, Robinhood Chain)

| Step | Check |
|---|---|
| `proposeRoot(root, totalCumulative, epoch, dataHash)` | publisher only; epoch increases; total ≥ previous; **pARB held + claimed ≥ total** |
| activation | after `activationDelay` (default 1 day, max 7) |
| `vetoPending()` | owner, during the delay |
| `claim(account, cumulative, proof)` | anyone may call; pays `cumulative − claimed[account]` to `account` |

pARB cannot be withdrawn from the distributor except by claims (`rescue` refuses pARB).

## Publication

Each epoch publishes `epoch-N.json`: week bounds, finalized block range, per-day NFT-seconds per holder
(`daily`), allocations, cumulative leaves, root and total. `dataHash` on-chain commits to the
file. Anyone can recompute it from public Transfer events with `offchain/lib/eligibility.mjs`.

## Worked example (from `offchain/test-eligibility.mjs`)

A holds #1 from before the week and sells it to B at day 3.5; A mints #2 on day 1 and redeems it on day 5.
A: 3.5 + 4 = 7.5 NFT-days; B: 3.5. Budget 1000 → A 681, B 318, 1 dust carried.

## Not promised

Rewards depend on fees actually collected, the ETH→USDG swap price, the vault's results and pARB redemption liquidity.
They can be zero, and pARB can be worth less than the USDG that bought it.
