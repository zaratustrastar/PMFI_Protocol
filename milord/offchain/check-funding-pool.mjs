// RPC_URL=<robinhood rpc> node offchain/check-funding-pool.mjs [pool] [usdg]
//
// READ-ONLY check of the ETH -> USDG funding route the ArbitrageFundingRouter will use (blocker B7). eth_call only:
// no keys, no transactions. Verifies, for the Uniswap V3 pool:
//   - it is a V3 pool of WETH and USDG (USDG: 6 decimals), and WETH is a WETH9 (deposit/withdraw selectors present);
//   - the 30-minute TWAP the router's UniV3TwapReference uses can be read now (observe([1800, 0]) succeeds);
//   - observation cardinality, in-range liquidity, and the spot vs 30-minute TWAP gap (UniV3TwapReference refuses
//     a gap above its maxTickDeviation, default 100 ticks ~ 1%);
//   - how much USDG one router swap at the proposed maximum (2 ETH) would move the price, from in-range liquidity.
// Exit code 1 when a requirement fails.
import { JsonRpcProvider, Contract } from "ethers";

const rpc = process.env.RPC_URL;
if (!rpc) throw new Error("set RPC_URL (read-only)");
const POOL = process.argv[2] ?? "0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a"; // web research, unverified (docs/launch/research)
const USDG = (process.argv[3] ?? "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168").toLowerCase();
const p = new JsonRpcProvider(rpc);
const pool = new Contract(POOL, [
  "function token0() view returns (address)", "function token1() view returns (address)", "function fee() view returns (uint24)",
  "function liquidity() view returns (uint128)", "function factory() view returns (address)",
  "function slot0() view returns (uint160 sqrtPriceX96,int24 tick,uint16 observationIndex,uint16 observationCardinality,uint16 observationCardinalityNext,uint8,bool)",
  "function observe(uint32[]) view returns (int56[] tickCumulatives, uint160[])",
], p);
const erc20 = (a) => new Contract(a, ["function decimals() view returns (uint8)", "function symbol() view returns (string)"], p);
const out = { chainId: Number((await p.getNetwork()).chainId), pool: POOL, checks: [] };
const check = (ok, what, detail) => out.checks.push({ ok, what, ...(detail !== undefined ? { detail } : {}) });

if ((await p.getCode(POOL)) === "0x") { check(false, "pool has code"); console.log(JSON.stringify(out, null, 2)); process.exit(1); }
const [t0, t1, fee, liq, s0, factory] = await Promise.all([pool.token0(), pool.token1(), pool.fee(), pool.liquidity(), pool.slot0(), pool.factory()]);
const usdgIs0 = t0.toLowerCase() === USDG;
const weth = usdgIs0 ? t1 : t0;
check(usdgIs0 || t1.toLowerCase() === USDG, "pool holds USDG", { token0: t0, token1: t1 });
const [ud, us, wd, ws] = await Promise.all([erc20(USDG).decimals(), erc20(USDG).symbol(), erc20(weth).decimals(), erc20(weth).symbol()]);
check(Number(ud) === 6, "USDG has 6 decimals", { symbol: us, decimals: Number(ud) });
check(Number(wd) === 18 && /WETH/i.test(ws), "other token is WETH", { address: weth, symbol: ws });
const wethCode = await p.getCode(weth);
check(wethCode.includes("d0e30db0") && wethCode.includes("2e1a7d4d"), "WETH has deposit()/withdraw(uint256)");
check(Number(s0.observationCardinality) >= 2, "observation cardinality >= 2", { cardinality: Number(s0.observationCardinality), next: Number(s0.observationCardinalityNext) });
let twapTick = null;
try {
  const [cum] = await pool.observe([1800, 0]);
  twapTick = Number((cum[1] - cum[0]) / 1800n);
  check(true, "30-minute observe() works", { twapTick });
} catch (e) {
  check(false, "30-minute observe() works", { error: e.shortMessage ?? e.message, fix: "anyone can call increaseObservationCardinalityNext; then wait 30 min of trades" });
}
if (twapTick !== null) check(Math.abs(twapTick - Number(s0.tick)) <= 100, "spot within 100 ticks of the 30-min TWAP", { spotTick: Number(s0.tick), twapTick });
// price: USDG per ETH from the spot tick (decimals 6 vs 18)
const ratio = Math.pow(1.0001, Number(s0.tick)); // token1 per token0 in raw units
// ratio = raw token1 per raw token0; USDG has 6 decimals and WETH 18, so human USDG per ETH = raw ratio x 1e12
const human = usdgIs0 ? (1 / ratio) * 1e12 : ratio * 1e12;
out.spot = { tick: Number(s0.tick), usdgPerEth: +human.toFixed(2), feePips: Number(fee), inRangeLiquidity: liq.toString(), factory };
// Price impact of a 2 ETH swap at constant in-range liquidity: d(sqrtP) = amountIn / L (in the input token's terms).
const L = Number(liq);
if (L > 0) {
  const sqrtP = Number(s0.sqrtPriceX96) / 2 ** 96;
  const ethIn = 2e18;
  const after = usdgIs0 ? 1 / (1 / sqrtP + ethIn / L) : sqrtP + ethIn / L; // WETH is token1: price rises; token0: 1/sqrtP rises
  const impactBps = Math.abs((after * after) / (sqrtP * sqrtP) - 1) * 1e4;
  out.swap2EthImpactBps = +impactBps.toFixed(1);
  check(impactBps <= 100, "a 2 ETH swap moves the price by at most the router's 100 bps slippage", { impactBps: +impactBps.toFixed(1) });
} else check(false, "pool has in-range liquidity");
console.log(JSON.stringify(out, null, 2));
process.exit(out.checks.every((c) => c.ok) ? 0 : 1);
