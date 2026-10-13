import assert from "node:assert/strict";
import { TwapOracle, weiPerMillionAtTick } from "./lib/twap.mjs";

// tick 203,280 ~ 10 ETH fully diluted: 1M MILORD ~ 0.00148 ETH
const w = weiPerMillionAtTick(203_280);
assert.ok(w > 1_45e13 && w < 1_50e13, `tick price ${w}`);

const o = new TwapOracle({ window: 1800, minHistory: 1800, maxStaleness: 120, maxDeviationBps: 1000 });
o.record(1000, 203_280);
assert.throws(() => o.price(1600), /insufficient price history/);               // launch: no quote
for (let t = 1060; t <= 2900; t += 60) o.record(t, 203_280);
assert.equal(o.price(2900).weiPerMillion, weiPerMillionAtTick(203_280));
assert.throws(() => o.price(3100), /stale/);                                      // outage: no quote, no fallback
o.record(2960, 203_280 - 3_000);                                                  // one-block pump (~35% higher)
assert.throws(() => o.price(2960), /deviates/);                                   // manipulation: no quote
const o2 = new TwapOracle({ window: 600, minHistory: 600 });
for (let t = 0; t <= 600; t += 60) o2.record(t, t < 300 ? 100_000 : 100_600);
assert.ok(Math.abs(o2.meanTick(600) - 100_300) < 1);                               // time-weighted
console.log("twap checks passed");
