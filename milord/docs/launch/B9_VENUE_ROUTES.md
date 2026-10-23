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

## Still open (owner approval needed for each live step)

1. Run `check-venue-routes.mjs` on the VPS and record the quotes here.
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
