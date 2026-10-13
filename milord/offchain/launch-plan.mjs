// node offchain/launch-plan.mjs  ->  prints and writes sim/output/launch-options.json
//
// ILLUSTRATIVE launch arithmetic for a MILORD-only Uniswap v4 position (ETH = currency0, MILORD = currency1).
// None of these rows is a recommendation or an approved production value. Uses concentrated-liquidity math
// for a single position with constant liquidity across its range (checked against the on-chain simulation).
import fs from "node:fs";

const SUPPLY = 6_767_000_000;
const TAX = 0.015, LP_FEE = 0.003, SPACING = 60;
const tickOf = (milordPerEth) => Math.log(milordPerEth) / Math.log(1.0001);
const floorTo = (t, s) => Math.floor(t / s) * s;

export function position({ fdvEth, share, rangeMultiple }) {
  const milord = SUPPLY * share;
  const tickUpper = floorTo(tickOf(SUPPLY / fdvEth), SPACING);
  const tickLower = tickUpper - floorTo(Math.log(rangeMultiple) / Math.log(1.0001), SPACING);
  const sqU = 1.0001 ** (tickUpper / 2), sqL = 1.0001 ** (tickLower / 2);
  const L = milord / (sqU - sqL); // amount1 = L (sqrtUpper - sqrtLower)
  return { milord, tickUpper, tickLower, sqU, sqL, L };
}

/** Exact-input buy of `ethIn` gross ETH from the opening price. */
export function buy(p, ethIn, from = p.sqU) {
  const net = ethIn * (1 - TAX) * (1 - LP_FEE);
  let inv = 1 / from + net / p.L; // token0 in: 1/sqrtP rises by dx/L
  let sq = 1 / inv;
  const capped = sq < p.sqL;
  if (capped) sq = p.sqL;
  const milordOut = p.L * (from - sq);
  return { sqAfter: sq, milordOut, capped, ethPerMillionAfter: 1e6 / (sq * sq) };
}

const ethPerMillion = (sq) => 1e6 / (sq * sq);
const rows = [];
for (const share of [0.4, 0.5, 0.6]) for (const fdvEth of [2.5, 5, 10, 25]) for (const rangeMultiple of [100, 1000]) {
  const p = position({ fdvEth, share, rangeMultiple });
  const open = ethPerMillion(p.sqU);
  const impacts = Object.fromEntries([0.1, 1, 5, 10].map((e) => { const b = buy(p, e); return [`${e}ETH`, { milordOut: Math.round(b.milordOut), priceChangePct: +(100 * (b.ethPerMillionAfter / open - 1)).toFixed(1) }]; }));
  const ethToClearRange = (p.L * (1 / p.sqL - 1 / p.sqU)) / ((1 - TAX) * (1 - LP_FEE));
  rows.push({
    share, milordInLiquidity: p.milord, milordOutsideLiquidity: SUPPLY - p.milord, openingFdvEth: fdvEth,
    openingEthPerMillion: +open.toPrecision(4), tickUpper: p.tickUpper, tickLower: p.tickLower, rangeTopFdvEth: +(fdvEth * rangeMultiple).toPrecision(4),
    impacts, grossEthToBuyWholePosition: +ethToClearRange.toFixed(1),
    entryFeeAtOpenEth: Math.max(0.1 * open, 0.002), minimumFeeUntilFdvEth: +(0.002 / 0.1 / 1e6 * SUPPLY).toFixed(1),
  });
}
fs.mkdirSync(new URL("../sim/output/", import.meta.url), { recursive: true });
fs.writeFileSync(new URL("../sim/output/launch-options.json", import.meta.url), JSON.stringify({ illustrative: true, assumptions: { tax: TAX, lpFee: LP_FEE, tickSpacing: SPACING, supply: SUPPLY }, rows }, null, 2));
const ref = rows.find((r) => r.share === 0.5 && r.openingFdvEth === 10 && r.rangeTopFdvEth === 10000);
console.log("reference (sim) row:", JSON.stringify(ref));
console.table(rows.map((r) => ({ share: r.share, fdv: r.openingFdvEth, top: r.rangeTopFdvEth, open: r.openingEthPerMillion, "1ETH%": r.impacts["1ETH"].priceChangePct, "10ETH%": r.impacts["10ETH"].priceChangePct, clearEth: r.grossEthToBuyWholePosition })));
