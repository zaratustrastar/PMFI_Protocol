# MILORD Safe + Polymarket pUSD recovery (B21)

Implemented for the existing binary position model on all three current Polymarket position systems
(standard CTF, neg-risk CTF, Protocol V2), paying pUSD, with isolated MILORD custody.
Base-mode settlement remains unchanged. Automatic recovery defaults OFF. This is not
approval to enable a production bot, fund a Safe, or execute a redemption.

The coordinator polls authoritative Kalshi settlements and CTF resolution, checks
exact tracked holdings, journals submission intent before invoking the existing
Safe relayer, and observes a canonical, confirmed receipt. It requires the matching
CTF payout event, matching collateral transfer (including a zero-payout losing
outcome), extinguished conditional tokens, and venue cash visible to NAV before
removing position cost basis. Fractional condition payouts use integer arithmetic.
Pending recoveries block signing a NAV, including Polymarket winners. Open positions
with Kalshi settlement evidence also block signing. Recovery and NAV reads share a
file lock and allow only one recovery in flight per journal; all workers must share the journal directory on one host. Cross-host
coordination is not implemented.

Configuration, only after a separately approved and verified route:

- `ARB_FUNDING_CHAIN=robinhood` (the existing funding-mode setting), `ARB_CTF_ENABLED=true`.
- `MILORD_POLY_SAFE_ADDRESS` must equal the configured `POLY_PROXY_ADDRESS`; the
  relayer signer must derive that Safe. Use a NEW isolated MILORD Safe and dedicated
  credentials, never PMFI's live custody. `MILORD_POLY_PRIVATE_KEY` has no live-key fallback.
- `MILORD_CTF_MARKETS_FILE` maps each pair ID to `{"system": "ctf" | "ctf_negrisk" | "v2", "conditionId": "0x…"}`.
  The system is declared, never guessed: it comes from the market's `negRisk` and `version` fields when the pair
  is opened. ctf/ctf_negrisk take the 32-byte CTF condition id, v2 the 31-byte Protocol V2 id. The transport
  re-derives the YES position id on chain and refuses if it differs from the tracked token. Side comes from the
  existing complementary Kalshi/Polymarket position model (YES = outcome 0, NO = outcome 1). The old
  `collateral` field and `MILORD_CTF_COLLATERAL` are gone: payout collateral is always pUSD, read from the
  adapter/Router configuration on chain.
- `ARB_CTF_JOURNAL` must be durable and shared with the reporter process.
- `ARB_CTF_CONFIRMATIONS` defaults to 20, must be a positive integer.
- `ARB_CTF_ROUTE_VERIFIED=true` is a separate submission gate; leave it unset now.

Recovery is deliberately conservative: shared holdings, partial token balances,
concurrent collateral movements, partial receipt payouts, stale/missing settlement
history, mismatched cash, and ambiguous submission outcomes require reconciliation.
A crash/timeout after durable `sending` never automatically submits again. Preserve
the journal and inspect the relayer and canonical chain receipt; do not delete the
row or reset it to `prepared`. A verified transaction hash can be assigned to that
row with status `submitted` while the coordinator is stopped, after independently
checking Safe, condition, collateral, index set and payout. Normal observation then
validates it again. Database failures after observation retry without resubmission.
If the database committed `redeemed` before the final journal update, retain both
records and reconcile the journal from the committed database and receipt; do not
re-open the position. Archive journals with their position generation; pair ID reuse
is refused when the tracked fingerprint changes.

## How positions map to Polymarket's position systems

Checked against the docs (docs.polymarket.com/trading/positions/manage, /resources/contracts) and the deployed
contracts' source (Polymarket/ctf-exchange-v2 `src/adapters`, Polymarket/polymarket-v2-external `src/routers`,
`src/modules`, `src/libraries/Ids.sol`). The engine tracks one Polymarket token id (`poly_yes_token`), the held
side and six-decimal shares per pair. All three systems fit that model without a schema change:

| System | Token ledger | Our tracked id is | Redeem call | Pays | Evidence checked in the receipt |
|---|---|---|---|---|---|
| `ctf` (standard) | ConditionalTokens `0x4D97…6045` | CTF position id with USDC.e as collateral (unchanged since V1) | `CtfCollateralAdapter 0xAdA1…cE1f .redeemPositions(pUSD, 0, conditionId, [1,2])` | pUSD | CTF `TransferBatch` Safe→adapter of exactly [YES, NO] balances; CTF `PayoutRedemption` (redeemer adapter, USDC.e, [1,2], payout); one pUSD mint to the Safe |
| `ctf_negrisk` | ConditionalTokens | CTF position id with the NegRiskAdapter's wrapped collateral | `NegRiskCtfCollateralAdapter 0xadA2…6eAab .redeemPositions(…)` | pUSD | CTF `TransferBatch` Safe→adapter; NegRiskAdapter `PayoutRedemption` (redeemer adapter, amounts, payout); one pUSD mint |
| `v2` (Protocol V2) | PositionManager `0x006F…9fEF` | `conditionId(31 bytes) << 8 \| outcome`; top byte = module (1 binary, 2 neg-risk) | `Router 0x1212…2600 .redeem(conditionId, outcomeIndex, amount)` | pUSD | PositionManager `TransferSingle` Safe→module of exactly the shares; module `PositionRedeemed` (Router, id, Safe, amount, payout); Router `RouterPositionRedeemed`; one pUSD mint |

What differs and is not assumed interchangeable:

- **Ids.** CTF ids come from `getCollectionId`/`getPositionId` with the market's position collateral (USDC.e, or
  the wrapped collateral for neg-risk), read on chain. V2 ids are bit-packed and carry their module. A neg-risk id
  declared as `ctf` (or the reverse) fails the id check; a CTF condition id declared as `v2` fails the length check.
- **What a call redeems.** The CTF adapters ignore their collateral/index-set arguments and redeem the caller's
  whole balance of both outcomes; `Router.redeem` burns an exact amount of one outcome. Because of the adapters,
  the Safe must hold the tracked side exactly and none of the opposite side (also enforced for V2, so one rule
  covers all three).
- **Resolution.** CTF: `payoutDenominator`/`payoutNumerators`. V2: the module's `hasResult`, and the payout from
  `PositionManager.getPayout`, which is what `redeem` itself uses (neg-risk V2 results can be derived, not stored).
- **Combinatorial V2 markets** (module 3) are not binary pairs and are refused.
- **Migration.** Polymarket's operator can migrate CTF positions into PositionManager. A migrated position shows a
  CTF balance of 0, which the transport reports as a balance mismatch for reconciliation; it is never re-guessed.
- **Approvals.** The Safe needs `ConditionalTokens.setApprovalForAll(adapter)` or
  `PositionManager.setApprovalForAll(Router)`. A plan without it returns `approval_required` with the exact call
  (`approvalTx`) and journals nothing; the approval is a separate, owner-approved Safe transaction and is never
  bundled into a redemption.
- **Stray adapter balances.** An adapter wraps all USDC.e it holds after a redemption, so a donation to the adapter
  would make the pUSD minted exceed the planned payout. That is treated as a mismatch, not as income.

Zero payouts are planned explicitly (`zeroPayout: true`, payout 0, no pUSD required in the receipt) and still
burn the tokens, so the position is provably extinguished. Partial balances, already-redeemed positions, shared
holdings, changed adapter/Router configuration, non-binary conditions and non-six-decimal collateral are refused.

## Tests

- Offline: `trading_bot/tests/ctf_redemption.test.mjs` (planning for each system, refusals, receipt evidence for
  each system including cross-system receipts, observation) and `bot/arb_monitor/tests/test_ctf_redemption.py`
  (journal, approval gate, mapping).
- Polygon fork, private Anvil only: `milord/fork/polygon-redemption-fork.sh`. It creates a fresh market of each
  system on the fork (CTF condition with the test account as oracle, a permissionless NegRiskAdapter market, and a
  BinaryModule condition with the resolver role granted by the impersonated module owner), splits through the real
  adapters and Router with pUSD minted by the impersonated BinaryModule, resolves, and runs the transport's own
  plan, calldata, receipt checks and observation. Cases: standard winner, zero-payout loser, 50/50 fractional,
  shared-holdings refusal, neg-risk winner (plus a wrong-system declaration refusal), V2 NO winner, and a refused
  second redemption for each. No key, API, relayer or Safe is involved.

## What remains before live use

- The fork test must pass on the VPS (this container cannot reach a Polygon RPC).
- Submission still goes through the existing Safe relayer client (`RelayerTxType.SAFE`), unchanged. Polymarket's
  docs now describe Deposit Wallet batches for new accounts; the MILORD Safe's relayer path must be confirmed when
  that custody is created.
- One owner-approved approval transaction per position system used, then one approved small redemption, before
  `ARB_CTF_ROUTE_VERIFIED=true`.
- The markets file must be produced from each pair's `negRisk`/`version` at open time; the engine does not write
  it yet.

Kalshi settlement evidence uses its existing account authentication and exact
portfolio settlement counts/revenue (cents), not a market-price heuristic:
https://docs.kalshi.com/api-reference/portfolio/get-settlements . Live API/receipt
fixtures and independently verified dedicated account configuration remain required.
Mocks validate recovery behavior; they do not certify venue integration or audit readiness.
