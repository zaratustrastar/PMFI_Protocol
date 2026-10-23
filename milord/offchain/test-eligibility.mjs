// node offchain/test-eligibility.mjs — unit checks for the pure eligibility code
import assert from "node:assert/strict";
import { ownershipIntervals, eligibility, allocate, accumulate, buildTree, proofFor, DAY, WEEK } from "./lib/eligibility.mjs";

const Z = "0x0000000000000000000000000000000000000000";
const conv = "0x00000000000000000000000000000000000000cc";
const A = "0x00000000000000000000000000000000000000a1";
const B = "0x00000000000000000000000000000000000000b2";
const S = 1_000_000;
let n = 0;
const tx = (tokenId, from, to, t) => ({ tokenId, from, to, timestamp: t, blockNumber: ++n, logIndex: 0 });

const transfers = [
  tx(1, Z, A, S - 10 * DAY),           // A minted #1 before the week
  tx(2, Z, A, S + 1 * DAY),            // A minted #2 on day 1
  tx(1, A, B, S + 3.5 * DAY),          // #1 sold to B mid-week
  tx(2, A, conv, S + 5 * DAY),         // A redeemed #2 on day 5 (inventory)
];
const iv = ownershipIntervals(transfers);
const { weights } = eligibility(iv, { start: S, end: S + WEEK, excluded: [conv] });
assert.equal(weights.get(A), BigInt(3.5 * DAY + 4 * DAY));
assert.equal(weights.get(B), BigInt(3.5 * DAY));
assert.equal(weights.get(conv), undefined, "inventory earns nothing");

const { allocations, dust } = allocate(1000n, weights);
const sum = [...allocations.values()].reduce((a, b) => a + b, 0n);
assert.equal(sum + dust, 1000n, "no pARB lost");
assert.equal(allocations.get(A), 681n); // floor(7.5/11 x 1000)
assert.equal(dust, 1n);
assert.equal(allocations.get(B), 318n);

// next week: B holds all; A keeps its earlier cumulative
const cum1 = accumulate(new Map(), allocations);
const cum2 = accumulate(cum1, new Map([[B, 500n]]));
assert.equal(cum2.get(A), 681n);
assert.equal(cum2.get(B), 818n);
const t = buildTree(cum2);
assert.equal(t.total, 1499n);
assert.equal(proofFor(t.tree, A).cumulative, 681n);

// no holders -> everything carries forward
assert.equal(allocate(77n, new Map()).dust, 77n);
// 30 holders all week, a 31st joins at day 3.5: each of the 30 gets 7 days of weight, the newcomer 3.5
{
  let k = 0;
  const xs = [];
  const addr = (i) => "0x" + i.toString(16).padStart(40, "0");
  for (let i = 1; i <= 30; i++) xs.push({ tokenId: i, from: Z, to: addr(i), timestamp: S - DAY, blockNumber: ++k, logIndex: 0 });
  xs.push({ tokenId: 31, from: Z, to: addr(31), timestamp: S + 3.5 * DAY, blockNumber: ++k, logIndex: 0 });
  const w = eligibility(ownershipIntervals(xs), { start: S, end: S + WEEK, excluded: [conv] }).weights;
  assert.equal(w.size, 31);
  assert.equal(w.get(addr(1)), BigInt(WEEK));
  assert.equal(w.get(addr(31)), BigInt(3.5 * DAY));
  const { allocations, dust } = allocate(10n ** 18n, w);
  const total = [...allocations.values()].reduce((a, b) => a + b, 0n) + dust;
  assert.equal(total, 10n ** 18n);
  assert.equal(allocations.get(addr(1)) * 1n, (10n ** 18n * BigInt(WEEK)) / BigInt(30 * WEEK + 3.5 * DAY));
}
// a transfer after the week closes does not change that week's weights
{
  const late = [...transfers, tx(1, B, A, S + WEEK + 10)];
  const w2 = eligibility(ownershipIntervals(late), { start: S, end: S + WEEK, excluded: [conv] }).weights;
  assert.equal(w2.get(B), BigInt(3.5 * DAY));
}
console.log("eligibility checks passed");
