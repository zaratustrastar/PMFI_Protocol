# Production testing: services, monitoring, backups and frontend

Nothing here touches Robinhood mainnet, real funds or Safe signatures. There are two test environments, because
Hookr-1 exists only on Robinhood mainnet (4663):

| Environment | What it can test | What it cannot |
|---|---|---|
| **A. Private fork soak** (`FORK_KEEP=yes ./fork/rehearse-full-launch.sh`) | The whole system on real Hookr, PoolManager, StateView, USDG and the WETH/USDG pool: keeper, publisher, root checker, quote server, monitor, frontend, conversions | Real time passing (Anvil mines only on transactions), real RPC behaviour, venues |
| **B. Robinhood testnet (46630)** | Deploy scripts with real keys and Safes, vault reports from the engine, router and vault with testnet assets, monitor and alerts, backups over days | The Hookr pool (no Hookr-1 on testnet), so no tax, buyback or entry quotes |

## Host capacity (read before starting anything)

The Hookr-testing VPS runs live PMFI services and has about 2.7 GB free. Check first, and do not start the stack if
less than 1.5 GB is available:

```bash
free -m; docker stats --no-stream 2>/dev/null | head
```

Each service container is capped at 192 MB (five services: 960 MB worst case); the fork soak adds Anvil (~40 MB
measured) and five Node processes (~60–90 MB each). A separate small host for testnet services is blocker B23 and an
infrastructure purchase that needs the owner's approval.

## A. Private fork soak (VPS, after the rehearsal passes)

```bash
cd /opt/milord-staging/milord
FORK_KEEP=yes ./fork/rehearse-full-launch.sh          # terminal 1: rehearsal, then keeps the fork on 127.0.0.1:8550

# terminal 2: the services against that fork, public Anvil keys only
DEP=$(ls -t fork/logs/full-rehearsal-*.deployments.json | head -1)
export ALLOW_LOCAL_FORK=yes RPC_URL=http://127.0.0.1:8550 CHAIN_ID=4663 DEPLOYMENTS_FILE=$PWD/$DEP STATE_DIR=/tmp/milord-soak INTERVAL_SECS=60
KEEPER_PRIVATE_KEY=0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a HEALTH_PORT=9101 MAX_HEAD_LAG=1000000000 node offchain/services/keeper.mjs &
HEALTH_PORT=9103 node offchain/services/root-checker.mjs &
QUOTE_SIGNER_PRIVATE_KEY=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a QUOTE_PORT=8787 MAX_HEAD_LAG=1000000000 node offchain/quote-server.mjs &
curl -s 127.0.0.1:9101/healthz; curl -s 127.0.0.1:8787/healthz
```

- `ALLOW_LOCAL_FORK=yes` accepts chain 4663 only with a loopback RPC (`offchain/services/lib/config.mjs` `chainAllowed`);
  any other URL still needs `ALLOW_MAINNET=yes`, which is reserved for the approved mainnet launch.
- The rehearsal moved the fork's clock about eight days ahead, so the wall-clock head check is relaxed with
  `MAX_HEAD_LAG` here only. Advance time with `cast rpc --rpc-url http://127.0.0.1:8550 evm_increaseTime <secs>` and
  `evm_mine` to exercise epochs.
- Stop everything with Ctrl-C in terminal 1 and `kill %1 %2 %3` in terminal 2; nothing persists outside
  `/tmp/milord-soak` and `fork/logs/`.

## B. Robinhood testnet (46630)

1. Testnet Safes with the real signer set; deployer, keeper, publisher, quote-signer and report-signer keys created in
   the secret store (testnet only).
2. `forge script script/DeployRobinhood.s.sol --rpc-url $RH_TESTNET --broadcast --ledger` with the testnet environment
   (testnet 6-decimal asset and WETH/asset pool; the script refuses without a funding pool).
3. Copy `offchain/services/*.env.example` to `*.env` and fill them in (git-ignored); `CHAIN_ID=46630`.
4. `docker compose -f offchain/services/docker-compose.yml up -d --build keeper epoch-publisher root-checker monitor`
   (the quote server needs a Hookr pool and stays off on testnet).
5. Engine in robinhood mode against the testnet vault (servicer and report signer keys), small testnet amounts.
6. Run for at least one full weekly epoch: one root published, checked, activated and claimed.

## Monitoring

| Endpoint | Healthy |
|---|---|
| keeper `:9101/healthz`, publisher `:9102`, checker `:9103`, monitor `:9104` | 200; 503 after 3 intervals without a successful tick |
| quote server `:8787/healthz` | 200 when the RPC answers and its head is within `MAX_HEAD_LAG`; `lastError` shows the last refused quote (an expected wait, not ill health) |
| `/metrics` on the keeper, publisher, checker and monitor ports | Prometheus text: `step_<name>_<done|skipped|failed>_total`, `tax_<buy|sell>_due_wei`, `lock_blocks_left` |

The monitor polls the other four `/healthz` endpoints (`MONITOR_HEALTH_URLS` in docker-compose.yml) and alerts through
`ALERT_WEBHOOK_URL`. Before calling an environment ready: send one test alert end to end, stop the keeper for three
intervals and confirm the critical alert and the resolved alert, and pause the router from the monitor key once.

## Backups and recovery

```bash
offchain/services/backup.sh /var/backups/milord                 # hourly from cron; copy the archive off-host
BACKUP_AGE_RECIPIENT=age1... offchain/services/backup.sh ...    # encrypted with age before it leaves the host
docker compose -f offchain/services/docker-compose.yml stop
offchain/services/restore.sh /var/backups/milord/milord-services-<stamp>.tar.gz   # checks SHA256SUMS; refuses non-empty volumes
docker compose -f offchain/services/docker-compose.yml start
```

The archive holds the four state volumes (the publisher's `epochs/<k>.json` allocation files included) and the
deployments file, with checksums. It never holds env files or keys. All service state can be rebuilt from chain (the
publisher recomputes every epoch from genesis), so losing it costs a slower restart, not money. The engine's ledgers
are different: in-transit money is known only there, and they follow OPERATIONS_PLAN.md §6.

Restore drill (testnet, before mainnet): stop the stack, delete the volumes, restore the latest archive, start, and
confirm every `/healthz` is 200, the root checker reports a match for the active root, and the keeper's next pass has
no failed step.

## Frontend

```bash
npx http-server . -p 8080        # from milord/, on the test host; reach it through an SSH tunnel, not a public port
# http://127.0.0.1:8080/frontend-integration/milords-app.html?deployments=/fork/logs/<file>.deployments.json&quote=http://127.0.0.1:8787/quote
```

The page reads the fee rule from `MilordConverter.feeParams()` (10% of the ETH value of 1M MILORD, minimum
0.002 ETH), checks every quote against the pool's own price before the wallet signs, and links to Hookr and the
marketplace only when `hookr` and `market` are given. For the fork soak, add a wallet network named
"MILORD fork (local)" with RPC `http://127.0.0.1:8550` and chain id 4663, import a public Anvil key, and remove the
network afterwards so it is never confused with Robinhood mainnet.
