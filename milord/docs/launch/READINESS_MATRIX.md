# MILORD V1 autonomous pipeline: readiness matrix (2026-10-23)

**Verdict: not production-ready.** The fee → USDG → pARB vault → engine → signed NAV → weekly NFT allocation loop
is implemented and tested end to end on a local chain, and the protocol side is fork-tested against real Hookr-1. It
is blocked on unverified venue and bridge routes, a missing Polymarket CTF redemption, an unaudited codebase, and
infrastructure that does not exist yet. Unit tests passing is not readiness.

Evidence at the staging commit (this machine, and CI on push):
- Foundry: 179 passed, 4 skipped (default via-IR profile and `lowmem`).
- Engine: `python3 -m unittest discover -s bot/arb_monitor/tests` → 143 tests, OK, 2 expected failures. Both are legacy
  ledger rows without a balance snapshot, and both are documented.
- Services: `node --test offchain/services/test/*.test.mjs` → 29/29.
- Simulation: `./sim/run-local.sh` → 46/46 checks (was 39); `./sim/run-entry-price.sh` → 19/19.
- VPS fork run at 406cc2a (Bobo, 2026-10-23): 33/33 in both passes, Hookr bytecode matched 23/23.

Columns: **Impl** = code exists. **Integ** = wired into the deploy scripts and the services that will run it.
**Tested** = with the evidence named. **Ops** = configured on real infrastructure. **Blocked** = what stops it.

| # | Area | Impl | Integ | Tested (evidence) | Ops | Blocked by |
|---|---|---|---|---|---|---|
| 1 | Splitters pay **ArbitrageFundingRouter** (not a Safe) | yes | yes: `DeployRobinhood.s.sol` now always deploys UniV3SwapVenue, UniV3TwapReference and the router, and asserts the router is payee of the entry, royalty and tax splitters | `RobinhoodDeployment.t.sol::test_deployScriptOnTestnetFromEnv` (payee positions, adapter operator, router owner, pauser, bounds, publisher ≠ keeper); sim | no | B7: WETH/USDG pool (`check-funding-pool.mjs`, read-only). B10: router bounds |
| 2a | NAV reporter: one gross figure, each bucket once | yes: V3 getters, fail-closed reads, integer raw units, one pinned block, positions at cost until the payout is visible, pending/claimable deductions left to the vault | yes (engine loop) | 66 tests in `test_reporter_invariants.py` and `test_reporter_fixes.py`: buckets, transfers between buckets change nothing, read errors abort, H4 rounding; `ReporterCompat.t.sol` fixture | no | V3 getters were checked against source only, not against a deployed or forked V3; testnet vault needed |
| 2b | Capital transfers create no profit or loss | yes | yes | Python invariants (vault↔servicer, servicer↔venue, bridges in transit); sim checks "capital moved between the servicer and the vault…" and "gross NAV unchanged by the liquidity return" | — | — |
| 3 | USDG funding and withdrawal (Polymarket, Kalshi) | yes: token/chain/spender/`to`/recipient allowlists, exact approvals, fee/slippage bounds, HTTP timeouts and retries, receipts, write-ahead intents, delta-based arrival, ledger locking | yes | 57 tests with fakes (allowlists, receipts, crash recovery, lock races) | no | **B9**: Across/Relay support for USDG on 4663; Polymarket USDG deposit on 4663; Kalshi Base deposit and withdrawal; real allowlist addresses. **B22**: `safe_proxy_withdraw_parb.ts:655` bridges the whole EOA balance (reported, not edited; Python records the measured amount) |
| 3b | Opinion disabled for V1 | yes: filtered before pre-warm; off in robinhood mode whatever the flag | yes | `test_funding_execution.py` | — | — |
| 4a | Engine deploys vault capital and runs hedged trades | unchanged live PMFI execution code; new liquidity gate blocks trading when the state is unknown | yes | gate tests; execution itself is the live base-mode engine (production history, not new tests) | no | B9 routes |
| 4b | Settlement and fund recovery | partial: `open → pending_redeem → redeemed`; reports refused while a Kalshi win is unpaid or a position is 6 h past expiry | yes | `test_reporter_fixes.py` | no | **B21**: Polymarket CTF redemption and payout observation raise `NotImplementedError`; payouts are recorded by hand (`mark_position_redeemed`) |
| 4c | Return liquidity for redemptions | yes: waterfall (servicer sweep → venue recalls, no duplicate recalls, cooldown), refill journaled | yes | sim step 27: 56 redemptions, 513 USDG needed against 103 idle, 7 processed on the first report, the engine returns 411 USDG, all paid shares × PPS; Python C4 tests | no | B9 (venue recalls) |
| 5 | Keeper, reporter, funding adapter, epoch publisher, root checker: integration and recovery | yes: tick watchdog, tx timeout + nonce reset, chunked log reads from `startBlock`, startup retry; engine intents reconcile on start | compose with per-service env files, separate keys | services tests (watchdog, timeouts, RPC outage, keeper down for a week, reporter down); sim step 28 (restart with empty state repeats nothing); engine crash tests (`test_funding_recovery.py`) | no hosts yet | testnet week; restore drill (OPERATIONS_PLAN §6) |
| 6 | End-to-end rehearsal | yes: Hookr fees, mint fees, royalties, ETH→USDG, deposits, pARB, signed NAV, weekly roots, claims, NFT transfers, redemptions, liquidity return, restarts | local anvil: real MILORD/vault/v4/v3 contracts | 46/46 | — | Mocked: the Hookr tax hook and launcher, USDG, venue P&L. Real Hookr is covered only by the fork suite. No run yet covers launch → tax queue → splitter → router → vault on a fork in one pass |
| 7 | Monitoring | yes: `monitor.mjs`. Gas, NAV staleness, PPS moves, vault paused, redemptions waiting, stuck splitter/router/tax queue, stale price, unsettled batch, missed epoch, lock deadline, unconfirmed bridges (engine `/bridges`), service health and failed ticks | compose service, read-only | `monitor.test.mjs` (6 cases + runtime) | no webhook, no host | ops |
| 8 | Deployment and operations plan | OPERATIONS_PLAN.md | — | — | not provisioned | hosts, secrets, Bobo's sizing approval |
| 9 | Atomic launch tooling | `DeployAtomicLaunch.s.sol` (local fork only, flat launch, Anti-Snipe refused), `safe-launch-batch.mjs`, `record-launch.mjs` (read-only) | yes | compiles; contract fork-tested 33/33 | — | VPS fork rehearsal of the script; launch and lock approvals |
| 10 | Quote server | testnet/mainnet mode added (deployments file, signer env, head-lag check) | compose: not yet (runs as its own process) | 19 entry-price checks; smoke run | no | KMS signer, TLS front |

## Documentation versus executable scripts (fixed in this round)
| Doc | Was | Now |
|---|---|---|
| MAINNET_RUNBOOK §0–4 | token minted to an allocation Safe, a manual pool seed, an Arbitrage Safe swapping by hand, weekly manual `fund` | atomic launch, the router and keeper do the weekly loop, record-launch, services |
| DEPLOYMENT_ORDER Phase C | allocation Safe launches through the Hookr UI | `DeployAtomicLaunch` → `DeployRobinhood` (predicted token) → Safe batches |
| SAFE_CONFIGURATION §4.3 | "Allocation Safe approve + launch (BLOCKED)" | Team Safe `MilordAtomicLaunch.launch` |
| MAINNET_LAUNCH_CHECKLIST | 139 tests / 36 checks; items now done | current counts; links to the go/no-go list |
| VPS_DEVELOPMENT | branch `milord/v1-protocol`, 39 checks | staging branch `milord/autonomy`, 46 checks |
| ROBINHOOD_VAULT_MIGRATION §3–4 | "code needs changing" table, short env list | done items marked; robinhood engine env list |
| services README / compose | one shared `.env`, three services, default file `sim/deployments.testnet.json` (missing) | per-service env files, four services, `deployments/robinhood-46630.json` |
| — | no script deployed MilordAtomicLaunch; nothing recorded Hookr addresses for the services | `DeployAtomicLaunch.s.sol`, `record-launch.mjs` |

Still not executable from the repo: venue-route verification (needs small real transfers, which need approval);
CTF redemption; provisioning the hosts.

## Codex hardening evidence (2026-10-23)

Starting point: 5ef8492. The handoff's VPS 33/33 Hookr tests in both modes at block
95,946,910 are prior evidence, not rerun here. Corrected funding preflight passed
read-only at block 95,960,925: proxy WETH calls with nonzero sender, factory/pool
identity, decimals, history and all four QuoterV2 simulations against the exact
integer router minimum. 2 ETH quoted 4,797.831415 USDG vs minimum 4,750.049672.
Quotes are snapshots, not execution or venue-route evidence.

New offline regressions: 24/24 (funding check, launch recorder, rehearsal failures).
Existing offchain tests: TWAP 9 groups, Option D 4, eligibility passed, services 29/29.
Recorder exit 0 means launch evidence validated and required service fields present;
exit 2 records validated launch evidence with explicit missing configuration;
exit 1 refuses inconsistent evidence. This is not a production-readiness certificate.
The stand-in atomic rehearsal intentionally exits 2 because it does not deploy the
services' protocol contracts. It retains a JSON record alongside its log.

B6 independent audit, B8 price-reference decision, B9 venue/bridge validation,
B10 owner parameters, infrastructure, testnet week and restore drills remain open.

B22 code correction: the withdrawal CLI now refuses insufficient proxy funds instead
of silently reducing the request; takes before/after raw EOA balances; bridges only
the exact requested delta; checks quote input and exact allowance; refuses uncertain
submission retries. Existing EOA funds are excluded. 17 offline boundary regressions
pass. This changes shared withdrawal safety in Base and Robinhood modes; no live
withdrawal was run. Legacy USDC.e routing remains unverified for current pUSD wallets.
