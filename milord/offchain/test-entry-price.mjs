// Integration test of the NFT entry-fee price on a local chain with the real Uniswap v4 PoolManager, the
// Hookr-style tax hook and the deployed MilordConverter. Swaps are real v4 swaps; the price service is the
// production code path (offchain/lib/entry-price.mjs -> PoolPriceSource -> TwapOracle).
// Run: ./sim/run-entry-price.sh   (fresh anvil + DeployLocal; local test keys only, nothing is broadcast elsewhere)
import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { JsonRpcProvider, Wallet, Contract, AbiCoder, keccak256, parseEther, formatEther, hexlify, randomBytes } from "ethers";
import { entryPriceSource, ENTRY_PRICE } from "./lib/entry-price.mjs";
import { verifyPoolKey } from "./lib/pool-price.mjs";
import { weiPerMillionAtTick } from "./lib/twap.mjs";
import { issueQuote } from "./lib/quote.mjs";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const D = JSON.parse(fs.readFileSync(path.join(root, "sim/deployments.local.json"), "utf8"));
const abi = (f) => JSON.parse(fs.readFileSync(path.join(root, `out/${f}.sol/${f}.json`), "utf8")).abi;
const provider = new JsonRpcProvider(process.env.RPC ?? "http://127.0.0.1:8545", undefined, { pollingInterval: 20, cacheTimeout: -1 });
const quoteSigner = new Wallet("0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6", provider); // anvil #3
const alice = new Wallet("0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e", provider); // anvil #6
const attacker = new Wallet("0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356", provider); // anvil #7
const ZERO = "0x0000000000000000000000000000000000000000";
const KEY = [ZERO, D.MILORD, D.lpFee, D.tickSpacing, D.HookrTaxHook];
const ID = keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], KEY));
const token = new Contract(D.MILORD, abi("MILORDToken"), provider);
const conv = new Contract(D.MilordConverter, abi("MilordConverter"), provider);
const router = new Contract(D.SwapRouter, abi("PoolSwapTest"), provider);
const sv = new Contract(D.StateView, ["function getSlot0(bytes32) view returns (uint160,int24,uint24,uint24)", "function getLiquidity(bytes32) view returns (uint128)"], provider);
const SETTINGS = [false, false];
const MIN_SQRT = 4295128740n, MAX_SQRT = 1461446703485210103287273052203988822378723970341n;
const chainId = Number((await provider.getNetwork()).chainId);

const now = async () => (await provider.getBlock("latest")).timestamp;
const mineAt = async (t) => { await provider.send("evm_mine", [t]); };
const slot0 = async () => { const s = await sv.getSlot0(ID); return { sqrt: s[0], tick: Number(s[1]) }; };
const sqrtAtTick = (tick) => BigInt(Math.round(Math.sqrt(1.0001 ** tick) * 2 ** 96));
const tickForFdv = (fdv) => Math.round(Math.log(6_767_000_000 / fdv) / Math.log(1.0001));
const fdvAtTick = (tick) => 6_767_000_000 / 1.0001 ** tick;
const near = (a, b, bps, msg) => { const d = a > b ? a - b : b - a; assert.ok(d * 10000n <= b * BigInt(bps), `${msg}: ${a} vs ${b}`); };
const results = []; const log = (name, data = {}) => { results.push({ name, ...data }); console.log("ok", name, JSON.stringify(data, (_, v) => (typeof v === "bigint" ? v.toString() : v))); };

// swaps (each mined in its own block unless automine is off)
const pushTo = (w, tick, opts = {}) => router.connect(w).swap(KEY, [true, -parseEther("5000"), sqrtAtTick(tick)], SETTINGS, "0x", { value: parseEther("5000"), ...opts });
const sellTo = (w, sqrt, opts = {}) => router.connect(w).swap(KEY, [false, -parseEther("6000000000"), sqrt], SETTINGS, "0x", opts);
const wait = async (p) => (await p).wait();

// The quote service's price source, configured as in production. The wall-clock head check is tested separately
// (section 7) because this test moves anvil's clock ahead of real time.
const LOCAL = { maxHeadLag: Infinity };
let src = entryPriceSource(provider, D, LOCAL);
const quote = async () => src.price();
const feeOf = async (q) => conv.entryFeeFor(q.weiPerMillion);
const hold = async (seconds, step = 60) => { const t0 = await now(); for (let t = t0 + step; t <= t0 + seconds; t += step) await mineAt(t); };

const init = (await src.pm.queryFilter(src.pm.filters.Initialize(ID)))[0];
const T = (await provider.getBlock(init.blockNumber)).timestamp;
assert.equal(Number(init.args.tick), ENTRY_PRICE.openingTick);
const liq = await sv.getLiquidity(ID);
// At exactly the opening tick (= tickUpper) the founding position is not yet active, so in-range liquidity reads 0
// until the first buy; quotes are refused until then (nobody can hold MILORD before the first buy anyway).
assert.equal(liq, 0n);
await mineAt(T + 30);
await assert.rejects(src.price(), /liquidity too low/);
log("pool opened at the approved launch tick; no quote before the first buy", { tick: Number(init.args.tick), fdvEth: fdvAtTick(Number(init.args.tick)).toFixed(2) });

// ── 9. token ordering and decimals ──
await verifyPoolKey(provider, KEY, D.MILORD);
await assert.rejects(verifyPoolKey(provider, [D.MILORD, ZERO, D.lpFee, D.tickSpacing, D.HookrTaxHook]), /currency0 must be native ETH/);
await assert.rejects(verifyPoolKey(provider, [ZERO, D.USDG, 500, 10, ZERO]), /6 decimals, expected 18/);
await assert.rejects(verifyPoolKey(provider, [ZERO, D.USDG, 500, 10, ZERO], D.MILORD), /currency1 is not MILORD/);
const wrongSrc = entryPriceSource(provider, { ...D, MILORD: D.USDG }, LOCAL);
await assert.rejects(wrongSrc.price(), /decimals/);
log("9 wrong token ordering / non-18-decimal token / wrong token rejected before any price is read");

// ── first 1 ETH purchase: ~19.8% price jump, how long do conversions wait? ──
const buyEth = (w, eth) => router.connect(w).swap(KEY, [true, -eth, MIN_SQRT], SETTINGS, "0x", { value: eth });
const pctMove = (fromTick, toTick) => (1.0001 ** (fromTick - toTick) - 1) * 100;
const snap = await provider.send("evm_snapshot", []);
{ // B: nobody buys for 2 hours, so the bootstrap window has passed and the 10% deviation rule applies
  await mineAt(T + 7200);
  await provider.send("evm_setNextBlockTimestamp", [T + 7200 + 1]);
  const r = await wait(buyEth(alice, parseEther("1")));
  const tBuy = (await provider.getBlock(r.blockNumber)).timestamp;
  const jump = pctMove(ENTRY_PRICE.openingTick, (await slot0()).tick);
  const sB = entryPriceSource(provider, D, LOCAL);
  let firstRefusal = null, reopenedAt = null;
  for (let t = tBuy + 12; t <= tBuy + 1800 && reopenedAt === null; t += 12) {
    await mineAt(t);
    try { await sB.price(); reopenedAt = t; } catch (e) { assert.match(e.message, /deviates/); firstRefusal ??= e.retryAfter; }
  }
  assert.ok(reopenedAt);
  const waited = reopenedAt - tBuy;
  log("3b first 1 ETH buy two hours after open (bootstrap over): conversions wait under the 10% rule", {
    priceJumpPct: jump.toFixed(2), waitSeconds: waited, waitMinutes: (waited / 60).toFixed(1), firstEstimateSeconds: firstRefusal });
  assert.ok(jump > 19.5 && jump < 20.2, `jump ${jump}`);
  assert.ok(Math.abs(firstRefusal - waited) <= 30, `estimate ${firstRefusal} vs ${waited}`);
  await provider.send("evm_revert", [snap]);
}
src = entryPriceSource(provider, D, LOCAL);

// ── A: launch as planned, first 1 ETH buy one minute after open: bounded bootstrap, 0.002 ETH, no wait ──
await provider.send("evm_setNextBlockTimestamp", [T + 60]);
await wait(buyEth(alice, parseEther("1")));
const launchJump = pctMove(ENTRY_PRICE.openingTick, (await slot0()).tick);
{
  const r = await src.price();
  assert.equal(r.mode, "bootstrap");
}
const liqActive = await sv.getLiquidity(ID);
assert.ok(liqActive >= ENTRY_PRICE.minLiquidity);
await wait(token.connect(alice).approve(D.MilordConverter, parseEther("10000000")));
await mineAt(T + 300);
const qBoot = await quote();
assert.equal(qBoot.mode, "bootstrap");
assert.ok(qBoot.weiPerMillion >= weiPerMillionAtTick((await slot0()).tick));
assert.equal(await feeOf(qBoot), parseEther("0.002"));
const convert = async (w, priceSource) => {
  const t = await now();
  const { quote: q, signature } = await issueQuote({ signer: quoteSigner, chainId, converter: D.MilordConverter, account: w.address, priceSource, now: t, randomNonce: hexlify(randomBytes(32)) });
  const fee = await conv.entryFeeFor(q.weiPerMillion);
  const r = await wait(conv.connect(w).convert(q, signature, fee, { value: fee }));
  const ev = r.logs.map((l) => { try { return conv.interface.parseLog(l); } catch { return null; } }).find((e) => e?.name === "Converted");
  return { tokenId: ev.args.tokenId, fee, gas: r.gasUsed };
};
const c1 = await convert(alice, async () => qBoot);
log("1 launch (~10 ETH FDV): bootstrap quote, entry fee 0.002 ETH, NFT minted", { mode: qBoot.mode, weiPerMillion: formatEther(qBoot.weiPerMillion), fee: formatEther(c1.fee), convertGas: c1.gas });

// ── bootstrap ends: after 30 minutes the quote is a full-window TWAP; no gap at the hand-over ──
let gaps = 0;
for (let t = (await now()) + 60; t <= T + 1800 + 600; t += 60) { await mineAt(t); try { await quote(); } catch { gaps++; } }
const qT = await quote();
assert.equal(qT.mode, "twap");
assert.equal(gaps, 0);
log("3a first 1 ETH buy right after open: bootstrap then TWAP, conversions never unavailable", { priceJumpPct: launchJump.toFixed(2), minutesChecked: 39, unavailableMinutes: gaps });

// ── 2-4. 100 / 200 / 500 ETH FDV held for a full window ──
const fees = {};
for (const [fdv, expectFee] of [[100, "0.002"], [200, "0.0029555"], [500, "0.0073888"]]) {
  await wait(pushTo(alice, tickForFdv(fdv)));
  await hold(1860);
  const q = await quote();
  const fee = await feeOf(q);
  assert.equal(q.mode, "twap");
  near(q.weiPerMillion, (BigInt(fdv) * 10n ** 18n) / 6767n, 5, `price @${fdv}`);
  near(fee, parseEther(expectFee), 5, `fee @${fdv}`);
  fees[fdv] = fee;
  log(`${fdv === 100 ? 2 : fdv === 200 ? 3 : 4} ${fdv} ETH FDV: entry fee`, { fee: formatEther(fee), expected: expectFee });
}
const c2 = await convert(alice, quote);
assert.ok(c2.fee >= fees[500]);
log("convert at 500 ETH FDV charges the TWAP fee", { fee: formatEther(c2.fee), convertGas: c2.gas });

// ── 5. same-block manipulation ──
const base = await quote();
const s0 = await slot0();
await wait(token.connect(alice).transfer(attacker.address, parseEther("500000000"))); // attacker inventory covers LP fees
const atkMilord = await token.balanceOf(attacker.address);
await wait(token.connect(attacker).approve(D.SwapRouter, 2n ** 255n));
await provider.send("evm_setAutomine", [false]);
let nonce = await provider.getTransactionCount(attacker.address);
const tx1 = await pushTo(attacker, tickForFdv(5000), { nonce: nonce++, gasLimit: 1_000_000 }); // 10x the price
const tx2 = await sellTo(attacker, s0.sqrt, { nonce: nonce++, gasLimit: 1_000_000 }); // ...and back, same block
await mineAt((await now()) + 12);
await provider.send("evm_setAutomine", [true]);
const [r1, r2] = [await tx1.wait(), await tx2.wait()];
assert.equal(r1.blockNumber, r2.blockNumber);
assert.equal((await slot0()).tick, s0.tick);
const afterSame = await quote();
assert.equal(afterSame.weiPerMillion, base.weiPerMillion);
log("5a push 10x and restore inside one block: zero weight, quote unchanged", { block: r1.blockNumber, quoteBefore: formatEther(base.weiPerMillion), quoteAfter: formatEther(afterSame.weiPerMillion), attackerMilordGained: formatEther((await token.balanceOf(attacker.address)) - atkMilord) });

await wait(pushTo(attacker, tickForFdv(1000))); // 2x, left in place at the end of the block
await assert.rejects(quote(), /deviates/);
await wait(sellTo(attacker, s0.sqrt)); // restored one block later
const afterOne = await quote();
assert.ok(afterOne.weiPerMillion >= base.weiPerMillion);
near(afterOne.weiPerMillion, base.weiPerMillion, 100, "one-block push leaves <1%");
log("5b push 2x held to the end of a block: quote refused; after restore the effect is <1%", { quote: formatEther(afterOne.weiPerMillion) });

// fresh window at 500 ETH FDV for the sustained tests
await hold(1860);
const clean = await quote();
const cleanFee = await feeOf(clean);

// ── 6. sustained multi-block manipulation ──
await wait(sellTo(attacker, sqrtAtTick(tickForFdv(475)))); // push the price 5% DOWN to pay a lower fee
await hold(300, 12);
const down = await quote();
const spotDown = weiPerMillionAtTick((await slot0()).tick);
assert.ok(down.weiPerMillion > spotDown); // quote = TWAP, above the pushed spot
const saving = Number(cleanFee - (await feeOf(down))) / Number(cleanFee);
assert.ok(saving < 0.01, `saving ${saving}`);
log("6a price held 5% low for 5 minutes (25 blocks): quote = TWAP above spot, fee only ~k/1800 of the push lower", { cleanFee: formatEther(cleanFee), feeAfter: formatEther(await feeOf(down)), savingPct: (saving * 100).toFixed(3) });
await wait(sellTo(attacker, sqrtAtTick(tickForFdv(400)))); // 20% down, held
await hold(300, 12);
await assert.rejects(quote(), /deviates/);
log("6b price held 20% low for 5 minutes: quotes refused (spot >10% from TWAP)");
await wait(pushTo(attacker, tickForFdv(500)));
await hold(1860);

// ── 7. missing or stale observations ──
const t7 = await now();
assert.throws(() => src.oracle.price(t7 + ENTRY_PRICE.maxStaleness + 1), /stale/); // indexer has not synced
const missing = entryPriceSource(provider, { ...D, lpFee: D.lpFee + 1 }, LOCAL); // a key with no Initialize event
await assert.rejects(missing.price(), /insufficient price history/);
await assert.rejects(issueQuote({ signer: quoteSigner, chainId, converter: D.MilordConverter, account: alice.address, priceSource: async () => src.oracle.price(t7 + 1000), now: t7 + 1000, randomNonce: hexlify(randomBytes(32)) }), /stale/);
const behind = entryPriceSource(provider, D, { maxHeadLag: 120, clock: () => t7 + 121 });
await assert.rejects(behind.price(), /chain head is behind/);
const current = entryPriceSource(provider, D, { maxHeadLag: 120, clock: () => t7 + 5 });
assert.ok(await current.price());
log("7 stale or missing observations, or an RPC head behind the wall clock: no quote issued (no fallback)");

// ── 8. inactive pool, low liquidity, out-of-range price ──
// No swaps for 6 hours; blocks keep coming (other activity on the chain) and the indexer syncs every 10 minutes.
const lastSwap = (await quote()).lastSwapAt;
let idleOk = 0;
for (let i = 1; i <= 36; i++) {
  await mineAt((await now()) + 600);
  const q = await quote();
  assert.equal(q.lastSwapAt, lastSwap);
  assert.equal(q.weiPerMillion, weiPerMillionAtTick((await slot0()).tick));
  idleOk++;
}
const idle = await quote();
const swapAge = (await now()) - idle.lastSwapAt;
assert.ok(swapAge >= 6 * 3600);
const tIdle = await now();
assert.throws(() => src.oracle.price(tIdle + ENTRY_PRICE.maxStaleness + 1), /stale/); // the same pool once the indexer stops
log("8a quiet pool: 6 h without swaps, indexer syncing: quotes keep working at the unchanged price; only indexer lag refuses", {
  quotesOk: idleOk, lastSwapAgeHours: (swapAge / 3600).toFixed(1), indexerLagSeconds: (await now()) - idle.observedAt, quote: formatEther(idle.weiPerMillion) });
// ── website SDK against a compromised quote signer (and a compromised quote service) ──
{
  const sdkSrc = fs.readFileSync(path.join(root, "frontend-integration/milord-sdk.mjs"), "utf8")
    .replace(/from "https:\/\/cdn\.jsdelivr\.net\/npm\/ethers@[^"]+"/, 'from "ethers"');
  const tmp = path.join(root, "offchain/.sdk-under-test.mjs");
  fs.writeFileSync(tmp, sdkSrc);
  const sdk = await import(tmp);
  fs.unlinkSync(tmp);
  const m = { account: alice.address, conv: conv.connect(alice), token: token.connect(alice), key: KEY, D,
    stateView: new Contract(D.StateView, ["function getSlot0(bytes32) view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)"], provider) };
  const serve = (wpm) => async () => {
    const t = await now();
    const { quote: q, signature } = await issueQuote({ signer: quoteSigner, chainId, converter: D.MilordConverter, account: alice.address,
      priceSource: async () => ({ weiPerMillion: wpm, observedAt: t }), now: t, randomNonce: hexlify(randomBytes(32)) });
    return { ok: true, json: async () => ({ quote: Object.fromEntries(Object.entries(q).map(([k, v]) => [k, v.toString()])), signature }) };
  };
  const fair = (await quote()).weiPerMillion;
  const ceiling = await sdk.feeCeilingWei(m);
  globalThis.fetch = serve(fair * 100n); // signer + service both compromised: 100x price
  const ethBefore = await provider.getBalance(alice.address);
  const nftBefore = await conv.backedCount();
  await assert.rejects(sdk.wake(m, "http://quote"), /above the .* ETH this page allows .*nothing was sent/);
  assert.equal(await provider.getBalance(alice.address), ethBefore);
  assert.equal(await conv.backedCount(), nftBefore);
  globalThis.fetch = serve(fair);
  await sdk.wake(m, "http://quote");
  assert.equal(await conv.backedCount(), nftBefore + 1n);
  globalThis.fetch = async () => ({ ok: false, json: async () => ({ error: "spot deviates from TWAP: entry waits", retryAfter: 840 }) });
  const pv = await sdk.previewFee(m, "http://quote");
  assert.match(pv.wait, /about 14 min/);
  log("website SDK: a 100x quote from a compromised signer is refused before the wallet is asked (nothing sent); fair quote converts; the wait is explained", {
    ceilingFee: formatEther(ceiling), fairFee: formatEther(await conv.entryFeeFor(fair)), refusedFee: formatEther(await conv.entryFeeFor(fair * 100n)), waitText: pv.wait });
}

const strict = entryPriceSource(provider, D, { minLiquidity: liqActive + 1n, ...LOCAL });
await assert.rejects(strict.price(), /liquidity too low/);
log("8b in-range liquidity below the configured minimum: quotes refused");
await wait(pushTo(attacker, ENTRY_PRICE.tickLower - 6000)); // buy through the top of the founding range
const outSlot = await slot0();
assert.ok(outSlot.tick < ENTRY_PRICE.tickLower);
assert.equal(await sv.getLiquidity(ID), 0n);
await assert.rejects(quote(), /outside the liquidity range|liquidity too low/);
log("8c price pushed past the founding range (no liquidity): quotes refused", { tick: outSlot.tick });

// ── 10. redemption does not depend on pricing ──
await assert.rejects(quote(), /entry waits/); // pricing is refusing right now
const before = await token.balanceOf(alice.address);
const rr = await wait(conv.connect(alice).redeem(c1.tokenId));
assert.ok((await token.balanceOf(alice.address)) > before);
log("10 redeem works while the price service refuses every quote", { redeemGas: rr.gasUsed });

fs.writeFileSync(path.join(root, "sim/entry-price-results.json"), JSON.stringify(results, (_, v) => (typeof v === "bigint" ? v.toString() : v), 2));
console.log(`entry-price integration: ${results.length} checks passed`);
