# Tokenomics

Nothing here is a promise of return. pARB value depends on arbitrage results, which can be negative.

## MILORD

| Item | Value |
|---|---|
| Chain | Robinhood Chain (id 4663) |
| Standard | ERC-20, 18 decimals, ERC-2612 permit |
| Supply | 6,767,000,000 minted once at deployment to one address |
| Minting | none after deployment (no mint function exists) |
| Burning | anyone can burn their own tokens; exit burns and buybacks reduce supply |
| Transfer tax | 0% on ordinary transfers (no transfer hooks) |
| Trading tax | flat 1.5% on buys and 1.5% on sells, paid in ETH, enforced by the Hookr pool (immutable once launched). Hookr keeps 20%; the other 80% goes to TradingTaxSplitter: ⅓ BuybackBurner, ⅓ Team Safe, ⅓ ArbitrageFundingRouter (owned by the Arbitrage Funding Safe). |
| Launch | one Hookr existing-token pool on Robinhood Chain, created atomically with the token (`MilordAtomicLaunch`): **100% of supply** in one MILORD-only founding position, 0 ETH, opening FDV ~10 ETH, range ticks 134,220–203,280 (up to ~1,000× the opening price), base LP fee 0.3%. No wallet keeps any MILORD. |
| LP fees | the founding position is to be locked forever in Hookr's family lock (approved architecture; execution needs the owner's separate go-ahead). Its fees go to HookrLockFeeCollector: ETH to buyback-and-burn, MILORD burned. Principal can never be withdrawn once locked. |
| Anti-Snipe, dev buy | off. Both are separate, unapproved decisions. |
| Legacy PMFI | no migration contract or reserve: legacy PMFI is sold and the proceeds buy MILORD on the market for holders. |

## Milords

| Item | Value |
|---|---|
| IDs | 1 – 6,767, each unique and persistent |
| Backing | exactly 1,000,000 MILORD escrowed per circulating NFT |
| Entry | 1,000,000 MILORD + ETH fee = max(10% × ETH value of 1M MILORD, 0.002 ETH) |
| Exit | 900,000 MILORD returned, 100,000 burned; NFT returns to inventory |
| Royalty | 6.7% ERC-2981 → 50% Team, 50% Arbitrage (marketplaces may ignore it) |
| Votes | 1 circulating NFT = 1 vote; inventory has 0 |

### Supply interaction

6,767 × 1,000,000 = 6,767,000,000 = the whole token supply. Every burn (exit burns, buybacks, voluntary
burns) makes it impossible for all 6,767 identities to be backed at once.
`MilordConverter.maxSimultaneouslyBacked()` reports `min(totalSupply / 1M, 6767)`. This is intended and
creates no insolvency: each circulating NFT is individually backed by escrow; IDs beyond what supply allows
simply stay in inventory or unminted.

Each round-trip (enter then exit) burns 100,000 MILORD (1.48 × 10⁻⁵ of supply). The supply is never at risk of
going to zero through the converter because the burn comes from the redeemer's own principal.

## pARB (Robinhood Chain)

pARB is the share token of a **new** pARB vault instance (`PMFIArbVaultV3`) dedicated to MILORD protocol fees,
with a 0% performance fee. Shares are minted only by depositing USDG at the official price per share (PPS).

- PPS starts at $1.00 for the first deposit only and then follows reported net assets. It goes down in losing
  periods and is never reset to $1.
- Redemption is request-based (request → next report → claim when liquidity is available); pARB cannot
  necessarily be sold or redeemed instantly.
- Holders of claimed pARB own it outright and can transfer or redeem it.

## Value flows

| Source | Paid in | Split | Ends as |
|---|---|---|---|
| Entry fee | ETH: max(10% × ETH value of 1M MILORD, 0.002 ETH) | 70% ArbitrageFundingRouter / 30% Team Safe | Arbitrage share → USDG → pARB → holders |
| Royalty | ETH/WETH | 50% Team / 50% ArbitrageFundingRouter | Arbitrage share → USDG → pARB → holders |
| Trading tax | ETH, 1.5% buy / 1.5% sell; Hookr keeps 20% | 80% in thirds: BuybackBurner / Team Safe / ArbitrageFundingRouter | burned MILORD; Arbitrage share → pARB |
| LP fees (locked position) | ETH and MILORD | ETH → BuybackBurner, MILORD burned | burned MILORD |
| Exit burn | MILORD | 100% burned | lower supply |

The router swaps its ETH to USDG within per-swap, daily and slippage bounds against a TWAP reference, and requests a
vault deposit; settled pARB goes to ShareDistributor, where holders claim daily allocations by cumulative Merkle root (REWARD_DISTRIBUTION.md).

Only fee revenue becomes pARB for holders. Principal deposited to convert is escrowed MILORD and is never
counted as revenue or deposited in the vault.
