// Eligibility, allocation and Merkle building for weekly pARB distributions.
// Pure functions: no RPC access, so the same code runs in tests, the simulation
// and the production epoch job.
import { StandardMerkleTree } from "@openzeppelin/merkle-tree";

export const DAY = 86400;
export const WEEK = 7 * DAY;
const ZERO = "0x0000000000000000000000000000000000000000";

/**
 * Replay Milords Transfer events into ownership intervals.
 * @param transfers [{tokenId, from, to, timestamp, blockNumber, logIndex}] from FINALIZED blocks only
 * @returns Map tokenId -> [{owner, start, end}] (end = Infinity while held)
 */
export function ownershipIntervals(transfers) {
  const sorted = [...transfers].sort((a, b) =>
    a.blockNumber !== b.blockNumber ? a.blockNumber - b.blockNumber : a.logIndex - b.logIndex);
  const byToken = new Map();
  for (const t of sorted) {
    const id = String(t.tokenId);
    const list = byToken.get(id) ?? [];
    const open = list[list.length - 1];
    if (open && open.end === Infinity) open.end = t.timestamp;
    if (t.to.toLowerCase() !== ZERO) list.push({ owner: t.to.toLowerCase(), start: t.timestamp, end: Infinity });
    byToken.set(id, list);
  }
  return byToken;
}

/**
 * Time-weighted eligibility for [start, end). Each UTC day is scored separately
 * (NFT-seconds held that day) so a daily breakdown can be published with the root.
 * Converter inventory and the zero address earn nothing.
 * @returns {weights: Map owner -> bigint NFT-seconds, daily: [{day, owner, seconds}]}
 */
export function eligibility(intervals, { start, end, excluded = [] }) {
  const ex = new Set(excluded.map((a) => a.toLowerCase()));
  const weights = new Map();
  const daily = [];
  for (const [tokenId, list] of intervals) {
    for (const iv of list) {
      if (ex.has(iv.owner)) continue;
      const s = Math.max(iv.start, start);
      const e = Math.min(iv.end, end);
      if (e <= s) continue;
      for (let d = start; d < end; d += DAY) {
        const ds = Math.max(s, d);
        const de = Math.min(e, d + DAY);
        if (de <= ds) continue;
        const secs = de - ds;
        weights.set(iv.owner, (weights.get(iv.owner) ?? 0n) + BigInt(secs));
        daily.push({ day: (d - start) / DAY, tokenId, owner: iv.owner, seconds: secs });
      }
    }
  }
  return { weights, daily };
}

/**
 * Split `amount` (bigint, pARB wei) by weight, rounding down. The remainder (dust)
 * is returned and carried into the next epoch, never lost.
 */
export function allocate(amount, weights) {
  let total = 0n;
  for (const w of weights.values()) total += w;
  const out = new Map();
  if (total === 0n || amount === 0n) return { allocations: out, dust: amount };
  let used = 0n;
  for (const [owner, w] of weights) {
    const a = (amount * w) / total;
    if (a > 0n) out.set(owner, a);
    used += a;
  }
  return { allocations: out, dust: amount - used };
}

/** Add this epoch to the running cumulative totals (never decreases). */
export function accumulate(prevCumulative, allocations) {
  const next = new Map(prevCumulative);
  for (const [a, v] of allocations) next.set(a, (next.get(a) ?? 0n) + v);
  return next;
}

/** OpenZeppelin StandardMerkleTree over [address, uint256 cumulative]. */
export function buildTree(cumulative) {
  const values = [...cumulative.entries()]
    .filter(([, v]) => v > 0n)
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([a, v]) => [a, v.toString()]);
  if (values.length === 0) return null;
  const tree = StandardMerkleTree.of(values, ["address", "uint256"]);
  const total = values.reduce((s, [, v]) => s + BigInt(v), 0n);
  return { tree, root: tree.root, total, values };
}

export function proofFor(tree, account) {
  for (const [i, v] of tree.entries()) {
    if (v[0].toLowerCase() === account.toLowerCase()) return { cumulative: BigInt(v[1]), proof: tree.getProof(i) };
  }
  return null;
}
