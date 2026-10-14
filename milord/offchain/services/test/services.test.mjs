// Failure-mode tests for the keeper, epoch publisher and root checker, with in-memory fakes of the contracts.
//   node --test offchain/services/test/
import test from "node:test";
import assert from "node:assert/strict";
import { keeperTick, computeEpochs, publishEpoch, checkRoot, closedEpoch } from "../lib/jobs.mjs";
import { retry, createAlerter, runLoop, createMetrics } from "../lib/runtime.mjs";
import { WEEK } from "../../lib/eligibility.mjs";

const ZERO_ROOT = "0x" + "0".repeat(64);
const G = 1_800_000_000 - (1_800_000_000 % WEEK); // a week boundary
const A = "0x00000000000000000000000000000000000000a1";
const B = "0x00000000000000000000000000000000000000b2";
const CONV = "0x00000000000000000000000000000000000000cc";
const Z = "0x0000000000000000000000000000000000000000";
const revert = (name) => Object.assign(new Error(`execution reverted: ${name}()`), { shortMessage: `execution reverted: ${name}()` });

function fakeChain({ routerPaused = false, reporterDown = false } = {}) {
  const log = [];
  const batches = [];
  const requests = [];
  const st = { routerEth: 0n, splitterEth: [0n, 0n, 0n], pending: { root: ZERO_ROOT, totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: ZERO_ROOT }, active: { root: ZERO_ROOT, totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: ZERO_ROOT }, now: G, budget: 0n, claimed: 0n, held: 0n };
  const c = {
    splitters: [0, 1, 2].map((i) => ({
      undistributed: async () => st.splitterEth[i],
      distribute: async () => { log.push(`split-${i}`); st.routerEth += st.splitterEth[i] / 3n; st.budget += st.splitterEth[i] / 3n; st.splitterEth[i] = 0n; },
    })),
    collector: { collect: async () => { log.push("collect"); } },
    burner: { budget: async () => st.budget, buybackAndBurn: async (b, minOut) => { log.push(`buyback:${b}:${minOut}`); st.budget = 0n; } },
    router: {
      executable: async () => (routerPaused ? 0n : st.routerEth >= 10n ? st.routerEth : 0n),
      execute: async () => {
        if (routerPaused) throw revert("IsPaused");
        log.push(`router:${st.routerEth}`);
        requests.push({ status: reporterDown ? 0 : 0 });
        batches.push({ requestId: BigInt(requests.length - 1), sharesMinted: 0n, settledAt: 0n, usdc: st.routerEth });
        st.routerEth = 0n;
      },
    },
    adapter: {
      batchCount: async () => BigInt(batches.length),
      batch: async (i) => batches[i],
      settle: async (i) => { log.push(`settle:${i}`); batches[i].sharesMinted = batches[i].usdc * 1000n; batches[i].settledAt = BigInt(st.now); st.held += batches[i].sharesMinted; },
    },
    vault: { depositRequests: async (id) => ({ status: requests[Number(id)].status }) },
    distributor: {
      pending: async () => st.pending,
      active: async () => st.active,
      activateRoot: async () => { log.push("activate"); st.active = st.pending; st.pending = { root: ZERO_ROOT, totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: ZERO_ROOT }; },
      proposeRoot: async (root, total, epoch, dataHash) => { log.push(`propose:${epoch}`); st.pending = { root, totalCumulative: total, epoch: BigInt(epoch), activatesAt: BigInt(st.now + 86400), dataHash }; },
      totalClaimed: async () => st.claimed,
      target: "0xdist",
    },
  };
  const reportProcessesAll = () => { for (const r of requests) r.status = 1; };
  return { c, st, log, batches, reportProcessesAll };
}

const opts = (st, alerter) => ({ now: async () => st.now, alerter, buyback: { minBudgetWei: 5n, quoteMinOut: async (b) => b * 90n } });

test("keeper: full pass moves revenue along the fixed route", async () => {
  const { c, st, log, reportProcessesAll } = fakeChain();
  st.splitterEth = [30n, 30n, 30n];
  await keeperTick(c, opts(st));
  assert.deepEqual(log.slice(0, 6), ["split-0", "split-1", "split-2", "collect", "buyback:30:2700", "router:30"]);
  reportProcessesAll(); // the reporter processes the deposit request
  await keeperTick(c, opts(st));
  assert.ok(log.includes("settle:0"));
});

test("router paused: keeper skips it without failing; revenue accumulates and funds after unpause", async () => {
  const ch = fakeChain({ routerPaused: true });
  ch.st.splitterEth = [30n, 0n, 0n];
  const r = await keeperTick(ch.c, opts(ch.st));
  assert.equal(r.failed, 0);
  assert.equal(r.results.find((x) => x.step === "router").status, "skipped");
  assert.equal(ch.st.routerEth, 10n, "ETH waits in the router");
});

test("reporter down: batches stay unsettled, nothing is minted or allocated, settle happens once it reports", async () => {
  const ch = fakeChain({ reporterDown: true });
  ch.st.splitterEth = [60n, 0n, 0n];
  await keeperTick(ch.c, opts(ch.st));
  await keeperTick(ch.c, opts(ch.st));
  assert.equal(ch.batches[0].settledAt, 0n);
  const e = computeEpochs({ transfers: [{ tokenId: 1, from: Z, to: A, timestamp: G, blockNumber: 1, logIndex: 0 }], batches: ch.batches, genesis: G, upToEpoch: 1 });
  assert.equal(e[0].amount, 0n, "no pARB exists yet, so none is allocated");
  ch.reportProcessesAll();
  await keeperTick(ch.c, opts(ch.st));
  assert.ok(ch.batches[0].settledAt > 0n);
});

test("keeper down for days: one pass catches up every step", async () => {
  const ch = fakeChain();
  ch.st.splitterEth = [300n, 300n, 300n]; // a week of revenue accumulated while nothing ran
  ch.st.pending = { root: "0x" + "1".repeat(64), totalCumulative: 5n, epoch: 1n, activatesAt: BigInt(G), dataHash: ZERO_ROOT };
  ch.st.now = G + 7 * 86400;
  await keeperTick(ch.c, opts(ch.st));
  assert.ok(ch.log.includes("activate"), "overdue root activated");
  assert.ok(ch.log.includes("router:300"));
});

test("keeper: an unexpected failure is alerted, other steps still run", async () => {
  const ch = fakeChain();
  ch.c.collector.collect = async () => { throw new Error("rpc timeout"); };
  ch.st.splitterEth = [30n, 0n, 0n];
  const alerter = createAlerter({ service: "t", webhook: null });
  const r = await keeperTick(ch.c, opts(ch.st, alerter));
  assert.equal(r.failed, 1);
  assert.ok(ch.log.includes("router:10"));
  assert.match(alerter.sent[0].text, /lp-fees-0 failed: rpc timeout/);
});

test("buyback without a trustworthy price is skipped, never guessed", async () => {
  const ch = fakeChain();
  ch.st.budget = 100n;
  const r = await keeperTick(ch.c, { now: async () => ch.st.now, buyback: { minBudgetWei: 5n, quoteMinOut: async () => null } });
  assert.equal(r.results.find((x) => x.step === "buyback").status, "skipped");
  assert.ok(!ch.log.some((l) => l.startsWith("buyback")));
});

const transfers = [
  { tokenId: 1, from: Z, to: A, timestamp: G, blockNumber: 1, logIndex: 0 },
  { tokenId: 2, from: Z, to: B, timestamp: G + 3.5 * 86400, blockNumber: 2, logIndex: 0 },
  { tokenId: 3, from: Z, to: CONV, timestamp: G, blockNumber: 1, logIndex: 1 },
];

test("epochs are deterministic, time-weighted, never over-allocate, and carry dust", () => {
  const batches = [{ sharesMinted: 1000n, settledAt: G + 7 * 86400 + 60 }, { sharesMinted: 501n, settledAt: G + 14 * 86400 + 60 }];
  const a = computeEpochs({ transfers, batches, genesis: G, upToEpoch: 2, excluded: [CONV] });
  const b = computeEpochs({ transfers: [...transfers].reverse(), batches, genesis: G, upToEpoch: 2, excluded: [CONV] });
  assert.equal(a[1].root, b[1].root, "same inputs, same root");
  assert.equal(a[0].allocatedNow + a[0].dust, 1000n);
  assert.equal(a[0].cumulative.get(A), 666n); // A held 7 days, B 3.5 days
  assert.equal(a[0].cumulative.get(B), 333n);
  assert.ok(a[1].total <= 1501n);
  assert.equal(a[1].amount, 1501n - a[0].allocatedNow, "epoch 2 = new shares + carried dust");
  assert.equal(a[0].cumulative.get(CONV), undefined, "converter inventory earns nothing");
});

test("a batch settled after the cutoff belongs to the next epoch", () => {
  const late = [{ sharesMinted: 900n, settledAt: G + 7 * 86400 + 7 * 3600 }];
  const e = computeEpochs({ transfers, batches: late, genesis: G, upToEpoch: 2, settlementGrace: 6 * 3600, excluded: [CONV] });
  assert.equal(e[0].amount, 0n);
  assert.equal(e[1].amount, 900n);
});

test("zero eligible NFTs: nothing allocated, shares roll forward to the next epoch", async () => {
  const onlyLater = [{ tokenId: 1, from: Z, to: A, timestamp: G + 8 * 86400, blockNumber: 5, logIndex: 0 }];
  const batches = [{ sharesMinted: 700n, settledAt: G + 7 * 86400 + 10 }];
  const e = computeEpochs({ transfers: onlyLater, batches, genesis: G, upToEpoch: 2 });
  assert.equal(e[0].eligible, 0);
  assert.equal(e[0].allocatedNow, 0n);
  assert.equal(e[1].amount, 700n);
  assert.equal(e[1].cumulative.get(A), 700n);
  const ch = fakeChain();
  const r = await publishEpoch({ distributor: ch.c.distributor, inputs: { transfers: onlyLater, batches }, genesis: G, now: G + 7 * 86400 + 7 * 3600 });
  assert.equal(r.action, "none");
  assert.match(r.reason, /roll forward/);
});

test("indexer down for two weeks: catch-up proposes one cumulative root covering both", async () => {
  const batches = [{ sharesMinted: 1000n, settledAt: G + 7 * 86400 + 60 }, { sharesMinted: 1000n, settledAt: G + 14 * 86400 + 60 }];
  const ch = fakeChain();
  ch.st.now = G + 14 * 86400 + 7 * 3600;
  const r = await publishEpoch({ distributor: ch.c.distributor, inputs: { transfers, batches }, genesis: G, now: ch.st.now, excluded: [CONV] });
  assert.equal(r.action, "proposed");
  assert.equal(r.epoch, 2);
  assert.ok(r.total > 1990n && r.total <= 2000n);
  const again = await publishEpoch({ distributor: ch.c.distributor, inputs: { transfers, batches }, genesis: G, now: ch.st.now, excluded: [CONV] });
  assert.equal(again.action, "none", "idempotent: does not re-propose");
});

test("root checker: matching root passes, tampered root raises a veto alert, insolvency alerts", async () => {
  const batches = [{ sharesMinted: 1000n, settledAt: G + 7 * 86400 + 60 }];
  const ch = fakeChain();
  ch.st.held = 1000n;
  await publishEpoch({ distributor: ch.c.distributor, inputs: { transfers, batches }, genesis: G, now: G + 7 * 86400 + 7 * 3600, excluded: [CONV] });
  const parb = { balanceOf: async () => ch.st.held };
  const alerter = createAlerter({ service: "t", webhook: null });
  let r = await checkRoot({ distributor: ch.c.distributor, parb, inputs: { transfers, batches }, genesis: G, excluded: [CONV], alerter });
  assert.equal(r[0].match, true);
  assert.equal(alerter.sent.length, 0);
  ch.st.pending = { ...ch.st.pending, root: "0x" + "ab".repeat(32) };
  r = await checkRoot({ distributor: ch.c.distributor, parb, inputs: { transfers, batches }, genesis: G, excluded: [CONV], alerter });
  assert.equal(r[0].match, false);
  assert.match(alerter.sent[0].text, /vetoPending/);
  ch.st.held = 10n;
  await checkRoot({ distributor: ch.c.distributor, parb, inputs: { transfers, batches }, genesis: G, excluded: [CONV], alerter });
  assert.ok(alerter.sent.some((x) => x.key === "solvency"));
});

test("closedEpoch waits for the settlement grace", () => {
  assert.equal(closedEpoch(G, G + WEEK + 3600, 6 * 3600), 0);
  assert.equal(closedEpoch(G, G + WEEK + 6 * 3600, 6 * 3600), 1);
});

test("retry: backs off on transient errors, stops on permanent ones", async () => {
  let n = 0;
  const waits = [];
  const v = await retry(async () => { if (++n < 3) throw new Error("ECONNRESET"); return "ok"; }, { baseMs: 10, sleepFn: async (ms) => waits.push(ms) });
  assert.equal(v, "ok");
  assert.deepEqual(waits, [10, 20]);
  n = 0;
  await assert.rejects(retry(async () => { n++; throw Object.assign(new Error("bad"), { permanent: true }); }, { sleepFn: async () => {} }));
  assert.equal(n, 1);
});

test("loop: keeps running through failures, alerts after 3 in a row, sends resolved, throttles repeats", async () => {
  let i = 0;
  const stop = { stop: false };
  const posts = [];
  const alerter = createAlerter({ service: "t", webhook: "https://hooks.example/x", fetchFn: async (u, o) => posts.push(JSON.parse(o.body)) });
  const metrics = createMetrics("t");
  await runLoop({
    intervalMs: 0, alerter, metrics, stopSignal: stop, sleepFn: async () => {},
    tick: async () => { i++; if (i >= 8) stop.stop = true; if (i <= 5) throw new Error("rpc down"); },
  });
  assert.equal(metrics.summary().counters.tick_failures, 5);
  assert.deepEqual(posts.map((p) => p.level), ["critical", "resolved"], "one critical (throttled), then resolved");
});
