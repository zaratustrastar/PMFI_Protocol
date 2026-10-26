// Monitor and runtime-hardening tests with in-memory fakes.
//   node --test offchain/services/test/
import test from "node:test";
import assert from "node:assert/strict";
import { monitorTick } from "../lib/monitor.mjs";
import { createAlerter, runLoop, createMetrics, sendAndWait, chunkedLogs, withTimeout } from "../lib/runtime.mjs";
import { DAY } from "../../lib/eligibility.mjs";

const T0 = 1_800_000_000 - (1_800_000_000 % DAY) + 12 * 3600; // 12:00 UTC
const ROOT0 = { root: "0x" + "0".repeat(64), totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: "0x" };
const memState = () => {
  let s = {};
  return { read: () => structuredClone(s), write: (x) => { s = structuredClone(x); } };
};

function world() {
  const w = {
    now: T0,
    balances: { keeper: 10n ** 17n, router: 0n },
    vault: { last: T0 - 60, pps: 10n ** 18n, paused: false, shutdown: false, pendingRedeem: 0n },
    splitter: { und: 0n, owed: 0n },
    router: { paused: false, last: BigInt(T0 - 60), minWei: 5n * 10n ** 16n, priceFails: false },
    batches: [],
    active: { ...ROOT0 },
    pending: { ...ROOT0 },
    tax: { owed: 0n, incoming: 0n },
    health: { ok: true, failures: 0 },
    bridges: [],
  };
  const c = {
    provider: { getBalance: async (a) => (a === "router" ? w.balances.router : w.balances.keeper) },
    vault: {
      lastReportTimestamp: async () => BigInt(w.vault.last), officialPPS: async () => w.vault.pps, paused: async () => w.vault.paused,
      shutdown: async () => w.vault.shutdown, totalPendingRedeemShares: async () => w.vault.pendingRedeem,
    },
    router: {
      address: "router", paused: async () => w.router.paused, executable: async () => 0n, minSwapWei: async () => w.router.minWei,
      lastExecution: async () => w.router.last,
    },
    reference: { assetPerEth: async () => { if (w.router.priceFails) throw new Error("execution reverted: SpotDeviatesFromTwap()"); return 3000n * 10n ** 6n; } },
    splitters: [{ name: "tax", contract: { undistributed: async () => w.splitter.und, totalOwed: async () => w.splitter.owed } }],
    adapter: { batchCount: async () => BigInt(w.batches.length), batch: async (i) => w.batches[i] },
    distributor: { active: async () => w.active, pending: async () => w.pending },
    taxQueues: [{ name: "buy", queue: { ledger: async () => ({ creatorOwed: w.tax.owed }), pending: async () => w.tax.incoming } }],
    wallets: [{ name: "keeper", address: "keeper" }],
    health: [{ name: "keeper", url: "http://keeper/healthz" }],
    bridges: async () => w.bridges,
  };
  const fetchFn = async () => ({ ok: w.health.ok, status: w.health.ok ? 200 : 503, json: async () => ({ lastSuccessAgeMs: 0, counters: { tick_failures: w.health.failures } }) });
  return { w, c, fetchFn };
}

function harness() {
  const { w, c, fetchFn } = world();
  const alerter = createAlerter({ service: "monitor", webhook: null, throttleMs: 0 });
  const state = memState();
  const genesis = T0 - 12 * 3600 - 21 * DAY; // 00:00 UTC, 21 daily epochs ago
  const pass = () => monitorTick(c, { alerter, state, now: async () => w.now, fetchFn, genesis, settlementGrace: 6 * 3600 });
  const keys = (r) => r.active.map((x) => x.key).sort();
  return { w, c, alerter, pass, keys, genesis };
}

test("monitor: a healthy system has no findings", async () => {
  const h = harness();
  const r = await h.pass();
  assert.deepEqual(h.keys(r), []);
  assert.deepEqual(r.errors, []);
});

test("monitor: low gas, missing NAV, paused vault, PPS jump", async () => {
  const h = harness();
  await h.pass(); // records PPS
  h.w.balances.keeper = 10n ** 15n;
  h.w.vault.last = h.w.now - 3 * 3600;
  h.w.vault.paused = true;
  h.w.vault.pps = (10n ** 18n * 103n) / 100n; // +3%
  const r = await h.pass();
  assert.deepEqual(h.keys(r), ["gas-keeper", "nav-missing", "pps-move", "vault-paused"]);
  assert.equal(r.active.find((x) => x.key === "gas-keeper").level, "critical");
  assert.equal(r.active.find((x) => x.key === "pps-move").level, "warning");
  // a PPS fall beyond the bound is critical
  h.w.vault.pps = (h.w.vault.pps * 98n) / 100n;
  const r2 = await h.pass();
  assert.equal(r2.active.find((x) => x.key === "pps-move").level, "critical");
});

test("monitor: stuck fees alert only after the grace period, and resolve", async () => {
  const h = harness();
  h.w.splitter.und = 10n ** 17n;
  h.w.tax.owed = 10n ** 16n;
  h.w.balances.router = 10n ** 17n;
  h.w.router.last = BigInt(h.w.now - 7 * 3600);
  let r = await h.pass();
  assert.deepEqual(h.keys(r), []); // onset recorded, within grace
  h.w.now += 6 * 3600 + 1;
  h.w.vault.last = h.w.now - 60;
  r = await h.pass();
  assert.deepEqual(h.keys(r), ["router-stuck", "splitter-tax-stuck", "tax-buy-stuck"]);
  h.w.splitter.und = 0n;
  h.w.tax.owed = 0n;
  h.w.balances.router = 0n;
  r = await h.pass();
  assert.deepEqual(h.keys(r), []);
  assert.equal(h.alerter.sent.filter((a) => a.level === "resolved").length, 3);
});

test("monitor: stale price, unsettled batch, unconfirmed bridge, unhealthy service, new failed ticks", async () => {
  const h = harness();
  await h.pass();
  h.w.router.priceFails = true;
  h.w.batches.push({ fundedAt: BigInt(h.w.now - 25 * 3600), settledAt: 0n, sharesMinted: 0n });
  h.w.bridges = [{ id: "w-1", kind: "polygon->robinhood", amount: "500", status: "submitted", startedAt: h.w.now - 3 * 3600 }, { id: "w-0", status: "confirmed", startedAt: 0 }];
  h.w.health.failures = 2;
  let r = await h.pass();
  assert.deepEqual(h.keys(r), ["batch-unsettled", "bridge-unconfirmed", "failed-keeper"]); // price has a 30 min grace
  h.w.now += 1801;
  h.w.vault.last = h.w.now - 60;
  h.w.health.ok = false;
  r = await h.pass();
  assert.ok(h.keys(r).includes("price-stale"), JSON.stringify(r));
  assert.ok(h.keys(r).includes("health-keeper"));
  assert.ok(!h.keys(r).includes("failed-keeper")); // no new failures since the last pass
});

test("monitor: missed daily epoch only when shares were settled and no root covers them", async () => {
  const h = harness();
  // day 1's cutoff passed long ago; one batch settled during day 1
  h.w.batches.push({ fundedAt: BigInt(h.genesis + 3600), settledAt: BigInt(h.genesis + 7200), sharesMinted: 1000n });
  let r = await h.pass();
  assert.deepEqual(h.keys(r), ["epoch-missed"]);
  h.w.active = { ...ROOT0, root: "0x" + "11".repeat(32), epoch: 2n, totalCumulative: 1000n };
  r = await h.pass();
  assert.deepEqual(h.keys(r), []);
});

test("monitor: a pending root in its review delay is not a missed day; an overdue activation is", async () => {
  const h = harness();
  h.w.batches.push({ fundedAt: BigInt(h.genesis + 3600), settledAt: BigInt(h.genesis + 19 * DAY), sharesMinted: 1000n });
  h.w.active = { ...ROOT0, root: "0x" + "11".repeat(32), epoch: 18n, totalCumulative: 400n };
  // the publisher proposed day 19 three hours ago; day 20 closed meanwhile and must wait for that root's activation
  h.w.pending = { ...ROOT0, root: "0x" + "22".repeat(32), epoch: 19n, totalCumulative: 400n, activatesAt: BigInt(h.w.now + 21 * 3600) };
  let r = await h.pass();
  assert.deepEqual(h.keys(r), [], JSON.stringify(r.active));
  // 7 h after it became activatable, still pending: keeper problem, and the publisher is now really behind
  h.w.now = Number(h.w.pending.activatesAt) + 7 * 3600;
  h.w.vault.last = h.w.now - 60;
  h.w.router.last = BigInt(h.w.now - 60);
  r = await h.pass();
  assert.deepEqual(h.keys(r), ["epoch-missed", "root-not-activated"]);
});

test("monitor: a failing read is reported and the other checks still run", async () => {
  const h = harness();
  h.c.vault.officialPPS = async () => { throw new Error("rpc down"); };
  h.w.balances.keeper = 1n;
  const r = await h.pass();
  assert.deepEqual(r.errors, ["vault"]);
  assert.deepEqual(h.keys(r), ["gas-keeper", "read-vault"]);
});

test("runtime: chunkedLogs splits ranges and halves on a range error", async () => {
  const calls = [];
  const logs = await chunkedLogs(async (a, b) => {
    calls.push([a, b]);
    if (b - a + 1 > 2500) throw new Error("query exceeds max block range 2500");
    return [a];
  }, 0, 9999, { step: 10_000, minStep: 100 });
  assert.equal(calls[0][1], 9999);
  assert.ok(calls.slice(-1)[0][1] === 9999);
  assert.deepEqual(logs, [0, 2500, 5000, 7500]);
  await assert.rejects(chunkedLogs(async () => { throw new Error("connection refused"); }, 0, 10), /connection refused/);
});

test("runtime: sendAndWait times out a dropped transaction and resets the nonce", async () => {
  let resets = 0;
  const signer = { reset: () => resets++ };
  await assert.rejects(sendAndWait(Promise.resolve({ hash: "0x1", wait: () => new Promise(() => {}) }), { timeoutMs: 20, signer }), /TxTimeout 0x1/);
  assert.equal(resets, 1);
  await assert.rejects(sendAndWait(Promise.resolve({ hash: "0x2", wait: async () => ({ status: 0 }) }), { signer }), /TxReverted 0x2/);
  const ok = await sendAndWait(Promise.resolve({ hash: "0x3", wait: async () => ({ status: 1, hash: "0x3" }) }));
  assert.equal(ok.status, 1);
});

test("runtime: the watchdog fails a hung tick and the loop keeps running", async () => {
  const alerter = createAlerter({ service: "t", webhook: null, throttleMs: 0 });
  const metrics = createMetrics("t");
  const stop = { stop: false };
  let n = 0;
  await runLoop({
    intervalMs: 0, alerter, metrics, failAlertAfter: 1, stopSignal: stop, sleepFn: async () => {}, tickTimeoutMs: 20,
    tick: async () => {
      n++;
      if (n === 1) return new Promise(() => {}); // hangs
      stop.stop = true;
    },
  });
  assert.equal(n, 2);
  assert.equal(metrics.summary().counters.tick_failures, 1);
  assert.match(alerter.sent[0].text, /TickTimeout/);
  await assert.rejects(withTimeout(new Promise(() => {}), 5, "X"), /X after 5 ms/);
});
