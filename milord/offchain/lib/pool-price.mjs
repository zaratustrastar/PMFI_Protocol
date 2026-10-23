// Feeds TwapOracle from the Uniswap v4 PoolManager: the Initialize event gives the opening tick and time,
// every Swap event gives the post-swap tick at its block time (the pool's complete price history), and the
// current slot0 + in-range liquidity mark how far the indexer has synced.
// History therefore survives a service restart and cannot be faked by the service's own uptime.
import { Contract, AbiCoder, keccak256 } from "ethers";
import { TwapOracle } from "./twap.mjs";
import { chunkedLogs } from "./logs.mjs";

const PM_ABI = [
  "event Initialize(bytes32 indexed id, address indexed currency0, address indexed currency1, uint24 fee, int24 tickSpacing, address hooks, uint160 sqrtPriceX96, int24 tick)",
  "event Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)",
];
const SV_ABI = [
  "function getSlot0(bytes32) view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)",
  "function getLiquidity(bytes32) view returns (uint128)",
];
const ERC20_ABI = ["function decimals() view returns (uint8)"];
const ZERO = "0x0000000000000000000000000000000000000000";

export const poolId = (key) => keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], key));

/**
 * Checks the token ordering and decimals the price formula relies on: currency0 must be native ETH and
 * currency1 an 18-decimal token. Throws otherwise (a misconfigured key would silently invert or rescale prices).
 */
export async function verifyPoolKey(provider, key, expectedToken) {
  if (key[0] !== ZERO) throw new Error("pool key: currency0 must be native ETH");
  if (expectedToken && key[1].toLowerCase() !== expectedToken.toLowerCase()) throw new Error("pool key: currency1 is not MILORD");
  const dec = Number(await new Contract(key[1], ERC20_ABI, provider).decimals());
  if (dec !== 18) throw new Error(`pool key: currency1 has ${dec} decimals, expected 18`);
}

export class PoolPriceSource {
  /** key = [currency0, currency1, fee, tickSpacing, hooks]; options as TwapOracle */
  constructor(provider, { poolManager, stateView, key, startBlock = 0, logStep = 10_000, maxHeadLag = Infinity, clock = () => Math.floor(Date.now() / 1000), ...oracleOpts }) {
    this.maxHeadLag = maxHeadLag; // wall-clock check that the RPC's chain head itself is current
    this.clock = clock;
    this.provider = provider;
    this.key = key;
    this.pm = new Contract(poolManager, PM_ABI, provider);
    this.sv = new Contract(stateView, SV_ABI, provider);
    this.id = poolId(key);
    this.oracle = new TwapOracle(oracleOpts);
    this.nextBlock = startBlock; // the deployments file's startBlock: no pool event can precede it
    this.logStep = logStep;
    this.ts = new Map();
    this.verified = false;
  }

  async #blockTime(n) {
    if (!this.ts.has(n)) this.ts.set(n, (await this.provider.getBlock(n)).timestamp);
    return this.ts.get(n);
  }

  /** Pull new events, then record the current tick and liquidity at the latest block time. */
  async sync() {
    if (!this.verified) {
      await verifyPoolKey(this.provider, this.key);
      this.verified = true;
    }
    const latest = await this.provider.getBlock("latest");
    if (this.nextBlock <= latest.number) {
      const logs = [
        ...(await chunkedLogs((a, b) => this.pm.queryFilter(this.pm.filters.Initialize(this.id), a, b), this.nextBlock, latest.number, { step: this.logStep })),
        ...(await chunkedLogs((a, b) => this.pm.queryFilter(this.pm.filters.Swap(this.id), a, b), this.nextBlock, latest.number, { step: this.logStep })),
      ].sort((a, b) => a.blockNumber - b.blockNumber || a.index - b.index);
      for (const l of logs) this.oracle.record(await this.#blockTime(l.blockNumber), Number(l.args.tick), null, true);
      this.nextBlock = latest.number + 1;
    }
    if (this.oracle.obs.length) {
      const [slot0, liq] = await Promise.all([this.sv.getSlot0(this.id), this.sv.getLiquidity(this.id)]);
      this.oracle.record(latest.timestamp, Number(slot0.tick), liq);
    }
    return latest.timestamp;
  }

  /** @returns {{weiPerMillion, observedAt, mode}} or throws "... entry waits" */
  async price() {
    const now = await this.sync();
    if (this.clock() - now > this.maxHeadLag) throw new Error("RPC chain head is behind the wall clock: entry waits");
    return this.oracle.price(now);
  }
}
