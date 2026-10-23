// Reference price for MilordConverter entry quotes, from the Hookr MILORD/ETH Uniswap v4 pool.
//
// Observations are the pool's COMPLETE price history, not snapshots: Uniswap v4 changes a pool's price only
// in swaps, and every swap emits PoolManager.Swap(id, ..., sqrtPriceX96, liquidity, tick, fee). Together with
// the Initialize event, those logs give the exact tick at every moment, so the time-weighted mean below is
// the same number an on-chain accumulator (Uniswap V3 style) would compute. It is recomputable by anyone
// from public logs, needs no change to Hookr's hook and no second pool.
//
// Why a one-block push does not work: each tick is weighted by how long it persisted. Several swaps in one
// block all carry the block timestamp, so only the last price of the block counts, and only from that block
// onwards. A price pushed and restored inside one block has weight 0; a price held for k seconds of a
// 1,800-second window moves the mean by k/1800 of the push, while holding it costs the 1.5% + 1.5% tax, the
// LP fee and the arbitrage loss on the whole position the attacker moved.
//
// The quote price is max(TWAP, spot), so pushing the price DOWN (to pay a lower fee) cannot lower the fee
// below the current market price; pushing it UP only makes entry more expensive (griefing, paid in tax).
// A quote is refused (entry waits, there is NO fallback price) when:
//   - the indexer has not synced the pool recently            (maxStaleness, default 120 s)
//     Freshness is how recently the INDEXER read the chain, not how recently someone swapped: a quiet pool keeps
//     quoting at its unchanged price for as long as the indexer keeps syncing (lastSwapAt is reported, not limited).
//   - spot differs from the TWAP by more than maxDeviationBps   (default 10%: active manipulation or a crash)
//   - spot is outside the founding position's range             (no liquidity there: price moves for free)
//   - in-range liquidity is below minLiquidity                  (pool drained / abnormal)
//   - history is shorter than the window and bootstrap does not apply
// Bootstrap (first `window` seconds after Initialize only): the price is the HIGHEST MILORD price seen since
// the pool opened (incl. spot), and only if the pool opened at the approved launch tick. This can only
// over-charge, never under-charge, and ends automatically when the window has full history.
//
// Pool convention: currency0 = native ETH (address 0 always sorts first), currency1 = MILORD, both 18 decimals,
// so 1.0001^tick = MILORD per ETH and the ETH value of 1,000,000 MILORD = 1e24 / 1.0001^tick wei.
// A LOWER tick means a HIGHER MILORD price.

export function weiPerMillionAtTick(tick) {
  // 1e24 / 1.0001^tick, computed in log space to avoid float overflow, returned as bigint wei
  const log10 = 24 - tick * Math.log10(1.0001);
  const exp = Math.floor(log10);
  const mantissa = Math.round(10 ** (log10 - exp) * 1e12); // 12 significant digits
  return exp >= 12 ? BigInt(mantissa) * 10n ** BigInt(exp - 12) : BigInt(mantissa) / 10n ** BigInt(12 - exp);
}

export class TwapOracle {
  constructor({
    window = 1800,
    minHistory = window,
    maxStaleness = 120,
    maxDeviationBps = 1000,
    tickLower = -887272, // founding position range; ticks outside it have no liquidity
    tickUpper = 887272,
    minLiquidity = 0n,
    bootstrapOpeningTick = null, // approved launch tick (203,280); null disables bootstrap
  } = {}) {
    Object.assign(this, { window, minHistory, maxStaleness, maxDeviationBps, tickLower, tickUpper, minLiquidity, bootstrapOpeningTick });
    this.obs = []; // { t, tick } ascending; each tick holds until the next observation
    this.liquidity = null; // latest in-range liquidity, when the source reports it
    this.lastSwapAt = null; // time of the last Initialize/Swap event (informational)
  }

  /** swap = true for Initialize/Swap events, false for an indexer sync point (slot0 at the latest block). */
  record(t, tick, liquidity, swap = false) {
    const last = this.obs[this.obs.length - 1];
    if (last && t < last.t) throw new Error("observations must be in time order");
    if (last && last.t === t) last.tick = tick; // same block: only the last price of the block persists
    else this.obs.push({ t, tick });
    if (liquidity !== undefined && liquidity !== null) this.liquidity = BigInt(liquidity);
    if (swap) this.lastSwapAt = t;
  }

  /** Seconds until spot is back within maxDeviationBps of the TWAP if the price stays where it is (null: > window). */
  retryAfter(now) {
    const last = this.obs[this.obs.length - 1];
    const spot = weiPerMillionAtTick(last.tick);
    for (let k = 15; k <= this.window; k += 15) {
      this.obs.push({ t: now + k, tick: last.tick });
      const twap = weiPerMillionAtTick(this.meanTick(now + k));
      this.obs.pop();
      const dev = twap > spot ? twap - spot : spot - twap;
      if (dev * 10000n <= twap * BigInt(this.maxDeviationBps)) return k;
    }
    return null;
  }

  #clamp(tick) {
    return Math.min(Math.max(tick, this.tickLower), this.tickUpper);
  }

  meanTick(now) {
    const from = now - this.window;
    let acc = 0, span = 0;
    for (let i = 0; i < this.obs.length; i++) {
      const a = Math.max(this.obs[i].t, from);
      const b = i + 1 < this.obs.length ? Math.min(this.obs[i + 1].t, now) : now;
      if (b > a) { acc += this.#clamp(this.obs[i].tick) * (b - a); span += b - a; }
    }
    return span ? acc / span : null;
  }

  /** @returns {{weiPerMillion: bigint, observedAt: number, meanTick: number|null, spotTick: number, mode: string}} or throws */
  price(now) {
    if (!this.obs.length) throw new Error("insufficient price history: entry waits");
    const last = this.obs[this.obs.length - 1];
    if (now - last.t > this.maxStaleness) throw new Error("price observations stale: entry waits");
    if (last.tick < this.tickLower || last.tick > this.tickUpper) throw new Error("spot outside the liquidity range: entry waits");
    if (this.minLiquidity > 0n && (this.liquidity === null || this.liquidity < this.minLiquidity)) throw new Error("pool liquidity too low: entry waits");
    const spot = weiPerMillionAtTick(last.tick);
    const age = now - this.obs[0].t;

    if (age < this.minHistory) {
      if (this.bootstrapOpeningTick === null || age >= this.window) throw new Error("insufficient price history: entry waits");
      if (this.obs[0].tick !== this.bootstrapOpeningTick) throw new Error("pool did not open at the approved launch price: entry waits");
      const highestPriceTick = Math.min(...this.obs.map((o) => this.#clamp(o.tick))); // lowest tick = highest MILORD price
      return { weiPerMillion: weiPerMillionAtTick(highestPriceTick), observedAt: last.t, meanTick: null, spotTick: last.tick, mode: "bootstrap", lastSwapAt: this.lastSwapAt };
    }

    const meanTick = this.meanTick(now);
    const twap = weiPerMillionAtTick(meanTick);
    const dev = twap > spot ? twap - spot : spot - twap;
    if (dev * 10000n > twap * BigInt(this.maxDeviationBps)) {
      throw Object.assign(new Error("spot deviates from TWAP: entry waits"), { retryAfter: this.retryAfter(now) });
    }
    return { weiPerMillion: twap > spot ? twap : spot, observedAt: last.t, meanTick, spotTick: last.tick, mode: "twap", lastSwapAt: this.lastSwapAt };
  }
}
