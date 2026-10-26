# MILORD automation services

Four small Node services share one image. They hold no funds and no Safe role.

| Service | Key | What it can do | Interval |
|---|---|---|---|
| `keeper.mjs` | `KEEPER_PRIVATE_KEY`: gas only, BuybackBurner operator | Hookr tax queues `payCreator` (buy and sell; pays only TradingTaxSplitter), splitter `distribute`, `HookrLockFeeCollector.collect`, bounded `buybackAndBurn` (min output from the 30-min MILORD TWAP, skipped when no price), `ArbitrageFundingRouter.execute`, `ParbFundingAdapter.settle`, `ShareDistributor.activateRoot` | 5 min |
| `epoch-publisher.mjs` | `PUBLISHER_PRIVATE_KEY`: ShareDistributor publisher | reads finalized NFT transfers and settled batches, rebuilds every daily epoch from genesis, writes `STATE_DIR/epochs/<k>.json` and `rewards.json`, calls `proposeRoot` when no root is in review | 2 min |
| `root-checker.mjs` | none | recomputes pending and active roots from its own RPC; critical alert ("Team Safe: vetoPending") on mismatch or insolvency | 10 min |
| `monitor.mjs` | none | read-only alerts (below) from its own RPC; polls the other three `/healthz` | 5 min |

Every keeper call except the buyback is permissionless, so if the keeper key is lost anyone can run the same pass. The Safe's controls stay with the Safe: pause the router (`pause`, also allowed for the monitor key), veto a root, change bounds.

## Epoch rule (deterministic)
Epoch *k* is one UTC day, `[genesis + (k-1)·86400, genesis + k·86400)`; `EPOCH_GENESIS` must be 00:00 UTC (services refuse to start otherwise). Its amount is all pARB settled into the distributor before `end + SETTLEMENT_GRACE_SECS`, minus everything allocated in earlier epochs. Weights are NFT-seconds held during the epoch; converter inventory earns nothing. Rounding dust and days with no eligible NFTs roll forward. Roots are cumulative (a root's `epoch` is the last day it covers), so missed days are covered by the next root and claims never double-pay. Nothing expires.

The publisher returns `waiting` while a root is in its review delay (the contract refuses to replace it) or the root would be underfunded, and `blocked` after a veto until the vetoed root is listed in `PUBLISHER_ACK_VETOES`. The monitor's `root-not-activated` warns when a pending root stays unactivated past its delay, and `epoch-missed` when closed days stay unpublished with no root in review.

## Run
```
cd offchain/services
for f in common keeper publisher checker monitor; do cp $f.env.example $f.env; done   # fill in; git-ignored
cd ../.. && docker compose -f offchain/services/docker-compose.yml up -d --build
curl 127.0.0.1:9101/healthz ; curl 127.0.0.1:9104/metrics
```
Each container gets only its own env file: the keeper key never reaches the publisher, and the checker and monitor get
no key. `DEPLOYMENTS_HOST_FILE` (default `deployments/robinhood-46630.json`) is mounted read-only.
Locally against anvil: `RPC_URL=http://127.0.0.1:8545 HOOKR_SETTLE=mock KEEPER_PRIVATE_KEY=<anvil #5> node offchain/services/keeper.mjs`.

## Monitoring and failures
- `/healthz` returns 503 when no tick has succeeded for 3 intervals; `/metrics` is Prometheus text.
- A tick that runs longer than 10 minutes is abandoned and counted as failed (watchdog). A transaction not mined in
  `TX_TIMEOUT_SECS` (180) fails its step and resets the nonce; a reverted receipt is a failed step. Every on-chain step
  is idempotent, so the next tick re-checks instead of repeating blindly.
- Logs are read from the deployments file's `startBlock` in `LOG_BLOCK_RANGE` windows (halved on RPC range errors).
- State lives in `STATE_DIR` (`/data`, a named volume): restarting a container resumes from chain state; nothing is
  lost if the volume is lost (the publisher rebuilds every epoch from genesis; the monitor re-learns onsets).
- RPC calls retry with exponential backoff. A tick that fails 3 times in a row sends a critical alert; recovery sends a resolved alert. Repeats are throttled to one per 15 minutes per key.
- Expected states (router paused, below threshold, request not processed yet, root delay) are skips, not failures.
- Tested failure modes (`node --test offchain/services/test/`): router paused, keeper down for a week, reporter down, unexpected step failure, missing buyback price, missed days, pending root in review, veto and restart, zero-revenue and zero-eligible days, late funding, tampered root, insolvency, retry and alert behaviour.

## Hookr tax settlement (mainnet)
Hookr holds the 1.5% tax in two HookrTaxQueue clones (buy, sell) frozen at launch. Nothing reaches TradingTaxSplitter until someone calls `payCreator()`, which settles the queue's HookrRules claim and pays the 80% creator share in ETH (Hookr's 20% stays for its own `sweepProtocol`). The keeper does this first in every pass, so the splitter distributes the same ETH in the same pass.
- **Addresses**: `HookrDirectionalTax`, `HookrRoot` and `MilordPoolId` in the deployments file. The keeper reads the queues from `HookrDirectionalTax.terms(root, poolId)` on start; if `HookrBuyTaxQueue`/`HookrSellTaxQueue` are also recorded (from `MilordAtomicLaunch.buyQueue()/sellQueue()`) and disagree, it refuses to start.
- **Fail closed**: before paying, each pass checks the queue's frozen terms (recipient = TradingTaxSplitter, no conversion route, native ETH, 20% Hookr share) and `accountingInvariant()`. Any mismatch: no call, critical alert `keeper:tax-<buy|sell>`.
- **Idempotent**: `payCreator` pays exactly what is owed. A repeat, or a retry after a transaction that landed but whose receipt was lost, reverts `NothingDue` in the `staticCall` and is a skip. Below `MIN_TAX_PAYOUT_WEI` (default 0.001 ETH) the pass waits.
- **Metrics**: `tax_<buy|sell>_due_wei` gauge, `tax_<buy|sell>_paid_total` counter, `step_hookr_tax_<buy|sell>_<done|skipped|failed>_total`. Alert if `due_wei` keeps growing with no `paid` (the keeper or the splitter's `receive` is failing).
- **Anyone can do it**: `payCreator` is permissionless and can only pay the frozen recipient, so a dead keeper delays the tax, never loses it.
- Fork-verified (test/fork, B14): the buy and sell queues book exactly 1.5%, `payCreator` pays 80% to the recipient.

## Monitor alerts (`monitor.mjs`, read-only)
| Key | Level | Condition (default) |
|---|---|---|
| `gas-<name>` | warning / critical below ¼ | service key below `MIN_GAS_WEI` (0.01 ETH) |
| `nav-missing` | warning, critical at 3× | no signed NAV report for `NAV_MAX_AGE_SECS` (2 h; the vault requires ≤ 1 h old reports) |
| `pps-move` | warning; critical on a fall or 5× | PPS moved more than `PPS_ALERT_BPS` (1%) between passes |
| `vault-paused` | critical | vault paused or shut down |
| `redeem-waiting` | warning | redeem requests pending longer than `SETTLE_STUCK_SECS` (24 h): the engine must return liquidity |
| `splitter-<x>-stuck` / `-owed` | warning | undistributed ETH for `FEE_STUCK_SECS` (6 h) / a failed push credited as owed |
| `router-stuck` / `router-paused` | warning | router holds ≥ `minSwapWei` with no swap for 6 h / paused |
| `price-stale` | warning | the router's price reference refuses a price for 30 min (the router will not swap) |
| `tax-<buy\|sell>-stuck` | warning | a Hookr tax queue owes MILORD for 6 h |
| `batch-unsettled` | warning | a funding batch not settled into pARB after 24 h |
| `epoch-missed` | critical | a closed day (+`EPOCH_GRACE_SECS`) with unallocated settled shares, no root and no root in review |
| `root-not-activated` | warning | a pending root still unactivated `EPOCH_GRACE_SECS` after its delay ended (keeper not calling `activateRoot`) |
| `keeper:lock-watch` | warning / critical | permanent lock not executed 1,800 / 300 parent blocks before `principalLockedUntil` |
| `bridge-unconfirmed` | warning, critical at 4× | an engine transfer in transit beyond `BRIDGE_MAX_SECS` (2 h), from `BRIDGE_STATUS_URL` |
| `health-<svc>` / `failed-<svc>` | critical / warning | a service's `/healthz` is not 200 / it had new failed ticks (failed transactions) |
| `read-<check>` | warning | the monitor itself could not read (RPC down); the tick fails, so `/healthz` shows it |

## Not yet verified
Robinhood Chain `finalized` block tag support (falls back to `FINALITY_CONFIRMATIONS`). The keeper's tax-queue step is unit-tested with fakes; it has not run against the real queues (it needs a launched pool: a fork run or testnet).
