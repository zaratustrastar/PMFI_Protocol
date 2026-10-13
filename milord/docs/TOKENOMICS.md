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
| Trading tax | up to 1.5% on buys and sells **at the pool level**, target ⅓ buyback-and-burn, ⅓ Team Safe, ⅓ Arbitrage Funding Safe; a launchpad commission reduces all three proportionally. Mechanism pending decision (see HOOKR_COMPATIBILITY). |

Initial allocation of the 6.767B (liquidity, team, legacy PMFI holders, treasury) is **not specified** in the
brief and is listed in OPEN_DECISIONS.

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

## pARB (Base)

pARB is the share token of a **new** pARB vault instance (`PMFIArbVaultV3`) dedicated to MILORD protocol fees,
with a 0% performance fee. Shares are minted only by depositing USDC at the official price per share (PPS).

- PPS starts at $1.00 for the first deposit only and then follows reported net assets. It goes down in losing
  periods and is never reset to $1.
- Redemption is request-based (request → next report → claim when liquidity is available); pARB cannot
  necessarily be sold or redeemed instantly.
- Holders of claimed pARB own it outright and can transfer or redeem it.

## Value flows

| Source | Paid in | Split | Ends as |
|---|---|---|---|
| Entry fee | ETH | 70% Arbitrage Safe / 30% Team Safe | Arbitrage share → USDC → pARB → holders |
| Royalty | ETH/WETH | 50% Team / 50% Arbitrage | Arbitrage share → USDC → pARB → holders |
| Trading tax | ETH (pool-dependent) | ⅓ BuybackBurner / ⅓ Team / ⅓ Arbitrage | burned MILORD; Arbitrage share → pARB |
| Exit burn | MILORD | 100% burned | lower supply |

Only fee revenue becomes pARB for holders. Principal deposited to convert is escrowed MILORD and is never
counted as revenue or deposited in the vault.
