# Disabled production preview — review-only deployment runbook

No commands in this runbook have been executed on production. No migration activation is authorized.

## Release boundary and approval
Publish only the isolated React build, backend service and narrowly scoped Nginx locations. Preserve `/root/pmfi`, `/var/www/pmfi` and `/var/www/app.pmfi.cc/current`. Do not run the landing repository's full deployment script. No homepage navigation change is required for the direct preview URL.

Before execution obtain owner approval for the reviewed frontend/backend commit SHAs, artifact checksums, pinned Node 24 release, exact Nginx diff and optional existing Privy bridge bytes. Record these values in the release ticket. Source snapshot: `4643ce388706f2119d8dab67c3ffac0e14e8a8a3`. Host is `root@147.182.206.35`, independently supplied ED25519 fingerprint `SHA256:FYGvNrniHCFaVEFTJ0d1Z8nPCTxbp05rrjTLNi9J1PQ`. SSH authentication is currently unavailable here. Do not disable host-key verification.

A disabled visual preview can omit the bridge. A wallet-capable preview additionally requires approval of its pinned hash and successful login-only test on `https://pmfi.cc`; dashboard allowlists/CSP are unverified. Neither variant requires real token/Safe addresses or an approved live migration window.

## 1. Read-only preflight
On authenticated host, record `uname -m`, `node --version`, `nginx -T`, `systemctl cat nginx`, `ss -ltnp`, `readlink -f /var/www/app.pmfi.cc/current`, and SHA256 of `/var/www/pmfi/index.html`. Inspect effective pmfi.cc TLS server, canonical www redirects, CSP headers, existing /migrate and /api locations, root and CDN real-IP directives. Confirm loopback 8787 is free. Record the exact Nginx vhost file as VHOST; do not guess it from a conventional filename. Save root/app HTML and headers from HTTPS for post-change comparison. Confirm sufficient disk space.

Stop here if actual configuration conflicts with the proposed locations. The effective Nginx file and architecture cannot be certified until this preflight runs; this is the remaining limit on an executable host-specific plan.

## 2. Prepare immutable release artifacts off-host
Use clean checkouts of the approved draft commit SHAs. In the landing repo, with dedicated Node 24 in PATH:

```sh
npm ci --ignore-scripts
npm run check
env -u VITE_MIGRATION_TRANSACTIONS npm run build:migration
```
Ensure there are no `.env*` files supplying VITE_MIGRATION_TRANSACTIONS. Run the default-build browser safety test against the release. Optional: `node script/copy-migration-bridge.mjs APPROVED_LOCAL_BRIDGE APPROVED_SHA256`. Package only `dist-migration/` contents. The package must contain migration.html and assets, not index.html, .env, database, server sources or homepage build.

In protocol migration directory, run `npm ci --ignore-scripts`, `npm test`, `npm run build`; package migration sources, package.json/lock, config.example.json and deploy files, excluding node_modules, ledgers, locks, test reports and private env files. Record SHA256 for both archives. No signing keys are needed.

## 3. Isolated Node and filesystem setup (after approval)
Select an official supported Node 24 patch release, pin the exact version and architecture after preflight, obtain its release archive plus signed SHA256 manifest from https://nodejs.org/download/release/, and verify its signature/checksum using the Node release keys before extraction. Tested application runtime here: v24.19.0. Do not use `latest`, curl-to-shell installers, apt upgrades or global symlinks. Install under `/opt/pmfi-migration/runtimes/node-VERSION-linux-ARCH/`; symlink `/opt/pmfi-migration/runtime` to that directory. Record `runtime/bin/node --version`. The existing system v20.19.5 and app service remain untouched.

Create a system user `pmfi-migration` with no login and no home if absent. Create root-owned `/opt/pmfi-migration/releases`, `/opt/pmfi-migration-web/releases`, and `/etc/pmfi-migration`; create `/var/lib/pmfi-migration` owned by that service user, mode 0700. Stage verified archives as `/opt/pmfi-migration/releases/RELEASE` and `/opt/pmfi-migration-web/releases/RELEASE`, preserving root ownership; web directories/files must be readable/traversable by Nginx (0755/0644). Service source is root-owned/read-only.

Install backend dependencies inside its staged directory with dedicated runtime in PATH: `npm ci --omit=dev --ignore-scripts`. Do not run a system-wide npm install. Copy disabled config.example.json to `/etc/pmfi-migration/config.json` and service.env.example to `/etc/pmfi-migration/service.env`; root:pmfi-migration, 0640. Verify mode=disabled, token/safe/start/end null, exact origin=https://pmfi.cc, BASE_RPC_URLS empty, no MIGRATION_APPROVAL variable or approval file. No production database is opened in disabled mode.

## 4. Activate disabled preview only
Record prior `current` targets if any. Create sibling temporary symlinks pointing at each staged RELEASE and atomically rename them over `/opt/pmfi-migration/current` and `/opt/pmfi-migration-web/current` with `mv -Tf`. Install the reviewed unit as `/etc/systemd/system/pmfi-migration.service`, then `systemctl daemon-reload` and `systemctl start pmfi-migration`. Do not enable at boot until smoke checks pass. Verify `curl -fsS http://127.0.0.1:8787/api/migration/state` returns mode=disabled, enabled=false and no addresses/dates.

Back up VHOST with its metadata and record the backup path. Install reviewed `deploy/nginx.locations.conf` as `/etc/nginx/snippets/pmfi-migration.locations.conf`; add one include inside the existing pmfi.cc TLS server only. This maps exact /migrate and /migrate/ to the isolated HTML, /migrate/assets/ to isolated hashed files, exact bridge path to reviewed bridge, unknown /migrate paths to 404, and /api/migration/ to loopback8787. It overwrites X-Real-IP and clears X-Forwarded-For. Preserve existing root, other locations and app vhost. Check inherited security headers: Nginx add_header in a location replaces parent inheritance, so duplicate required site headers in the reviewed snippet if necessary. Never weaken the site-wide CSP merely to make the bridge load.

Run `nginx -t`; on failure restore the vhost backup and do not reload. On success `systemctl reload nginx`.

## 5. Smoke and acceptance
Fetch https://pmfi.cc/migrate and its hashed assets; confirm correct MIME, no-store HTML/API and immutable hashed assets. Fetch /migrate/unknown (404). Check API state disabled, empty addresses/dates, unavailable totals; POST challenge/bind must fail with 503 even with a valid Origin. Run browser default-build safety check, mobile/desktop visual check and, if bridge approved, login-only check. No personal_sign or eth_sendTransaction is permitted during this preview test. Confirm connection errors are visible, not blank screens.

Recompute original homepage index SHA256 and compare root/app HTTP responses, existing application symlink, system Node version and app service status. All must match the recorded preflight state. Check journal and Nginx errors. Only then optionally `systemctl enable pmfi-migration`. Save release SHAs/checksums, before/after evidence and approval reference. Do not activate transfers or advertise migration as open.

## Rollback
For first installation: restore the exact VHOST backup, run `nginx -t`, reload Nginx, then stop/disable pmfi-migration. Retain staged files/logs for diagnosis. No homepage/app restore should be required because neither was changed.

For an update: stop migration service, atomically restore both previously recorded current symlink targets, restore corresponding unit/config/snippet backups, daemon-reload, nginx -t, restart disabled service, reload Nginx, and repeat disabled/API/homepage/app checks. If no earlier preview existed, use first-install removal. Never delete or roll back a ledger database once real deposits exist; future live rollback requires reviewed schema compatibility and a consistent SQLite backup with service stopped. Stale writer-lock removal requires proof the recorded process has stopped. CLI maintenance requires exclusive ownership and must not run beside the service.

## Separate activation decision
A later launch requires approved token/Safe/code/owner parameters, actual token accounting review, policy and UTC window, canonical index reconciliation, destination asset/chain validation, settlement operations review, explicit config-hash approval and an independently reviewed live-enabled UI build. This runbook does none of these.
