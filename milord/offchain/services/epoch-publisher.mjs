// Daily pARB epoch publisher (eligibility indexer + Merkle builder + proposeRoot).
//   node offchain/services/epoch-publisher.mjs
// Env: RPC_URL, DEPLOYMENTS_FILE, PUBLISHER_PRIVATE_KEY (ShareDistributor publisher), EPOCH_GENESIS (00:00 UTC),
//      SETTLEMENT_GRACE_SECS, FINALITY_CONFIRMATIONS, INTERVAL_SECS, PUBLISHER_ACK_VETOES, HEALTH_PORT,
//      ALERT_WEBHOOK_URL, STATE_DIR.
// Reads only finalized blocks and recomputes every daily epoch from genesis (no hidden state, so restarts are safe).
// Day k closes at its end + SETTLEMENT_GRACE_SECS. The publisher then proposes one cumulative root through the latest
// closed day, but never while a pending root is still in the distributor's 1-day review delay (the contract refuses
// that too); days closed meanwhile go into the next root. Before proposing it writes STATE_DIR/epochs/<k>.json (proofs).
// Every tick it also writes STATE_DIR/rewards.json for the frontend: per account earned / allocated / active, and
// NFT-seconds accruing in days not yet closed. Rewards are claimed by holders; nothing is sent to wallets automatically.
import fs from "node:fs";
import path from "node:path";
import { Contract } from "ethers";
import { loadConfig, connect, finalizedBlock } from "./lib/config.mjs";
import { publishEpoch, readInputs, rewardsStatus } from "./lib/jobs.mjs";
import { assertUtcMidnight } from "../lib/eligibility.mjs";
import { createAlerter, createMetrics, startHealthServer, runLoop, retry } from "./lib/runtime.mjs";

const cfg = loadConfig();
if (!cfg.genesis) throw new Error("EPOCH_GENESIS (00:00 UTC of distribution day 1) is required");
assertUtcMidnight(cfg.genesis);
const { provider, contracts } = connect(cfg, "PUBLISHER_PRIVATE_KEY");
const parb = new Contract(cfg.D.pARBVault, ["function balanceOf(address) view returns (uint256)"], provider);
const alerter = createAlerter({ service: "epoch-publisher" });
const metrics = createMetrics("epoch_publisher");
startHealthServer({ port: cfg.healthPort, metrics, staleMs: 3 * cfg.intervalMs });

const writeJson = (file, data) => {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(`${file}.tmp`, JSON.stringify(data, null, 2));
  fs.renameSync(`${file}.tmp`, file); // atomic: the frontend never reads a half-written file
};

await runLoop({
  intervalMs: cfg.intervalMs,
  alerter,
  metrics,
  tick: async () => {
    const fin = await retry(() => finalizedBlock(provider, cfg.confirmations));
    const inputs = await retry(() => readInputs({ ...contracts, provider, fromBlock: cfg.startBlock, toBlock: fin.number, logStep: cfg.logStep }));
    const r = await retry(() =>
      publishEpoch({
        distributor: contracts.distributor, inputs, genesis: cfg.genesis, now: fin.timestamp, parb, alerter,
        settlementGrace: cfg.settlementGrace, excluded: [cfg.D.MilordConverter], acknowledgedVetoes: cfg.ackVetoes,
        save: (e) => writeJson(path.join(cfg.stateDir, "epochs", `${e.epoch}.json`), {
          epoch: e.epoch, start: e.start, end: e.end, cutoff: e.cutoff, root: e.root, total: e.total.toString(),
          dataHash: e.dataHash, amount: e.amount.toString(), dust: e.dust.toString(), values: e.values,
          claims: Object.fromEntries([...e.tree.entries()].map(([i, v]) => [v[0], { cumulative: v[1], proof: e.tree.getProof(i) }])),
        }),
      }), { attempts: 3 });
    metrics.set("last_epoch_checked", r.epoch ?? 0);
    metrics.set("publisher_waiting", r.action === "waiting" ? 1 : 0);
    metrics.set("publisher_blocked", r.action === "blocked" ? 1 : 0);
    if (r.action === "proposed") {
      metrics.inc("roots_proposed");
      await alerter.alert("info", `proposed:${r.epoch}`, `proposed root through day ${r.epoch} (days ${r.covers.join("-")}): ${r.root} total ${r.total}`);
    }
    const [active, pending] = await Promise.all([contracts.distributor.active(), contracts.distributor.pending()]);
    writeJson(path.join(cfg.stateDir, "rewards.json"), rewardsStatus({
      inputs, genesis: cfg.genesis, now: fin.timestamp, settlementGrace: cfg.settlementGrace, excluded: [cfg.D.MilordConverter], active, pending,
    }));
    console.log(JSON.stringify({ publisher: { ...r, total: r.total?.toString(), carried: r.carried?.toString(), activatesAt: r.activatesAt?.toString() } }));
  },
});
