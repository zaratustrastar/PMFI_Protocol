# Proposed deployment and rollback — approval required

Nothing in this document has been executed against production. Exact modifications to the existing landing build cannot be prescribed until SSH/source access is available. The new feature's paths below are proposed, isolated paths; the existing `/var/www/pmfi` and app release symlink remain untouched.

## 1. Read-only infrastructure validation

With owner-authorized SSH access and the verified fingerprint, inspect:

```sh
cd /root/pmfi
pwd
git status --short
git rev-parse HEAD
cat package.json
find . -name AGENTS.md -not -path './node_modules/*'
readlink -f /var/www/app.pmfi.cc/current
sudo nginx -T
```

Privately inspect Nginx output; it may contain sensitive upstream configuration. Identify the exact pmfi.cc TLS server include and current source route/navigation architecture. Confirm the deployed asset/build correspondence. Do not overwrite `/root/pmfi` or copy this module over the homepage. Reuse its navigation/component wrapper if source permits. A scoped proxy route is an alternative requiring owner approval.

Confirm Node 24 path, local disk/backups, service user, port 8787 availability, a reliable Base RPC primary plus fallback, and Privy's allowed origin `https://pmfi.cc`. Audit the existing bridge before copying it. Test the actual wallet flow in the approved staging origin first.

## 2. Stage an inactive release

After approval to install the service (still no public route or deposits):

1. Create unprivileged `pmfi-migration` user; directories `/opt/pmfi-migration/releases/<commit>`, `/etc/pmfi-migration`, `/var/lib/pmfi-migration`. Config should be root-owned/group-readable by the service; DB directory service-owned with mode 0700.
2. Copy only `migration/` source and lockfile into the release. Use verified Node 24 to run `npm ci --ignore-scripts`, `npm test`, then `PMFI_PRIVY_BRIDGE=<reviewed-bridge-path> npm run build`. Keep node_modules/src/config out of the public web root.
3. Put the disabled example config in `/etc/pmfi-migration/config.json`; copy and privately populate `deploy/service.env.example`. Do not set an approval path or live addresses yet.
4. Point `/opt/pmfi-migration/current` at this release. Review/install the service unit; verify `/usr/bin/node` is Node 24 (change ExecStart to the verified path if needed).
5. Start the service; check `curl http://127.0.0.1:8787/api/migration/state` returns `enabled:false`, `total:null`. Inspect logs and disabled page via an SSH tunnel. No public Nginx change yet.
6. Back up the existing Nginx include and record its path/hash. Obtain approval for the concrete scoped route diff.

## 3. Publish the disabled page only after route approval

Add the contents of `deploy/nginx.locations.conf` to the existing **pmfi.cc** TLS server. They match only `/migrate`, `/migrate/…`, and `/api/migration/…`; do not change root/index/app.pmfi.cc. Confirm no overlapping locations or upstream conflicts. Run `nginx -t`; only if it passes, reload Nginx. Test homepage/app/docs as before, plus the migration page on desktop/mobile. Verify no RPC secrets/config files are served. Add a homepage navigation link through the actual source build only after it is inspected and that source change is reviewed.

## 4. Activate only after a separate explicit approval

- Obtain the inputs/approvals listed in README, including audited legacy token transfer semantics and Base receiving Safe.
- Create reviewed live config with approved UTC window, startBlock, code hashes, owners, threshold and policy. No production approval artifact is included in this change.
- Use fresh persistent DB for this approved immutable ledger identity. Index historical data before offering deposits; establish baseline and verify balance reconciliation. Confirm provider lag stays below the configured maximum.
- Owner approval evidence must reference the exact config fingerprint. Install the private approval artifact and set `MIGRATION_APPROVAL` only after approval. Restart service and verify enabled remains false before the approved start.
- At start, inspect the fully rendered addresses/review using read-only wallet interaction. Do not test with mainnet funds without separate authorization. Monitor sync age, balance mismatch, contract/Safe drift, disk and service errors throughout the window.

## 5. Rollback / emergency stop

To pause new UI deposits, remove the approval file path from the service environment and restart. Keep valid live config and the indexer running so direct transfers continue to be recorded. Do not alter the receiving address, token, timestamps or delete the DB to pause. Those change the ledger identity and require a reviewed migration.

To roll back code, stop service, take a consistent SQLite backup (SQLite online backup API/CLI, or copy DB plus WAL/SHM while stopped), switch `/opt/pmfi-migration/current` to the previous reviewed release and restart. Preserve all ledger/audit files and allocation manifests. Never roll back to a version with an incompatible DB schema without a reviewed data migration.

To remove the public route, restore the exact saved Nginx include, run `nginx -t`, then reload. Restore a navigation change through the landing source's existing deployment process. Homepage and app release roots were never replaced. Keep the indexer and reporting available privately for deposits already received and any late direct transfers.

If Safe ownership/code, token behavior, chain continuity or balances differ, leave deposits paused and investigate. A deep reorg across baseline needs a separate rebuilt ledger and preserved old audit; a post-allocation reorg invalidates the allocation. No refund, sale, purchase or distribution is executed automatically by rollback.
