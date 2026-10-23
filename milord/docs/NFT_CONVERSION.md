# NFT conversion

## Entry: MILORD → Milord

1. The frontend requests a quote from the quote service for the connected account.
2. The service reads a manipulation-resistant price for 1,000,000 MILORD in ETH (time-weighted pool price,
   cross-checked; never the OpenSea floor). If the source is unavailable or stale it refuses; there is no
   minimum-fee fallback.
3. It signs `EntryQuote{account, weiPerMillion, issuedAt, expiresAt, nonce}` (EIP-712, domain
   `MilordConverter` v1, chain id, converter address).
4. The user calls `convert(quote, sig, maxFee)` with ETH ≥ fee. The converter checks:
   - quote bound to `msg.sender`, not before `issuedAt`, not after `expiresAt`, lifetime ≤ `maxQuoteLifetime` (5 min, cap 30)
   - nonce unused (single use), signer approved
   - fee = max(entryFeeBps × price, minEntryFeeWei) ≤ `maxFee`
   - exactly 1,000,000 MILORD arrives (balance delta)
5. Identity: the most recently returned ID comes out of inventory first (LIFO); otherwise the next never-minted
   ID. Users cannot pick IDs, so rare IDs cannot be sniped by redeem/convert loops.
6. Fee goes to the entry splitter; excess ETH is refunded.

Defaults: 10% fee, 0.002 ETH minimum. Ceilings (immutable): 20% fee, 0.02 ETH minimum, 10% exit burn.

## Exit: Milord → MILORD

`redeem(id)` by the holder: NFT goes to converter inventory, 100,000 MILORD burned, 900,000 returned.
**Exit can never be paused.** Votes and reward eligibility stop at that moment.

## Escrow guarantees

- `balanceOf(converter) ≥ backedCount × 1M` at all times (invariant-tested).
- The only paths moving MILORD out are `redeem` (to the holder) and the exit burn.
- `rescueERC20` refuses MILORD. There is no owner, governor or upgrade path to principal.

## Reissue rules

A reissued ID keeps its number and artwork, gets a new `activationCount`, and does **not** inherit the previous
holder's characteristic selection. Rewards earned by previous holders stay with them (see REWARD_DISTRIBUTION).

## ERC-6551

Milords are standard ERC-721, so any ERC-6551 registry can create token-bound accounts. V1 adds no TBA guard.
Risk: a seller could drain the NFT's TBA after a buyer commits. The frontend should show TBA balances and warn;
marketplaces vary. Funded wallets are not part of V1.

## Admin bounds

| Action | Rule |
|---|---|
| Lower fees / exit burn | immediate |
| Raise within ceilings | queue, 2 days |
| Change entry-fee recipient | queue, 2 days |
| Pause entry | immediate; exit unaffected |
| Quote signers | add/remove immediately |
