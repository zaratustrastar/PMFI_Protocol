# B9: venue and bridge routes for Robinhood USDG

Checked 2026-10-23 against official documentation and public route APIs. Nothing was signed or sent.
The VPS command `node offchain/check-venue-routes.mjs` repeats the API part read-only (route lists and quotes).

## Summary

| Leg | Route | Status | Evidence |
|---|---|---|---|
| Polymarket deposit | USDG on 4663 → the Safe's Polymarket bridge deposit address (`evm`) → pUSD on Polygon | **Supported by Polymarket**; quotes pending VPS run; first live deposit needs owner approval | `bridge.polymarket.com/supported-assets` lists chain 4663 `USDG 0x5fc5…1d168` (6 dec, min $2), with WETH, USDE and ETH. Deposit docs: "Assets are bridged and swapped to pUSD automatically". |
| Polymarket recall | Safe pUSD → `POST /withdraw` address → USDG on 4663 (servicer) | **Supported by the docs** ("withdraw to any supported chain and token"); implemented, disabled until verified | Withdraw docs: `POST /withdraw {address, toChainId, toTokenAddress, recipientAddr}`, then "send pUSD to the `evm` address". Instant and free; >$50k should be split. |
| Polymarket collateral | **pUSD**, not USDC.e (since the CLOB V2 upgrade, 2026-04-28) | **Breaks the old recall script** | `docs.polymarket.com/trading/ctf/redeem`: pUSD `0xC011a7E1…2DFB`; V2 migration notes. |
| Kalshi deposit | USDG (4663) → Across → USDC (Base) → the account's Kalshi deposit address | **Across route exists**; Kalshi deposit address comes from the Kalshi app | Across `available-routes`: 4663 USDG → 8453 USDC `0x8335…2913` and 8453 USDC → 4663 USDG. Kalshi help: USDC deposits, up to ~30 min, provider limits shown in the app. |
| Kalshi recall | Kalshi app crypto withdrawal → USDC → Across → USDG (4663) | **Manual (operator)** | Kalshi API docs list only `GET /portfolio/withdrawals`; no endpoint creates a withdrawal. Help center: "A prior crypto deposit is required", document verification may be needed, no fee, ~30 min. |
| Across | USDG ↔ USDC on Base, Polygon, Arbitrum, Ethereum, … | **Listed** (39 routes from 4663) | `app.across.to/api/available-routes?originChainId=4663`; Across blog: ~2 s fills. |
| Relay | USDG ↔ Base USDC | **Not confirmed here** (chain list truncated in this container); engine fallback only | Earlier LI.FI research quoted Relay on 4663; `check-venue-routes.mjs` reports it as `fallbackOk`. |
| USDG | `0x5fc5360D…1d168` on 4663; LayerZero OFT `0x0d54…28d1` | Matches Paxos | `docs.paxos.com/guides/stablecoin/usdg/mainnet` |

## What changed in code

- `bot/arb_monitor/core/arb_venue_routes.py` and `trading_bot/poly_bridge_withdraw.mjs`: in robinhood mode a
  Polymarket recall uses only the Polymarket bridge (pUSD → USDG on 4663), set with
  `ARB_POLY_WITHDRAW_ROUTE=polymarket_bridge`. The script refuses to send until `ARB_POLY_BRIDGE_ROUTE_VERIFIED=true`,
  which is set only after an owner-approved small withdrawal. One Safe transaction: `pUSD.transfer(bridgeAddress, exact amount)`.
  The bridge address is logged before submission, and an unknown outcome stays in transit for a person to check
  with `GET /status/<address>`. It is never resent.
- The legacy USDC.e recall (`safe_proxy_withdraw_parb.ts`) is refused in robinhood mode. Base mode, the live PMFI
  engine, is unchanged by this round.
- Kalshi recall in robinhood mode logs an operator task instead of calling the undocumented
  `POST /portfolio/balance/withdrawals`, unless `ARB_KALSHI_API_WITHDRAW_VERIFIED=true`.
- `milord/offchain/check-venue-routes.mjs`: read-only route and fee check (Polymarket, Across, Relay).

## Route hardening after the VPS route check (2026-10-23)

**Across quotes.** The VPS check found the routes but every quote returned `HTTP 400: POST method required when
request.body is provided`. The checker's transport sent `content-type: application/json` on its GET requests, which
Across reads as a request with a body. `GET /swap/approval` is the documented quote call; `POST` is only for embedded
destination actions. `httpClient()` in `offchain/lib/venue-routes.mjs` now sends GETs with only `accept` and no body,
and POSTs (Polymarket `/quote`) with a JSON body and content type. This cause is inferred from the error and the
docs: the container cannot reach Across, so the VPS run of the new commit is the confirmation. The Python engine's
GET (`arb_http.request_json`) never sent a body and is unchanged.

**Failure classes.** Every check now has a `kind`, and the output has a `summary`:
`routesUnavailable` (a route list lacks the pair), `apiErrors` (HTTP errors, unparseable or implausible quotes) and
`feePolicyFailures` (a quote that works but costs more than `ARB_BRIDGE_MAX_FEE_BPS`, default 50 bps per leg, which
is never raised to pass). Relay stays an optional fallback and is never required.

**Polymarket direct route kept as preferred.** Polymarket's own bridge stays the route for Polymarket's legs (quotes
tagged `preferred`); no Relay bridging was added. The observed VPS fees against the 50 bps limit:

| Size | Deposit | Recall |
|---|---|---|
| $10 | 60.25 bps, refused | 80.55 bps, refused |
| $100 | 25.84 bps, within | 58.43 bps, refused |
| $1,000 | 22.30 bps, within | 56.20 bps, refused |

So at 50 bps, deposits of about $100 or more pass and every Polymarket recall is refused, by the checker and by
`poly_bridge_withdraw.mjs`'s own `validateQuote` alike. Fail-closed is kept; whether recalls get a separate,
higher limit is an owner decision. A regression test pins exactly these four refusals.

**Kalshi withdrawals: what the code and docs show.**

| Claim | Status | Evidence |
|---|---|---|
| The PMFI engine requests Kalshi crypto withdrawals by API | Code exists | `withdraw_from_kalshi` (`bot/arb_monitor/core/arb_withdrawals.py`) sends `POST {KALSHI_BASE_URL}/portfolio/balance/withdrawals` with `{amount: cents, method: "crypto", address}`, signed like every Kalshi call; Base mode calls it from the recall path. It has been in the repository since the first import (3c8b66e, 2026-04-28). |
| That endpoint is part of Kalshi's API | **Undocumented** | Kalshi's API reference lists only `GET /portfolio/withdrawals` (status, type, amount, fee); no create-withdrawal call. The clearing-member `POST /margin/withdraw_settlement_balance` is a wire withdrawal with no on-chain address and is not usable here. |
| It has ever paid out | **Unproven** | The repository holds no transaction hashes, logs or ledger rows from a real Kalshi withdrawal. The only evidence is on the PMFI host: the engine's withdrawal ledger (each Kalshi row's `reference` is the id the POST returned) and Kalshi's own history. Base-mode ledger rows are completed by ETA, so a "completed" row is not proof of arrival. |
| Kalshi pays out USDC on Base | Documented | Help center: crypto deposits and withdrawals in USDC, Base among the networks, a prior crypto deposit needed, about 30 minutes, daily limit $2,500. |
| Robinhood USDG is supported | **Not supported in any Kalshi doc** | Deposits and withdrawals list USDC only; nothing names Robinhood Chain or USDG. Not assumed. |

`bot/arb_monitor/scripts/kalshi_withdrawal_evidence.py` settles the "unproven" row without moving money. It reads
the engine's ledger and, with `--api`, makes two signed GETs (`/portfolio/balance`, `/portfolio/withdrawals`). It
then reports `confirmed` if an engine-recorded id appears in Kalshi's history as an applied crypto withdrawal,
`contradicted` if Kalshi knows none of the engine's ids, and otherwise `unconfirmed`. A test fails if it ever sends
anything but GET. If it reports `confirmed`, robinhood mode can reuse the same Base call unchanged: Kalshi pays USDC
on Base, Across brings it to USDG, and `ARB_KALSHI_API_WITHDRAW_VERIFIED=true` turns the call back on after one
approved $10 test on MILORD's own account. Until then the recall stays an operator task.

## Still open (owner approval needed for each live step)

1. Run `check-venue-routes.mjs` on the VPS at the hardening commit and confirm the Across quotes now return.
   Decide the Polymarket recall fee limit (every recall is above 50 bps).
2. One approved small Polymarket deposit from 4663 USDG (≥ $2 minimum; suggest $10) to the MILORD Safe's
   deposit address, confirmed by `GET /status/<address>` and the Safe's pUSD balance.
3. One approved small Polymarket recall to the servicer on 4663 (suggest $10), then set
   `ARB_POLY_BRIDGE_ROUTE_VERIFIED=true`.
4. Kalshi: MILORD's own Kalshi account (KYC/entity), its Base USDC deposit address in `KALSHI_BASE_DEPOSIT_ADDR`,
   one approved $10 deposit, one manual crypto withdrawal to the servicer on Base. Daily withdrawal limits are shown in the app.
5. Fill `ARB_BRIDGE_ALLOWLIST` with the Across SpokePool on 4663 and Base (and Relay's depository if Relay stays the fallback).
6. Polymarket Safe/CTF redemption (B21): Polymarket now redeems through `CtfCollateralAdapter`
   `0xAdA1…cE1f` (standard), `NegRiskCtfCollateralAdapter` `0xadA2…6eAab` (neg-risk) or the V2 Router
   `0x1212…2600` (V2 markets), all paying pUSD. The Codex transport calls ConditionalTokens directly with a
   configured collateral, which pays the market's original collateral (USDC.e for pre-V2 markets) and refuses
   neg-risk and V2 markets. It is disabled by default and fails closed, but it must be moved to the adapters
   before it is enabled.

Sources: [Polymarket deposit](https://docs.polymarket.com/trading/bridge/deposit),
[Polymarket withdraw](https://docs.polymarket.com/trading/bridge/withdraw),
[Polymarket supported assets](https://bridge.polymarket.com/supported-assets),
[Polymarket redeem](https://docs.polymarket.com/trading/ctf/redeem),
[Polymarket V2 migration](https://docs.polymarket.com/v2-migration),
[Kalshi crypto deposits](https://help.kalshi.com/en/articles/13823799-crypto-deposits),
[Kalshi crypto withdrawals](https://help.kalshi.com/en/articles/13823804-crypto-withdrawals),
[Kalshi GET withdrawals API](https://docs.kalshi.com/api-reference/portfolio/get-withdrawals),
[Across routes](https://app.across.to/api/available-routes?originChainId=4663),
[Across on Robinhood Chain](https://across.to/blog/bridge-to-robinhood-chain-with-across),
[Paxos USDG addresses](https://docs.paxos.com/guides/stablecoin/usdg/mainnet).
