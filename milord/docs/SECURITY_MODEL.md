# Security model

**Status: unaudited. Not production-ready.** Local tests pass; no external review has happened.

## Trust assumptions

| Role | Holder (recommended) | Can | Cannot |
|---|---|---|---|
| Converter owner | Team Safe (multisig) | lower fees now; raise within ceilings after 2 days; pause entry; manage quote signers; change fee recipient after 2 days | touch escrowed MILORD; pause exits; exceed ceilings; mint |
| Quote signer | dedicated hot key in the quote service | price entry quotes (≤30 min, single use, account-bound) | change fees, move funds. Worst case: wrong fee for entries until revoked. |
| NFT owner | Team Safe | royalty ≤10%, metadata until frozen | move NFTs, change converter |
| Splitter owner | Team Safe | shares within fixed bounds; payee change after 2 days | withdraw funds |
| Burner owner / operator | Team Safe / ops key | limits, operators; buyback within size and slippage bounds | send ETH anywhere except the swap adapter |
| Vault owner | Team Safe | config, pause, shutdown, approve large PPS move, allowlist, emergency withdraw of **unreserved** USDG in shutdown | take reserved USDG; mint shares; change perf fee |
| Report signer | reporter bot key | publish NAV within guards | move PPS > 10% without owner approval; report below on-chain cash |
| Funding operator | ops key | deposit USDG held by the adapter | fund the same `sourceRef` twice; take shares |
| Root publisher | ops key | propose a fully funded root | activate before delay; publish unfunded totals; withdraw pARB |
| Distributor owner | Team Safe | veto pending roots; set delay ≤ 7 days | withdraw pARB |

Neither the team nor governance has any withdrawal path to MILORD redemption escrow or distributed pARB.

## Key risks and mitigations

| Risk | Mitigation |
|---|---|
| Quote manipulation (flash price move) | TWAP + cross-check in the service, 5-min single-use account-bound quotes, user `maxFee` |
| Quote service down | Entry waits (no fallback, per spec); exits unaffected |
| Report signer compromise | on-chain cash floor, 10% move cap, owner veto via pause; recommend 2-of-N signing in a later version |
| Off-chain asset misreporting | reporter fixes in PARBITRAGE_INTEGRATION; reconciliation log |
| Wrong Merkle root | funded check, activation delay, owner veto, public data file and recomputation script |
| Reorgs on Robinhood Chain | eligibility uses finalized blocks only |
| ERC-6551 TBA drained before sale | UI warning; documented |
| Royalty non-payment | treated as optional income; nothing depends on it |
| pARB illiquidity | redemption is queued; documented; no instant-exit promise |
| Smart-contract wallets | same chain: pARB is paid to the holder address itself |
| USDG issuer controls (upgradeable proxy, pause, freeze and wipe) | out of our control; pARB claims keep working while USDG is paused (tested); documented in ROBINHOOD_VAULT_MIGRATION |
| Venue balances in USDC/USDT reported as USDG | assumes a 1:1 peg; reporter should value at market and flag deviations |
| Supply burns vs 6,767 backing | `maxSimultaneouslyBacked()`; no insolvency |
| Admin key loss | Safes; no function is required for exits or claims |

## Testing

- Unit, fuzz (512 runs) and invariant (128 × 64) tests: `forge test` (104 tests).
- Invariants: escrow ≥ backed × 1M; backed == circulating; votes == circulating; supply drop == exit burns;
  distributor solvency; shares tracked once; claims ≤ cumulative; vault reserves covered; unclaimed share
  counter exact; PPS consistent with backing.
- Legacy V2 issues reproduced in `test/base/LegacyV2Bugs.t.sol`.
- End-to-end: `sim/run-local.sh` (12 scenario checks).

## Before mainnet

External audit of all contracts and the off-chain epoch job; reporter fixes; Safe setup; signer key custody;
monitoring and alerting on reports, roots, fee changes and pauses.
