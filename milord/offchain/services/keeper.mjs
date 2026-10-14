// MILORD keeper: permissionless upkeep plus the bounded buyback (operator key). One pass every INTERVAL_SECS.
//   node offchain/services/keeper.mjs
// Env: RPC_URL, DEPLOYMENTS_FILE, KEEPER_PRIVATE_KEY (gas-only key; BuybackBurner operator), INTERVAL_SECS,
//      MIN_BUYBACK_WEI, BUYBACK_MAX_BELOW_TWAP_BPS, HEALTH_PORT, ALERT_WEBHOOK_URL, STATE_DIR.
// The key can call only: splitter.distribute, collector.collect, burner.buybackAndBurn (bounded by the burner's
// limits and a TWAP-derived minimum), router.execute, adapter.settle, distributor.activateRoot. It holds no funds
// beyond gas and has no role on any Safe.
import path from "node:path";
import { loadConfig, connect } from "./lib/config.mjs";
import { keeperTick } from "./lib/jobs.mjs";
import { createAlerter, createMetrics, startHealthServer, runLoop, stateFile } from "./lib/runtime.mjs";
import { entryPriceSource } from "../lib/entry-price.mjs";

const cfg = loadConfig();
const { provider, contracts } = connect(cfg, "KEEPER_PRIVATE_KEY");
const alerter = createAlerter({ service: "keeper" });
const metrics = createMetrics("keeper");
const state = stateFile(path.join(cfg.stateDir, "keeper.json"), { passes: 0 });
const D = cfg.D;
const milordPrice = D.PoolManager && D.StateView
  ? entryPriceSource(provider, D, { maxStaleness: Number(process.env.MAX_STALENESS ?? 120) })
  : null;

startHealthServer({ port: cfg.healthPort, metrics, staleMs: 3 * cfg.intervalMs });

await runLoop({
  intervalMs: cfg.intervalMs,
  alerter,
  metrics,
  tick: async () => {
    const r = await keeperTick(contracts, {
      alerter,
      now: async () => (await provider.getBlock("latest")).timestamp,
      buyback: milordPrice && {
        minBudgetWei: cfg.minBuybackWei,
        quoteMinOut: async (budget) => {
          try {
            const { weiPerMillion } = await milordPrice.price();
            return ((budget * 10n ** 24n) / weiPerMillion) * BigInt(10_000 - cfg.buybackMaxBelowTwapBps) / 10_000n;
          } catch (e) {
            await alerter.alert("info", "buyback-price", `buyback skipped: ${e.message}`);
            return null;
          }
        },
      },
    });
    for (const x of r.results) metrics.inc(`step_${x.step.replace(/-/g, "_")}_${x.status}`);
    const s = state.read();
    state.write({ ...s, passes: s.passes + 1, last: r.results, at: new Date().toISOString() });
    console.log(JSON.stringify({ keeper: r.results.filter((x) => x.status !== "skipped") }));
    if (r.failed > 0) throw new Error(`${r.failed} keeper steps failed`);
  },
});
