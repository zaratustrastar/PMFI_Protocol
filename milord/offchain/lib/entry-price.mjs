// The one place the NFT entry-fee price source is configured (quote service, keeper, tests).
// Approved economics: fee = max(0.002 ETH, 10% of the ETH value of 1,000,000 MILORD) (MilordConverter).
import { PoolPriceSource } from "./pool-price.mjs";

export const ENTRY_PRICE = {
  window: 1800, // 30-minute TWAP
  maxDeviationBps: 1000, // refuse when spot and TWAP differ by more than 10%
  maxStaleness: 120, // indexer must have synced within 2 minutes (indexer freshness, NOT time since the last swap)
  maxHeadLag: 120, // the RPC's latest block must be within 2 minutes of the wall clock
  openingTick: 203_280, // approved launch price (~10 ETH FDV); bootstrap only if the pool opened here
  tickLower: 134_220, // founding position range: outside it the pool has no liquidity
  tickUpper: 203_280,
  minLiquidity: 10n ** 23n, // ~37% of the founding position's liquidity (2.694e23)
};

/** D = deployments json (MILORD, PoolManager, StateView, lpFee, tickSpacing, hook address). */
export function entryPriceSource(provider, D, overrides = {}) {
  const c = { ...ENTRY_PRICE, ...overrides };
  return new PoolPriceSource(provider, {
    poolManager: D.PoolManager,
    stateView: D.StateView,
    key: ["0x0000000000000000000000000000000000000000", D.MILORD, D.lpFee, D.tickSpacing, D.HookrTaxHook ?? D.HookrRoot],
    window: c.window,
    minHistory: c.window,
    maxDeviationBps: c.maxDeviationBps,
    maxStaleness: c.maxStaleness,
    maxHeadLag: c.maxHeadLag,
    ...(c.clock ? { clock: c.clock } : {}),
    tickLower: c.tickLower,
    tickUpper: c.tickUpper,
    minLiquidity: c.minLiquidity,
    bootstrapOpeningTick: c.openingTick,
  });
}
