// MILORD monitor: read-only (no private key, no transactions). One pass every INTERVAL_SECS (default 300).
//   node offchain/services/monitor.mjs
// Watches: gas on service keys, missing NAV reports, unexpected PPS moves, vault paused, redemptions waiting on
// liquidity, undistributed splitter ETH, router stuck or paused, stale funding price, Hookr tax queues not paid,
// funding batches not settled, missed weekly epochs, the permanent-lock deadline, unconfirmed bridge transfers, and
// the other services' /healthz and failed ticks.
// Env: RPC_URL (use a provider different from the keeper's), DEPLOYMENTS_FILE, CHAIN_ID, EPOCH_GENESIS,
//      SETTLEMENT_GRACE_SECS, ALERT_WEBHOOK_URL, HEALTH_PORT, STATE_DIR, INTERVAL_SECS,
//      MONITOR_WALLETS     name=0xaddr[:minWei],...   e.g. keeper=0x..,publisher=0x..,reporter=0x..,servicer=0x..
//      MONITOR_HEALTH_URLS name=url,...               e.g. keeper=http://keeper:9101/healthz,...
//      BRIDGE_STATUS_URL   JSON {rows:[{id, kind, amount, status, startedAt}]} served by the arbitrage engine (optional)
//      NAV_MAX_AGE_SECS, PPS_ALERT_BPS, FEE_STUCK_SECS, SETTLE_STUCK_SECS, EPOCH_GRACE_SECS, BRIDGE_MAX_SECS, MIN_GAS_WEI
import path from "node:path";
import { Contract } from "ethers";
import { loadConfig, connect, resolveTaxQueues, parentBlock, ABI } from "./lib/config.mjs";
import { watchLockHandover } from "./lib/jobs.mjs";
import { monitorTick, MONITOR_ABI } from "./lib/monitor.mjs";
import { createAlerter, createMetrics, startHealthServer, runLoop, stateFile, retry } from "./lib/runtime.mjs";

const cfg = loadConfig();
const D = cfg.D;
const { provider } = connect(cfg); // never a signer
const alerter = createAlerter({ service: "monitor" });
const metrics = createMetrics("monitor");
const state = stateFile(path.join(cfg.stateDir, "monitor.json"), {});
const env = process.env;
const pairs = (s) => (s ?? "").split(",").map((x) => x.trim()).filter(Boolean).map((x) => x.split("="));
const c = (a, abi) => (a ? new Contract(a, abi, provider) : null);

const queues = await retry(() => resolveTaxQueues(D, provider), { attempts: 8, isPermanent: (e) => /disagrees|no terms|without/.test(String(e?.message)) });
if (queues) Object.assign(D, queues);
const router = c(D.ArbitrageFundingRouter, MONITOR_ABI.router);
const reference = D.TwapReference ? c(D.TwapReference, MONITOR_ABI.reference) : router ? c(await router.priceReference(), MONITOR_ABI.reference) : null;

const contracts = {
  provider,
  vault: c(D.pARBVault, MONITOR_ABI.vault),
  router,
  reference,
  splitters: [["entry", D.EntryFeeSplitter], ["royalty", D.RoyaltySplitter], ["tax", D.TradingTaxSplitter]]
    .filter(([, a]) => a).map(([name, a]) => ({ name, contract: c(a, MONITOR_ABI.splitter) })),
  adapter: c(D.ParbFundingAdapter, ABI.adapter),
  distributor: c(D.ShareDistributor, ABI.distributor),
  taxQueues: [["buy", D.HookrBuyTaxQueue], ["sell", D.HookrSellTaxQueue]].filter(([, a]) => a).map(([name, a]) => ({ name, queue: c(a, ABI.taxQueue) })),
  lockWatch: D.HookrLauncher && D.MilordFamilyId && D.HookrFamilyLock
    ? { launcher: c(D.HookrLauncher, ABI.launcher), familyId: D.MilordFamilyId, familyLock: D.HookrFamilyLock }
    : null,
  wallets: pairs(env.MONITOR_WALLETS).map(([name, v]) => {
    const [address, minWei] = v.split(":");
    return { name, address, ...(minWei ? { minWei: BigInt(minWei) } : {}) };
  }),
  health: pairs(env.MONITOR_HEALTH_URLS).map(([name, url]) => ({ name, url })),
  bridges: env.BRIDGE_STATUS_URL
    ? async () => {
        const r = await fetch(env.BRIDGE_STATUS_URL, { signal: AbortSignal.timeout(5000) });
        if (!r.ok) throw new Error(`bridge status HTTP ${r.status}`);
        return (await r.json()).rows ?? [];
      }
    : null,
};

const n = (k, d) => (env[k] === undefined ? d : Number(env[k]));
startHealthServer({ port: cfg.healthPort, metrics, staleMs: 3 * cfg.intervalMs });
await runLoop({
  intervalMs: cfg.intervalMs,
  alerter,
  metrics,
  tick: async () => {
    const r = await monitorTick(contracts, {
      alerter,
      metrics,
      state,
      now: async () => (await provider.getBlock("latest")).timestamp,
      genesis: cfg.genesis,
      settlementGrace: cfg.settlementGrace,
      watchLock: (w) => watchLockHandover(w, { alerter, metrics, parentBlock: () => parentBlock(provider) }),
      navMaxAgeSecs: n("NAV_MAX_AGE_SECS", 7200),
      ppsAlertBps: n("PPS_ALERT_BPS", 100),
      feeStuckSecs: n("FEE_STUCK_SECS", 6 * 3600),
      settleStuckSecs: n("SETTLE_STUCK_SECS", 24 * 3600),
      epochGraceSecs: n("EPOCH_GRACE_SECS", 6 * 3600),
      bridgeMaxSecs: n("BRIDGE_MAX_SECS", 7200),
      ...(env.MIN_GAS_WEI ? { minGasWei: BigInt(env.MIN_GAS_WEI) } : {}),
    });
    console.log(JSON.stringify({ monitor: { active: r.active.map((x) => x.key), readErrors: r.errors } }));
    // Read errors fail the tick, so a monitor that cannot see the chain is itself alerted and shows unhealthy.
    if (r.errors.length) throw new Error(`monitor reads failed: ${r.errors.join(", ")}`);
  },
});
