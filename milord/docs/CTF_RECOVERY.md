# MILORD Safe + CTF recovery (B21)

Implemented for the existing binary CTF position model, with isolated MILORD custody.
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
- `MILORD_CTF_COLLATERAL` identifies the verified six-decimal direct CTF collateral.
- `MILORD_CTF_MARKETS_FILE` maps each pair ID to `conditionId` and `collateral`.
  The derived YES position ID must match the existing tracked token ID. Side comes
  from the existing complementary Kalshi/Polymarket position model.
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

## Route blockers remain

This transport currently supports **direct CTF redemption only**, not collateral
adapters, negative-risk adapters, or Protocol V2 positions. Current Polymarket docs
separately describe CTF/pUSD collateral adapters and Protocol V2 positions:
https://docs.polymarket.com/trading/positions/manage and
https://docs.polymarket.com/v2-migration . The former legacy direct USDC.e route is
not evidence that USDG deposits or current pUSD redemption work with this transport.
If the verified MILORD deposit creates CTF positions requiring an adapter, add a
small collateral/redemption adapter preserving this position model and journal.
If it necessarily creates Protocol V2 positions, document that incompatibility and
obtain an owner decision before changing the engine.

Kalshi settlement evidence uses its existing account authentication and exact
portfolio settlement counts/revenue (cents), not a market-price heuristic:
https://docs.kalshi.com/api-reference/portfolio/get-settlements . Live API/receipt
fixtures and independently verified dedicated account configuration remain required.
Mocks validate recovery behavior; they do not certify venue integration or audit readiness.
