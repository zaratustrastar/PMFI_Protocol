// Reference price for MilordConverter entry quotes, from the MILORD/ETH pool.
//
// The pool's instantaneous price is NOT used: one large swap can move it for a block. Instead the quote
// service samples the pool tick (StateView.getSlot0) every block and signs the time-weighted mean over
// `window`. It refuses to quote when:
//   - the pool has less than `minHistory` seconds of observations (launch period: entry waits),
//   - the newest observation is older than `maxStaleness` (oracle outage: entry waits),
//   - spot differs from the TWAP by more than `maxDeviationBps` (active manipulation or a crash: entry waits).
// There is no fallback price, so the 0.002 ETH minimum fee can never be reached by breaking the oracle:
// the minimum applies only when a valid TWAP says 10% of the 1M-MILORD value is below it.
//
// Pool convention: currency0 = native ETH, currency1 = MILORD, both 18 decimals, so
// 1.0001^tick = MILORD per ETH and the ETH value of 1,000,000 MILORD = 1e24 / 1.0001^tick wei.

export function weiPerMillionAtTick(tick) {
  // 1e24 / 1.0001^tick, computed in log space to avoid float overflow, returned as bigint wei
  const log10 = 24 - tick * Math.log10(1.0001);
  const exp = Math.floor(log10);
  const mantissa = Math.round(10 ** (log10 - exp) * 1e12); // 12 significant digits
  return exp >= 12 ? BigInt(mantissa) * 10n ** BigInt(exp - 12) : BigInt(mantissa) / 10n ** BigInt(12 - exp);
}

export class TwapOracle {
  constructor({ window = 1800, minHistory = 1800, maxStaleness = 120, maxDeviationBps = 1000 } = {}) {
    Object.assign(this, { window, minHistory, maxStaleness, maxDeviationBps });
    this.obs = []; // { t, tick } ascending; each tick holds until the next observation
  }

  record(t, tick) {
    const last = this.obs[this.obs.length - 1];
    if (last && t < last.t) throw new Error("observations must be in time order");
    if (last && last.t === t) last.tick = tick;
    else this.obs.push({ t, tick });
  }

  meanTick(now) {
    const from = now - this.window;
    let acc = 0, span = 0;
    for (let i = 0; i < this.obs.length; i++) {
      const a = Math.max(this.obs[i].t, from);
      const b = i + 1 < this.obs.length ? Math.min(this.obs[i + 1].t, now) : now;
      if (b > a) { acc += this.obs[i].tick * (b - a); span += b - a; }
    }
    return span ? acc / span : null;
  }

  /** @returns {{weiPerMillion: bigint, observedAt: number, meanTick: number, spotTick: number}} or throws */
  price(now) {
    if (!this.obs.length || now - this.obs[0].t < this.minHistory) throw new Error("insufficient price history: entry waits");
    const last = this.obs[this.obs.length - 1];
    if (now - last.t > this.maxStaleness) throw new Error("price observations stale: entry waits");
    const meanTick = this.meanTick(now);
    const twap = weiPerMillionAtTick(meanTick);
    const spot = weiPerMillionAtTick(last.tick);
    const dev = twap > spot ? twap - spot : spot - twap;
    if (dev * 10000n > twap * BigInt(this.maxDeviationBps)) throw new Error("spot deviates from TWAP: entry waits");
    return { weiPerMillion: twap, observedAt: last.t, meanTick, spotTick: last.tick };
  }
}
