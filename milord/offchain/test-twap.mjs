// Unit tests for the entry-fee price (offchain/lib/twap.mjs). Run: node offchain/test-twap.mjs
import assert from "node:assert/strict";
import { TwapOracle, weiPerMillionAtTick } from "./lib/twap.mjs";
import { ENTRY_PRICE } from "./lib/entry-price.mjs";

const SUPPLY_M = 6767n; // 6,767,000,000 MILORD = 6,767 x 1M
const E = 10n ** 18n;
const feeFor = (wpm) => { const p = wpm / 10n; const min = 2n * 10n ** 15n; return p > min ? p : min; }; // MilordConverter.entryFeeFor
const tickForFdv = (fdvEth) => Math.round(Math.log(6_767_000_000 / fdvEth) / Math.log(1.0001)); // MILORD per ETH
const near = (a, b, bps, msg) => { const d = a > b ? a - b : b - a; assert.ok(d * 10000n <= b * BigInt(bps), `${msg}: ${a} vs ${b}`); };
const oracle = (o = {}) => new TwapOracle({
  window: ENTRY_PRICE.window, maxDeviationBps: ENTRY_PRICE.maxDeviationBps, maxStaleness: ENTRY_PRICE.maxStaleness,
  tickLower: ENTRY_PRICE.tickLower, tickUpper: ENTRY_PRICE.tickUpper, minLiquidity: ENTRY_PRICE.minLiquidity,
  bootstrapOpeningTick: ENTRY_PRICE.openingTick, ...o,
});
const L = 269_400_000_000_000_000_000_000n; // founding position liquidity (~2.694e23)
let n = 0; const t = (name, f) => { f(); n++; console.log("ok", name); };

t("1-4 fee at 10/100/200/500 ETH FDV from pool ticks", () => {
  // 203,280 is the tick-spacing-aligned (60) launch tick nearest 10 ETH FDV (exact tick 203,338: FDV ~10.06 ETH)
  assert.ok(Math.abs(tickForFdv(10) - 203_280) < 60, `10 ETH FDV tick ${tickForFdv(10)} ~ 203,280`);
  assert.equal(feeFor(weiPerMillionAtTick(203_280)), 2n * 10n ** 15n);
  for (const [fdv, fee] of [[10, 2_000_000n], [100, 2_000_000n], [200, 2_955_519n], [500, 7_388_799n]]) {
    const wpm = weiPerMillionAtTick(tickForFdv(fdv));
    near(wpm, (BigInt(fdv) * E) / SUPPLY_M, 1, `wpm @${fdv}`);
    near(feeFor(wpm), fee * 10n ** 9n, 1, `fee @${fdv}`);
  }
  // crossover: 10% of the value exceeds 0.002 ETH above ~135.34 ETH FDV
  assert.equal(feeFor(weiPerMillionAtTick(tickForFdv(135))), 2n * 10n ** 15n);
  assert.ok(feeFor(weiPerMillionAtTick(tickForFdv(136))) > 2n * 10n ** 15n);
});

t("bootstrap: bounded to the first 30 min, highest price since open, approved opening tick only", () => {
  const o = oracle();
  o.record(1000, 203_280, L);
  o.record(1060, 202_000, L); // buys
  o.record(1200, 202_500, L); // a sell back
  const q = o.price(1300);
  assert.equal(q.mode, "bootstrap");
  assert.equal(q.weiPerMillion, weiPerMillionAtTick(202_000)); // highest price seen, above spot
  assert.equal(feeFor(q.weiPerMillion), 2n * 10n ** 15n);
  for (let s = 1300; s <= 2800; s += 60) o.record(s, 202_500, L);
  assert.equal(o.price(2800).mode, "twap"); // full window: bootstrap is over for good
  const wrong = oracle(); wrong.record(1000, 190_000, L);
  assert.throws(() => wrong.price(1100), /approved launch price/);
  const none = oracle({ bootstrapOpeningTick: null }); none.record(1000, 203_280, L);
  assert.throws(() => none.price(1100), /insufficient price history/);
});

t("bootstrap pushed price down cannot lower the quote", () => {
  const o = oracle();
  o.record(1000, 203_280, L); o.record(1060, 200_000, L); o.record(1120, 203_280, L); // pump then dump
  assert.equal(o.price(1200).weiPerMillion, weiPerMillionAtTick(200_000));
});

t("5 same-block manipulation has zero weight", () => {
  const o = oracle();
  for (let s = 0; s <= 1800; s += 12) o.record(s, 190_000, L);
  o.record(1812, 150_000, L); o.record(1812, 190_000, L); // push and restore in one block
  const q = o.price(1820);
  assert.equal(q.mode, "twap");
  assert.equal(q.meanTick, 190_000);
  assert.equal(q.weiPerMillion, weiPerMillionAtTick(190_000));
  // push held at the end of the block: refused (spot >10% from TWAP), never priced
  o.record(1824, 180_000, L);
  assert.throws(() => o.price(1830), /deviates/);
  // push the price DOWN in one block (cheaper fee): refused too
  const d = oracle(); for (let s = 0; s <= 1800; s += 12) d.record(s, 190_000, L);
  d.record(1812, 195_000, L);
  assert.throws(() => d.price(1813), /deviates/);
});

t("6 sustained multi-block push moves the TWAP by k/1800 and can only raise the fee", () => {
  const o = oracle();
  for (let s = 0; s <= 1800; s += 12) o.record(s, 190_000, L);
  o.record(1800, 189_400, L); // ~6% higher price, held
  o.record(2100, 189_400, L); // indexer sync (latest block, no new swap)
  const q = o.price(2100); // held 5 minutes
  assert.ok(Math.abs(q.meanTick - (190_000 - 600 * 300 / 1800)) < 1e-6, `${q.meanTick}`);
  assert.equal(q.weiPerMillion, weiPerMillionAtTick(189_400)); // max(TWAP, spot) = spot
  // sustained push DOWN (cheaper): quote = TWAP, which is above spot
  const d = oracle(); for (let s = 0; s <= 1800; s += 12) d.record(s, 190_000, L);
  d.record(1800, 190_600, L); d.record(2100, 190_600, L);
  const qd = d.price(2100);
  assert.ok(qd.weiPerMillion > weiPerMillionAtTick(190_600));
  assert.equal(qd.weiPerMillion, weiPerMillionAtTick(qd.meanTick));
});

t("7 missing or stale observations refuse quotes (no fallback)", () => {
  assert.throws(() => oracle().price(100), /insufficient price history/);
  const o = oracle();
  for (let s = 0; s <= 1800; s += 60) o.record(s, 190_000, L);
  assert.ok(o.price(1800));
  assert.throws(() => o.price(1800 + 121), /stale/);
  assert.throws(() => o.record(1700, 190_000, L), /time order/);
});

t("8 inactive pool is fine; low liquidity and out-of-range spot refuse", () => {
  const o = oracle();
  o.record(0, 190_000, L);
  o.record(36_000, 190_000, L); // no swaps for 10 hours, indexer still synced: the price did not change
  assert.equal(o.price(36_010).weiPerMillion, weiPerMillionAtTick(190_000));
  const thin = oracle(); for (let s = 0; s <= 1800; s += 60) thin.record(s, 190_000, 5n * 10n ** 22n);
  assert.throws(() => thin.price(1800), /liquidity too low/);
  const noLiq = oracle(); for (let s = 0; s <= 1800; s += 60) noLiq.record(s, 190_000);
  assert.throws(() => noLiq.price(1800), /liquidity too low/);
  const out = oracle(); for (let s = 0; s <= 1800; s += 60) out.record(s, 190_000, L);
  out.record(1860, 120_000, 0n); // pushed past the top of the range into empty ticks
  assert.throws(() => out.price(1860), /outside the liquidity range/);
  // a brief excursion outside the range is clamped in the mean
  const ex = oracle(); for (let s = 0; s <= 1800; s += 60) ex.record(s, 140_000, L);
  ex.record(1801, 100_000, L); ex.record(1802, 140_000, L);
  assert.ok(ex.price(1810).meanTick >= ENTRY_PRICE.tickLower);
});

t("first-buy jump after bootstrap: refusal carries a retryAfter estimate (~14 min for 19.8%)", () => {
  const o = oracle();
  for (let s = 0; s <= 7200; s += 60) o.record(s, 203_280, L);
  const jumped = 203_280 - Math.round(Math.log(1.198) / Math.log(1.0001));
  o.record(7201, jumped, L, true);
  let err; try { o.price(7201); } catch (e) { err = e; }
  assert.match(err.message, /deviates/);
  assert.ok(err.retryAfter >= 840 && err.retryAfter <= 870, `${err.retryAfter}`);
  for (let s = 7260; s <= 7201 + err.retryAfter; s += 60) o.record(s, jumped, L);
  o.record(7201 + err.retryAfter, jumped, L);
  assert.equal(o.price(7201 + err.retryAfter).mode, "twap");
  assert.equal(o.lastSwapAt, 7201); // sync points do not count as swaps
});

t("9 tick to wei conversion: ETH is currency0, both 18 decimals", () => {
  assert.equal(weiPerMillionAtTick(0), 10n ** 24n); // 1:1 => 1M MILORD = 1M ETH
  near(weiPerMillionAtTick(203_280), 1_486_288_643_256_000n, 1, "launch tick (FDV ~10.06 ETH)");
  assert.ok(weiPerMillionAtTick(100_000) > weiPerMillionAtTick(100_001)); // lower tick = pricier MILORD
});

console.log(`${n} twap test groups passed`);
