# Audit evidence — 21 October 2026

## Repository

Cloned the current `milord/v1-protocol` branch, HEAD `ff1b23efa2eddd773eb3d3a751ac95c3ee1b5c83`, then created `feature/legacy-pmfi-migration`. Read `OPEN_DECISIONS.md`, `CURRENT_PROJECT_AUDIT.md`, launch `LAUNCH_LIQUIDITY.md`, `SAFE_CONFIGURATION.md`, `README.md`, the frontend integration README/SDK, and legacy frontend code.

The final launch plan allocates 100% of MILORD to the founding liquidity position; there is no migration reserve. The older reserve suggestion in OPEN_DECISIONS is superseded by that decision. No tokenomics changed. Launch Safes on Robinhood are not Base migration Safes.

## Live public assets inspected

- `https://pmfi.cc/` references `/assets/index-_8W44MrG-preload-20260708-213027-dollarfix-20260708-213223.js` and `/assets/index-CUt2IYbd.css`.
- `https://app.pmfi.cc/` references `/privy-bridge.bundle.js?v=20260703T022207Z` and `/assets/index-uq8lcUVo.js?build=app-b420-collateral-20260709T103103Z`.
- Public Privy bridge exposes `ready`, `connect`, `restore`, `logout`, `isAuthenticated`; connect returns `{wallet, address, ethereumProvider}`. It already supports Base and wallet login. Migration uses this contract, not the old repository invite gate or frontend RPC proxy.
- Live landing CSS `.pmfi-landing` uses background `#060a14`, ink `#eaeffa`, accent `#54e6ae`, Archivo/system typography. These informed the scoped migration page.
- Actual React source/build metadata remains inaccessible. No inference that the separate `zaratustrastar/pmfi` trading repo is the landing source.

## VPS

Historical repo documentation points to `147.182.239.195`; host verification there failed, so it was not trusted. Owner then supplied and independently verified `root@147.182.206.35`, Ubuntu 24.04, fingerprint `SHA256:FYGvNrniHCFaVEFTJ0d1Z8nPCTxbp05rrjTLNi9J1PQ`. `ssh-keyscan` and `ssh-keygen` matched that fingerprint. Read-only SSH attempt failed `Permission denied (publickey)`.

Owner-reported directories (not inspected through SSH here):
- `/root/pmfi`: probable landing source.
- `/var/www/pmfi`: deployed landing.
- `/var/www/app.pmfi.cc/current` → `/var/www/app.pmfi.cc/releases/app-b420-collateral-20260709T103103Z`.

No production files changed. Source/build/Nginx validation and actual Privy-origin smoke test remain release blockers.

## Legacy token investigation

Public results contain multiple unrelated PMFI tokens. The candidate linked to pmfi.cc by public listings is `0xd77ce6d3137342bb5174673bdab5f51db16fcba3`:
- https://www.fxempire.com/crypto/pmfi (links pmfi.cc and d77…fcba3).
- https://x.com/i/status/2062356074825683353 (public post associates pmfi.cc and the full address).

Read-only JSON-RPC calls to `https://mainnet.base.org` returned:
- chain ID: 8453
- symbol: PMFI
- decimals: 18
- totalSupply base units: `100000000000000000000000000000`
- runtime bytecode hash: `0x1b94bb4916d4fc17a55098371986dd98042ebe752d7dc32e545f644c34e7f75a`

**This verifies contract existence/metadata on Base, not official ownership or transfer semantics.** No verified source/implementation/transfer-tax review or owner confirmation was obtained. The address is deliberately absent from runtime configuration. Receiving Base Safe, owners, threshold, window and policy are also absent. No real token transfer was made.

## Test evidence and limits

Automated Node tests cover exact units/rounding, deterministic dust, boundary eligibility, disabled approval gates, wallet preflight/errors, persistent deduplication, reorgs, RPC failure, balance mismatch, ledger configuration drift, signed recipient binding/replay/expiry/freeze, settlement arithmetic, distribution receipt/finality checks, and CLI allocation/exports.

Local Anvil end-to-end tests deploy mock token/Safe ONLY on 31337, connect through a Privy-shaped browser fixture, sign binding through the real HTTP API, perform a direct ERC-20 transfer, verify its receipt/logs/balance, wait confirmations, refresh history, test wallet rejection/network switching/insufficient balance/gas/reverts, test taxed net receipt, simulate an actual EVM reorg, reopen persistent SQLite and test EIP-1271. Browser renders at desktop and 390px mobile; disabled-state screenshots contain no artificial live metrics.

The production frontend build passes. npm dependency audit returned zero vulnerabilities after selecting ethers 6.17.0. Existing root `npm test` is a placeholder; we did not claim the unrelated protocol/contract suite passed. Live Privy authentication, actual Base PMFI transfer behavior, production RPC service levels, real landing integration, and Robinhood distribution are not tested.
