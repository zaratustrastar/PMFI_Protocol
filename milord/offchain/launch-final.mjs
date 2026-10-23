// node offchain/launch-final.mjs  ->  sim/output/launch-final.json
// Final launch configuration (Bobo, 2026-10-14): 100% of supply (6,767,000,000 MILORD) in one MILORD-only
// Uniswap v4 position, 0 ETH, ~10 ETH opening FDV, range top ~1,000x the opening price, 1.5% buy/sell tax, 0.3% LP
// fee. First-buy price impact and purchasable amounts. Same math as launch-plan.mjs, checked on-chain by
// test/launch/HookrFullSupply.t.sol.
import fs from "node:fs";
import { position, buy } from "./launch-plan.mjs";

const SUPPLY = 6_767_000_000, TAX = 0.015, LP_FEE = 0.003;
const ethPerMillion = (sq) => 1e6 / (sq * sq);
const p = position({ fdvEth: 10, share: 1, rangeMultiple: 1000 });
const open = ethPerMillion(p.sqU);
const grossAll = (p.L * (1 / p.sqL - 1 / p.sqU)) / ((1 - TAX) * (1 - LP_FEE));
const SIZES = [0.002, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 200];
const buys = SIZES.map((e) => {
  const b = buy(p, e);
  return {
    ethIn: e, milordOut: Math.round(b.milordOut), pctOfSupply: +(100 * b.milordOut / SUPPLY).toFixed(3),
    nftsWorth: Math.floor(b.milordOut / 1e6), avgEthPerMillion: +(e / (b.milordOut / 1e6)).toPrecision(4),
    priceAfterEthPerMillion: +b.ethPerMillionAfter.toPrecision(4), priceChangePct: +(100 * (b.ethPerMillionAfter / open - 1)).toFixed(2),
    fdvAfterEth: +(b.ethPerMillionAfter * SUPPLY / 1e6).toFixed(2),
  };
});
// ETH that buys exactly N MILORD at open (inverse of buy)
const ethFor = (milord) => { const sq = p.sqU - milord / p.L; return p.L * (1 / sq - 1 / p.sqU) / ((1 - TAX) * (1 - LP_FEE)); };
// net ETH of buys needed before the 10% entry fee exceeds the 0.002 ETH minimum (FDV >= 135.34 ETH)
const sqAt = (fdv) => Math.sqrt(SUPPLY / fdv);
const ethToFdv = (fdv) => p.L * (1 / sqAt(fdv) - 1 / p.sqU) / ((1 - TAX) * (1 - LP_FEE));
const out = {
  config: { supply: SUPPLY, milordInLiquidity: SUPPLY, milordOutside: 0, ethSeeded: 0, tickLower: p.tickLower, tickUpper: p.tickUpper,
    tickSpacing: 60, lpFee: LP_FEE, tax: TAX, openingEthPerMillion: +open.toPrecision(5), openingFdvEth: +(open * SUPPLY / 1e6).toFixed(3),
    rangeTopEthPerMillion: +ethPerMillion(p.sqL).toPrecision(4), rangeTopFdvEth: Math.round(ethPerMillion(p.sqL) * SUPPLY / 1e6) },
  maxPurchasable: { milord: SUPPLY, grossEthToBuyAll: +grossAll.toFixed(1),
    note: "no wallet or transaction limits; one swap can buy any amount up to the whole position; above the range top the pool holds only ETH" },
  oneMilordAtOpen: { milord: 1e6, grossEth: +ethFor(1e6).toPrecision(4), plusMinimumEntryFeeEth: 0.002 },
  tenPercentFeePathStartsAt: { fdvEth: 135.34, grossEthOfNetBuysNeeded: +ethToFdv(135.34).toFixed(1) },
  buys,
};
fs.mkdirSync(new URL("../sim/output/", import.meta.url), { recursive: true });
fs.writeFileSync(new URL("../sim/output/launch-final.json", import.meta.url), JSON.stringify(out, null, 2));
console.log(JSON.stringify(out.config), JSON.stringify(out.maxPurchasable), JSON.stringify(out.oneMilordAtOpen), JSON.stringify(out.tenPercentFeePathStartsAt));
for (const b of buys) console.log(b.ethIn, b.milordOut, b.pctOfSupply + "%", b.nftsWorth, b.avgEthPerMillion, b.priceChangePct + "%", b.fdvAfterEth);
