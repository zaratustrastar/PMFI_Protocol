# MILORD architecture

Three things, kept small: a tradeable **MILORD** token, unique convertible **Milords** NFTs, and
protocol-fee-funded **pARB** shares distributed to their holders.

Everything below runs on **Robinhood Chain** (one chain, no reward bridge).

```
 MILORD (ERC-20, fixed supply, burnable)
   │ 1,000,000 + ETH fee                                       PMFIArbVaultV3 (pARB, asset USDG, 0% perf fee)
   ▼                                                              ▲ requestDeposit(receiver = distributor)
 MilordConverter ──issue/retire──► MilordsNFT (6,767 IDs)         │
   │ escrow (untouchable)            │ votes, ERC-2981            ParbFundingAdapter ◄── USDG (swapped fees)
   │ ETH fee                         │ Transfer events ──┐        │ shares
   ▼                                 ▼                   │        ▼
 RevenueSplitter (entry 70/30)   RevenueSplitter (royalty 50/50)  ShareDistributor (cumulative Merkle)
 RevenueSplitter (tax ⅓ ⅓ ⅓) ──► BuybackBurner                    ▲ weekly root          │ claim
   │ Arbitrage Safe ETH ── swap ETH→USDG (Safe tx) ──────────────┘ │                     ▼
   │                                                  off-chain epoch job  ◄── finalized Transfer events
 StrategyRegistry (characteristics → strategy)        (eligibility, Merkle)     holder address
 MilordGovernor (advisory)
```

The vault's trading capital still leaves the chain: the arbitrage engine trades on Polymarket, Kalshi and
Opinion, so the servicer moves USDG out to those venues and back. That is engine funding, not reward
distribution; see ROBINHOOD_VAULT_MIGRATION.

## Contracts

| Contract | Chain | Purpose | Admin powers |
|---|---|---|---|
| `MILORDToken` | RH | 6,767,000,000 × 1e18 minted once; burnable; permit; no owner, no tax | none |
| `MilordsNFT` | RH | ERC-721 + ERC-2981 + Votes (timestamp clock); IDs 1–6767; only converter issues/retires | royalty ≤10%, base URI until frozen, converter set once |
| `MilordConverter` | RH | Escrow 1M MILORD + ETH fee → NFT; NFT → 900k back, 100k burned | fee changes within ceilings (increases 2-day delay, reductions immediate), quote signers, entry pause. **No path to escrow.** |
| `RevenueSplitter` ×3 | RH | Entry fee 70/30, royalties 50/50, trading tax ⅓/⅓/⅓ | shares within fixed bounds; payee address change 2-day delay |
| `BuybackBurner` | RH | Holds burn budget; operator buys MILORD with slippage bounds and burns | operators, size limits, adapter change 2-day delay |
| `StrategyRegistry` | RH | 8+ characteristics, pick 2, deterministic pair → strategy, history | append characteristics/strategies, enable/disable, map pairs |
| `MilordGovernor` | RH | OZ Governor over NFT votes (advisory in V1) | none |
| `PMFIArbVaultV3` | RH | Fork of pARB V2 with fixes, perf fee 0, asset USDG (6 decimals enforced) | V2 admin set minus unsafe paths (see SECURITY_MODEL) |
| `ParbFundingAdapter` | RH | Deposits fee USDG into the vault for the distributor; one `sourceRef` once | operators |
| `ShareDistributor` | RH | Cumulative Merkle claims of pARB | publisher, activation delay, veto pending root. **No pARB withdrawal.** |

No proxies, no AMM, no custom Uniswap hook, no ERC-404, no migration contract.

## Off-chain components

| Component | File | Notes |
|---|---|---|
| Quote signer | `offchain/lib/quote.mjs` | Signs EIP-712 quotes only from a fresh manipulation-resistant price; no fallback. |
| Report signer | existing `arb_reporter.py` (needs fixes) + `offchain/lib/report.mjs` | V3 uses the V2 struct with a new salt. |
| Epoch job | `offchain/lib/eligibility.mjs` | Finalized Transfer events → NFT-seconds per UTC day → weekly allocation → cumulative StandardMerkleTree. |
| Simulation | `sim/simulate.mjs`, `sim/run-local.sh` | Full 3-week run on anvil against the real contracts. |

## Weekly cycle

1. Revenue lands in splitters (permissionless `distribute`).
2. Arbitrage Safe swaps ETH to USDG on Robinhood Chain (Safe transaction; no contract custody).
3. Operator calls `adapter.fund(sourceRef, usdc)`; the vault's next report processes it at the official PPS;
   `adapter.settle` mints the shares to the distributor.
4. After the UTC week closes and the Robinhood blocks are final, the epoch job computes eligibility and
   publishes a cumulative root (must be fully funded); it activates after the delay unless vetoed.
5. Holders (or anyone on their behalf) claim; pARB is redeemable through the vault's normal request flow.

## Exact movements per operation

| Operation | MILORD | ETH | USDG | pARB | NFT |
|---|---|---|---|---|---|
| Wake (convert) | user → converter escrow: 1,000,000 | user → EntryFeeSplitter: fee; excess refunded | — | — | inventory or new ID → user |
| Entry fee split (`distribute`) | — | splitter → Arbitrage Safe 70%, Team Safe 30% | — | — | — |
| Redeem | escrow → user 900,000; escrow burns 100,000 (supply −100,000) | — | — | — | user → converter inventory |
| Secondary sale | — | marketplace → RoyaltySplitter 6.7% *if it honours ERC-2981* → 50/50 | — | — | seller → buyer |
| Trading tax (pending D1) | sells: MILORD fee → BuybackBurner → burned | buys: ETH → TradingTaxSplitter → ⅓ burner / ⅓ team / ⅓ arbitrage | — | — | — |
| Buyback | swap adapter → burner → burned | burner → swap adapter | — | — | — |
| Funding (Safe ops) | — | Arbitrage Safe → swap | swap → ParbFundingAdapter | — | — |
| `adapter.fund` | — | — | adapter → vault (pending deposit) | — | — |
| Vault report + `adapter.settle` | — | — | stays in vault (then `tend` → servicer) | vault mints `usdg / PPS` → ShareDistributor | — |
| Weekly root | — | — | — | none (commitment only; must be covered by distributor balance) | — |
| Claim | — | — | — | distributor → holder address | — |
| pARB redeem | — | — | vault → holder at a later report's PPS, when liquid | holder → vault (burned at processing) | — |

## Why these choices

- **Signed quotes, not an on-chain oracle**: Robinhood Chain has no Chainlink MILORD feed and a fresh v4 pool is
  manipulable in one block. A signer reading a TWAP plus sanity bounds, with 5-minute single-use account-bound
  quotes, gives manipulation resistance without a custom hook. If the signer is down, entry waits (spec: no fallback).
- **Merkle distribution, not on-chain per-NFT accounting**: time-weighted per-NFT accounting in the NFT's
  transfer hook would make every transfer expensive and couple the NFT to the vault. Weekly roots with a funded
  check, delay and veto stay simple, and the data can be recomputed by anyone.
- **V3 fork, not V2 reuse**: V2 has a fixed 20% fee and PPS-inflation bugs (see audit). V3 keeps every V2
  interface the frontend and reporter use, so the existing deposit/report/claim UI and bot work with an address change (plus the reporter's chain settings).
