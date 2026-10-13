// Feeds TwapOracle from the Uniswap v4 PoolManager: the Initialize event gives the opening tick and time,
// every Swap event gives the post-swap tick at its block time, and the current slot0 closes the series.
// History therefore survives a service restart and cannot be faked by the service's own uptime.
import { Contract, AbiCoder, keccak256 } from "ethers";
import { TwapOracle } from "./twap.mjs";

const PM_ABI = [
  "event Initialize(bytes32 indexed id, address indexed currency0, address indexed currency1, uint24 fee, int24 tickSpacing, address hooks, uint160 sqrtPriceX96, int24 tick)",
  "event Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)",
];
const SV_ABI = ["function getSlot0(bytes32) view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)"];

export const poolId = (key) => keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], key));

export class PoolPriceSource {
  /** key = [currency0, currency1, fee, tickSpacing, hooks]; options as TwapOracle */
  constructor(provider, { poolManager, stateView, key, ...oracleOpts }) {
    this.provider = provider;
    this.pm = new Contract(poolManager, PM_ABI, provider);
    this.sv = new Contract(stateView, SV_ABI, provider);
    this.id = poolId(key);
    this.oracle = new TwapOracle(oracleOpts);
    this.nextBlock = 0;
    this.ts = new Map();
  }

  async #blockTime(n) {
    if (!this.ts.has(n)) this.ts.set(n, (await this.provider.getBlock(n)).timestamp);
    return this.ts.get(n);
  }

  /** Pull new events, then record the current tick at the latest block time. */
  async sync() {
    const latest = await this.provider.getBlock("latest");
    if (this.nextBlock <= latest.number) {
      const logs = [
        ...(await this.pm.queryFilter(this.pm.filters.Initialize(this.id), this.nextBlock, latest.number)),
        ...(await this.pm.queryFilter(this.pm.filters.Swap(this.id), this.nextBlock, latest.number)),
      ].sort((a, b) => a.blockNumber - b.blockNumber || a.index - b.index);
      for (const l of logs) this.oracle.record(await this.#blockTime(l.blockNumber), Number(l.args.tick));
      this.nextBlock = latest.number + 1;
    }
    if (this.oracle.obs.length) this.oracle.record(latest.timestamp, Number((await this.sv.getSlot0(this.id)).tick));
    return latest.timestamp;
  }

  /** @returns {{weiPerMillion, observedAt}} or throws "... entry waits" */
  async price() {
    const now = await this.sync();
    return this.oracle.price(now);
  }
}
