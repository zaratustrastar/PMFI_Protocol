// Weekly pARB epoch publisher (eligibility indexer + Merkle builder + proposeRoot).
//   node offchain/services/epoch-publisher.mjs
// Env: RPC_URL, DEPLOYMENTS_FILE, PUBLISHER_PRIVATE_KEY (ShareDistributor publisher), EPOCH_GENESIS,
//      SETTLEMENT_GRACE_SECS, FINALITY_CONFIRMATIONS, INTERVAL_SECS, HEALTH_PORT, ALERT_WEBHOOK_URL, STATE_DIR.
// Reads only finalized blocks, recomputes every epoch from genesis (no hidden state), writes the allocation file
// for the epoch to STATE_DIR/epochs/<k>.json, then proposes its root. A root activates only after the
// distributor's delay (1 day), during which the root checker can raise a veto alert to the Team Safe.
import fs from "node:fs";
import path from "node:path";
import { loadConfig, connect, finalizedBlock } from "./lib/config.mjs";
import { publishEpoch, readInputs } from "./lib/jobs.mjs";
import { createAlerter, createMetrics, startHealthServer, runLoop, retry } from "./lib/runtime.mjs";

const cfg = loadConfig();
if (!cfg.genesis) throw new Error("EPOCH_GENESIS (Monday 00:00 UTC of week 1) is required");
const { provider, contracts } = connect(cfg, "PUBLISHER_PRIVATE_KEY");
const alerter = createAlerter({ service: "epoch-publisher" });
const metrics = createMetrics("epoch_publisher");
startHealthServer({ port: cfg.healthPort, metrics, staleMs: 3 * cfg.intervalMs });

await runLoop({
  intervalMs: cfg.intervalMs,
  alerter,
  metrics,
  tick: async () => {
    const fin = await retry(() => finalizedBlock(provider, cfg.confirmations));
    const inputs = await retry(() => readInputs({ ...contracts, provider, fromBlock: cfg.startBlock, toBlock: fin.number, logStep: cfg.logStep }));
    const r = await retry(() =>
      publishEpoch({
        distributor: contracts.distributor, inputs, genesis: cfg.genesis, now: fin.timestamp,
        settlementGrace: cfg.settlementGrace, excluded: [cfg.D.MilordConverter],
        save: (e) => {
          const dir = path.join(cfg.stateDir, "epochs");
          fs.mkdirSync(dir, { recursive: true });
          fs.writeFileSync(path.join(dir, `${e.epoch}.json`), JSON.stringify({
            epoch: e.epoch, start: e.start, end: e.end, cutoff: e.cutoff, root: e.root, total: e.total.toString(),
            dataHash: e.dataHash, amount: e.amount.toString(), dust: e.dust.toString(), values: e.values,
            claims: Object.fromEntries([...e.tree.entries()].map(([i, v]) => [v[0], { cumulative: v[1], proof: e.tree.getProof(i) }])),
          }, null, 2));
        },
      }), { attempts: 3 });
    metrics.set("last_epoch_checked", r.epoch ?? 0);
    if (r.action === "proposed") {
      metrics.inc("roots_proposed");
      await alerter.alert("info", `proposed:${r.epoch}`, `proposed root for epoch ${r.epoch}: ${r.root} total ${r.total}`);
    }
    console.log(JSON.stringify({ publisher: { ...r, total: r.total?.toString(), carried: r.carried?.toString() } }));
  },
});
