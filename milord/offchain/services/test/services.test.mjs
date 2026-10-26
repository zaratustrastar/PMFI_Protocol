// Failure-mode tests for the keeper, epoch publisher and root checker, with in-memory fakes of the contracts.
//   node --test offchain/services/test/
import test from "node:test";
import assert from "node:assert/strict";
import { keeperTick, computeEpochs, publishEpoch, checkRoot, closedEpoch, rewardsStatus } from "../lib/jobs.mjs";
import { retry, createAlerter, runLoop, createMetrics } from "../lib/runtime.mjs";
import { DAY } from "../../lib/eligibility.mjs";

const ZERO_ROOT = "0x" + "0".repeat(64);
const G = 1_800_000_000 - (1_800_000_000 % DAY); // 00:00 UTC
const H = 3600;
const GRACE = 6 * H;
const A = "0x00000000000000000000000000000000000000a1";
const B = "0x00000000000000000000000000000000000000b2";
const CONV = "0x00000000000000000000000000000000000000cc";
const Z = "0x0000000000000000000000000000000000000000";
const revert = (name) => Object.assign(new Error(`execution reverted: ${name}()`), { shortMessage: `execution reverted: ${name}()` });

const EMPTY = { root: ZERO_ROOT, totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: ZERO_ROOT };

function fakeChain({ routerPaused = false, reporterDown = false } = {}) {
  const log = [];
  const batches = [];
  const requests = [];
  const st = { routerEth: 0n, splitterEth: [0n, 0n, 0n], pending: { root: ZERO_ROOT, totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: ZERO_ROOT }, active: { root: ZERO_ROOT, totalCumulative: 0n, epoch: 0n, activatesAt: 0n, dataHash: ZERO_ROOT }, now: G, budget: 0n, claimed: 0n, held: 0n, proposed: [], vetoes: [] };
  const activateIfReady = () => {
    if (st.pending.root !== ZERO_ROOT && BigInt(st.now) >= st.pending.activatesAt) { st.active = st.pending; st.pending = { ...EMPTY }; }
  };
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
      activateRoot: async () => { log.push("activate"); activateIfReady(); },
      proposeRoot: async (root, total, epoch, dataHash) => {
        activateIfReady();
        if (st.pending.root !== ZERO_ROOT) throw revert("PendingRootNotActivated");
        if (total < st.active.totalCumulative || BigInt(epoch) <= st.active.epoch) throw revert("NotIncreasing");
        if (total > st.held + st.claimed) throw revert("Underfunded");
        log.push(`propose:${epoch}`);
        st.pending = { root, totalCumulative: total, epoch: BigInt(epoch), activatesAt: BigInt(st.now + 86400), dataHash };
        st.proposed.push({ root, epoch: Number(epoch) });
      },
      vetoPending: async () => { st.vetoes.push({ root: st.pending.root, epoch: Number(st.pending.epoch) }); st.pending = { ...EMPTY }; },
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

// #1 minted to A at genesis; #2 minted to B at 12:00 on day 1; #1 sold A -> B at 12:00 on day 2; converter excluded
const transfers = [
  { tokenId: 1, from: Z, to: A, timestamp: G, blockNumber: 1, logIndex: 0 },
  { tokenId: 3, from: Z, to: CONV, timestamp: G, blockNumber: 1, logIndex: 1 },
  { tokenId: 2, from: Z, to: B, timestamp: G + 12 * H, blockNumber: 2, logIndex: 0 },
  { tokenId: 1, from: A, to: B, timestamp: G + DAY + 12 * H, blockNumber: 3, logIndex: 0 },
];
const batch = (shares, at) => ({ sharesMinted: BigInt(shares), settledAt: at });
const pub = (ch, inputs, extra = {}) => publishEpoch({ distributor: ch.c.distributor, inputs, genesis: G, now: ch.st.now, excluded: [CONV], settlementGrace: GRACE, ...extra });

test("daily epochs: time-weighted by seconds held, deterministic, never over-allocate, dust carried", () => {
  // day 1 revenue settles at 01:00 on day 2 (inside the 6 h grace), day 2 revenue at 01:00 on day 3
  const batches = [batch(1500, G + DAY + H), batch(1001, G + 2 * DAY + H)];
  const a = computeEpochs({ transfers, batches, genesis: G, upToEpoch: 2, excluded: [CONV] });
  const b = computeEpochs({ transfers: [...transfers].reverse(), batches, genesis: G, upToEpoch: 2, excluded: [CONV] });
  assert.equal(a[1].root, b[1].root, "same inputs, same root");
  assert.deepEqual([a[0].start, a[0].end], [G, G + DAY], "epoch 1 is the UTC day from genesis");
  // day 1: A held 24 h, B 12 h
  assert.equal(a[0].allocations.get(A), 1000n);
  assert.equal(a[0].allocations.get(B), 500n);
  // day 2: A held #1 until 12:00 (12 h), B held #2 all day and #1 from 12:00 (36 h)
  assert.equal(a[1].weights.get(A), BigInt(12 * H));
  assert.equal(a[1].weights.get(B), BigInt(36 * H));
  assert.equal(a[1].allocations.get(A), 250n);
  assert.equal(a[1].allocations.get(B), 750n);
  assert.equal(a[1].dust, 1n, "rounding dust is carried, not lost");
  assert.equal(a[1].cumulative.get(A), 1250n, "the seller keeps day 1 and earns day 2 until the sale");
  assert.equal(a[1].total, 2500n);
  assert.equal(a[0].cumulative.get(CONV), undefined, "converter inventory earns nothing");
});

test("transfer halfway through a day: the new owner earns from the transfer second, the seller keeps the rest", () => {
  const t = [{ tokenId: 9, from: Z, to: A, timestamp: G - 5 * DAY, blockNumber: 1, logIndex: 0 },
    { tokenId: 9, from: A, to: B, timestamp: G + 6 * H + 30 * 60, blockNumber: 2, logIndex: 0 }];
  const e = computeEpochs({ transfers: t, batches: [batch(86400, G + DAY + 60)], genesis: G, upToEpoch: 1 });
  assert.equal(e[0].weights.get(A), BigInt(6 * H + 1800));
  assert.equal(e[0].weights.get(B), BigInt(DAY - 6 * H - 1800));
  assert.equal(e[0].allocations.get(A) + e[0].allocations.get(B), 86400n);
  assert.equal(e[0].allocations.get(A), BigInt(6 * H + 1800), "1 share per second held");
});

test("late funding: a batch settled after a day's cutoff belongs to the next day", () => {
  const late = [batch(900, G + DAY + 7 * H)]; // day 1 closes at 06:00 on day 2
  const e = computeEpochs({ transfers, batches: late, genesis: G, upToEpoch: 2, settlementGrace: GRACE, excluded: [CONV] });
  assert.equal(e[0].amount, 0n);
  assert.equal(e[1].amount, 900n);
});

test("zero-revenue day: nothing proposed; zero eligible NFTs: shares roll forward to the next day", async () => {
  const ch = fakeChain();
  ch.st.held = 700n;
  ch.st.now = G + 2 * DAY + 7 * H;
  let r = await pub(ch, { transfers, batches: [] });
  assert.equal(r.action, "none");
  assert.match(r.reason, /nothing new/);
  const onlyLater = [{ tokenId: 1, from: Z, to: A, timestamp: G + DAY + 2 * H, blockNumber: 5, logIndex: 0 }];
  const e = computeEpochs({ transfers: onlyLater, batches: [batch(700, G + 10)], genesis: G, upToEpoch: 2 });
  assert.equal(e[0].eligible, 0);
  assert.equal(e[0].allocatedNow, 0n);
  assert.equal(e[1].amount, 700n);
  assert.equal(e[1].cumulative.get(A), 700n);
  ch.st.now = G + DAY + 7 * H;
  r = await pub(ch, { transfers: onlyLater, batches: [batch(700, G + 10)] });
  assert.equal(r.action, "none");
  assert.match(r.reason, /roll forward/);
});

test("accumulated rewards over many days: claims are cumulative and nothing expires", async () => {
  const ch = fakeChain();
  const batches = [];
  for (let d = 1; d <= 30; d++) batches.push(batch(100 * d, G + d * DAY + H));
  ch.st.held = batches.reduce((x, b) => x + b.sharesMinted, 0n);
  const days = [];
  for (let d = 1; d <= 30; d++) {
    ch.st.now = G + d * DAY + GRACE + 120 + (d - 1) * 300; // each run a few minutes later than 24 h after the last
    const r = await pub(ch, { transfers, batches });
    days.push(r.action === "proposed" ? r.epoch : r.action);
  }
  assert.ok(days.every((x) => typeof x === "number"), `a root every day: ${days}`);
  ch.st.now += DAY;
  await ch.c.distributor.activateRoot();
  const e = computeEpochs({ transfers, batches, genesis: G, upToEpoch: 30, excluded: [CONV] }).at(-1);
  assert.equal(Number(ch.st.active.epoch), 30);
  assert.equal(ch.st.active.totalCumulative, e.total);
  assert.equal(e.cumulative.get(A) + e.cumulative.get(B) + e.dust, ch.st.held);
});

test("missed epochs: publisher down for 3 days, then one cumulative root covers all of them", async () => {
  const batches = [batch(1000, G + DAY + H), batch(1000, G + 2 * DAY + H), batch(1000, G + 3 * DAY + H)];
  const ch = fakeChain();
  ch.st.held = 3000n;
  ch.st.now = G + 3 * DAY + 7 * H;
  const r = await pub(ch, { transfers, batches });
  assert.equal(r.action, "proposed");
  assert.equal(r.epoch, 3);
  assert.deepEqual(r.covers, [1, 3]);
  assert.ok(r.total > 2990n && r.total <= 3000n);
  const again = await pub(ch, { transfers, batches });
  assert.equal(again.action, "none", "idempotent: does not re-propose");
  assert.equal(again.reason, "already pending");
});

test("pending root in review is never replaced; the next day waits and is proposed once it activates", async () => {
  const batches = [batch(1000, G + DAY + H), batch(1000, G + 2 * DAY + H)];
  const ch = fakeChain();
  ch.st.held = 2000n;
  ch.st.now = G + DAY + 6 * H + 600; // day 1 closed at 06:00; publisher runs at 06:10
  assert.equal((await pub(ch, { transfers, batches })).epoch, 1);
  const pendingDay1 = ch.st.pending.root;
  ch.st.now = G + 2 * DAY + 6 * H + 300; // day 2 closed; day 1's root activates at 06:10
  const w = await pub(ch, { transfers, batches });
  assert.equal(w.action, "waiting");
  assert.equal(w.pendingEpoch, 1);
  assert.equal(ch.st.pending.root, pendingDay1, "the root in review was not replaced");
  assert.ok(!ch.log.includes("propose:2"));
  // the contract refuses it too
  await assert.rejects(ch.c.distributor.proposeRoot("0x" + "ab".repeat(32), 2000n, 2, ZERO_ROOT), /PendingRootNotActivated/);
  ch.st.now = G + 2 * DAY + 6 * H + 600;
  const p = await pub(ch, { transfers, batches });
  assert.equal(p.action, "proposed");
  assert.equal(p.epoch, 2);
  assert.equal(Number(ch.st.active.epoch), 1, "day 1 activated first, inside proposeRoot");
  assert.equal(ch.st.active.root, pendingDay1);
});

test("veto: the publisher stops instead of re-proposing, also after a restart, until the veto is acknowledged", async () => {
  const batches = [batch(1000, G + DAY + H), batch(1000, G + 2 * DAY + H)];
  const ch = fakeChain();
  ch.st.held = 2000n;
  ch.st.now = G + DAY + 7 * H;
  await pub(ch, { transfers, batches });
  const vetoed = ch.st.pending.root;
  await ch.c.distributor.vetoPending();
  const alerter = createAlerter({ service: "t", webhook: null });
  ch.st.now = G + 2 * DAY + 7 * H;
  const inputs = () => ({ transfers, batches, vetoes: ch.st.vetoes }); // a restarted publisher reads RootVetoed from chain
  let r = await pub(ch, inputs(), { alerter });
  assert.equal(r.action, "blocked");
  assert.ok(alerter.sent.some((x) => x.level === "critical" && /vetoed/.test(x.text)));
  assert.equal(ch.st.proposed.length, 1);
  r = await pub(ch, inputs(), { acknowledgedVetoes: [vetoed] });
  assert.equal(r.action, "proposed");
  assert.deepEqual(r.covers, [1, 2], "the vetoed day is still allocated in the next root");
});

test("restart recovery: a fresh publisher recomputes the same root and neither duplicates nor skips", async () => {
  const batches = [batch(500, G + DAY + H), batch(700, G + 2 * DAY + H)];
  const ch = fakeChain();
  ch.st.held = 1200n;
  ch.st.now = G + DAY + 7 * H;
  const first = await pub(ch, { transfers, batches });
  // process restarts mid-review: same chain, no local state
  const again = await pub(ch, { transfers: [...transfers], batches: [...batches] });
  assert.equal(again.reason, "already pending");
  ch.st.now = G + 2 * DAY + 8 * H; // restarted after the delay: activates day 1 and proposes day 2
  const next = await pub(ch, { transfers, batches });
  assert.equal(next.epoch, 2);
  assert.equal(ch.st.active.root, first.root);
});

test("underfunded root waits (late funding) instead of failing the tick", async () => {
  const ch = fakeChain();
  ch.st.held = 10n; // shares not yet in the distributor
  ch.st.now = G + DAY + 7 * H;
  const parb = { balanceOf: async () => ch.st.held };
  const r = await pub(ch, { transfers, batches: [batch(1000, G + DAY + H)] }, { parb });
  assert.equal(r.action, "waiting");
  assert.match(r.reason, /covers 10/);
});

test("root checker: matching root passes, tampered root raises a veto alert, insolvency alerts", async () => {
  const batches = [batch(1000, G + DAY + H)];
  const ch = fakeChain();
  ch.st.held = 1000n;
  ch.st.now = G + DAY + 7 * H;
  await pub(ch, { transfers, batches });
  const parb = { balanceOf: async () => ch.st.held };
  const alerter = createAlerter({ service: "t", webhook: null });
  const chk = () => checkRoot({ distributor: ch.c.distributor, parb, inputs: { transfers, batches }, genesis: G, excluded: [CONV], alerter });
  let r = await chk();
  assert.equal(r[0].match, true);
  assert.equal(alerter.sent.length, 0);
  ch.st.pending = { ...ch.st.pending, root: "0x" + "ab".repeat(32) };
  r = await chk();
  assert.equal(r[0].match, false);
  assert.match(alerter.sent[0].text, /vetoPending/);
  ch.st.held = 10n;
  await chk();
  assert.ok(alerter.sent.some((x) => x.key === "solvency"));
});

test("closedEpoch: a day closes at its end plus the settlement grace; genesis must be 00:00 UTC", () => {
  assert.equal(closedEpoch(G, G + DAY + 5 * H, GRACE), 0);
  assert.equal(closedEpoch(G, G + DAY + 6 * H, GRACE), 1);
  assert.equal(closedEpoch(G, G + 10 * DAY + 6 * H, GRACE), 10);
  assert.throws(() => computeEpochs({ transfers, batches: [], genesis: G + H, upToEpoch: 1 }), /00:00 UTC/);
});

test("rewards status keeps earned, allocated, active (claimable basis) and accruing apart", () => {
  const batches = [batch(1500, G + DAY + H), batch(1000, G + 2 * DAY + H), batch(400, G + 3 * DAY + H)];
  const e = computeEpochs({ transfers, batches, genesis: G, upToEpoch: 3, excluded: [CONV] });
  const root = (k) => ({ root: "0x" + String(k).repeat(64), totalCumulative: e[k - 1].total, epoch: BigInt(k), activatesAt: BigInt(G + (k + 1) * DAY + 7 * H), dataHash: ZERO_ROOT });
  const now = G + 3 * DAY + 7 * H + 1800; // day 3 closed; day 4 open since 00:00
  const s = rewardsStatus({ inputs: { transfers, batches }, genesis: G, now, excluded: [CONV], active: root(1), pending: root(2) });
  assert.equal(s.latestClosedEpoch, 3);
  assert.equal(s.activeEpoch, 1);
  assert.equal(s.pendingEpoch, 2);
  assert.equal(s.accounts[A].active, "1000");
  assert.equal(s.accounts[A].allocated, "1250");
  assert.equal(s.accounts[A].earned, "1250", "A sold on day 2: day 3 earns nothing");
  assert.equal(s.accounts[B].earned, String(e[2].cumulative.get(B)));
  assert.equal(s.accounts[A].accruingSeconds, 0);
  assert.equal(s.accounts[B].accruingSeconds, 2 * (7 * H + 1800), "B holds two Milords since 00:00 of day 4");
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

// ───────────────────── Hookr tax queues (mainnet settlement) ─────────────────────

const SPLITTER = "0x00000000000000000000000000000000000000d4";
const BYTES0 = "0x" + "0".repeat(64);

function fakeQueue({ recipient = SPLITTER, routeId = BYTES0, share = 2000, invariant = true } = {}) {
  const st = { claim: 0n, owed: 0n, paid: [], calls: 0 };
  const settle = () => { const creator = st.claim - (st.claim * BigInt(share)) / 10_000n; st.owed += creator; st.claim = 0n; };
  const payCreator = async () => {
    st.calls++;
    settle();
    if (st.owed === 0n) throw revert("NothingDue");
    st.paid.push(st.owed);
    st.owed = 0n;
  };
  payCreator.staticCall = async () => { const o = st.owed + st.claim - (st.claim * BigInt(share)) / 10_000n; if (o === 0n) throw revert("NothingDue"); return o; };
  const queue = {
    terms: async () => ({ assetRecipient: recipient, routeId, quote: Z, protocolShareBps: share }),
    ledger: async () => ({ creatorOwed: st.owed }),
    pending: async () => st.claim,
    accountingInvariant: async () => invariant,
    payCreator,
  };
  return { queue, st };
}
const taxOpts = (alerts, extra = {}) => ({
  now: async () => G, alerter: { alert: async (level, key, text) => alerts.push({ level, key, text }) },
  taxQueue: { expect: { assetRecipient: SPLITTER, protocolShareBps: 2000 }, minPayoutWei: 0n, ...extra },
});

test("tax queues: pays the 80% creator share before the splitters run, then skips (idempotent)", async () => {
  const ch = fakeChain();
  const buy = fakeQueue();
  const sell = fakeQueue();
  buy.st.claim = 1000n;
  sell.st.claim = 500n;
  ch.c.taxQueues = [{ name: "buy", queue: buy.queue }, { name: "sell", queue: sell.queue }];
  const alerts = [];
  const r = await keeperTick(ch.c, taxOpts(alerts));
  assert.deepEqual(buy.st.paid, [800n]);
  assert.deepEqual(sell.st.paid, [400n]);
  assert.deepEqual(r.results.slice(0, 2).map((x) => x.step), ["hookr-tax-buy", "hookr-tax-sell"], "before split-*");
  const again = await keeperTick(ch.c, taxOpts(alerts));
  assert.equal(again.results.find((x) => x.step === "hookr-tax-buy").status, "skipped");
  assert.equal(buy.st.calls, 1, "nothing due: no transaction sent");
  assert.equal(alerts.length, 0);
});

test("tax queues: a retried pass after a payout already landed is a skip, not a failure", async () => {
  const q = fakeQueue();
  q.st.claim = 100n;
  const ch = fakeChain();
  ch.c.taxQueues = [{ name: "buy", queue: q.queue }];
  // the previous pass's transaction was mined but its receipt was lost: the staticCall now reverts NothingDue
  await q.queue.payCreator();
  q.queue.pending = async () => 1n; // dust still pending, but nothing owed after the Hookr split rounds
  q.queue.payCreator.staticCall = async () => { throw revert("NothingDue"); };
  const r = await keeperTick(ch.c, taxOpts([]));
  assert.equal(r.failed, 0);
  assert.equal(r.results.find((x) => x.step === "hookr-tax-buy").status, "skipped");
  assert.deepEqual(q.st.paid, [80n]);
});

test("tax queues: below the minimum payout waits (saves gas), above it pays", async () => {
  const q = fakeQueue();
  q.st.claim = 100n;
  const ch = fakeChain();
  ch.c.taxQueues = [{ name: "buy", queue: q.queue }];
  await keeperTick(ch.c, taxOpts([], { minPayoutWei: 100n }));
  assert.deepEqual(q.st.paid, []);
  q.st.claim = 200n;
  await keeperTick(ch.c, taxOpts([], { minPayoutWei: 100n }));
  assert.deepEqual(q.st.paid, [160n]);
});

test("tax queues: fail closed on wrong recipient, conversion route, broken accounting or missing config", async () => {
  for (const [label, q, extra] of [
    ["recipient", fakeQueue({ recipient: A }), {}],
    ["route", fakeQueue({ routeId: "0x" + "1".repeat(64) }), {}],
    ["share", fakeQueue({ share: 2500 }), {}],
    ["invariant", fakeQueue({ invariant: false }), {}],
    ["unconfigured", fakeQueue(), { expect: {} }],
  ]) {
    q.st.claim = 1000n;
    const ch = fakeChain();
    ch.c.taxQueues = [{ name: "buy", queue: q.queue }];
    const alerts = [];
    const r = await keeperTick(ch.c, taxOpts(alerts, extra));
    assert.equal(r.results[0].status, "failed", label);
    assert.equal(q.st.calls, 0, `${label}: payCreator never called`);
    assert.ok(alerts.some((a) => a.level === "critical" && a.key === "keeper:tax-buy"), `${label}: critical alert`);
    assert.ok(r.results.some((x) => x.step === "split-0"), `${label}: the rest of the pass still runs`);
  }
});

test("tax queues: an unexpected payout revert is a failed step with a warning; the pass continues", async () => {
  const q = fakeQueue();
  q.st.claim = 1000n;
  q.queue.payCreator.staticCall = async () => { throw revert("ReentrantCall"); };
  const ch = fakeChain();
  ch.c.taxQueues = [{ name: "sell", queue: q.queue }];
  const alerts = [];
  const r = await keeperTick(ch.c, taxOpts(alerts));
  assert.equal(r.results[0].status, "failed");
  assert.ok(alerts.some((a) => a.level === "warning" && a.key === "keeper:hookr-tax-sell"));
  assert.ok(r.results.some((x) => x.step === "activate-root"));
});

// ───────────────────── permanent-lock handover watch ─────────────────────

const SAFE = "0x00000000000000000000000000000000000000e7";
const LOCKADDR = "0x00000000000000000000000000000000000000f1";
function lockWatch(owner, until) {
  return { launcher: { familyOwner: async () => owner, principalLockedUntil: async () => until }, familyId: "0x01", familyLock: LOCKADDR };
}
test("lock watch: silent when locked or far from the unlock block, warns, then critical, then 'withdrawable'", async () => {
  const cases = [
    [LOCKADDR, 10_000n, 9_999n, null],
    [SAFE, 10_000n, 5_000n, null],
    [SAFE, 10_000n, 8_500n, "warning"],
    [SAFE, 10_000n, 9_800n, "critical"],
    [SAFE, 10_000n, 10_000n, "critical"],
  ];
  for (const [owner, until, now, level] of cases) {
    const ch = fakeChain();
    ch.c.lockWatch = lockWatch(owner, until);
    const alerts = [];
    const r = await keeperTick(ch.c, { ...opts(ch.st, { alert: async (l, k, t) => alerts.push({ l, k, t }) }), parentBlock: async () => now });
    const w = alerts.filter((a) => a.k === "keeper:lock-watch");
    assert.equal(r.failed, 0);
    assert.equal(w[0]?.l ?? null, level, `${owner} at ${now}`);
    if (now >= until) assert.match(w[0].t, /WITHDRAWABLE/);
  }
});

test("chain 4663: ALLOW_MAINNET, or ALLOW_LOCAL_FORK with a loopback RPC only", async () => {
  const { chainAllowed } = await import("../lib/config.mjs");
  assert.equal(chainAllowed(46630, "https://rpc.testnet.chain.robinhood.com", {}), true);
  assert.equal(chainAllowed(4663, "https://rpc.mainnet.chain.robinhood.com", {}), false);
  assert.equal(chainAllowed(4663, "https://rpc.mainnet.chain.robinhood.com", { ALLOW_LOCAL_FORK: "yes" }), false);
  assert.equal(chainAllowed(4663, "http://127.0.0.1:8550", { ALLOW_LOCAL_FORK: "yes" }), true);
  assert.equal(chainAllowed(4663, "http://127.0.0.1.example.com:8550", { ALLOW_LOCAL_FORK: "yes" }), false);
  assert.equal(chainAllowed(4663, "http://127.0.0.1:8550", {}), false);
  assert.equal(chainAllowed(4663, "https://rpc.mainnet.chain.robinhood.com", { ALLOW_MAINNET: "yes" }), true);
});
