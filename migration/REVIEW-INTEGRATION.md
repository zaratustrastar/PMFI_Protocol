# Integration review and corrections

The landing frontend lives in its own repository; this repository remains the only ledger and allocation authority.

- **Accounting:** gross incoming events with a same-transaction Safe outflow are ambiguous, not net receipts. Mark affected deposits accounting-review and pause health/allocation. Any positive Safe outflow during collection also pauses the service: refunds/recycling need an explicit reviewed resolution, not silent gross-credit allocation. Post-close sales remain reconcilable. No automatic refund or accounting override was introduced.
- **RPC freshness:** successful polling alone no longer certifies freshness. Canonical head timestamps must be within maxIndexAgeSeconds of local time, with at most 30 seconds future skew. Persist that timestamp and reevaluate it on API reads. Reorg and balance reconciliation checks still apply.
- **Concurrency:** exclusive ledger ownership prevents CLI allocation/indexing racing HTTP recipient writes. Stop service for maintenance. After abnormal termination an operator must establish the owner is stopped before removing a stale writer-lock; never auto-steal a lock.
- **Nginx quotas:** distinguish per-client read/write quotas using X-Real-IP only when explicitly enabled and the peer is loopback. Nginx overwrites the header. Direct clients cannot override their address. If a trusted CDN is present, separately review Nginx real_ip configuration; do not blindly trust supplied forwarding headers.
- **Integration:** state exposes configuration identity and registry freeze status; history exposes allocation token decimals. Clearing build output prevents accidentally retaining an old wallet bundle. Invalidated allocation manifests are suppressed from export.
- **Runtime:** systemd uses a dedicated /opt/pmfi-migration/runtime/bin/node, leaving the VPS system Node unchanged.

18 unit/regression tests and 15 browser/local-Anvil integration checks pass. Browser tests exercise the native React frontend when MIGRATION_FRONTEND_URL is set. Default-build safety also has a browser check against a simulated live-enabled API (test/disabled-preview.mjs). No production tokens, transfers or Safe transactions were used.

Remaining activation prerequisites: approve exact token/Safe/code hashes/owners/threshold, review actual token event and tax semantics, choose UTC window/start block/confirmations, publish refunds/late-deposit/deductions policy, verify recipient chain and actual MILORD asset, review acquisition/settlement custody, production-origin wallet smoke test, then separately approve config hash and live UI build. None are satisfied merely by merging this draft.
