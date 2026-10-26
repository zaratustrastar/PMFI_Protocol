// The three MILORD automation jobs as plain functions over contract handles, so the same code runs in the
// services, the anvil simulation and the failure tests (with fakes).
//
//   keeperTick   - permissionless upkeep: settle tax, split revenue, collect LP fees into buyback-and-burn,
//                  bounded buyback, ETH -> USDG router, settle vault deposits, activate roots.
//   computeEpochs - deterministic DAILY pARB allocation from finalized NFT transfers and settled batches.
//   publishEpoch / checkRoot - propose the next root (publisher key) / recompute it independently and alert.
import { keccak256, toUtf8Bytes } from "ethers";
import { ownershipIntervals, eligibility, allocate, accumulate, buildTree, assertUtcMidnight, EPOCH } from "../../lib/eligibility.mjs";
import { isExpectedRevert, sendAndWait, chunkedLogs } from "./runtime.mjs";

const ZERO = "0x0000000000000000000000000000000000000000";
const msg = (e) => String(e?.shortMessage ?? e?.reason ?? e?.message ?? e);
// Every keeper/publisher transaction is bounded: not mined in TX_TIMEOUT_SECS (default 180) -> TxTimeout, alerted.
const TX_TIMEOUT_MS = Number(process.env.TX_TIMEOUT_SECS ?? 180) * 1000;
const wait = (txp) => sendAndWait(txp, { timeoutMs: TX_TIMEOUT_MS });

// ───────────────────────────── keeper ─────────────────────────────

/**
 * One keeper pass. Each step is independent: an expected revert (paused, below threshold, nothing to settle)
 * is recorded as "skipped"; any other failure is recorded as "failed" and alerted, and the pass continues.
 * Nothing here can move funds anywhere except along the contracts' fixed routes.
 *
 * c: { hookrSettle?, taxQueues?: [{ name, queue }], splitters[], collector?, burner?, router?, adapter, vault, distributor }
 * opts: { buyback: { minBudgetWei, quoteMinOut(budget) -> bigint | null, deadlineSecs }, now(), alerter, members: [0],
 *         taxQueue: { expect: { assetRecipient, protocolShareBps }, minPayoutWei }, metrics }
 */
export async function keeperTick(c, opts = {}) {
  const res = [];
  const run = async (name, fn) => {
    try {
      const out = await fn();
      res.push({ step: name, status: out === undefined ? "done" : out === null ? "skipped" : "done", detail: out ?? undefined });
    } catch (e) {
      const expected = isExpectedRevert(e);
      res.push({ step: name, status: expected ? "skipped" : "failed", error: msg(e) });
      if (!expected && opts.alerter) await opts.alerter.alert("warning", `keeper:${name}`, `${name} failed: ${msg(e)}`);
    }
  };

  // 1. Hookr tax settlement. Local simulation: the mock tax hook. Mainnet: the pair of HookrTaxQueue clones
  //    (buy, sell) frozen at launch; payCreator pays the 80% creator share in ETH to TradingTaxSplitter.
  if (c.hookrSettle) await run("hookr-settle", async () => { await wait(c.hookrSettle()); });
  for (const { name, queue } of c.taxQueues ?? []) {
    await run(`hookr-tax-${name}`, () => payTaxQueue(name, queue, opts));
  }

  // 2. revenue splitters (ETH)
  for (const [i, s] of (c.splitters ?? []).entries()) {
    await run(`split-${i}`, async () => {
      if ((await s.undistributed(ZERO)) === 0n) return null;
      await wait(s.distribute(ZERO));
    });
  }

  // 3. LP fees of the locked founding position -> BuybackBurner (ETH) and burn (MILORD)
  if (c.collector) {
    for (const m of opts.members ?? [0]) await run(`lp-fees-${m}`, async () => { await wait(c.collector.collect(m)); });
  }

  // 4. bounded buyback (operator key; min output from the off-chain MILORD TWAP, never from spot)
  if (c.burner && opts.buyback) {
    await run("buyback", async () => {
      const budget = await c.burner.budget();
      if (budget < opts.buyback.minBudgetWei) return null;
      const minOut = await opts.buyback.quoteMinOut(budget);
      if (minOut === null || minOut === undefined) return null; // no trustworthy reference price: skip, never guess
      await wait(c.burner.buybackAndBurn(budget, minOut, (await opts.now()) + (opts.buyback.deadlineSecs ?? 600)));
      return { budget };
    });
  }

  // 5. arbitrage share: ETH -> USDG -> vault deposit request (router enforces every bound)
  if (c.router) {
    await run("router", async () => {
      if ((await c.router.executable()) === 0n) return null;
      await wait(c.router.execute());
    });
  }

  // 6. settle every batch the vault has processed (permissionless; pARB minted to the distributor)
  await run("settle", async () => {
    const n = Number(await c.adapter.batchCount());
    const settled = [];
    for (let i = 0; i < n; i++) {
      const b = await c.adapter.batch(i);
      if (b.settledAt !== 0n && b.settledAt !== 0) continue;
      const r = await c.vault.depositRequests(b.requestId);
      if (Number(r.status ?? r[4]) !== 1) continue; // still pending: the reporter has not processed it yet
      await wait(c.adapter.settle(i));
      settled.push(i);
    }
    return settled.length ? { settled } : null;
  });

  // 7. activate a pending root once its delay has passed (permissionless)
  await run("activate-root", async () => {
    const p = await c.distributor.pending();
    const root = p.root ?? p[0];
    const eta = BigInt(p.activatesAt ?? p[3]);
    if (/^0x0+$/.test(root) || BigInt(await opts.now()) < eta) return null;
    await wait(c.distributor.activateRoot());
  });

  // 8. read-only watch on the permanent-lock handover (docs/launch/LOCK_PROCEDURE.md)
  if (c.lockWatch) await run("lock-watch", () => watchLockHandover(c.lockWatch, opts));

  const failed = res.filter((r) => r.status === "failed");
  return { results: res, failed: failed.length };
}

/**
 * Until HookrFamilyLock owns the MILORD family, principal becomes withdrawable by the family owner at
 * principalLockedUntil (a PARENT-chain block: block.number on Robinhood Chain, not eth_blockNumber). Alerts as that
 * block approaches and, once it has passed, on every pass. Read-only: never sends anything.
 * w: { launcher, familyId, familyLock, warnBlocks = [1800, 300] }; opts.parentBlock() -> current block.number.
 */
export async function watchLockHandover(w, opts = {}) {
  const owner = String(await w.launcher.familyOwner(w.familyId)).toLowerCase();
  if (owner === w.familyLock.toLowerCase()) return null; // locked: nothing to watch
  const until = BigInt(await w.launcher.principalLockedUntil(w.familyId));
  const now = BigInt(await opts.parentBlock());
  const left = until - now;
  opts.metrics?.set("lock_blocks_left", left);
  const [early, late] = (w.warnBlocks ?? [1800, 300]).map(BigInt);
  const level = left <= 0n ? "critical" : left <= late ? "critical" : left <= early ? "warning" : null;
  if (level && opts.alerter) {
    const text = left <= 0n
      ? `MILORD principal is WITHDRAWABLE by the family owner ${owner} since parent block ${until}: execute the Safe lock batch now`
      : `MILORD permanent lock not executed: principal unlocks in ${left} parent blocks (~${(Number(left) * 12) / 60} min, block ${until})`;
    await opts.alerter.alert(level, "keeper:lock-watch", text);
  }
  return { owner, until, left };
}

/**
 * Pay one HookrTaxQueue's creator share to TradingTaxSplitter. Idempotent: payCreator pays exactly what the queue
 * owes and then owes nothing, so a repeated or retried call reverts NothingDue (a skip). Fails closed: if the queue's
 * frozen terms are not MILORD's (recipient, direct ETH payout, 20% Hookr share) or its own accounting invariant does
 * not hold, nothing is called and a critical alert goes out; that needs a person, not a retry.
 */
export async function payTaxQueue(name, q, opts = {}) {
  const t = await q.terms();
  const exp = opts.taxQueue?.expect ?? {};
  const bad = [];
  if (!exp.assetRecipient) bad.push("no expected recipient configured (TradingTaxSplitter)");
  else if (t.assetRecipient.toLowerCase() !== exp.assetRecipient.toLowerCase()) bad.push(`assetRecipient ${t.assetRecipient}`);
  if (!/^0x0+$/.test(t.routeId)) bad.push(`routeId ${t.routeId} (conversion route: direct payout disabled)`);
  if (BigInt(t.quote) !== 0n) bad.push(`quote ${t.quote} is not native ETH`);
  if (exp.protocolShareBps !== undefined && Number(t.protocolShareBps) !== Number(exp.protocolShareBps)) bad.push(`protocolShareBps ${t.protocolShareBps}`);
  if (!(await q.accountingInvariant())) bad.push("accountingInvariant() is false");
  if (bad.length) {
    if (opts.alerter) await opts.alerter.alert("critical", `keeper:tax-${name}`, `Hookr ${name} tax queue refused, nothing paid: ${bad.join("; ")}`);
    throw new Error(`tax queue ${name} refused: ${bad.join("; ")}`);
  }

  const owed = BigInt((await q.ledger()).creatorOwed);
  const incoming = BigInt(await q.pending()); // not yet booked: pulled from HookrRules and split on the next settle
  const due = owed + incoming - (incoming * BigInt(t.protocolShareBps)) / 10_000n;
  opts.metrics?.set(`tax_${name}_due_wei`, due);
  if (due === 0n || due < (opts.taxQueue?.minPayoutWei ?? 0n)) return null;
  if (q.payCreator.staticCall) await q.payCreator.staticCall(); // surfaces NothingDue/other reverts before spending gas
  const r = await wait(q.payCreator());
  opts.metrics?.inc(`tax_${name}_paid`);
  return { due, tx: r?.hash };
}

// ───────────────────────────── epochs ─────────────────────────────
//
// Daily epochs: epoch k (k = 1, 2, ...) is the UTC day [genesis + (k-1) DAY, genesis + k DAY), genesis at 00:00 UTC.
// A root's on-chain `epoch` is the last day it covers; every root is cumulative from genesis, so a late, missed or
// vetoed day is covered by the next root and no allocation is ever lost or expires.
//
// Publication cadence. The distributor accepts one pending root at a time and keeps it under review for
// activationDelay (1 day). Day k closes at its end + settlementGrace (6 h); the publisher proposes once that has passed
// AND no pending root is still in its delay (proposeRoot activates a ready pending root first). With a 1-day delay
// each root is therefore proposed at the moment the previous one becomes activatable, covering every day closed by
// then. Nothing is skipped: a day that misses one root is in the next.

const ZERO_ROOT = /^0x0*$/;

/** Normalise a RootInfo returned by ethers (struct or tuple) or by a fake. */
export function rootInfo(r) {
  const root = r.root ?? r[0];
  return {
    root, set: !ZERO_ROOT.test(root), total: BigInt(r.totalCumulative ?? r[1]), epoch: Number(r.epoch ?? r[2]),
    activatesAt: BigInt(r.activatesAt ?? r[3]), dataHash: r.dataHash ?? r[4],
  };
}

/**
 * Deterministic daily allocation. Epoch k's amount is every pARB share settled into the distributor before
 * `end + settlementGrace`, minus everything already allocated in earlier epochs: rounding dust, zero-revenue days and
 * days with no eligible NFT roll forward, a batch settled after a day's cutoff (late funding) belongs to the next
 * day, and the same shares are never allocated twice. Weights are NFT-seconds held during the day (converter
 * excluded): a Milord sold at 12:00 earns half the day for the seller, half for the buyer.
 *
 * Merkle trees are built for the last epoch only (trees: "all" builds every one); every epoch keeps its cumulative
 * map and total. transfers: finalized NFT Transfer events; batches: [{sharesMinted, settledAt}] from ParbFundingAdapter.
 */
export function computeEpochs({ transfers, batches, genesis, upToEpoch, settlementGrace = 6 * 3600, excluded = [], trees = "last" }) {
  assertUtcMidnight(genesis);
  const intervals = ownershipIntervals(transfers);
  const settledBatches = batches
    .filter((b) => Number(b.settledAt) > 0)
    .map((b) => ({ at: Number(b.settledAt), shares: BigInt(b.sharesMinted) }))
    .sort((a, b) => a.at - b.at);
  let next = 0;
  let settled = 0n;
  let cumulative = new Map();
  let allocatedTotal = 0n;
  const epochs = [];
  for (let k = 1; k <= upToEpoch; k++) {
    const start = genesis + (k - 1) * EPOCH;
    const end = start + EPOCH;
    const cutoff = end + settlementGrace;
    while (next < settledBatches.length && settledBatches[next].at < cutoff) settled += settledBatches[next++].shares;
    const amount = settled - allocatedTotal;
    const { weights, daily } = eligibility(intervals, { start, end, excluded });
    const { allocations, dust } = allocate(amount, weights);
    cumulative = accumulate(cumulative, allocations);
    const allocatedNow = [...allocations.values()].reduce((a, b) => a + b, 0n);
    allocatedTotal += allocatedNow;
    const built = trees === "all" || (trees === "last" && k === upToEpoch) ? buildTree(cumulative) : null;
    epochs.push({
      epoch: k, start, end, cutoff, settled, amount, allocatedNow, dust, eligible: weights.size, weights, allocations,
      cumulative: new Map(cumulative), total: allocatedTotal, root: built?.root ?? null, tree: built?.tree ?? null,
      values: built?.values ?? [], dataHash: built ? dataHashOf(k, built.values) : null, daily,
    });
  }
  return epochs;
}

/** Hash of the published allocation file (epoch + sorted [address, cumulative] list). */
export function dataHashOf(epoch, values) {
  return keccak256(toUtf8Bytes(JSON.stringify({ epoch, values })));
}

/** Latest daily epoch whose cutoff (day end + settlement grace) has passed at time `now`. */
export function closedEpoch(genesis, now, settlementGrace = 6 * 3600) {
  return Math.max(0, Math.floor((now - genesis - settlementGrace) / EPOCH));
}

/** Read finalized inputs from chain. `toBlock` should be a finalized block number. With `distributor`, also the
 *  proposed and vetoed roots (so a vetoed root is never silently re-proposed, including after a restart). */
export async function readInputs({ nft, adapter, distributor, provider, fromBlock = 0, toBlock, logStep }) {
  // from the deployment block (deployments file `startBlock`), in bounded ranges
  const logs = await chunkedLogs((a, b) => nft.queryFilter(nft.filters.Transfer(), a, b), fromBlock, toBlock, { step: logStep });
  const ts = new Map();
  const transfers = [];
  for (const l of logs) {
    if (!ts.has(l.blockNumber)) ts.set(l.blockNumber, (await provider.getBlock(l.blockNumber)).timestamp);
    transfers.push({ tokenId: Number(l.args.tokenId), from: l.args.from, to: l.args.to, timestamp: ts.get(l.blockNumber), blockNumber: l.blockNumber, logIndex: l.index });
  }
  const n = Number(await adapter.batchCount({ blockTag: toBlock }));
  const batches = [];
  for (let i = 0; i < n; i++) {
    const b = await adapter.batch(i, { blockTag: toBlock });
    batches.push({ sharesMinted: b.sharesMinted, settledAt: Number(b.settledAt) });
  }
  const out = { transfers, batches };
  if (distributor?.filters?.RootVetoed) {
    const q = (f) => chunkedLogs((a, b) => distributor.queryFilter(f, a, b), fromBlock, toBlock, { step: logStep });
    const epochOf = new Map((await q(distributor.filters.RootProposed())).map((l) => [l.args.root, Number(l.args.epoch)]));
    out.vetoes = (await q(distributor.filters.RootVetoed())).map((l) => ({ root: l.args.root, epoch: epochOf.get(l.args.root) ?? 0 }));
  }
  return out;
}

/**
 * Publisher: propose the cumulative root through the latest closed day. Returns what it did:
 *   none     - nothing to do (no closed day, already pending/active, nothing new to allocate);
 *   waiting  - a pending root is still in its review delay (it is never replaced), or the root is not yet funded;
 *   blocked  - a root proposed after the active one was vetoed: the publisher stops until an operator investigates and
 *              lists that root in `acknowledgedVetoes` (PUBLISHER_ACK_VETOES), so a disputed allocation is never
 *              re-proposed automatically, including after a restart;
 *   proposed - proposeRoot sent; the allocation file is written through `save` first.
 * Stateless: every run recomputes from genesis and reads the distributor, so restarts and repeated runs are safe.
 */
export async function publishEpoch({ distributor, inputs, genesis, now, settlementGrace, excluded, save = () => {}, parb, acknowledgedVetoes = [], alerter }) {
  assertUtcMidnight(genesis);
  const k = closedEpoch(genesis, now, settlementGrace);
  if (k === 0) return { action: "none", reason: "no closed day yet" };
  const active = rootInfo(await distributor.active());
  const pending = rootInfo(await distributor.pending());
  const pendingReady = pending.set && BigInt(now) >= pending.activatesAt;
  const base = pendingReady ? pending : active; // proposeRoot activates a ready pending root before anything else
  if (base.epoch >= k) return { action: "none", reason: "already active", epoch: k };

  const ack = new Set(acknowledgedVetoes.map((r) => r.toLowerCase()));
  const vetoed = (inputs.vetoes ?? []).filter((v) => v.epoch > base.epoch && !ack.has(v.root.toLowerCase()));
  if (vetoed.length) {
    const reason = `root ${vetoed[0].root} (epoch ${vetoed[0].epoch}) was vetoed by the Team Safe: not re-proposing until it is investigated and acknowledged (PUBLISHER_ACK_VETOES)`;
    if (alerter) await alerter.alert("critical", `publisher:vetoed:${vetoed[0].root}`, reason);
    return { action: "blocked", reason, epoch: k };
  }

  const e = computeEpochs({ ...inputs, genesis, upToEpoch: k, settlementGrace, excluded }).at(-1);
  if (pending.set && !pendingReady) {
    if (pending.epoch === k && pending.root === e.root) return { action: "none", reason: "already pending", epoch: k };
    return {
      action: "waiting", epoch: k, pendingEpoch: pending.epoch, activatesAt: pending.activatesAt,
      reason: `pending root for epoch ${pending.epoch} is in its review delay until ${pending.activatesAt}; days through ${k} go into the next root`,
    };
  }
  if (!e.root || e.total <= base.total) {
    return { action: "none", reason: e.eligible === 0 ? "no eligible NFTs: shares roll forward" : "nothing new to allocate", epoch: k, carried: e.amount };
  }
  if (parb) {
    const covered = (await parb.balanceOf(distributor.target ?? distributor.address)) + BigInt(await distributor.totalClaimed());
    if (e.total > covered) return { action: "waiting", reason: `root needs ${e.total} pARB, distributor covers ${covered}`, epoch: k };
  }
  save(e);
  if (distributor.proposeRoot.staticCall) await distributor.proposeRoot.staticCall(e.root, e.total, k, e.dataHash);
  await wait(distributor.proposeRoot(e.root, e.total, k, e.dataHash));
  return { action: "proposed", epoch: k, root: e.root, total: e.total, dataHash: e.dataHash, covers: [base.epoch + 1, k] };
}

/**
 * Independent checker: recompute the pending (and active) root from chain data and compare. A mismatch is a
 * critical alert asking the Team Safe to veto during the activation delay. Also checks solvency.
 */
export async function checkRoot({ distributor, parb, inputs, genesis, settlementGrace, excluded, alerter }) {
  const out = [];
  for (const which of ["pending", "active"]) {
    const r = rootInfo(await distributor[which]());
    if (!r.set) continue;
    const e = computeEpochs({ ...inputs, genesis, upToEpoch: r.epoch, settlementGrace, excluded }).at(-1);
    const match = Boolean(e) && e.root === r.root && e.total === r.total && e.dataHash === r.dataHash;
    out.push({ which, epoch: r.epoch, match, expected: e?.root ?? null, onchain: r.root });
    if (!match && alerter) {
      await alerter.alert("critical", `root:${which}:${r.epoch}`,
        which === "pending"
          ? `PENDING ROOT MISMATCH for day ${r.epoch}: on-chain ${r.root} (total ${r.total}), recomputed ${e?.root} (total ${e?.total}). Team Safe should call ShareDistributor.vetoPending() before it activates at ${r.activatesAt}.`
          : `ACTIVE ROOT MISMATCH for day ${r.epoch}: on-chain ${r.root}, recomputed ${e?.root}. Pause claims review required.`);
    }
  }
  if (parb) {
    const held = await parb.balanceOf(distributor.target ?? distributor.address);
    const claimed = await distributor.totalClaimed();
    const need = rootInfo(await distributor.pending()).total;
    if (need > held + claimed && alerter) await alerter.alert("critical", "solvency", `pending root needs ${need} pARB, distributor covers ${held + claimed}`);
  }
  return out;
}

/**
 * Per-account reward state for the frontend, kept apart:
 *   earned          pARB computed for the account through the latest closed day (in a root or not yet);
 *   allocated       cumulative in the latest proposed root (pending or active);
 *   active          cumulative in the active root: claimable = active - claimed (claimed is read on chain);
 *   accruingSeconds NFT-seconds held since the latest closed day began closing (its pARB amount is set only when
 *                   that day's revenue has settled and its cutoff has passed).
 * Rewards are never sent automatically: an account (or anyone on its behalf) calls claim().
 */
export function rewardsStatus({ inputs, genesis, now, settlementGrace = 6 * 3600, excluded = [], active, pending }) {
  const a = rootInfo(active);
  const p = rootInfo(pending);
  const k = closedEpoch(genesis, now, settlementGrace);
  const epochs = k ? computeEpochs({ ...inputs, genesis, upToEpoch: k, settlementGrace, excluded, trees: "none" }) : [];
  const at = (n) => (n > 0 && n <= epochs.length ? epochs[n - 1].cumulative : new Map());
  const earned = at(k);
  const allocated = at(p.set ? p.epoch : a.epoch);
  const activeMap = at(a.epoch);
  const openFrom = genesis + k * EPOCH;
  const accruing = now > openFrom ? eligibility(ownershipIntervals(inputs.transfers), { start: openFrom, end: now, excluded }).weights : new Map();
  const accounts = {};
  for (const addr of new Set([...earned.keys(), ...accruing.keys()])) {
    accounts[addr] = {
      earned: String(earned.get(addr) ?? 0n), allocated: String(allocated.get(addr) ?? 0n), active: String(activeMap.get(addr) ?? 0n),
      accruingSeconds: Number(accruing.get(addr) ?? 0n),
    };
  }
  return {
    asOf: now, genesis, epochSeconds: EPOCH, latestClosedEpoch: k, activeEpoch: a.epoch,
    pendingEpoch: p.set ? p.epoch : null, pendingActivatesAt: p.set ? Number(p.activatesAt) : null,
    totals: { earned: String(epochs.at(-1)?.total ?? 0n), allocated: String(p.set ? p.total : a.total), active: String(a.total) },
    accounts,
  };
}
