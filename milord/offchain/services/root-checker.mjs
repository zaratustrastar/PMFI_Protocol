// Independent root checker: recomputes pending and active roots from finalized chain data with its own RPC
// endpoint and alerts (critical) on any mismatch or insolvency, so the Team Safe can veto within the 1-day
// activation delay. Epochs are daily UTC days; a pending root covers every day through its epoch. It holds NO key and cannot change anything on chain.
//   node offchain/services/root-checker.mjs
// Env: RPC_URL (use a different provider from the publisher), DEPLOYMENTS_FILE, EPOCH_GENESIS,
//      SETTLEMENT_GRACE_SECS, FINALITY_CONFIRMATIONS, INTERVAL_SECS, HEALTH_PORT, ALERT_WEBHOOK_URL.
import { Contract } from "ethers";
import { loadConfig, connect, finalizedBlock } from "./lib/config.mjs";
import { checkRoot, readInputs } from "./lib/jobs.mjs";
import { assertUtcMidnight } from "../lib/eligibility.mjs";
import { createAlerter, createMetrics, startHealthServer, runLoop, retry } from "./lib/runtime.mjs";

const cfg = loadConfig();
if (!cfg.genesis) throw new Error("EPOCH_GENESIS is required");
assertUtcMidnight(cfg.genesis);
const { provider, contracts } = connect(cfg, null);
const alerter = createAlerter({ service: "root-checker" });
const metrics = createMetrics("root_checker");
const parb = new Contract(cfg.D.pARBVault, ["function balanceOf(address) view returns (uint256)"], provider);
startHealthServer({ port: cfg.healthPort, metrics, staleMs: 3 * cfg.intervalMs });

await runLoop({
  intervalMs: cfg.intervalMs,
  alerter,
  metrics,
  tick: async () => {
    // pending roots may reference batches up to 'latest'; read inputs at latest for the comparison,
    // and confirm again at the finalized block before any veto decision.
    const fin = await retry(() => finalizedBlock(provider, cfg.confirmations));
    const inputs = await retry(() => readInputs({ ...contracts, provider, fromBlock: cfg.startBlock, toBlock: fin.number, logStep: cfg.logStep }));
    const r = await checkRoot({ distributor: contracts.distributor, parb, inputs, genesis: cfg.genesis, settlementGrace: cfg.settlementGrace, excluded: [cfg.D.MilordConverter], alerter });
    for (const x of r) metrics.set(`${x.which}_root_ok`, x.match ? 1 : 0);
    console.log(JSON.stringify({ checker: r }));
  },
});
