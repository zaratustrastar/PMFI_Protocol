# MILORD V1 deployment and operations plan (draft, 2026-10-23)

Nothing here has been provisioned or deployed. No real funds, no mainnet transactions, no Safe signatures. Resource
figures marked *measured* come from the VPS fork runs; every other figure is a sizing estimate to be replaced by
measurements on testnet.

## 1. What runs where

The existing Hookr-testing VPS also runs the live PMFI services and has about 2.7 GB free. It gets **no new
long-running MILORD service**. It keeps doing what it does today: fork tests and read-only checks, one at a time.

| Host | Purpose | Services | Keys on the host | Size (estimate) |
|---|---|---|---|---|
| **existing VPS** (PMFI production) | verification only | none permanent. On demand: `./fork/run-fork-tests.sh` (*measured* forge 117 MB, anvil 38 MB), `check-funding-pool.mjs`, `robinhood-clock-probe.mjs`, `record-launch.mjs` (read-only) | none new | unchanged |
| **milord-auto** (new) | protocol upkeep | `keeper`, `epoch-publisher` (docker compose, `mem_limit` 192 MB each), `quote-server` (entry-fee quotes) | keeper (gas only), publisher, quote signer | 2 vCPU, 2 GB RAM, 20 GB disk |
| **milord-engine** (new, separate from PMFI's engine) | arbitrage engine in robinhood mode | `bot/` engine with `ARB_FUNDING_CHAIN=robinhood`, NAV reporter (inside the engine loop), `python3 -m arb_monitor.core.arb_bridge_status` on the private interface, PostgreSQL for positions | servicer (trading funds), report signer, venue API credentials | 2 vCPU, 4 GB RAM, 40 GB disk |
| **milord-watch** (new, different provider and RPC) | independent checks | `root-checker`, `monitor` | none | 1 vCPU, 1 GB RAM, 10 GB disk |

Why this split: the root checker must not share a provider or RPC with the publisher; the monitor must survive the
loss of the host it watches; the servicer key (which holds trading capital) is never on the same host as the
keeper/publisher keys. The hosts talk over a private network (WireGuard or the provider's VPC): the monitor reads
`:9101-9103/healthz` on milord-auto and `:9110/bridges` on milord-engine; nothing listens publicly except the quote
server behind TLS.

RPC: two independent Robinhood Chain providers (one for milord-auto and the engine, one for milord-watch); a Base
and a Polygon RPC for the engine's venue legs.

## 2. Bring-up order (testnet 46630, then mainnet only after approval)

| # | Step | Command | Evidence it worked |
|---|---|---|---|
| 1 | Read-only route checks on 4663 | `RPC_URL=… node offchain/check-funding-pool.mjs` and `node offchain/robinhood-clock-probe.mjs 30 600` | exit 0; record numbers in BLOCKERS B7/B18 |
| 2 | Testnet protocol deploy | `forge script script/DeployRobinhood.s.sol --rpc-url $RH_TESTNET --broadcast` (refuses 4663) with the env in `script/DeployRobinhood.s.sol` header | `deployments/robinhood-46630.json`; router is payee of all three splitters (asserted by the script) |
| 3 | Safes accept ownership | `node offchain/safe-launch-batch.mjs <config>` → `safe-team-setup.json`, `safe-arbitrage-setup.json` | `owner()` = Safes |
| 4 | Services | §5 commands on milord-auto and milord-watch | all four `/healthz` 200; a test alert reaches the webhook |
| 5 | Engine (testnet vault, small amounts only after approval) | robinhood-mode env (§4) | first signed report accepted; `nav-missing` stays clear |
| 6 | Mainnet fork rehearsal of the launch | `MILORD_LOCAL_FORK=yes forge script script/DeployAtomicLaunch.s.sol --rpc-url http://127.0.0.1:<anvil fork port> --broadcast --private-key <anvil #0>` then the fork suite | predicted address printed; fork suite green |
| 7 | Mainnet | **not approved**; needs the go/no-go list (§7) and Bobo's written approval per step | — |

Hookr-1 exists only on mainnet, so the atomic launch is rehearsed on a local fork (step 6), not on testnet.
After a real launch, `RPC_URL=… node offchain/record-launch.mjs deployments/robinhood-4663.json <helper> <StateView>`
(read-only) adds the Hookr keys the keeper, monitor and quote server need.

## 3. Secrets

| Secret | Holder | Storage | Never |
|---|---|---|---|
| Team Safe, Arbitrage Safe signer keys | named signers | hardware wallets | on any server |
| Deployer key | operator, one-time | hardware wallet (`--ledger`) | reused after deployment; keeps no role |
| Keeper key (gas only, BuybackBurner operator, router keeper) | milord-auto | `keeper.env` 0600, owned by the deploy user, or a docker secret; ≤ 0.1 ETH | shared with the publisher (deploy script refuses keeper = publisher) |
| Publisher key | milord-auto | `publisher.env` 0600 | any other role |
| Quote signer | milord-auto | KMS/HSM preferred; else 0600 file | logged; on the engine host |
| Report signer | milord-engine | 0600 file or KMS | on milord-auto |
| Servicer key (trading capital) | milord-engine | 0600 file, host disk encrypted | on any other host |
| Venue API keys (Polymarket, Kalshi) | milord-engine | 0600 env file | in git or logs |
| Alert webhook URL | all hosts | `common.env` | public |

Rules: env files are git-ignored (`offchain/services/*.env`); no secret in docker images, compose files, the
deployments JSON or memory notes. Rotation: compromise of a hot key → Team Safe removes its role (keeper:
`BuybackBurner.setOperator(key,false)`, `router.setKeeper`; publisher: `setPublisher`; quote signer:
`setQuoteSigner(key,false)`; report signer is immutable in V3 → `vault.setPaused(true)` and migrate). Every
keeper step except the buyback is permissionless, so a lost keeper key delays nothing that matters.

## 4. Engine configuration (robinhood mode)

Required (the engine refuses to report or fund without them): `ARB_FUNDING_CHAIN=robinhood`, `ROBINHOOD_CHAIN_ID`,
`ARB_USDG_ADDRESS`, `ARB_VAULT_ASSET` (= USDG), `ARB_VAULT_RPC_URL`, `ARB_VAULT_CHAIN_ID`,
`ARB_VAULT_DOMAIN_SALT=PMFIArbVaultV3.milord.v1`, `ARB_VAULT_V2_ADDRESS=<V3 vault>`, `ARB_BRIDGE_ALLOWLIST`
(4663 and 8453 entries), `POLY_RH_DEPOSIT_ADDR` + `POLY_RH_DEPOSIT_CHAIN_ID` + `POLY_RH_DEPOSIT_TOKEN`,
`KALSHI_BASE_DEPOSIT_ADDR` + `KALSHI_BASE_DEPOSIT_CHAIN_ID` + `KALSHI_BASE_DEPOSIT_TOKEN`, `DATABASE_URL`,
`ARB_OPINION_ENABLED=false` (Opinion is off in robinhood mode regardless). Tunables and defaults:
ROBINHOOD_VAULT_MIGRATION.md §4.

## 5. Service commands

```
# milord-auto and milord-watch (from milord/)
cd offchain/services && for f in common keeper publisher checker monitor; do cp -n $f.env.example $f.env; done; cd ../..
docker compose -f offchain/services/docker-compose.yml up -d --build keeper epoch-publisher     # milord-auto
docker compose -f offchain/services/docker-compose.yml up -d --build root-checker monitor       # milord-watch
docker stats --no-stream                                                                        # measure, then fix mem_limit
```
Restart policy `unless-stopped`; state in named volumes; logs rotate at 10 MB × 5.

## 6. Backups

| State | Where | Recoverable from chain? | Backup |
|---|---|---|---|
| Engine ledgers (withdrawals, bridges, venue deposits, intents, cash-flow journal) | milord-engine | **no**: in-transit money is only known here | every 5 min + before every upgrade, encrypted, off-host; restore before restarting the engine (intents reconcile on start) |
| Positions database | milord-engine PostgreSQL | partly (venue APIs) | nightly `pg_dump`, WAL archiving, 30-day retention |
| Epoch files (`STATE_DIR/epochs/<k>.json`) | milord-auto volume | yes (publisher rebuilds every epoch from genesis) | publish each to IPFS; volume snapshot weekly |
| Deployments JSON | git release tag + each host (read-only mount) | yes | in the release |
| Monitor / keeper state | volumes | yes (re-learned) | none needed |

Restore drill before mainnet: delete the engine host, restore ledgers and DB onto a new host, confirm the first
report's NAV matches the last one (no artificial P&L).

## 7. Go / no-go checklist (all must be yes)

**Contracts and launch**
- [ ] Independent security review done, findings fixed (B6)
- [ ] Fork suite green at the release SHA, both passes (ArbSys stub on and off)
- [ ] Hookr bytecode verification on-chain phase green at the release SHA (B17)
- [ ] `DeployAtomicLaunch` rehearsed on a fork; predicted address used by `DeployRobinhood` (`MILORD_PREDICTED_TOKEN`)
- [ ] Lock signatures collected right after launch; `lock-watch` alerting tested (B16); Bobo's go-ahead to execute the lock
- [ ] Anti-Snipe decision recorded (default: flat launch; the candidate stays unapproved)

**Funding path**
- [ ] `check-funding-pool.mjs` exit 0 on the launch day (B7); router bounds confirmed (B10)
- [ ] Second price reference decided (B8)
- [ ] Polymarket USDG deposit on 4663, Kalshi Base deposit and withdrawal, Across/Relay routes confirmed with small approved amounts; `ARB_BRIDGE_ALLOWLIST` filled from verified addresses (B9)
- [ ] CTF redemption (`request_venue_redemption`) and payout observation implemented and tested (B21)
- [ ] `trading_bot/safe_proxy_withdraw_parb.ts` bridges the requested amount, not the whole balance (B22)

**Automation and monitoring**
- [ ] Testnet: router payee wiring verified on chain, several full daily cycles (fee → USDG → deposit → report → settle → root → claim)
- [ ] Testnet: a redemption that exceeds the idle buffer is filled by the engine's waterfall
- [ ] All four services healthy for 7 days on testnet; every monitor alert fired once in a drill and reached a person
- [ ] Engine crash/restore drill passed (§6)
- [ ] `finalized` tag support checked (B12)

**People**
- [ ] Safe signer sets and thresholds confirmed (B10); on-call rota and incident contacts (Hookr, Paxos, venues)
- [ ] Bobo's written approval of the mainnet deployment and its gas
