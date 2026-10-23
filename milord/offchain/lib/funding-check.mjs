import { sqrtPriceAtTick } from './v4math.mjs';
export const FUNDING = {
  pool: '0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a',
  weth: '0x0bd7d308f8e1639fab988df18a8011f41eacad73',
  usdg: '0x5fc5360d0400a0fd4f2af552add042d716f1d168',
  factory: '0x1f7d7550b1b028f7571e69a784071f0205fd2efa',
  quoter: '0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7',
};
export function meanTick(delta) {
  return Number(delta / 1800n - (delta < 0n && delta % 1800n !== 0n ? 1n : 0n));
}
export function referencePrice(tick, zero) {
  if (!Number.isInteger(tick) || Math.abs(tick) > 887272) throw Error('invalid tick');
  const s = sqrtPriceAtTick(tick);
  const shift = s <= (1n << 128n) - 1n ? 192n : 128n;
  const ratio = s * s / (1n << (192n - shift));
  return zero ? ratio * 10n ** 18n / (1n << shift) : (1n << shift) * 10n ** 18n / ratio;
}
export async function checkFunding(read, expected = FUNDING) {
  const checks = [], quotes = [];
  const require = (ok, what) => { checks.push({ok: !!ok, what}); if (!ok) throw Error(what); };
  const eq = (a,b) => a.toLowerCase() === b.toLowerCase();
  try {
    require(await read.chainId() === 4663, 'chain is Robinhood 4663');
    for (const name of ['pool','weth','usdg','factory','quoter']) require(await read.code(expected[name]) !== '0x', `${name} has code`);
    const [t0,t1,fee,factory,s0] = await Promise.all(['token0','token1','fee','factory','slot0'].map(m => read.call(expected.pool,m)));
    require((eq(t0,expected.weth) && eq(t1,expected.usdg)) || (eq(t1,expected.weth) && eq(t0,expected.usdg)), 'pool is WETH/USDG');
    require(eq(factory,expected.factory) && Number(fee) === 500, 'expected factory and fee');
    require(eq(await read.call(factory,'getPool',[t0,t1,fee]),expected.pool), 'factory identifies this pool');
    require(eq(await read.call(expected.quoter,'factory'),factory), 'quoter uses the pool factory');
    require(Number(await read.call(expected.usdg,'decimals')) === 6 && Number(await read.call(expected.weth,'decimals')) === 18, 'token decimals');
    // eth_call only, with a nonzero sender: works through the WETH proxy.
    await read.call(expected.weth,'deposit');
    await read.call(expected.weth,'withdraw',[0n]);
    require(true,'WETH deposit(0) and withdraw(0) simulations');
    require(Number(s0[3]) >= 2, 'observation cardinality');
    const [cum] = await read.call(expected.pool,'observe',[[1800,0]]);
    require(cum.length === 2, '30-minute history');
    const tick = meanTick(cum[1]-cum[0]);
    require(Math.abs(tick-Number(s0[1])) <= 100,'spot within 100 ticks of TWAP');
    const price = referencePrice(tick,eq(t0,expected.weth));
    require(price > 0n,'positive reference price');
    for (const amount of [5n*10n**16n,25n*10n**16n,10n**18n,2n*10n**18n]) {
      const q = await read.call(expected.quoter,'quoteExactInputSingle',[[expected.weth,expected.usdg,amount,fee,0]]);
      const minOut = (amount * price / 10n**18n) * 9900n / 10000n;
      quotes.push({amountIn:amount.toString(),amountOut:q[0].toString(),minOut:minOut.toString(),sqrtPriceAfter:q[1].toString(),initializedTicksCrossed:Number(q[2])});
      require(q[0] > 0n && q[0] >= minOut && q[1] > 0n, `quote ${amount} meets router TWAP minOut`);
    }
  } catch (e) { checks.push({ok:false,what:e.shortMessage ?? e.message}); }
  return {validated:checks.every(c=>c.ok),checks,quotes};
}
