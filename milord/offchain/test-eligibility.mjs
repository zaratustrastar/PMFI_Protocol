// node offchain/test-eligibility.mjs — unit checks for the pure eligibility code (daily UTC epochs)
import assert from "node:assert/strict";
import { ownershipIntervals, eligibility, allocate, accumulate, buildTree, proofFor, assertUtcMidnight, DAY, EPOCH } from "./lib/eligibility.mjs";

const Z = "0x0000000000000000000000000000000000000000";
const conv = "0x00000000000000000000000000000000000000cc";
const A = "0x00000000000000000000000000000000000000a1";
const B = "0x00000000000000000000000000000000000000b2";
const H = 3600;
const S = 20_000 * DAY; // 00:00 UTC
let n = 0;
const tx = (tokenId, from, to, t) => ({ tokenId, from, to, timestamp: t, blockNumber: ++n, logIndex: 0 });

assert.equal(EPOCH, DAY, "one epoch = one UTC day");
assert.equal(assertUtcMidnight(S), S);
assert.throws(() => assertUtcMidnight(S + 1), /00:00 UTC/);

const transfers = [
  tx(1, Z, A, S - 10 * DAY),        // A minted #1 before the day
  tx(2, Z, A, S + 3 * H),           // A minted #2 at 03:00
  tx(1, A, B, S + 12 * H),          // #1 sold to B at 12:00
  tx(2, A, conv, S + 18 * H),       // A redeemed #2 at 18:00 (inventory)
];
const iv = ownershipIntervals(transfers);
const { weights } = eligibility(iv, { start: S, end: S + DAY, excluded: [conv] });
assert.equal(weights.get(A), BigInt(12 * H + 15 * H), "seller: #1 until the sale, #2 until redemption");
assert.equal(weights.get(B), BigInt(12 * H), "buyer: from the transfer second");
assert.equal(weights.get(conv), undefined, "inventory earns nothing");

const { allocations, dust } = allocate(1000n, weights);
const sum = [...allocations.values()].reduce((a, b) => a + b, 0n);
assert.equal(sum + dust, 1000n, "no pARB lost");
assert.equal(allocations.get(A), 692n); // floor(27/39 x 1000)
assert.equal(allocations.get(B), 307n);
assert.equal(dust, 1n);

// next day: B holds all; A keeps its earlier cumulative
const cum1 = accumulate(new Map(), allocations);
const day2 = eligibility(iv, { start: S + DAY, end: S + 2 * DAY, excluded: [conv] }).weights;
assert.equal(day2.get(A), undefined);
assert.equal(day2.get(B), BigInt(DAY));
const cum2 = accumulate(cum1, allocate(500n, day2).allocations);
assert.equal(cum2.get(A), 692n);
assert.equal(cum2.get(B), 807n);
const t = buildTree(cum2);
assert.equal(t.total, 1499n);
assert.equal(proofFor(t.tree, A).cumulative, 692n);

// no holders -> everything carries forward
assert.equal(allocate(77n, new Map()).dust, 77n);
// 30 holders all day, a 31st joins at 12:00: each of the 30 gets 24 h of weight, the newcomer 12 h
{
  let k = 0;
  const xs = [];
  const addr = (i) => "0x" + i.toString(16).padStart(40, "0");
  for (let i = 1; i <= 30; i++) xs.push({ tokenId: i, from: Z, to: addr(i), timestamp: S - DAY, blockNumber: ++k, logIndex: 0 });
  xs.push({ tokenId: 31, from: Z, to: addr(31), timestamp: S + 12 * H, blockNumber: ++k, logIndex: 0 });
  const w = eligibility(ownershipIntervals(xs), { start: S, end: S + DAY, excluded: [conv] }).weights;
  assert.equal(w.size, 31);
  assert.equal(w.get(addr(1)), BigInt(DAY));
  assert.equal(w.get(addr(31)), BigInt(12 * H));
  const { allocations, dust } = allocate(10n ** 18n, w);
  const total = [...allocations.values()].reduce((a, b) => a + b, 0n) + dust;
  assert.equal(total, 10n ** 18n);
  assert.equal(allocations.get(addr(1)), (10n ** 18n * BigInt(DAY)) / BigInt(30 * DAY + 12 * H));
}
// a transfer after the day closes does not change that day's weights
{
  const late = [...transfers, tx(1, B, A, S + DAY + 10)];
  const w2 = eligibility(ownershipIntervals(late), { start: S, end: S + DAY, excluded: [conv] }).weights;
  assert.equal(w2.get(B), BigInt(12 * H));
}
// a Milord bought and sold within the same day earns exactly the seconds it was held
{
  const xs = [tx(7, Z, A, S - DAY), tx(7, A, B, S + 9 * H), tx(7, B, A, S + 9 * H + 1234)];
  const w = eligibility(ownershipIntervals(xs), { start: S, end: S + DAY }).weights;
  assert.equal(w.get(B), 1234n);
  assert.equal(w.get(A), BigInt(DAY - 1234));
}
console.log("eligibility checks passed");
