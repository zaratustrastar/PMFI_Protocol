# PMFI → MILORD migration

**Implemented locally; disabled by default. No production files, Nginx routes, contracts, Safe transactions or public-chain transfers have been changed.** This directory is an isolated, additive feature on `feature/legacy-pmfi-migration`, based on protocol branch HEAD `ff1b23efa2eddd773eb3d3a751ac95c3ee1b5c83`.

## What is delivered

- Scoped `/migrate` page, matching the live landing's navy/mint colors and PMFI typography; no homepage replacement. Direct token transfer, balance/MAX, gas estimate, explicit address/terms review, network checks, receipt feedback and returning-wallet ledger history.
- Existing `window.pmfiPrivy.connect()` integration returning `{address, ethereumProvider}`. No invite gate, new wallet SDK, approval/spender, private key or deposit signature. The only signature is explicit payout-address authorization.
- Node 24 service using ethers 6 and built-in SQLite (WAL, FULL synchronous). RPC-backed canonical Transfer events and receipts; incremental/backfill indexing, retry/fallback RPCs, confirmation threshold, UTC block-time cutoff, reorg rollback with orphan audit, and block-specific Safe balance reconciliation.
- EOA signature verification and EIP-1271 verification on the source chain. Different destinations require a second signature proving recipient EOA control. Base contract wallets must choose a controlled EOA: same-address cross-chain Safe deployment is never assumed. Supporting destination contract wallets requires a separately reviewed destination-chain verification path.
- OS-protected CLI CSV/JSON exports; exact largest-remainder proportional allocation; settlement arithmetic; immutable allocation audit; read-only verification of later finalized MILORD distribution events. No automated selling, buying or distribution.

## Audit findings and unfinished integration

Read [AUDIT.md](AUDIT.md). The live landing is a separate compiled React build; repository `frontend/` is not its source. The owner verified `/root/pmfi` and `/var/www/pmfi` on `147.182.206.35`; our trusted host-key check matched but SSH authentication failed. Consequently **the actual landing source, build scripts and current Nginx configuration could not be inspected**. No source integration or exact production filesystem modification has been pretended.

The module can be mounted by the three scoped locations in `deploy/nginx.locations.conf`, preserving the homepage. Final source integration (e.g. navigation link and reusable React wrapper) and the Privy `pmfi.cc` origin allowlist must be verified on the VPS. Cross-subdomain login is not assumed. The reference screenshot mentioned in the brief was not attached; the page uses the requested structure and live PMFI visual tokens.

## Local build and tests

Use Node **24+** (Node 18 on the machine is insufficient for built-in SQLite). Independent package installation avoids changing the protocol repository's existing dependencies.

```sh
cd migration
npm ci --ignore-scripts
npm test
npm run build
npm start
# http://127.0.0.1:8787/migrate — disabled, no dates/addresses/counters invented
```

The default build omits the existing wallet bridge. For a wallet-enabled staging build, copy the currently reviewed PMFI bridge as a build input:

```sh
PMFI_PRIVY_BRIDGE=/path/to/reviewed/privy-bridge.bundle.js npm run build
```

This is the existing public app asset, not a newly configured Privy app. Its bytes are not committed here. Confirm the current version and authorized origin before live use. Loading it on `pmfi.cc` does not share app.pmfi.cc authentication automatically.

Actual local-only EVM/browser test (Foundry/Anvil and Chrome required):

```sh
forge build --root test/evm
ANVIL=/path/to/anvil CHROME=/path/to/chrome node test/e2e.mjs
```

Fixtures in `test/evm/` are only mock tokens/Safes for local chain 31337. Do not deploy them publicly. E2E starts its own Anvil on 18545 and HTTP server on 18787, uses an isolated browser and shuts them down. All fixture balances and screenshots with `local` in their name are test data. `SCREENSHOT_DIR` overrides the output directory.

## Configuration and activation gates

Copy `config.example.json` outside the public directory. Keep `mode: disabled` until configuration is reviewed. `mode: local` accepts only chain 31337. `mode: live` requires Base 8453, destination 4663, exact addresses and hashes, Safe owners/threshold, token-accounting review, policy URL, valid UTC times and an explicit approval record.

Owner must approve:

1. Official Base PMFI token address and its transfer behavior. The audit's candidate is NOT an approved address.
2. Base receiving Safe address, owners, threshold (at least 2), proxy runtime and singleton implementation code hashes; review Safe modules/guards independently.
3. Start/end ISO UTC timestamps, inclusive start/exclusive end, and the backfill `startBlock` at or before the first permitted deposit. Use a dedicated receiving Safe, preferably with zero starting balance.
4. Published terms: sale/acquisition process, deductions, late/early transfers, exceptions and refund policy; no guaranteed rate or reserve.
5. Confirmation threshold (example 64, an operational proposal), reliable server RPC providers, archival access for startBlock and receipt/log availability.
6. Payout terms, final verified MILORD token and decimals, settlement record and execution approval later.

Required hashes are `keccak256(eth_getCode(address))`. Safe `getOwners`, `getThreshold`, `masterCopy` and singleton code must match at runtime. For proxy tokens, bytecode hash alone does not pin implementation: independently review the implementation/admin/storage slots and do not activate this version for upgradeable/rebasing/nonstandard accounting until its monitoring is extended. `tokenAccountingReview: net-transfer-events-no-rebase` is an explicit reviewed assertion, not automatic token-behavior detection. The balance reconciliation stops eligibility publication on mismatch; it is not a substitute for source review.

After explicit owner approval, create a private approval JSON with `ownerApproved: true`, a nonempty evidence `reference`, and `configHash` from `node src/cli.mjs fingerprint` for the **exact live config**. Set `MIGRATION_APPROVAL` to this file. Do not generate or sign an approval on the owner's behalf. Any config-byte change requires reapproval. Mode alone cannot enable live deposits. Approval files and RPC credentials stay outside web roots.

A stale/unhealthy index, wrong network, missing binding, closed window or mismatched contract/Safe config disables the form. Disabling the UI cannot prevent direct transfers to the Safe; the indexer continues recording them. Eligibility uses block timestamps and net incoming events, never browser amounts or click time.

## Ledger behavior and limits

Each event is keyed by chain + transaction hash + log index. Stored fields include token, sender, recipient, exact amount, block number/hash/time, orphan status; confirmations and eligibility are derived from the persisted canonical head. Binding, wallet allocation and distribution references are joined into exports. Multiple deposits aggregate exactly. Outgoing Safe transfers are indexed for balance reconciliation, never as contributions. Mint/self/zero-value transfers require review; early and late deposits remain separately visible.

Indexing begins at startBlock, with a balance/hash baseline at startBlock-1. Each fully verified block commits atomically. Reorgs mark old events orphaned and rescan to the current tip. A reorg crossing the baseline fails closed and requires a reviewed rebuild with a new database and preserved audit. Any reorg after allocation invalidates the manifest until reviewed; never distribute an invalidated allocation. The frozen allocation is not automatically rewritten.

The initial implementation deliberately scans bounded blocks with receipt checks, favoring correctness. For large historical backfills it may be slow: run before activation, measure provider throughput and lag, and keep the UI disabled until caught up. No real mainnet RPC load/uptime test or actual PMFI behavior test has been claimed. The public Base endpoint was used only for read-only candidate metadata verification; production needs selected dependable RPC providers.

One indexer process owns a database. Do not run CLI `index` concurrently with the service. SQLite is local persistent storage, not a public DB server. Export and allocate should run in an operator maintenance window with no concurrent indexer/binding writes. Protect files with OS permissions; take consistent SQLite backups, not copies of only a live WAL database's main file. HTTP exposes no admin mutation/export endpoint. Signatures and full audits belong in protected exports, not a public web root. Public wallet history is already public on-chain information.

A browser refresh/reconnection retrieves indexed history. Pending transaction hashes are displayed in-session, but are not proof of deposit and are not accepted by an ingestion endpoint. After a wallet receipt timeout, inspect the explorer before resubmitting. A mined transfer may still miss the cutoff and become late-review.

## Admin exports, settlement and future distributions

Set `MIGRATION_CONFIG`, `MIGRATION_DB`, and server-only RPC environment variables. Export to a new private prefix (existing files are never overwritten):

```sh
node src/cli.mjs export /private/report/migration-YYYYMMDD
```

JSON contains config fingerprint, canonical tip, reconciliation, exact totals, bindings, deposit events, allocation/distribution records and audit. CSV contains individual deposits. `walletAllocation` is a **wallet total repeated for reference**, not a per-deposit allocation; never sum that CSV column. `amount` is always integer PMFI base units.

After the cutoff itself has required confirmations, freeze bindings (`bindingFrozen: true`), stop public writes in an operator window, back up and run a fresh sync, then prepare `settlement.example.json` from actual sales and purchases. Sale records: `{tx, pmfiUnits, proceedsUnits}`. Acquisition records: `{tx, milordUnits, proceedsSpentUnits}`. All amounts are integer strings; use one reconciled proceeds asset. PMFI sold + remaining must equal eligible PMFI. Proceeds spent + deductions + remaining must equal proceeds. Acquired MILORD minus deductions must equal the distribution pool. Trade amounts are explicitly **owner-attested**, not independently inferred from swap receipts; retain venue/execution evidence with the review.

```sh
node src/cli.mjs index
node src/cli.mjs allocate /private/reviewed-settlement.json
node src/cli.mjs export /private/report/final-allocation
```

Allocation uses integer floor shares, then gives residual units to the largest fractional remainders, with lexical wallet-address tie breaking. Sum of allocation equals exactly the net pool. Every eligible wallet must have a signed recipient; missing bindings block allocation instead of reallocating its share. Multiple source wallets may share a recipient; execution must aggregate them. The manifest contains no executable transaction and sends nothing.

After separately approved manual distribution, provide a JSON array `[{"tx":"0x…","logIndex":0}]` containing **all** distribution events to date. Set `ROBINHOOD_RPC_URL`:

```sh
node src/cli.mjs verify-distribution /private/distribution-events.json
```

Read-only verification checks chain 4663, reviewed MILORD bytecode, successful canonical finalized receipts, exact token Transfer logs, approved distribution source Safe, post-allocation timestamps, deduplication and recipient totals. Unknown/excess payments are rejected. The dashboard only reports Sent after the recipient's full aggregated allocation is verified. If the RPC cannot return finalized blocks, verification stays blocked. This future path is tested with RPC fixtures; no Robinhood mainnet distribution occurred.

## Deployment and rollback

See [DEPLOYMENT.md](DEPLOYMENT.md). Production deployment and activation are separate owner approvals, both still outstanding.
