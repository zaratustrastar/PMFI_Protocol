// Unit tests for the Option D window split (offchain/lib/option-d.mjs). Run: node offchain/test-option-d.mjs
import assert from "node:assert/strict";
import { Interface } from "ethers";
import { reconcileOptionD, optionDBatch, PIPS } from "./lib/option-d.mjs";

const HELPER = "0x00000000000000000000000000000000000000a1";
const SAFE = "0x00000000000000000000000000000000000000b2";
const E = 10n ** 18n;
const BASE = 3000n;
let n = 0;
const t = (name, f) => { f(); n++; console.log("ok", name); };
const ceil = (a, b) => (a + b - 1n) / b;
// Snipe linear 40% -> 0 over 3600 s; LP fee = base + 80% of it (MilordForkBase._expectedLpFee)
const lpFeeAt = (sec) => { const s = sec >= 3600 ? 0n : (400_000n * BigInt(3600 - sec)) / 3600n; return BASE + s - (s * 2000n) / 10_000n; };
let li = 0;
const buy = (block, ethIn, fee) => ({ blockNumber: block, logIndex: li++, txHash: `0xb${li}`, amount0: -ethIn, amount1: 1n, fee });
const sell = (block, milordIn, fee = BASE) => ({ blockNumber: block, logIndex: li++, txHash: `0xs${li}`, amount0: 1n, amount1: -milordIn, fee });
const opts = { baseFeePips: 3000, guardEndBlock: 1300, handover: { blockNumber: 1400, logIndex: 5 }, recipients: [HELPER, SAFE] };

function window() {
  li = 0;
  const swaps = [];
  for (let i = 0; i < 6; i++) swaps.push(buy(1000 + i * 10, E / 2n, lpFeeAt(i * 600)));
  swaps.push(sell(1100, 1_000_000n * E));
  swaps.push(buy(1350, E, BASE)); // after the guard, before the handover: base only
  swaps.push(buy(1500, E, BASE)); // after the handover: not part of the window
  let eth = 0n, mil = 0n, snipe = 0n;
  for (const s of swaps.slice(0, 8)) {
    const b = s.amount0 < 0n;
    const f = ceil((b ? -s.amount0 : -s.amount1) * s.fee, PIPS);
    if (b) { eth += f; snipe += (f * (s.fee - BASE)) / s.fee; } else mil += f;
  }
  return { swaps, eth, mil, snipe };
}

t("exact 90/10 of the post-commission Snipe; all base fees and the rest to the burner", () => {
  const w = window();
  // fees reached us twice: on the Safe's acceptFamily (to the helper, then swept) and at the handover (to the Safe)
  const early = { blockNumber: 1001, logIndex: 0, txHash: "0xacc", recipient: HELPER, amount0: 1000n, amount1: 0n };
  const late = { blockNumber: 1400, logIndex: 3, txHash: "0xlock", recipient: SAFE, amount0: w.eth - 1000n - 3n, amount1: w.mil - 2n };
  const r = reconcileOptionD(w.swaps, [early, late], opts);
  assert.equal(r.swaps, 8, "the post-handover swap is excluded");
  assert.equal(r.guardSwaps, 7);
  assert.equal(r.surcharge, w.snipe);
  assert.equal(r.toTeam, w.snipe / 10n);
  assert.equal(r.toTeam + r.toBurner, r.collectedEth, "every collected wei is assigned");
  assert.equal(r.milordToBurn, w.mil - 2n);
  assert.ok(r.surcharge > (r.collectedEth * 9n) / 10n, "at 40% the Snipe part dominates the window's LP fees");
});

t("refuses a fee to anyone but the helper or the Safe, a Snipe fee outside the guard, or a total above the events", () => {
  const w = window();
  const c = (recipient, amount0) => [{ blockNumber: 1400, logIndex: 3, txHash: "0x", recipient, amount0, amount1: 0n }];
  assert.throws(() => reconcileOptionD(w.swaps, c("0x00000000000000000000000000000000000000ee", w.eth), opts), /not the helper or the Safe/);
  assert.throws(() => reconcileOptionD(w.swaps, c(SAFE, w.eth + 1000n), opts), /event estimate/);
  assert.throws(() => reconcileOptionD(w.swaps, c(SAFE, w.snipe / 2n), opts), /covers the Snipe/);
  const bad = [...w.swaps, buy(1360, E, 5000n)];
  assert.throws(() => reconcileOptionD(bad, c(SAFE, w.eth), opts), /outside a guard buy/);
});

t("outside LPs after the guard: we collect less than the estimate, the team still gets exactly 10% of the Snipe", () => {
  const w = window();
  const r = reconcileOptionD(w.swaps, [{ blockNumber: 1400, logIndex: 3, txHash: "0x", recipient: SAFE, amount0: w.eth - 1000n, amount1: 0n }], opts);
  assert.equal(r.toTeam, w.snipe / 10n);
  assert.equal(r.toBurner, w.eth - 1000n - w.snipe / 10n);
});

t("Safe batch: ETH to burner and team, MILORD to burner, then burn; values are the split", () => {
  const w = window();
  const r = reconcileOptionD(w.swaps, [{ blockNumber: 1400, logIndex: 3, txHash: "0x", recipient: SAFE, amount0: w.eth, amount1: w.mil }], opts);
  const iface = { erc20: new Interface(["function transfer(address,uint256)"]), burner: new Interface(["function burnHeldTokens()"]) };
  const b = optionDBatch({ chainId: 4663, safe: SAFE, teamSafe: "0x00000000000000000000000000000000000000c3", burner: "0x00000000000000000000000000000000000000d4", milord: "0x00000000000000000000000000000000000000e5", split: r, iface, createdAt: 0 });
  assert.equal(b.transactions.length, 4);
  assert.equal(BigInt(b.transactions[0].value), r.toBurner);
  assert.equal(BigInt(b.transactions[1].value), r.toTeam);
  assert.deepEqual(iface.erc20.decodeFunctionData("transfer", b.transactions[2].data).map((x) => String(x).toLowerCase()), ["0x00000000000000000000000000000000000000d4", String(r.milordToBurn)]);
  assert.equal(b.transactions[3].data, iface.burner.encodeFunctionData("burnHeldTokens"));
});

console.log(`${n} Option D tests passed`);
