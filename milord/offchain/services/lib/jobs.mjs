// The three MILORD automation jobs as plain functions over contract handles, so the same code runs in the
// services, the anvil simulation and the failure tests (with fakes).
//
//   keeperTick   - permissionless upkeep: settle tax, split revenue, collect LP fees into buyback-and-burn,
//                  bounded buyback, ETH -> USDG router, settle vault deposits, activate roots.
//   computeEpochs - deterministic weekly pARB allocation from finalized NFT transfers and settled batches.
//   publishEpoch / checkRoot - propose the next root (publisher key) / recompute it independently and alert.
import { keccak256, toUtf8Bytes } from "ethers";
import { ownershipIntervals, eligibility, allocate, accumulate, buildTree, WEEK } from "../../lib/eligibility.mjs";
import { isExpectedRevert } from "./runtime.mjs";

const ZERO = "0x0000000000000000000000000000000000000000";
const msg = (e) => String(e?.shortMessage ?? e?.reason ?? e?.message ?? e);
const wait = async (txp) => {
  const tx = await txp;
  return tx?.wait ? tx.wait() : tx;
};

// ───────────────────────────── keeper ─────────────────────────────

/**
 * One keeper pass. Each step is independent: an expected revert (paused, below threshold, nothing to settle)
 * is recorded as "skipped"; any other failure is recorded as "failed" and alerted, and the pass continues.
 * Nothing here can move funds anywhere except along the contracts' fixed routes.
 *
 * c: { hookrSettle?, splitters[], collector?, burner?, router?, adapter, vault, distributor }
 * opts: { buyback: { minBudgetWei, quoteMinOut(budget) -> bigint | null, deadlineSecs }, now(), alerter, members: [0] }
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

  // 1. Hookr tax settlement (on mainnet: whatever settle/payout call the Tax + Conversion block exposes)
  if (c.hookrSettle) await run("hookr-settle", async () => { await wait(c.hookrSettle()); });

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

  const failed = res.filter((r) => r.status === "failed");
  return { results: res, failed: failed.length };
}

// ───────────────────────────── epochs ─────────────────────────────

/**
 * Deterministic weekly allocation. Epoch k (k = 1, 2, ...) covers [genesis + (k-1) WEEK, genesis + k WEEK).
 * Its amount is every pARB share settled into the distributor before `end + settlementGrace`, minus everything
 * already allocated in earlier epochs (so rounding dust and weeks with no eligible NFTs roll forward, and the
 * same shares are never allocated twice). Weights are NFT-seconds held during the epoch (converter excluded).
 *
 * transfers: finalized NFT Transfer events; batches: [{sharesMinted, settledAt}] from ParbFundingAdapter.
 */
export function computeEpochs({ transfers, batches, genesis, upToEpoch, settlementGrace = 6 * 3600, excluded = [] }) {
  const intervals = ownershipIntervals(transfers);
  let cumulative = new Map();
  let allocatedTotal = 0n;
  const epochs = [];
  for (let k = 1; k <= upToEpoch; k++) {
    const start = genesis + (k - 1) * WEEK;
    const end = start + WEEK;
    const cutoff = end + settlementGrace;
    const settled = batches
      .filter((b) => Number(b.settledAt) > 0 && Number(b.settledAt) < cutoff)
      .reduce((a, b) => a + BigInt(b.sharesMinted), 0n);
    const amount = settled - allocatedTotal;
    const { weights, daily } = eligibility(intervals, { start, end, excluded });
    const { allocations, dust } = allocate(amount, weights);
    cumulative = accumulate(cumulative, allocations);
    const allocatedNow = [...allocations.values()].reduce((a, b) => a + b, 0n);
    allocatedTotal += allocatedNow;
    const built = buildTree(cumulative);
    epochs.push({
      epoch: k, start, end, cutoff, settled, amount, allocatedNow, dust, eligible: weights.size, weights, allocations,
      cumulative: new Map(cumulative), total: built?.total ?? 0n, root: built?.root ?? null, tree: built?.tree ?? null,
      values: built?.values ?? [], dataHash: built ? dataHashOf(k, built.values) : null, daily,
    });
  }
  return epochs;
}

/** Hash of the published allocation file (epoch + sorted [address, cumulative] list). */
export function dataHashOf(epoch, values) {
  return keccak256(toUtf8Bytes(JSON.stringify({ epoch, values })));
}

/** Latest epoch whose cutoff (plus finality margin) has passed at time `now`. */
export function closedEpoch(genesis, now, settlementGrace = 6 * 3600) {
  return Math.max(0, Math.floor((now - genesis - settlementGrace) / WEEK));
}

/** Read finalized inputs from chain. `toBlock` should be a finalized block number. */
export async function readInputs({ nft, adapter, provider, fromBlock = 0, toBlock }) {
  const logs = await nft.queryFilter(nft.filters.Transfer(), fromBlock, toBlock);
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
  return { transfers, batches };
}

/**
 * Publisher: propose the root of the latest closed epoch if it allocates more than the active root and is not
 * already pending. Returns what it did. Writes the allocation file through `save` before proposing.
 */
export async function publishEpoch({ distributor, inputs, genesis, now, settlementGrace, excluded, save = () => {} }) {
  const k = closedEpoch(genesis, now, settlementGrace);
  if (k === 0) return { action: "none", reason: "no closed epoch yet" };
  const epochs = computeEpochs({ ...inputs, genesis, upToEpoch: k, settlementGrace, excluded });
  const e = epochs.at(-1);
  const active = await distributor.active();
  const pending = await distributor.pending();
  const activeTotal = BigInt(active.totalCumulative ?? active[1]);
  const activeEpoch = Number(active.epoch ?? active[2]);
  if (Number(pending.epoch ?? pending[2]) === k && (pending.root ?? pending[0]) === e.root) return { action: "none", reason: "already pending", epoch: k };
  if (activeEpoch >= k) return { action: "none", reason: "already active", epoch: k };
  if (!e.root || e.total <= activeTotal) return { action: "none", reason: e.eligible === 0 ? "no eligible NFTs: shares roll forward" : "nothing new to allocate", epoch: k, carried: e.amount };
  save(e);
  await wait(distributor.proposeRoot(e.root, e.total, k, e.dataHash));
  return { action: "proposed", epoch: k, root: e.root, total: e.total, dataHash: e.dataHash };
}

/**
 * Independent checker: recompute the pending (and active) root from chain data and compare. A mismatch is a
 * critical alert asking the Team Safe to veto during the activation delay. Also checks solvency.
 */
export async function checkRoot({ distributor, parb, inputs, genesis, settlementGrace, excluded, alerter }) {
  const out = [];
  for (const which of ["pending", "active"]) {
    const r = await distributor[which]();
    const root = r.root ?? r[0];
    if (/^0x0+$/.test(root)) continue;
    const epoch = Number(r.epoch ?? r[2]);
    const total = BigInt(r.totalCumulative ?? r[1]);
    const dataHash = r.dataHash ?? r[4];
    const e = computeEpochs({ ...inputs, genesis, upToEpoch: epoch, settlementGrace, excluded }).at(-1);
    const match = e && e.root === root && e.total === total && e.dataHash === dataHash;
    out.push({ which, epoch, match, expected: e?.root ?? null, onchain: root });
    if (!match && alerter) {
      await alerter.alert("critical", `root:${which}:${epoch}`,
        which === "pending"
          ? `PENDING ROOT MISMATCH for epoch ${epoch}: on-chain ${root} (total ${total}), recomputed ${e?.root} (total ${e?.total}). Team Safe should call ShareDistributor.vetoPending() before it activates.`
          : `ACTIVE ROOT MISMATCH for epoch ${epoch}: on-chain ${root}, recomputed ${e?.root}. Pause claims review required.`);
    }
  }
  if (parb) {
    const held = await parb.balanceOf(distributor.target ?? distributor.address);
    const claimed = await distributor.totalClaimed();
    const p = await distributor.pending();
    const need = BigInt(p.totalCumulative ?? p[1]);
    if (need > held + claimed && alerter) await alerter.alert("critical", "solvency", `pending root needs ${need} pARB, distributor covers ${held + claimed}`);
  }
  return out;
}
