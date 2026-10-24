# Blocker matrix update (2026-10-23, production-readiness round on milord/production-readiness)

Built on Codex PR #4 (`4982bdb`). No deployment, broadcast, venue order, Safe signature or real funds.

## Vault pilot round (2026-10-24, docs/VAULT_PILOT_RUNBOOK.md)

B21 closed: Bobo's VPS Polygon fork run passed 6/6 at `f2ee9dba`. The vault-only pilot (existing PMFIArbVaultV3,
USDG on 4663, no MILORD/Hookr/NFTs/distributor) has its deploy script, Prediction.com feed, exact venue fees,
Kalshi current-API orders, V2 SDK proxy routing, shadow mode and an isolated pilot runner. Open for the pilot:
VPS connectivity check, owner approvals (vault deployment hash, $10 route round trips, deposit, live mode),
the pilot's own Polymarket wallet and Kalshi account, and a Prediction.com Dev+ API key.

| # | Severity | Item | Status | What clears it |
|---|---|---|---|---|
| B9 | High | Venue and bridge routes for Robinhood USDG | **Routes confirmed in official docs and route APIs** (B9_VENUE_ROUTES.md): Polymarket bridge USDG(4663) ↔ pUSD both ways; Across USDG(4663) ↔ Base USDC both ways; Kalshi deposits USDC on Base, withdrawals only in the Kalshi app. Robinhood-mode recall now uses the Polymarket bridge (exact pUSD amount, gated) and a Kalshi operator task. 2026-10-23: Across quote transport fixed (GET without a body); failures classed as route/API/fee; at the 50 bps limit every observed Polymarket recall (56–81 bps) and the $10 deposit are refused; the engine's Kalshi API withdrawal is undocumented and unproven, checked read-only by `kalshi_withdrawal_evidence.py`; Kalshi lists no USDG | VPS `node offchain/check-venue-routes.mjs` at the hardening commit; owner decision on the Polymarket recall fee limit; Kalshi evidence run on the PMFI host; owner-approved $10 Polymarket deposit and recall; MILORD's own Kalshi account and one $10 round trip; real addresses in `ARB_BRIDGE_ALLOWLIST` |
| B21 | High | Polymarket redemption | **Closed 2026-10-24: VPS Polygon fork 6/6 at `f2ee9dba`.** Implemented 2026-10-23: the transport redeems through CtfCollateralAdapter (standard CTF), NegRiskCtfCollateralAdapter (neg-risk CTF) and the V2 Router (PositionManager positions), all paying pUSD, with the system declared per pair and every id re-derived on chain; exact receipt evidence per system, approval gate, zero-payout and shared/partial/migrated refusals (CTF_RECOVERY.md). No position-model change was needed. Disabled by default; `ARB_CTF_ROUTE_VERIFIED` unset | VPS `./fork/polygon-redemption-fork.sh`; markets file written from each pair's `negRisk`/`version`; owner-approved approval tx and one small redemption |
| B22 | High | Base withdrawal script | Codex fixed exact amounts. Review finding: the same script still moves USDC.e, not pUSD, and its new $5–$5,000 bound and removed retries also change the LIVE Base path once deployed to the PMFI host | owner decides when the Base engine takes the new script; robinhood mode no longer uses it |
| B26 | High | Integrated launch rehearsal | **Passed on the VPS at `96f7dad89e3d0b227fa22570d965fef3d75f6927` (2026-10-23)**: `./fork/rehearse-full-launch.sh`, LAUNCH and OPERATE phases, on a private Robinhood fork: every launch gate (G1–G7) with the real scripts and Safe batches, then trading, keeper, conversion, report, weekly root and claim | Re-run at the release SHA |
| B27 | Medium | Mainnet deploy approvals | **Code**: the three deploy scripts accept 4663 only on a private fork (Anvil #0) or with `MILORD_MAINNET_APPROVAL` = the owner-approved configuration hash (MAINNET_RUNBOOK.md §1–2) | owner approvals at launch time |
| B23 | Medium | Hosts | open; testnet/fork-soak guide, backup and restore scripts ready (TESTNET_SERVICES.md) | owner approves sizing; testnet week; restore drill |
| B6 | High | Independent security review | open | auditor engagement |

# Blocker matrix update (2026-10-23, autonomous pipeline round)

B13 is **closed**: VPS run at 406cc2a, 33/33 in both passes, pre-emption regression green; PR #1 fast-forwarded.
Per-component evidence: READINESS_MATRIX.md. New and changed items:

| # | Severity | Item | Status | What clears it |
|---|---|---|---|---|
| B21 | High | Polymarket redemption: superseded by the 2026-10-23 row above (pUSD adapters and V2 Router implemented, fork test pending) | open | VPS Polygon fork run |
| B22 | High | `trading_bot/safe_proxy_withdraw_parb.ts:655` bridges the whole EOA balance instead of the requested amount | open (Python records the measured amount) | fix the script; small approved test |
| B9 | High (was Medium) | Funding/withdrawal routes: code hardened (allowlists, receipts, intents, delta arrival), routes still unverified | Code | Across/Relay USDG on 4663, Polymarket USDG deposit, Kalshi Base legs confirmed with approved small amounts; fill `ARB_BRIDGE_ALLOWLIST` |
| B7 | Medium | WETH/USDG pool for the router | Code: `offchain/check-funding-pool.mjs` (read-only) | VPS run exits 0 |
| B23 | Medium | No hosts: milord-auto, milord-engine, milord-watch (OPERATIONS_PLAN.md) | open | Bobo approves sizing; provision; testnet week |
| B24 | Low | V3 reporter getter names checked against source only | open | first report on a testnet V3 vault |
| B25 | Low | Display NAV endpoint (`/api/arb-vault/nav`, base mode) still treats read errors as 0 | open | not used for signing; fix with the base engine owner |

# Blocker matrix (2026-10-21, production-readiness round)

No deployment, broadcast, lock or real funds. The Anti-Snipe 40% / 60-minute setting is a candidate, not a decision.
Status key: **Code** = implemented and tested here; **Fork** = run on the VPS against real Hookr-1; **Fork pending** =
compiled, waiting for the VPS run; **Ops** = operational step or assumption; **Owner** = needs Bobo's decision.

| # | Severity | Item | Status | What clears it |
|---|---|---|---|---|
| B13 | Critical until the VPS confirms | Pool squatting (fork-confirmed for a deployed token) | Code: `MilordAtomicLaunch`. The 2026-10-21 VPS failure was a test bug (expectRevert consumed by a view call in the value expression); source shows Hookr refuses codeless tokens in the launcher and again in HookrLane. Regression tests now prove the attack on a deployed token and its refusal on the predicted address | VPS run of the five `test_preemption_*` tests |
| B15 | Medium | Launch encoding | Code: `safe-launch-batch.mjs` encodes the atomic launch, accept and lock batches | VPS run, then testnet rehearsal |
| B14 | High | Tax is pulled, not pushed | Code: keeper pays both queues, fails closed, idempotent (services README) | keeper run against a launched pool (fork or testnet) |
| B16 | High | Principal withdrawable between `principalLockedUntil` and the lock | Ops: LOCK_PROCEDURE.md (pre-signed lock, `lock-watch` alerts, late lock still works) | Bobo's go-ahead to execute the lock; signatures collected right after the launch |
| B17 | High | Hookr-1 bytecode vs published source | Code: `hookr-verify/`; offline phase passes for all 23 contracts MILORD touches (settings, sources, metadata, 4 libraries by keccak); no upgradeable proxies (HOOKR_BYTECODE_VERIFICATION.md) | VPS on-chain phase; check the two tax-queue clones after launch |
| B20 | Medium | Only HookrDirectionalTax A (0xE73f…1e1F) fits: B (0x9901…5d3c) is HookrPartnerTax, for HookrAttributionLauncher pools only | Code: the helper takes the tax address as config; use A | confirm A is admitted under the root at launch time (the helper refuses otherwise) |
| B18 | Medium | Robinhood `block.number` cadence (guard and lock lengths) | Code: clock probe; Fork pending: no-ArbSys-stub pass (CLOCK_AND_ARBSYS.md) | run the probe; set guard/lock from its numbers |
| B19 | Medium | Option D split is off-chain | Code: reconciliation + Safe batch from events; Fork pending: event check | only if the Anti-Snipe candidate is chosen (Owner) |
| B6 | High (pre-mainnet) | Independent security review | Owner: scope in AUDIT_SCOPE.md | auditor engagement |
| B7 | Medium | WETH/USDG V3 pool depth and cardinality on 4663 | open | read the pool; raise cardinality if low |
| B8 | Medium | Second ETH/USD reference | Owner | TWAP only, or TWAP + feed |
| B9 | Medium | Polymarket/Kalshi USDG funding routes, bridge allowlist | Owner / engine team | confirm routes |
| B10 | Medium | Safe signer sets, thresholds, router bounds | Owner | confirm values |
| B11 | Low | Art, metadata, website | Owner | content |
| B12 | Low | `finalized` tag on Robinhood RPC | open | probe the RPC |
| — | Owner | Anti-Snipe: flat launch or the 40%/60 min candidate (with Option D or 100% burn) | Owner | decision; both paths are implemented |

Closed by the 2026-10-21 fork run: B1 (flat 1.5% tax config), B2 (20% commission), B3 (buyback through Hookr works),
B4 (lock semantics), B5 (VPS forks 4663).

# History: status update (2026-10-21, fork suite)

The VPS can now fork Robinhood Chain, and `test/fork/` tests B1–B4 against the deployed Hookr-1 contracts
(docs/launch/FORK_INTEGRATION.md, one command: `./fork/run-fork-tests.sh`). Until that run reports green:

| # | Status |
|---|---|
| B1 | Resolved from source, fork run confirms: Tax + Conversion is 0xE73f…1e1F / 0x9901…5d3c; config is `abi.encode(Config)` (320 bytes), flat 1.5% from the launch block with no launch-tax decay |
| B2 | Resolved from source, fork run confirms: commission = `protocolShareBps` 2000, applied as 20% of the tax (queue split) and 20% of any Snipe; no Hookr minimum fee asserted |
| B3 | Fork run decides: `test_buybackAndBurnThroughHookr` buys through V4SwapAdapter (unauthenticated) |
| B4 | Resolved from source, fork run confirms: `lock` performs `acceptFamily`; forever = 2^64-1; `collect` pays the beneficiary directly; `claims = 0` pays tokens |
| B5 | Resolved by the VPS (forks 4663 locally); this environment still cannot |
| New B13 | High: an outsider may open the ETH/MILORD Hookr pool first (ETH-only, no MILORD), making our launch revert. Fix: deploy MILORD and launch atomically. `test_poolSquattingRisk` confirms or rules it out |
| New B14 | High: the tax is pulled, not pushed. The keeper must call `payCreator()` on both tax queues before `splitter.distribute` |
| New B15 | Medium: `offchain/safe-launch-batch.mjs` must encode `launchFamily` with the fork-tested parameters |

# Remaining blockers, most severe first (2026-10-14)

| # | Severity | Blocker | Who can clear it | What clears it |
|---|---|---|---|---|
| B1 | Critical (launch) | Hookr Tax + Conversion advisory address and config encoding are not published; the launch call cannot be encoded, and the flat 1.5% from trade one with no snipe/guard surcharge cannot be proven | Hookr / Bobo | Hookr publishes the advisory address + config schema, then a Robinhood fork run (HOOKR_VERIFICATION.md) |
| B2 | Critical (launch) | Hookr commission of 20% not confirmed on the deployed contracts (field unclear: `protocolShareBps` vs advisory `quoteTakePips`) | Hookr / fork test | fork run measuring the split of the first trades |
| B3 | High | HookrRoot may refuse or treat differently unauthenticated swaps; BuybackBurner's V4SwapAdapter swaps directly on PoolManager | fork test | if refused: a HookrFamilyRouter adapter (BuybackBurner already supports a timelocked adapter change) |
| B4 | High | Lock details: exact "forever" `unlockBlock`, `transferFamily` `claims` argument, whether `lock` performs `acceptFamily`, whether `collect` pays `msg.sender` or holds ERC-6909 claims | fork test | fork run steps 4-5; HookrLockFeeCollector already handles both payout forms |
| B5 | High | No fork testing is possible from this environment: RPC, explorer and Sourcify API hosts are blocked by the network proxy | workspace admin | allow `rpc.mainnet.chain.robinhood.com`, `robin.etherscan.io`, `sourcify.dev` in a custom cloud environment (Project settings → environment) or run the fork script locally |
| B6 | High (pre-mainnet) | Independent security review of all new contracts (router, references, venue, collector, converter, splitters, distributor, vault V3 diff) | Bobo | auditor engagement; nothing is production-ready before it |
| B7 | Medium | WETH/USDG Uniswap V3 pool on Robinhood Chain: address, depth and observation cardinality not verified; the router's TWAP needs >= 30 min of history and enough cardinality | fork / on-chain read | read the pool; call `increaseObservationCardinalityNext` if low (anyone can) |
| B8 | Medium | No Chainlink ETH/USD feed confirmed on 4663: the router runs on the V3 TWAP alone unless a second reference is added (DualReference is ready) | Bobo | choose: TWAP only, or TWAP + feed |
| B9 | Medium | Funding bot (Polymarket/Kalshi): Across/Relay support for 4663 and a Polymarket USDG deposit route are unconfirmed; bridge deposit contracts are not allowlisted | Bobo / engine team | confirm routes; add a bridge-contract allowlist before live funds |
| B10 | Medium | Safe signer sets and thresholds for Team Safe and Arbitrage Funding Safe; router production bounds (SAFE_CONFIGURATION.md §3) | Bobo | confirm values |
| B11 | Low | Art, metadata, characteristic names, website hosting | Bobo | content |
| B12 | Low | Robinhood Chain `finalized` block tag support for the epoch publisher (falls back to 64 confirmations) | fork / docs | check RPC |

Not blockers (done and tested locally): permanent-lock fee path, LP fee burn, automation router with caps / TWAP / pause / sweep-to-owner, keeper / epoch publisher / root checker services with failure tests, funding bot USDG mode, Safe batches (lock batch prepared, not executed).

## Codex update (2026-10-23)
B7 read-only preflight corrected and passed at block 95,960,925: WETH proxy calls,
factory identity, 30-minute TWAP and tick-crossing QuoterV2 simulations for
0.05/0.25/1/2 ETH. Execution funding fork evidence is tracked separately. This does
not clear B9 or guarantee future liquidity. Atomic recorder and rehearsal now
fail closed on inconsistent data and distinguish incomplete service configuration.
B22 exact-amount code fix and 17 deterministic regressions implemented. Still needs
review of the shared Base withdrawal path and an explicitly approved venue test;
current pUSD collateral conversion is not proven by these tests.

B5 is resolved for this local Codex environment: read-only public Robinhood access
and private forks worked. Historical state is pruned; archival replay is not assured.
New real-contract funding tests passed 3/3. B21 now has offline-tested direct CTF
recovery and NAV guards, but remains **live-integration blocked** pending dedicated
custody/account verification and the actual collateral/adapter/redemption route.
B9 and B6 remain open. Optimized launch rehearsal validates launch evidence, not
full service deployment; missing StateView/service configuration is still explicit.
