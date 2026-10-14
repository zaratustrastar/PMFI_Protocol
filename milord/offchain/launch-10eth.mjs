// node offchain/launch-10eth.mjs  ->  sim/output/launch-10eth.json
// One-sided MILORD-only position at the owner-approved ~10 ETH opening FDV (2026-10-14), comparing 25 / 50 / 75%
// of supply in liquidity. Same math as launch-plan.mjs (checked on-chain). 1.5% tax and 0.3% LP fee included.
import fs from "node:fs";
import { position, buy } from "./launch-plan.mjs";

const SUPPLY = 6_767_000_000, TAX = 0.015, LP_FEE = 0.003;
const ethPerMillion = (sq) => 1e6 / (sq * sq);
const SIZES = [0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10];
const out = [];
for (const rangeMultiple of [100, 1000]) for (const share of [0.25, 0.5, 0.75]) {
  const p = position({ fdvEth: 10, share, rangeMultiple });
  const open = ethPerMillion(p.sqU);
  const buys = SIZES.map((e) => {
    const b = buy(p, e);
    return {
      ethIn: e, milordOut: Math.round(b.milordOut), nftsWorth: +(b.milordOut / 1e6).toFixed(1),
      avgEthPerMillion: +(e / (b.milordOut / 1e6)).toPrecision(4),
      priceAfterEthPerMillion: +b.ethPerMillionAfter.toPrecision(4),
      priceChangePct: +(100 * (b.ethPerMillionAfter / open - 1)).toFixed(2),
      fdvAfterEth: +(b.ethPerMillionAfter * SUPPLY / 1e6).toFixed(2), capped: b.capped,
    };
  });
  // ETH that a seller can receive right after a 1 ETH first buy: pool holds the buyer's net ETH (after tax+LP fee).
  const b1 = buy(p, 1);
  const ethInPoolAfter1 = 1 * (1 - TAX) * (1 - LP_FEE);
  // a seller of 1M MILORD right after that buy: token1 in -> sqrt price falls; net of LP fee and then 1.5% tax on ETH out
  const sellM = 1e6 * (1 - LP_FEE);
  const sqAfterSell = b1.sqAfter + sellM / p.L;
  const ethOutGross = p.L * (1 / b1.sqAfter - 1 / sqAfterSell);
  out.push({
    share, rangeMultiple, milordInLiquidity: p.milord, milordReserved: SUPPLY - p.milord,
    tickLower: p.tickLower, tickUpper: p.tickUpper,
    openingEthPerMillion: +open.toPrecision(4), openingFdvEth: +(open * SUPPLY / 1e6).toFixed(3),
    rangeTopFdvEth: +(ethPerMillion(p.sqL) * SUPPLY / 1e6).toFixed(0),
    grossEthToBuyWholePosition: +((p.L * (1 / p.sqL - 1 / p.sqU)) / ((1 - TAX) * (1 - LP_FEE))).toFixed(1),
    ethInPoolAfterFirst1EthBuy: +ethInPoolAfter1.toFixed(4),
    sell1MAfterFirst1EthBuyEthNet: +(ethOutGross * (1 - TAX)).toFixed(6),
    maxNftsBackableFromPool: Math.floor(p.milord / 1e6),
    buys,
  });
}
fs.writeFileSync(new URL("../sim/output/launch-10eth.json", import.meta.url), JSON.stringify({ approvedOpeningFdvEth: 10, assumptions: { tax: TAX, lpFee: LP_FEE, supply: SUPPLY, tickSpacing: 60 }, rows: out }, null, 2));
for (const r of out) console.log(r.rangeMultiple + "x", r.share, r.tickLower, r.tickUpper, r.openingFdvEth, r.rangeTopFdvEth, r.grossEthToBuyWholePosition, r.sell1MAfterFirst1EthBuyEthNet, r.buys.map((b) => `${b.ethIn}:${b.priceChangePct}%/${b.nftsWorth}M/${b.avgEthPerMillion}`).join(" "));
