# MILORD automation services

Three small Node services share one image. They hold no funds and no Safe role.

| Service | Key | What it can do | Interval |
|---|---|---|---|
| `keeper.mjs` | `KEEPER_PRIVATE_KEY`: gas only, BuybackBurner operator | Hookr tax queues `payCreator` (buy and sell; pays only TradingTaxSplitter), splitter `distribute`, `HookrLockFeeCollector.collect`, bounded `buybackAndBurn` (min output from the 30-min MILORD TWAP, skipped when no price), `ArbitrageFundingRouter.execute`, `ParbFundingAdapter.settle`, `ShareDistributor.activateRoot` | 5 min |
| `epoch-publisher.mjs` | `PUBLISHER_PRIVATE_KEY`: ShareDistributor publisher | reads finalized NFT transfers and settled batches, rebuilds every epoch from genesis, writes `STATE_DIR/epochs/<k>.json`, calls `proposeRoot` | 15 min |
| `root-checker.mjs` | none | recomputes pending and active roots from its own RPC; critical alert ("Team Safe: vetoPending") on mismatch or insolvency | 10 min |

Every keeper call except the buyback is permissionless, so if the keeper key is lost anyone can run the same pass. The Safe's controls stay with the Safe: pause the router (`pause`, also allowed for the monitor key), veto a root, change bounds.

## Epoch rule (deterministic)
Epoch *k* covers `[genesis + (k-1)·week, genesis + k·week)`. Its amount is all pARB settled into the distributor before `end + SETTLEMENT_GRACE_SECS`, minus everything allocated in earlier epochs. Weights are NFT-seconds held during the epoch; converter inventory earns nothing. Rounding dust and weeks with no eligible NFTs roll forward. Roots are cumulative, so a missed week is covered by the next root and claims never double-pay.

## Run
```
cp offchain/services/.env.example offchain/services/.env    # fill in
docker compose -f offchain/services/docker-compose.yml --env-file offchain/services/.env up -d
curl 127.0.0.1:9101/healthz ; curl 127.0.0.1:9101/metrics
```
Locally against anvil: `RPC_URL=http://127.0.0.1:8545 HOOKR_SETTLE=mock KEEPER_PRIVATE_KEY=<anvil #5> node offchain/services/keeper.mjs`.

## Monitoring and failures
- `/healthz` returns 503 when no tick has succeeded for 3 intervals; `/metrics` is Prometheus text.
- RPC calls retry with exponential backoff. A tick that fails 3 times in a row sends a critical alert; recovery sends a resolved alert. Repeats are throttled to one per 15 minutes per key.
- Expected states (router paused, below threshold, request not processed yet, root delay) are skips, not failures.
- Tested failure modes (`node --test offchain/services/test/`): router paused, keeper down for a week, reporter down, unexpected step failure, missing buyback price, indexer down for two weeks, zero eligible NFTs, late settlement, tampered root, insolvency, retry and alert behaviour.

## Hookr tax settlement (mainnet)
Hookr holds the 1.5% tax in two HookrTaxQueue clones (buy, sell) frozen at launch. Nothing reaches TradingTaxSplitter until someone calls `payCreator()`, which settles the queue's HookrRules claim and pays the 80% creator share in ETH (Hookr's 20% stays for its own `sweepProtocol`). The keeper does this first in every pass, so the splitter distributes the same ETH in the same pass.
- **Addresses**: `HookrDirectionalTax`, `HookrRoot` and `MilordPoolId` in the deployments file. The keeper reads the queues from `HookrDirectionalTax.terms(root, poolId)` on start; if `HookrBuyTaxQueue`/`HookrSellTaxQueue` are also recorded (from `MilordAtomicLaunch.buyQueue()/sellQueue()`) and disagree, it refuses to start.
- **Fail closed**: before paying, each pass checks the queue's frozen terms (recipient = TradingTaxSplitter, no conversion route, native ETH, 20% Hookr share) and `accountingInvariant()`. Any mismatch: no call, critical alert `keeper:tax-<buy|sell>`.
- **Idempotent**: `payCreator` pays exactly what is owed. A repeat, or a retry after a transaction that landed but whose receipt was lost, reverts `NothingDue` in the `staticCall` and is a skip. Below `MIN_TAX_PAYOUT_WEI` (default 0.001 ETH) the pass waits.
- **Metrics**: `tax_<buy|sell>_due_wei` gauge, `tax_<buy|sell>_paid_total` counter, `step_hookr_tax_<buy|sell>_<done|skipped|failed>_total`. Alert if `due_wei` keeps growing with no `paid` (the keeper or the splitter's `receive` is failing).
- **Anyone can do it**: `payCreator` is permissionless and can only pay the frozen recipient, so a dead keeper delays the tax, never loses it.
- Fork-verified (test/fork, B14): the buy and sell queues book exactly 1.5%, `payCreator` pays 80% to the recipient.

## Not yet verified
Robinhood Chain `finalized` block tag support (falls back to `FINALITY_CONFIRMATIONS`). The keeper's tax-queue step is unit-tested with fakes; it has not run against the real queues (it needs a launched pool: a fork run or testnet).
