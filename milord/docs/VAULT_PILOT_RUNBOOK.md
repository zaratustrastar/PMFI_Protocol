# Robinhood vault pilot (controlled real capital)

Goal: run the existing PMFI arbitrage business on Robinhood Chain with the existing `PMFIArbVaultV3`, USDG and
the existing engine, end to end, with a small owner-approved amount:

USDG → pARB deposit → vault `tend` to the servicer → Polymarket (pUSD) and Kalshi funding → matched trade →
settlement / redemption → NAV report → venue withdrawal → pARB redemption for USDG.

Nothing here deploys MILORD, Hookr, NFTs, splitters or the distributor. Every step that moves money or
broadcasts a transaction is marked **APPROVAL** and waits for the owner.

## What was built for it

| Piece | Where |
|---|---|
| Vault-only deploy, hash-gated on 4663, restricted to named depositors, owned by the Safe | `milord/script/DeployVaultPilot.s.sol` (tests: `test/parb/VaultPilotDeploy.t.sol`) |
| Prediction.com feed next to Oddpool (`ARB_OPPORTUNITY_FEED=oddpool\|prediction_com\|compare`) | `bot/arb_monitor/adapters/prediction_com.py`, `adapters/opportunity_feed.py` |
| Polymarket CLOB V2 taker fee in the VWAP gate (per-market `fd.r`, `fd.e`) | `core/venue_fees.py`, `core/arb_executor.py` |
| Public data direct in the pilot (`ARB_PUBLIC_DATA_ROUTE`), malformed proxy URLs ignored with a sanitized log, V2 SDK proxy off by default | `http_client.py`, `core/clob_proxy.py` |
| Polymarket eligibility from the direct-egress geoblock (cached 10 min, fails closed); never via a proxy | `core/venue_eligibility.py` |
| Kalshi orders in fixed-point dollars, fill-or-kill, `fill_count` parsing | `core/arb_executor.py` |
| Shadow mode: separate read-only cycle (feed → live books → fees → sizing → journal) plus a guard at every state-changing choke point | `core/shadow_guard.py`, `core/arb_execution_loop._shadow_cycle` |
| Pilot runner: authoritative env file, inherited engine vars dropped, DB / ledgers / credentials compared with the production env | `bot/arb_monitor/run_pilot.py`, `bot/arb_monitor/pilot.env.example` |
| Read-only proxy/API/account check | `bot/arb_monitor/scripts/venue_connectivity_check.py` |

Unchanged and reused: the V3 vault accounting, reporter (ReportDataV2 bound to vault + chain 4663 + salt
`PMFIArbVaultV3.milord.v1`), liquidity waterfall, withdrawal queue, robinhood funding mode (B9 routes,
allowlists, write-ahead intents, receipts), B21 pUSD redemption, positions database and risk limits.

## Robinhood path audit (no Base assumptions)

* Vault, reporter and funder use 4663 and USDG from env; the reporter refuses robinhood mode with
  `ARB_VAULT_CHAIN_ID=8453` (`core/arb_reporter.py:79`), and a Base-signed report is rejected by the vault
  (`VaultPilotDeploy.t.sol: test_reportSignedForBaseIsRejected`).
* Base USDC appears in robinhood mode only where it is the venue's own requirement: Kalshi deposits and
  withdrawals are USDC on Base (Kalshi lists no USDG), bridged USDG ↔ Base USDC by Across (B9), plus the sweep of
  stray Base USDC back to USDG. The remaining Base constants (`arb_nav.py` V1 NAV signer, LI.FI BSC routes) are
  base-mode or Opinion code that robinhood mode does not call.

## P0 Read-only checks on the VPS (no approval needed)

Do not stop, restart or reconfigure the live PMFI services. From the repository checkout:

```
git fetch origin milord/production-readiness && git checkout <SHA>
python3 -m bot.arb_monitor.scripts.venue_connectivity_check --env-file /path/to/live-engine.env      # current routes
python3 -m bot.arb_monitor.scripts.venue_connectivity_check --env-file /path/to/pilot.env            # pilot accounts
(cd milord && node offchain/check-venue-routes.mjs)                                                  # B9 bridge quotes
```

The check prints JSON with: proxy endpoints (DNS, TCP connect; a malformed URL is a sanitized diagnostic),
the route each engine call takes, the direct Polymarket geoblock, Gamma, CLOB, the V2 SDK's authenticated
balance, Kalshi's signed balance and exchange status, Prediction.com (status, rate-limit headers, signal age)
and the Robinhood RPC (chain 4663, USDG decimals 6). `access` separates, per venue, public data, authenticated
balance and trade execution; `verdict.shadow_ready` needs only public data. Polymarket trade execution is
authorized only when the direct geoblock says `blocked=false`; a proxy result is informational. Exit code
0 = `ready`.

## P1 Vault deployment

1. Private fork rehearsal (no approval): Anvil fork of 4663 on 127.0.0.1, public Anvil key.
   ```
   cd milord
   anvil --fork-url https://rpc.mainnet.chain.robinhood.com --host 127.0.0.1 --port 8549 &
   MILORD_LOCAL_FORK=yes MILORD_OWNER_SAFE=<safe> MILORD_TEAM_SAFE=<safe> MILORD_SERVICER=<servicer> \
   MILORD_REPORT_SIGNER=<signer> MILORD_VAULT_ASSET=0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168 \
   MILORD_VAULT_CAP=<cap, 6 decimals> MILORD_PILOT_DEPOSITORS=<owner wallet(s)> \
   MILORD_DEPLOYMENTS_OUT=./deployments/robinhood-pilot-fork.json \
   FOUNDRY_PROFILE=lowmem FOUNDRY_OPTIMIZER=true FOUNDRY_OUT=out-pilot FOUNDRY_CACHE_PATH=cache-pilot \
   forge script script/DeployVaultPilot.s.sol --rpc-url http://127.0.0.1:8549 --broadcast \
     --sender 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 --unlocked
   ```
   The optimizer is required: the unoptimized lowmem vault is 28,906 bytes, above the 24,576-byte limit; with the
   optimizer and no via-IR it is 16,658 bytes and compiles in little memory (checked on a local Anvil: deployed,
   owner = Safe, depositor allowed). The mainnet broadcast uses the production (via-IR) profile from CI artifacts
   or a machine with at least 8 GB RAM, never a via-IR build on the VPS.
2. Dry run against mainnet without `--broadcast`: prints `vault pilot configuration hash`.
3. **APPROVAL**: owner confirms every address, the cap and the depositors, and sets
   `MILORD_PILOT_APPROVAL=<hash>`. Broadcast with `--ledger` or `--account` (never a key in env).
4. The manifest `deployments/robinhood-pilot-4663.json` gives `ARB_VAULT_V2_ADDRESS`.

## P2 Shadow operation (no money moves)

```
cd bot && python3 -m arb_monitor.run_pilot --env-file /path/pilot.env --check
cd bot && python3 -m arb_monitor.run_pilot --env-file /path/pilot.env           # ARB_EXECUTION_MODE=shadow
```

In shadow mode the cycle never calls the reporter, tend, funding, bridges, withdrawals, approvals, intent
reconciliation, balance reads, the positions database, order placement or cancellation, and no JSON-RPC
`eth_send*` can be sent (`core/shadow_guard.py`; `tests/test_shadow_mode.py`). The only file written is
`ARB_SHADOW_JOURNAL_PATH` (plus `ARB_FEED_COMPARE_LOG` in compare mode), and each row records whether
Polymarket trading is available from this host.

Run on a separate host or as a separate process with its own data dir and database (the runner refuses the live
engine's `~/PolyNotifyBot/data`). Leave `ARB_OPPORTUNITY_FEED=compare` for at least a day:
`arb_feed_compare.jsonl` shows overlap and edge differences between Oddpool and Prediction.com, and
`arb_shadow_trades.jsonl` lists every trade that passed the live-book gate with fees, at its real size.
Switch the primary feed with `ARB_FEED_COMPARE_PRIMARY=prediction_com`, then `ARB_OPPORTUNITY_FEED=prediction_com`
to disable Oddpool.

## P3 Funding round trips (B9), each **APPROVAL**

1. $10 USDG → Polymarket bridge → pUSD in the pilot Polymarket wallet; $10 pUSD → USDG back. Then
   `ARB_POLY_BRIDGE_ROUTE_VERIFIED=true` and the observed recall fee decides the recall fee limit.
2. $10 USDG → Across → Base USDC → Kalshi deposit address; Kalshi withdrawal to the servicer on Base **in the
   Kalshi app** (no documented withdrawal API: the engine logs an operator task and keeps the amount in transit
   until the Base balance delta arrives); Across back to USDG.
3. Fill `ARB_BRIDGE_ALLOWLIST` with the bridge contracts those transactions used.

## P4 Vault capital, each **APPROVAL** (amounts set by the owner)

1. Owner wallet (an allowed depositor): `USDG.approve(vault, X)`, `vault.requestDeposit(X, owner)`.
2. The engine's reporter tick signs and submits `report()`, which processes the deposit at the official PPS;
   claim pARB with `claimDeposit` (or the engine's `autoClaimDeposits`).
3. `tend()` moves idle USDG above the buffer to the servicer.

## P5 First live trades, **APPROVAL**

Set `ARB_EXECUTION_MODE=live` with `ARB_MAX_PAIR_USDC=25` and `ARB_MAX_DEPLOYED_USDC=<pilot amount>`. The executor
funds both legs for the selected trade (USDG → Polymarket bridge, USDG → Across → Kalshi), re-verifies both live
books, places the Polymarket FOK and the Kalshi fill-or-kill orders, and persists the position.

## P6 Settlement and return of capital

* Polymarket: after resolution, B21 redemption (`MILORD_CTF_MARKETS_FILE` per pair; first approval transaction
  and first redemption **APPROVAL**, then `ARB_CTF_ROUTE_VERIFIED=true`). pUSD → USDG by the Polymarket bridge.
* Kalshi: settles to cash automatically; withdrawal is the manual app step above.
* The reporter includes venue cash, open positions at cost, in-transit bridge amounts and servicer USDG in NAV.

## P7 Redemption, **APPROVAL**

Owner `requestRedeem(shares, owner)`; the next report processes it from the idle buffer (the engine recalls venue
cash when the buffer is short); `claimRedeem` returns USDG. Compare USDG out with USDG in and the trade log.

## Stop conditions

`vault.setPaused(true)` (owner Safe), `ARB_EXECUTION_MODE=shadow` and restart the pilot runner, or stop the
runner. In-transit amounts are in the pilot ledgers; restore them before restarting (intents reconcile on start).
