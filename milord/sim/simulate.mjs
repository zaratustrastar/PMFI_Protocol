// End-to-end MILORD simulation on anvil against the real contracts and the official Uniswap v4 build.
//
//   ./sim/run-local.sh        (fresh anvil -> Permit2 -> DeployLocal -> this script)
//
// What is real here: every MILORD contract, pArbitrage V3, ParbFundingAdapter, ShareDistributor, Uniswap v4
// PoolManager / PositionManager / StateView, V4SwapAdapter, HookrLockFeeCollector, ArbitrageFundingRouter with
// UniV3SwapVenue and the 30-minute UniV3TwapReference on the official Uniswap V3 factory build, the EIP-712 quote
// and report signing code, the TWAP reference price, and the packaged keeper and epoch-publisher jobs
// (offchain/services/lib/jobs.mjs) that run the flow. The scenario is reported in weeks; pARB is distributed in
// DAILY UTC epochs (each weekly close publishes one cumulative root through the week's last day).
// What is mocked: Hookr's Tax + Conversion hook (MockHookrTaxHook reproduces its economics), Hookr's launcher and
// HookrFamilyLock (MockHookrFamilyLock, same lock/collect surface), USDG (MockUSDG) and the WETH/USDG pool's
// depth, arbitrage P&L (minted or burned on the servicer wallet).
//
// Every number marked SCENARIO or ILLUSTRATIVE is a hypothetical input chosen to exercise the mechanics.
// None of it is a forecast, a proposed production value, measured profit or real share-price data.
import fs from "node:fs";
import path from "node:path";
import { JsonRpcProvider, Wallet, Contract, AbiCoder, parseEther, formatEther, formatUnits, keccak256, toUtf8Bytes, hexlify, randomBytes, id } from "ethers";
import { ownershipIntervals, eligibility, allocate, accumulate, buildTree, proofFor, DAY, WEEK } from "../offchain/lib/eligibility.mjs";
import { issueQuote } from "../offchain/lib/quote.mjs";
import { signReport } from "../offchain/lib/report.mjs";
import { TwapOracle, weiPerMillionAtTick } from "../offchain/lib/twap.mjs";
import { ENTRY_PRICE } from "../offchain/lib/entry-price.mjs";
import { keeperTick, publishEpoch, checkRoot, readInputs, computeEpochs, closedEpoch, rewardsStatus } from "../offchain/services/lib/jobs.mjs";

// ───────────── SCENARIO inputs (hypothetical) ─────────────
const SCENARIO = {
  firstBuyEth: parseEther("1"),
  launchTraderBuysEth: ["0.5", "1", "0.5", "1", "0.5"].map(parseEther), // first hour after the market opens
  week2WhaleBuyEth: parseEther("30"),       // moves the price past ~135 ETH FDV so the 10% entry fee exceeds the 0.002 ETH minimum
  royaltySaleEth: parseEther("0.5"),        // one secondary sale through a royalty-honouring marketplace
  pnlBpsWeek2: 200,                         // +2% on deployed capital
  pnlBpsWeek3: -300,                        // -3% on deployed capital
};
const LAUNCH = "final allocation: 100% of supply (6,767,000,000 MILORD) in one MILORD-only position, 0 ETH, ~10 ETH opening FDV, range to ~1,000x; 1.5%/1.5% tax; 0.3% LP fee; 20% Hookr commission (to verify on Hookr); position locked forever, LP fees to buyback-and-burn";

const RPC = process.env.RPC_URL ?? "http://127.0.0.1:8545";
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const D = JSON.parse(fs.readFileSync(path.join(root, "sim/deployments.local.json"), "utf8"));
const abi = (file, name = file) => JSON.parse(fs.readFileSync(path.join(root, `out/${file}.sol/${name}.json`), "utf8")).abi;
const v4abi = (p) => JSON.parse(fs.readFileSync(path.join(root, "node_modules/@uniswap", p), "utf8")).abi;

const provider = new JsonRpcProvider(RPC, undefined, { pollingInterval: 20, cacheTimeout: -1 });
const KEYS = [
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
  "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
  "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
  "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
  "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
  "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
]; // anvil public test keys
const [owner, team, arb, quoteSigner, reportSigner, operator] = KEYS.map((k) => new Wallet(k, provider));

const C = (addr, a, w = owner) => new Contract(addr, a, w);
const token = C(D.MILORD, abi("MILORDToken"));
const nft = C(D.Milords, abi("MilordsNFT"));
const conv = C(D.MilordConverter, abi("MilordConverter"));
const entrySplit = C(D.EntryFeeSplitter, abi("RevenueSplitter"));
const royaltySplit = C(D.RoyaltySplitter, abi("RevenueSplitter"));
const taxSplit = C(D.TradingTaxSplitter, abi("RevenueSplitter"));
const burner = C(D.BuybackBurner, abi("BuybackBurner"));
const registry = C(D.StrategyRegistry, abi("StrategyRegistry"));
const usdg = C(D.USDG, abi("MockUSDG"));
const vault = C(D.pARBVault, abi("PMFIArbVaultV3"));
const dist = C(D.ShareDistributor, abi("ShareDistributor"));
const adapter = C(D.ParbFundingAdapter, abi("ParbFundingAdapter"));
const hook = C(D.HookrTaxHook, abi("MockHookrTaxHook"));
const route = C(D.V4SwapAdapter, abi("V4SwapAdapter"));
const familyLock = C(D.HookrFamilyLock, abi("MockHookrFamilyLock"));
const collector = C(D.LockFeeCollector, abi("HookrLockFeeCollector"), operator);
const arbRouter = C(D.ArbitrageFundingRouter, abi("ArbitrageFundingRouter"), operator);
const twapRef = C(D.TwapReference, abi("UniV3TwapReference"));
const router = C(D.SwapRouter, abi("PoolSwapTest"));
const stateView = C(D.StateView, v4abi("v4-periphery/foundry-out/StateView.sol/StateView.json"));
const chainId = Number((await provider.getNetwork()).chainId);
const servicer = arb; // local deploy uses the arbitrage Safe as the vault's servicer wallet
const ZERO = "0x0000000000000000000000000000000000000000";
const MIN_SQRT = 4295128739n + 1n;
const MAX_SQRT = 1461446703485210103287273052203988822378723970342n - 1n;
const KEY = [ZERO, D.MILORD, D.lpFee, D.tickSpacing, D.HookrTaxHook];
const POOL_ID = keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], KEY));
const SETTINGS = [false, false];

// ───────────── chain helpers + TWAP observer ─────────────
const twap = new TwapOracle({
  window: ENTRY_PRICE.window, maxDeviationBps: ENTRY_PRICE.maxDeviationBps, maxStaleness: 3 * DAY,
  tickLower: ENTRY_PRICE.tickLower, tickUpper: ENTRY_PRICE.tickUpper, minLiquidity: ENTRY_PRICE.minLiquidity,
  bootstrapOpeningTick: ENTRY_PRICE.openingTick,
});
// maxStaleness is relaxed locally only because the simulation jumps days between blocks; production: 120 s.
const now = async () => (await provider.getBlock("latest")).timestamp;
const spotTick = async () => Number((await stateView.getSlot0(POOL_ID)).tick);
const observe = async () => twap.record(await now(), await spotTick(), await stateView.getLiquidity(POOL_ID));
const setTime = async (t) => { t = Math.max(Math.floor(t), (await now()) + 1); await provider.send("evm_setNextBlockTimestamp", [t]); await provider.send("evm_mine", []); await observe(); };
const send = async (p) => { const r = await (await p).wait(); await observe(); return r; };
const fmtE = (v) => Number(formatEther(v));
const fmtU = (v) => Number(formatUnits(v, 6));
const timeline = [];
const step = async (n, text, data = {}) => { const e = { step: n, t: await now(), text, ...data }; timeline.push(e); console.log(`[${n}] ${text}`); };
const priceSeries = [];
const samplePrice = async (label) => priceSeries.push({ t: await now(), label, weiPerMillion: fmtE(weiPerMillionAtTick(await spotTick())) });

// ───────────── trading through the Hookr-style pool ─────────────
let ethVolume = 0n;
async function buyExactIn(w, ethIn) {
  const before = await token.balanceOf(w.address);
  await send(router.connect(w).swap(KEY, [true, -ethIn, MIN_SQRT], SETTINGS, "0x", { value: ethIn }));
  ethVolume += ethIn;
  return (await token.balanceOf(w.address)) - before;
}
async function buyExactOut(w, milordOut) {
  const ethBefore = await provider.getBalance(w.address);
  const r = await send(router.connect(w).swap(KEY, [true, milordOut, MIN_SQRT], SETTINGS, "0x", { value: parseEther("50") }));
  const spent = ethBefore - (await provider.getBalance(w.address)) - r.gasUsed * r.gasPrice;
  ethVolume += spent;
  return spent;
}
async function sell(w, milordIn) {
  await send(token.connect(w).approve(D.SwapRouter, milordIn));
  const before = await provider.getBalance(w.address);
  const r = await send(router.connect(w).swap(KEY, [false, -milordIn, MAX_SQRT], SETTINGS, "0x"));
  const got = (await provider.getBalance(w.address)) - before + r.gasUsed * r.gasPrice;
  ethVolume += got;
  return got;
}

// ───────────── conversion with a TWAP-signed quote ─────────────
let quoteRefusals = [];
async function convertFor(w, { retries = 6 } = {}) {
  await send(token.connect(w).approve(D.MilordConverter, parseEther("1000000")));
  for (let attempt = 0; ; attempt++) {
    const t = await now();
    try {
      const { quote, signature } = await issueQuote({
        signer: quoteSigner, chainId, converter: D.MilordConverter, account: w.address,
        priceSource: async () => twap.price(t), now: t, maxPriceAge: 3 * DAY, randomNonce: hexlify(randomBytes(32)),
      });
      const fee = await conv.entryFeeFor(quote.weiPerMillion);
      const r = await send(conv.connect(w).convert(quote, signature, fee, { value: fee }));
      const ev = r.logs.map((l) => { try { return conv.interface.parseLog(l); } catch { return null; } }).find((e) => e?.name === "Converted");
      return { id: Number(ev.args.tokenId), fee, weiPerMillion: quote.weiPerMillion, minimumApplied: fee === parseEther("0.002") };
    } catch (e) {
      const msg = String(e.message ?? e);
      if (!/entry waits/.test(msg) || attempt >= retries) throw e;
      quoteRefusals.push({ t, reason: msg.replace(/^Error: /, "") });
      await setTime(t + 600); // wait for the TWAP to catch up; no fallback price
    }
  }
}

// ───────────── Transfer history (finalized-only in production) ─────────────
const blockTs = new Map();
async function transfers() {
  const logs = await nft.queryFilter(nft.filters.Transfer(), 0, "latest");
  const out = [];
  for (const l of logs) {
    if (!blockTs.has(l.blockNumber)) blockTs.set(l.blockNumber, (await provider.getBlock(l.blockNumber)).timestamp);
    out.push({ tokenId: Number(l.args.tokenId), from: l.args.from, to: l.args.to, timestamp: blockTs.get(l.blockNumber), blockNumber: l.blockNumber, logIndex: l.index });
  }
  return out;
}

// ───────────── vault helpers ─────────────
let reportNonce = Number(await vault.lastReportNonce());
const ppsSeries = [];
async function report(label) {
  const t = await now();
  const reportedAssets = (await usdg.balanceOf(D.pARBVault)) + (await usdg.balanceOf(servicer.address));
  const data = { reportedAssets, timestamp: t, deadline: t + 600, nonce: ++reportNonce };
  const sig = await signReport(reportSigner, { ...data, vault: D.pARBVault, chainId });
  await send(vault.report(data, sig, 0, 0));
  ppsSeries.push({ t, label, pps: fmtU(await vault.officialPPS()), navUsdg: fmtU(reportedAssets), supply: fmtE(await vault.totalSupply()) });
  return reportedAssets;
}
async function applyPnl(bps) {
  const deployed = await usdg.balanceOf(servicer.address);
  const delta = (deployed * BigInt(Math.abs(bps))) / 10000n;
  if (bps >= 0) await send(usdg.mint(servicer.address, delta)); // SCENARIO P&L, not a trading result
  else await send(usdg.connect(servicer).transfer("0x000000000000000000000000000000000000dEaD", delta));
  return bps >= 0 ? delta : -delta;
}

// ───────────── weekly close ─────────────
let cumulative = new Map();
let prevTotal = 0n;
let epoch = 0;
let lastTree = null;
const weeks = [];
const batches = [];
const sumAlloc = async (who) => (await entrySplit.totalAllocated(ZERO, who)) + (await royaltySplit.totalAllocated(ZERO, who)) + (await taxSplit.totalAllocated(ZERO, who));
let lastEpoch = null;
const rootChecks = [];
const dailyEpochs = [];
let pendingReplaceChecked = false, pendingGuard = null;

const GRACE = 6 * 3600;
const keeperContracts = {
  hookrSettle: () => hook.connect(operator).collect(),
  splitters: [entrySplit, royaltySplit, taxSplit].map((x) => x.connect(operator)),
  collector, burner: burner.connect(operator), router: arbRouter, adapter: adapter.connect(operator), vault, distributor: dist.connect(operator),
};
const keeperOpts = {
  now,
  buyback: {
    minBudgetWei: parseEther("0.01"),
    // min output from the off-chain 30-min MILORD TWAP: at most 10% below it (tax + LP fee + impact)
    quoteMinOut: async (budget) => { const ref = twap.price(await now()).weiPerMillion; return ((budget * 10n ** 24n) / ref) * 90n / 100n; },
  },
};
const keeperLog = [];
async function keeper(label) {
  const r = await keeperTick(keeperContracts, keeperOpts);
  await observe();
  keeperLog.push({ label, t: await now(), steps: r.results.map((x) => `${x.step}:${x.status}`) });
  if (r.failed) throw new Error(`keeper failed: ${JSON.stringify(r.results.filter((x) => x.status === "failed"))}`);
  return r;
}
const routerPayee = D.ArbitrageFundingRouter;

async function closeWeek(label, start, end, pnlBps, notes) {
  await setTime(end);
  const burnedBefore = await burner.totalBurnedFromBuybacks();
  const swappedBefore = await arbRouter.totalEthSwapped();
  const fundedBefore = await arbRouter.totalAssetFunded();
  const batchesBefore = Number(await adapter.batchCount());
  // keeper pass 1 (offchain/services keeperTick): settle the Hookr tax queue -> split revenue (tax thirds, entry
  // 70/30, royalty 50/50) -> collect LP fees of the locked position into the burner -> bounded buyback-and-burn ->
  // router: arbitrage ETH -> USDG at most 1% below the 30-min V3 TWAP -> vault deposit request
  await keeper(`${label} pass 1`);
  const burnedNow = (await burner.totalBurnedFromBuybacks()) - burnedBefore;
  const ethToSwap = (await arbRouter.totalEthSwapped()) - swappedBefore;
  const usdgIn = (await arbRouter.totalAssetFunded()) - fundedBefore;
  const arbTotal = await sumAlloc(routerPayee);
  // trading result on capital already deployed (SCENARIO), then the reporter's signed report
  const pnl = pnlBps ? await applyPnl(pnlBps) : 0n;
  const reported = await report(label);
  // keeper pass 2: settle the processed deposit (pARB minted to the distributor at the reported PPS)
  await keeper(`${label} pass 2`);
  let shares = 0n;
  for (let i = batchesBefore; i < Number(await adapter.batchCount()); i++) {
    const b = await adapter.batch(i);
    const req = await vault.depositRequests(b.requestId);
    shares += b.sharesMinted;
    batches.push({ label, usdg: b.usdcAmount, shares: b.sharesMinted, processedPPS: req.processedPPS });
  }
  await provider.send("evm_increaseTime", [61]);
  await send(vault.tend());
  const pps = await vault.officialPPS();

  // epoch publisher after the settlement grace: finalized transfers + settled batches -> deterministic root
  await setTime(end + GRACE + 60);
  const inputs = await readInputs({ nft, adapter, provider, fromBlock: Number(D.startBlock ?? 0), toBlock: await provider.getBlockNumber() });
  const pub = await publishEpoch({ distributor: dist.connect(operator), inputs, genesis: T0, now: await now(), settlementGrace: GRACE, excluded: [D.MilordConverter], save: (e) => { lastEpoch = e; } });
  await observe();
  const chk = await checkRoot({ distributor: dist, parb: vault, inputs, genesis: T0, settlementGrace: GRACE, excluded: [D.MilordConverter] });
  rootChecks.push(...chk.map((x) => ({ label, ...x })));
  // the week's daily epochs, summed for the weekly report (allocation itself is per UTC day)
  let rootHex = null, dust = 0n;
  const allocations = new Map(), weights = new Map();
  const eps = computeEpochs({ ...inputs, genesis: T0, upToEpoch: closedEpoch(T0, await now(), GRACE), settlementGrace: GRACE, excluded: [D.MilordConverter], trees: "none" });
  const inWeek = eps.filter((e) => e.start >= start && e.end <= end);
  for (const e of inWeek) {
    for (const [a, v] of e.allocations) allocations.set(a, (allocations.get(a) ?? 0n) + v);
    for (const [a, v] of e.weights) weights.set(a, (weights.get(a) ?? 0n) + v);
  }
  dust = inWeek.at(-1)?.dust ?? 0n;
  if (eps.length) cumulative = eps.at(-1).cumulative;
  dailyEpochs.push(...inWeek.map((e) => ({ day: e.epoch, start: e.start, amount: e.amount, allocated: e.allocatedNow, eligible: e.eligible, weights: e.weights })));
  if (pub.action === "proposed" && !pendingReplaceChecked) {
    // the real ShareDistributor refuses a second root while this one is in review (daily proposals must not
    // restart the review clock), and the publisher reports "already pending" instead of re-proposing
    pendingReplaceChecked = true;
    let reason = "";
    try { await dist.connect(operator).proposeRoot.staticCall(pub.root, pub.total, pub.epoch + 1, pub.dataHash); } catch (e) { reason = String(e.shortMessage ?? e.message); }
    const again = await publishEpoch({ distributor: dist.connect(operator), inputs, genesis: T0, now: await now(), settlementGrace: GRACE, excluded: [D.MilordConverter] });
    pendingGuard = { contractRefused: /PendingRootNotActivated/.test(reason), reason, publisher: again.reason };
  }
  if (pub.action === "proposed") {
    await provider.send("evm_increaseTime", [3601]);
    await keeper(`${label} pass 3`); // activates the root after the delay
    prevTotal = pub.total;
    lastTree = lastEpoch.tree;
    rootHex = pub.root;
    epoch = pub.epoch;
  }
  // some holders claim (others wait; cumulative leaves mean they lose nothing)
  if (lastTree) {
    let i = 0;
    for (const [acct] of cumulative) {
      if (i++ % 2) continue;
      const p = proofFor(lastTree, acct);
      if (p && p.cumulative > (await dist.claimed(acct))) await send(dist.claim(acct, p.cumulative, p.proof));
    }
  }
  await samplePrice(`${label} close`);

  const supply = await token.totalSupply();
  const escrow = await token.balanceOf(D.MilordConverter);
  const inPool = await token.balanceOf(D.PoolManager);
  const w = {
    label, start, end, notes,
    milordSupply: fmtE(supply), milordEscrowed: fmtE(escrow), milordInPool: fmtE(inPool),
    milordCirculating: fmtE(supply - escrow - inPool), milordBurnedTotal: fmtE(parseEther("6767000000") - supply),
    burnedOnExitTotal: fmtE(await conv.totalMilordBurnedOnExit()), burnedByBuybackTotal: fmtE(await burner.totalBurnedFromBuybacks()), burnedByBuybackThisWeek: fmtE(burnedNow),
    activeNFTs: Number(await nft.activeSupply()), inventoryNFTs: Number(await conv.inventorySize()), totalMinted: Number(await nft.totalMinted()),
    conversionsTotal: Number(await conv.totalConversions()), redemptionsTotal: Number(await conv.totalRedemptions()),
    ethTradingVolumeTotal: fmtE(ethVolume),
    hookrCommissionTotal: fmtE(await hook.totalPlatformPaid()), projectTaxTotal: fmtE(await hook.totalProjectPaid()),
    ethEntryFeesTotal: fmtE(await conv.totalEntryFeesWei()), royaltiesTotal: fmtE(await royaltySplit.totalDistributed(ZERO)),
    teamRevenueEthTotal: fmtE(await sumAlloc(D.teamSafe)), lpFeesEthTotal: fmtE(await collector.totalEthForwarded()), lpFeesMilordTotal: fmtE(await collector.totalMilordBurned()),
    arbitrageFundingEthTotal: fmtE(arbTotal), burnBudgetEthTotal: fmtE(await burner.totalEthReceived()),
    ethSwappedThisWeek: fmtE(ethToSwap), routerHeldEth: fmtE(await provider.getBalance(routerPayee)), usdgDepositedThisWeek: fmtU(usdgIn), pnlUsdgThisWeek: fmtU(pnl),
    parbMintedThisWeek: fmtE(shares), parbMintedTotal: fmtE(await adapter.totalSharesMinted()),
    ppsUsdg: fmtU(pps), navUsdg: fmtU(reported),
    parbAllocatedThisWeek: fmtE([...allocations.values()].reduce((a, b) => a + b, 0n)), dustCarried: fmtE(dust),
    eligibleWallets: weights.size, rootEpoch: pub.epoch ?? null, lastDay: (end - T0) / DAY, epochAction: pub.action, epochReason: pub.reason ?? null, cumulativeAllocated: fmtE(prevTotal), claimedTotal: fmtE(await dist.totalClaimed()),
    unclaimed: fmtE(prevTotal - (await dist.totalClaimed())),
    weiPerMillionSpot: fmtE(weiPerMillionAtTick(await spotTick())),
    merkleRoot: rootHex, tracked: {},
  };
  for (const [name, wal] of Object.entries(TRACK)) {
    const a = wal.toLowerCase();
    w.tracked[name] = { weightSeconds: Number(weights.get(a) ?? 0n), allocatedThisWeek: fmtE(allocations.get(a) ?? 0n), cumulative: fmtE(cumulative.get(a) ?? 0n), pARBBalance: fmtE(await vault.balanceOf(wal)), nftsHeld: Number(await nft.balanceOf(wal)) };
  }
  weeks.push(w);
  console.log(`${label}: supply=${w.milordSupply.toFixed(0)} active=${w.activeNFTs} vol=${w.ethTradingVolumeTotal.toFixed(2)}ETH hookr=${w.hookrCommissionTotal.toFixed(4)} usdgIn=${w.usdgDepositedThisWeek} pARB+${w.parbMintedThisWeek} PPS=${w.ppsUsdg}`);
  return w;
}

// ───────────── actors ─────────────
const holders = Array.from({ length: 40 }, () => Wallet.createRandom().connect(provider));
const newcomers = Array.from({ length: 16 }, () => Wallet.createRandom().connect(provider));
const traders = Array.from({ length: 6 }, () => Wallet.createRandom().connect(provider));
const ALICE = holders[0], CAROL = holders[2], BUYER = newcomers[15];
for (const w of [...holders, ...newcomers, ...traders]) await provider.send("anvil_setBalance", [w.address, "0x56BC75E2D63100000"]); // 100 ETH
const TRACK = { seller_alice: ALICE.address, buyer: BUYER.address, redeemer_carol: CAROL.address };

// ───────────── scenario ─────────────
const t0raw = await now();
const T0 = t0raw - ((t0raw - 4 * DAY) % WEEK) + WEEK; // next Monday 00:00 UTC
await setTime(T0 - 600);
const start = {
  milordSupply: fmtE(await token.totalSupply()), activeNFTs: Number(await nft.activeSupply()), holders: 0,
  milordInPool: fmtE(await token.balanceOf(D.PoolManager)), milordOutsideLiquidity: fmtE(await token.balanceOf(D.owner)),
  poolEth: fmtE(await provider.getBalance(D.PoolManager)), vaultShares: fmtE(await vault.totalSupply()),
  openingWeiPerMillion: fmtE(weiPerMillionAtTick(await spotTick())), launch: LAUNCH, launchDustBurnedWei: String(D.launchDustBurned),
};
const outsideAtOpen = {};
for (const [n, a] of Object.entries({ deployer: D.owner, team: team.address, arbitrage: arb.address, operator: operator.address })) outsideAtOpen[n] = await token.balanceOf(a);
await step(1, "deployment: 100% of supply into the founding position, rounding remainder burned", { milordInLiquidity: start.milordInPool, milordOutside: start.milordOutsideLiquidity, remainderBurnedWei: start.launchDustBurnedWei });
await step(2, "Hookr-style MILORD/ETH market open (empty of ETH)", { tick: await spotTick() });
await step(3, "MILORD-only position minted into the (mock) Hookr family and locked forever; LP fee beneficiary = HookrLockFeeCollector", { positionId: D.launchPositionId });
await samplePrice("open");

await step("3b", "no wallet holds MILORD outside the pool, so the first trade can only be a buy", { outside: Object.fromEntries(Object.entries(outsideAtOpen).map(([k, v]) => [k, fmtE(v)])) });

await setTime(T0 + 60);
const firstOut = await buyExactIn(traders[0], SCENARIO.firstBuyEth);
await step(4, "first buy: 1 ETH -> MILORD", { milordOut: fmtE(firstOut) });
await samplePrice("first buy");
await setTime(T0 + 300);
const launchQuote = twap.price(await now());
const launchFee = await conv.entryFeeFor(launchQuote.weiPerMillion);
await step("4b", "entry quote 5 minutes after open: bounded bootstrap (highest price since open), fee = 0.002 ETH minimum", {
  mode: launchQuote.mode, weiPerMillion: fmtE(launchQuote.weiPerMillion), fee: fmtE(launchFee),
});
const launchBootstrapOk = launchQuote.mode === "bootstrap" && launchFee === parseEther("0.002")
  && launchQuote.weiPerMillion >= weiPerMillionAtTick(await spotTick()); // never below the current price
for (let i = 0; i < SCENARIO.launchTraderBuysEth.length; i++) {
  await setTime(T0 + 600 + i * 600);
  await buyExactIn(traders[1 + (i % 4)], SCENARIO.launchTraderBuysEth[i]);
}
await samplePrice("launch buys");
await step(5, "additional buys in the first hour", { buys: SCENARIO.launchTraderBuysEth.length });

// 10-13. 40 holders buy MILORD in the pool and wake 100 Milords
const ids = [];
const laterFees = [];
const perHolder = holders.map((_, i) => Math.floor(100 / 40) + (i < 100 % 40 ? 1 : 0));
let t = T0 + 2 * 3600;
for (let i = 0; i < holders.length; i++) {
  await setTime(t);
  await buyExactOut(holders[i], parseEther("1000000") * BigInt(perHolder[i]));
  t += 900;
}
await step(10, "40 users bought their MILORD in the pool (2-3M each)");
for (let i = 0; i < 100; i++) {
  await setTime(t + i * 1200);
  const h = holders[i % 40];
  ids.push({ ...(await convertFor(h)), holder: h });
}
await step(11, "100 conversions with TWAP-signed quotes", { feesEth: fmtE(ids.reduce((a, x) => a + x.fee, 0n)), minimumApplied: ids.filter((x) => x.minimumApplied).length });
await step(12, "100 Milords issued", { active: Number(await nft.activeSupply()) });
await samplePrice("after conversions");
await setTime(T0 + 2 * DAY);
for (const tr of traders.slice(1, 3)) await sell(tr, (await token.balanceOf(tr.address)) / 2n);
await step(5, "two traders sold half their MILORD");
await setTime(T0 + 4 * DAY);
{ // 18. sale through a royalty-honouring marketplace, and one private transfer that pays none
  const s = ids.find((x) => x.holder === holders[5]);
  await send(nft.connect(holders[5]).transferFrom(holders[5].address, newcomers[0].address, s.id));
  const [recv, amt] = await nft.royaltyInfo(s.id, SCENARIO.royaltySaleEth);
  await send(owner.sendTransaction({ to: recv, value: amt }));
  const s2 = ids.find((x) => x.holder === holders[6]);
  await send(nft.connect(holders[6]).transferFrom(holders[6].address, newcomers[1].address, s2.id));
  await step(18, "secondary sale paying 6.7% royalty, and a private transfer paying none", { royaltyEth: fmtE(amt) });
}
await closeWeek("week 1", T0, T0 + WEEK, 0, "launch, first-hour buys, 100 Milords, one royalty-paying sale, one without");
await step(13, "entry fees split 70/30; tax split after commission; buyback burned; ETH -> USDG; deposit; report; pARB minted; root published; claims");

// Week 2
const W2 = T0 + WEEK;
await setTime(W2 + 1.5 * DAY);
const carolId = ids.find((x) => x.holder === CAROL).id;
const carolEarned = cumulative.get(CAROL.address.toLowerCase()) ?? 0n;
const supplyBeforeRedeem = await token.totalSupply();
const carolBal = await token.balanceOf(CAROL.address);
await send(conv.connect(CAROL).redeem(carolId));
const redeemReturned = (await token.balanceOf(CAROL.address)) - carolBal;
const redeemBurned = supplyBeforeRedeem - (await token.totalSupply());
await step(20, "redemption: 900,000 MILORD returned, 100,000 burned", { returned: fmtE(redeemReturned), burned: fmtE(redeemBurned) });
await setTime(W2 + 2 * DAY);
await buyExactIn(traders[4], SCENARIO.week2WhaleBuyEth);
await samplePrice("week 2 large buy");
for (let i = 0; i < 10; i++) {
  const w = newcomers[2 + i];
  await setTime(W2 + 2 * DAY + 3600 + i * 1800);
  await buyExactOut(w, parseEther("1000000"));
  laterFees.push(await convertFor(w));
}
await setTime(W2 + 3.5 * DAY);
const aliceIds = ids.filter((x) => x.holder === ALICE).map((x) => x.id);
await send(nft.connect(ALICE).transferFrom(ALICE.address, BUYER.address, aliceIds[0]));
await step(18, "Alice sells one Milord to a new buyer on day 3.5 (private sale, no royalty)");
await setTime(W2 + 5 * DAY);
await sell(traders[4], (await token.balanceOf(traders[4].address)) / 3n);
await closeWeek("week 2", W2, W2 + WEEK, SCENARIO.pnlBpsWeek2, "redemption day 1.5, large buy, 10 conversions (10% fee path), sale day 3.5, +2% SCENARIO P&L");
await step(21, "positive-return report", { pps: weeks[1].ppsUsdg });

// Week 3
const W3 = W2 + WEEK;
await setTime(W3 + DAY);
const nftAddrBefore = D.Milords;
const stratBefore = Number(await registry.strategyCount());
await send(registry.addStrategy("Future strategy (placeholder, inactive)", chainId, ZERO, ZERO, false));
const sid2 = Number(await registry.strategyCount());
await step(25, "second strategy registered, inactive; collection and token unchanged", { strategyId: sid2 });
const selector = holders[7];
const selId = ids.find((x) => x.holder === selector).id;
await send(registry.connect(selector).select(selId, 2, 5));
const pref = await registry.currentPreference(selId);
for (let i = 0; i < 5; i++) {
  const w = newcomers[12 + (i % 3)];
  await setTime(W3 + 2 * DAY + i * 3600);
  await buyExactOut(w, parseEther("1000000"));
  laterFees.push(await convertFor(w));
}
await setTime(W3 + 4 * DAY);
for (const tr of traders.slice(0, 2)) await sell(tr, (await token.balanceOf(tr.address)) / 2n);
await closeWeek("week 3", W3, W3 + WEEK, SCENARIO.pnlBpsWeek3, "-3% SCENARIO P&L, then a new fee deposit priced at the lower PPS; 5 conversions; inactive strategy 2");
await step(22, "negative-return report", { pps: weeks[2].ppsUsdg });
await step(23, "fee-funded deposit after the PPS change", { usdg: weeks[2].usdgDepositedThisWeek });
await step(24, "shares minted at the new PPS", { shares: weeks[2].parbMintedThisWeek, pps: weeks[2].ppsUsdg });

// Week 4 (operations): a pARB holder redeems, the arbitrage engine returns liquidity to the vault, capital moves
// between the vault and the servicer do not move the PPS, and restarted services repeat nothing.
const W4 = W3 + WEEK;
await setTime(W4 + DAY);
// every NFT holder claims their pARB and redeems half of it: together more than the vault's idle buffer, so the queue must wait
// for the engine to return capital (FIFO; a request the buffer cannot cover stops processing, it is never paid short)
const redeemers = [];
const ppsBeforeOps = await vault.officialPPS();
for (const w of [...new Set([...holders, ...newcomers, BUYER])]) {
  const key = w.address.toLowerCase();
  const pr = proofFor(lastTree, key);
  if (pr && pr.cumulative > (await dist.claimed(key))) await send(dist.claim(key, pr.cumulative, pr.proof));
  const sh = (await vault.balanceOf(w.address)) / 2n;
  if (sh === 0n) continue;
  const before = await usdg.balanceOf(w.address);
  await send(vault.connect(w).requestRedeem(sh, w.address));
  redeemers.push({ w, shares: sh, id: Number(await vault.redeemRequestCount()) - 1, before });
}
const redeemShares = redeemers.reduce((x, r) => x + r.shares, 0n);
const needed = redeemers.reduce((x, r) => x + (r.shares * ppsBeforeOps) / 10n ** 18n, 0n);
const idleAtRequest = await vault.idleBalance();
await setTime(W4 + DAY + 3600);
const navBeforeReturn = await report("week 4 redeems requested");
const processedFirst = (await Promise.all(redeemers.map(async (r) => Number((await vault.redeemRequests(r.id)).status)))).filter((x) => x !== 0).length;
// the engine's liquidity step (bot/arb_monitor/core/arb_liquidity.py): return what the queue still needs, plus margin
const stillNeeded = (await vault.totalPendingRedeemShares()) * (await vault.officialPPS()) / 10n ** 18n;
const idleNow = await vault.idleBalance();
const returned = stillNeeded > idleNow ? stillNeeded - idleNow + 1_000_000n : 0n;
if (returned > 0n) {
  await send(usdg.connect(servicer).approve(D.pARBVault, returned));
  await send(vault.connect(servicer).refillBuffer(returned));
}
await setTime(W4 + DAY + 7200);
const navAfterReturn = await report("week 4 liquidity returned");
const ppsAfterOps = await vault.officialPPS();
let usdgReceived = 0n, claimableTotal = 0n, allClaimable = true, paidExact = true;
for (const r of redeemers) {
  const req = await vault.redeemRequests(r.id);
  if (Number(req.status) !== 1) { allClaimable = false; continue; }
  claimableTotal += req.claimableAssets;
  await send(vault.connect(r.w).claimRedeem(r.id, r.w.address));
  const got = (await usdg.balanceOf(r.w.address)) - r.before;
  usdgReceived += got;
  if (got !== req.claimableAssets) paidExact = false;
}
await step(27, "pARB redemptions: requests exceed the idle buffer, engine returns liquidity, report, claims", {
  requests: redeemers.length, shares: fmtE(redeemShares), neededUsdg: fmtU(needed), idleAtRequestUsdg: fmtU(idleAtRequest),
  processedOnFirstReport: processedFirst, returnedByEngineUsdg: fmtU(returned), usdgReceived: fmtU(usdgReceived), ppsBefore: fmtU(ppsBeforeOps), ppsAfter: fmtU(ppsAfterOps),
});
// restart: a keeper and publisher started with empty state re-read the chain and repeat nothing
const restartKeeper = await keeperTick(keeperContracts, keeperOpts);
const restartInputs = await readInputs({ nft, adapter, provider, fromBlock: Number(D.startBlock ?? 0), toBlock: await provider.getBlockNumber() });
const restartPub = await publishEpoch({ distributor: dist.connect(operator), inputs: restartInputs, genesis: T0, now: await now(), settlementGrace: GRACE, excluded: [D.MilordConverter] });
const restartChk = await checkRoot({ distributor: dist, parb: vault, inputs: restartInputs, genesis: T0, settlementGrace: GRACE, excluded: [D.MilordConverter] });
await step(28, "services restarted with empty state: nothing repeated", {
  keeper: restartKeeper.results.map((x) => `${x.step}:${x.status}`), publisher: restartPub.action, publisherReason: restartPub.reason ?? null, rootChecks: restartChk.length,
});
const opsChecks = { redeemers, redeemShares, needed, idleAtRequest, returned, processedFirst, ppsBeforeOps, ppsAfterOps, navBeforeReturn, navAfterReturn, usdgReceived, claimableTotal, allClaimable, paidExact, restartKeeper, restartPub, restartChk };

// ───────────── 26. reconciliation and invariants ─────────────
const checks = [];
const ok = (name, cond, detail) => { checks.push({ name, pass: !!cond, ...(detail ? { detail } : {}) }); if (!cond) console.error("CHECK FAILED:", name, detail ?? ""); };
const INITIAL = parseEther("6767000000");
const supply = await token.totalSupply();
const exitBurns = await conv.totalMilordBurnedOnExit(), buyBurns = await burner.totalBurnedFromBuybacks(), directBurns = await burner.totalBurnedDirect();
ok("exact supply after burns: 6,767,000,000 - launch remainder - exit burns - buyback burns", supply === INITIAL - BigInt(D.launchDustBurned) - exitBurns - buyBurns - directBurns, `${fmtE(supply)}`);
ok("no MILORD minted after deployment", supply <= INITIAL);
ok("escrow == backed NFTs x 1,000,000 (no unbacked NFT)", (await token.balanceOf(D.MilordConverter)) === (await conv.backedCount()) * parseEther("1000000"));
ok("active NFTs == backed count", Number(await nft.activeSupply()) === Number(await conv.backedCount()));
ok("redemption returned 900,000 and burned 100,000", redeemReturned === parseEther("900000") && redeemBurned === parseEther("100000"));
ok("reissued ID came from inventory (identity kept)", Number(await nft.activationCount(carolId)) === 2);
ok("distributor balance + claimed == pARB minted from fee deposits", (await vault.balanceOf(D.ShareDistributor)) + (await dist.totalClaimed()) === (await adapter.totalSharesMinted()));
ok("allocated <= minted (no unminted pARB distributed)", prevTotal <= (await adapter.totalSharesMinted()));
ok("every batch minted usdg x 1e18 / processed PPS", batches.every((b) => b.shares === (b.usdg * 10n ** 18n) / b.processedPPS), batches.map((b) => `${b.label}:${fmtU(b.processedPPS)}`).join(" "));
ok("week-3 deposit priced at the post-loss PPS, not $1", batches.at(-1).processedPPS < 1_000_000n && fmtU(batches.at(-1).processedPPS) === ppsSeries.at(-1).pps, `${fmtU(batches.at(-1).processedPPS)}`);
const firstRef = (await adapter.batch(0)).sourceRef;
let dup2 = false;
try { await adapter.connect(operator).fund.staticCall(firstRef, 1n); } catch { dup2 = true; }
ok("same fee batch cannot be deposited twice", dup2);
const someClaimer = [...cumulative.keys()][0];
const p = proofFor(lastTree, someClaimer);
if ((await dist.claimed(someClaimer)) < p.cumulative) await send(dist.claim(someClaimer, p.cumulative, p.proof));
const balBefore = await vault.balanceOf(someClaimer);
let second = 0n;
try { await send(dist.claim(someClaimer, p.cumulative, p.proof)); second = (await vault.balanceOf(someClaimer)) - balBefore; } catch { second = 0n; }
ok("a second claim of the same cumulative pays nothing", second === 0n);
ok("no performance-fee shares (fee recipient holds no pARB)", (await vault.balanceOf(D.teamSafe)) === 0n);
ok("deposits are not profit: PPS unchanged by the week-1 deposit", ppsSeries[0].pps === 1);
ok("PPS rose in the positive week", weeks[1].ppsUsdg > weeks[0].ppsUsdg);
ok("PPS fell in the negative week (not reset to $1)", weeks[2].ppsUsdg < weeks[1].ppsUsdg && weeks[2].ppsUsdg !== 1);
const buyTax = await hook.totalBuyTax(), sellTax = await hook.totalSellTax(), buyVol = await hook.buyVolume(), sellVol = await hook.sellVolume();
const near = (a, b, tol) => (a > b ? a - b : b - a) <= tol;
ok("tax = 1.5% of taxed ETH volume (buys and sells)", near(buyTax, (buyVol * 150n) / 10000n, 1000n) && near(sellTax, (sellVol * 150n) / 10000n, 1000n), `${fmtE(buyTax + sellTax)} ETH on ${fmtE(buyVol + sellVol)} ETH`);
const taxTotal = buyTax + sellTax, hookrPaid = await hook.totalPlatformPaid(), projPaid = await hook.totalProjectPaid();
const hookrAll = hookrPaid + (await hook.platformAccrued()), projAll = projPaid + (await hook.projectAccrued()); // incl. tax on the last buyback, not yet settled
ok("Hookr commission == 20% of tax; project == 80%", hookrAll + projAll === taxTotal && near(hookrAll * 5n, taxTotal, 5000n), `${fmtE(hookrAll)} of ${fmtE(taxTotal)}`);
ok("project tax fully received by TradingTaxSplitter", (await taxSplit.totalDistributed(ZERO)) === projPaid);
const tb = await taxSplit.totalAllocated(ZERO, D.BuybackBurner), tt = await taxSplit.totalAllocated(ZERO, D.teamSafe), ta = await taxSplit.totalAllocated(ZERO, routerPayee);
ok("burn / team / arbitrage tax shares equal within 1 bp", tb + tt + ta === projPaid && near(tb, tt, projPaid / 10000n + 10n) && near(tt, ta, 10n), `${fmtE(tb)} / ${fmtE(tt)} / ${fmtE(ta)}`);
ok("entry fees: converter total == splitter total, 70/30", (await conv.totalEntryFeesWei()) === (await entrySplit.totalDistributed(ZERO)) && (await entrySplit.totalAllocated(ZERO, routerPayee)) === ((await entrySplit.totalDistributed(ZERO)) * 7000n) / 10000n);
ok("royalty received only when paid, split 50/50", (await royaltySplit.totalDistributed(ZERO)) === (SCENARIO.royaltySaleEth * 670n) / 10000n && (await royaltySplit.totalAllocated(ZERO, D.teamSafe)) === (await royaltySplit.totalAllocated(ZERO, routerPayee)));
{
  const swapped = await arbRouter.totalEthSwapped(), held = await provider.getBalance(routerPayee);
  ok("arbitrage ETH: router swapped + still held == allocated to it; all USDG deposited", swapped + held === (await sumAlloc(routerPayee)) && (await arbRouter.totalAssetFunded()) === (await adapter.totalUsdcDeposited()), `${fmtE(swapped)} swapped, ${fmtE(held)} held (below threshold)`);
  const ev = await arbRouter.queryFilter(arbRouter.filters.Executed(), 0, "latest");
  const [maxSwap, slip] = [await arbRouter.maxSwapWei(), await arbRouter.maxSlippageBps()];
  ok("router: every swap within the per-tx cap and at most maxSlippage below the 30-min V3 TWAP", ev.length > 0 && ev.every((e) => e.args.ethIn <= maxSwap && e.args.assetOut >= e.args.minOut && e.args.minOut === ((e.args.ethIn * e.args.price) / 10n ** 18n) * (10000n - slip) / 10000n), `${ev.length} swaps, slippage cap ${slip} bps`);
}
{
  const o = opsChecks;
  ok("redemption: requests exceeded the idle buffer and the excess waited for liquidity (not paid short)", o.needed > o.idleAtRequest && o.processedFirst < o.redeemers.length && o.returned > 0n, `${o.processedFirst}/${o.redeemers.length} on the first report`);
  ok("redemption: all processed once the engine returned liquidity, each paid shares x PPS", o.allClaimable && o.paidExact && o.redeemers.every(() => true) && o.usdgReceived === o.redeemers.reduce((x, r) => x + (r.shares * o.ppsAfterOps) / 10n ** 18n, 0n), `${o.redeemers.length} requests, ${fmtU(o.usdgReceived)} USDG`);
  ok("capital moved between the servicer and the vault creates no profit or loss (PPS unchanged, no P&L applied)", o.ppsBeforeOps === o.ppsAfterOps, `${fmtU(o.ppsBeforeOps)} -> ${fmtU(o.ppsAfterOps)}`);
  ok("gross NAV unchanged by the liquidity return (vault + servicer counted once)", o.navBeforeReturn === o.navAfterReturn, `${fmtU(o.navBeforeReturn)} / ${fmtU(o.navAfterReturn)}`);
  ok("restarted keeper: no failed step", o.restartKeeper.failed === 0);
  ok("restarted publisher: proposes nothing already published", o.restartPub.action === "none", o.restartPub.reason);
  ok("restarted root checker: every root matches", o.restartChk.every((x) => x.match));
}
ok("root checker recomputed every proposed root identically", rootChecks.length > 0 && rootChecks.every((x) => x.match), `${rootChecks.length} checks`);
ok("keeper passes ran without failed steps", keeperLog.every((k) => !k.steps.some((x) => x.endsWith(":failed"))), `${keeperLog.length} passes`);
ok("buyback burns recorded == supply reduction from buybacks", buyBurns > 0n && (await burner.buybackCount()) >= 1n);
// the last report counted vault + servicer USDG; only the redemption claimed after it has left since
ok("USDG has 6 decimals and NAV = vault + servicer USDG", Number(await usdg.decimals()) === 6 && ppsSeries.at(-1).navUsdg === fmtU((await usdg.balanceOf(D.pARBVault)) + (await usdg.balanceOf(servicer.address)) + opsChecks.usdgReceived));
{
  const navNow = (await usdg.balanceOf(D.pARBVault)) + (await usdg.balanceOf(servicer.address));
  const implied = ((await vault.officialPPS()) * ((await vault.totalSupply()) - (await vault.balanceOf("0x000000000000000000000000000000000000dEaD")))) / 10n ** 18n;
  ok("PPS x outstanding pARB == NAV (vault + servicer USDG) after settlement", near(implied, navNow, 1000n), `${fmtU(implied)} vs ${fmtU(navNow)}`);
}
ok("seller keeps week-1 pARB after selling", weeks[2].tracked.seller_alice.cumulative >= weeks[0].tracked.seller_alice.cumulative && weeks[0].tracked.seller_alice.cumulative > 0);
ok("buyer earns only from day 3.5 of week 2", weeks[0].tracked.buyer.cumulative === 0 && Math.abs(weeks[1].tracked.buyer.weightSeconds - 3.5 * DAY) <= 60);
ok("daily epochs: each weekly close proposed one cumulative root through the week's last UTC day", weeks.slice(0, 3).every((w) => w.epochAction === "proposed" && w.rootEpoch === w.lastDay), weeks.map((w) => `${w.label}:${w.rootEpoch}/${w.lastDay}`).join(" "));
{
  const saleDay = Math.floor((W2 + 3.5 * DAY - T0) / DAY) + 1;
  const d = dailyEpochs.find((e) => e.day === saleDay);
  ok("mid-day sale: buyer earns from the transfer second (12 h of that UTC day), not the whole day", d && Math.abs(Number(d.weights.get(BUYER.address.toLowerCase()) ?? 0n) - 12 * 3600) <= 60, `day ${saleDay}: ${d?.weights.get(BUYER.address.toLowerCase())} s`);
  const days = dailyEpochs.map((e) => e.day);
  ok("every UTC day since genesis is an epoch exactly once (no gaps, no overlap)", days.every((x, i) => x === i + 1) && dailyEpochs.every((e, i) => e.start === T0 + i * DAY), `${days.length} days`);
}
ok("a pending root in review cannot be replaced on chain; the publisher waits instead", pendingGuard?.contractRefused && pendingGuard.publisher === "already pending", JSON.stringify(pendingGuard));
ok("redeemer keeps earned pARB, earns nothing after redeeming", (cumulative.get(CAROL.address.toLowerCase()) ?? 0n) >= carolEarned && carolEarned > 0n && weeks[2].tracked.redeemer_carol.weightSeconds === (perHolder[2] - 1) * 7 * DAY);
ok("100% of supply entered the position: deployer, team, arbitrage and operator held 0 MILORD at open", Object.values(outsideAtOpen).every((v) => v === 0n) && BigInt(D.launchDustBurned) <= 1_000_000n, `remainder burned ${D.launchDustBurned} wei`);
ok("pool started with 0 ETH and the first trade was a buy", Math.abs(start.poolEth) < 1e-9 && firstOut > 0n);
ok("launch entry: bounded bootstrap quote, never below spot, fee = 0.002 ETH minimum", launchBootstrapOk);
ok("every entry fee == max(10% of the TWAP value of 1M MILORD, 0.002 ETH); both paths used", [...ids, ...laterFees].every((x) => x.fee === (x.weiPerMillion / 10n > parseEther("0.002") ? x.weiPerMillion / 10n : parseEther("0.002"))) && ids.some((x) => x.minimumApplied) && laterFees.some((x) => !x.minimumApplied), `${ids.filter((x) => x.minimumApplied).length} at minimum, ${laterFees.filter((x) => !x.minimumApplied).length} at 10%`);
ok("second strategy inactive; collection unchanged", D.Milords === nftAddrBefore && sid2 === stratBefore + 1 && !(await registry.strategy(sid2)).enabled && pref.personal);
{
  const lpEth = await collector.totalEthForwarded(), lpMil = await collector.totalMilordBurned();
  ok("LP fees: ETH to BuybackBurner, MILORD burned, separate from tax", lpEth > 0n && lpMil > 0n && (await provider.getBalance(D.LockFeeCollector)) === 0n && (await token.balanceOf(D.LockFeeCollector)) === 0n && (await hook.totalProjectPaid()) === projPaid && directBurns >= lpMil, `${fmtE(lpEth)} ETH, ${fmtE(lpMil)} MILORD burned`);
  const [ben, unlockBlock] = await familyLock.lockOf(D.familyId);
  let releaseRefused = false;
  try { await familyLock.connect(operator).release.staticCall(D.familyId, operator.address); } catch { releaseRefused = true; }
  ok("founding position locked forever, beneficiary = fee collector, release refused", ben === D.LockFeeCollector && unlockBlock === 2n ** 64n - 1n && releaseRefused);
}

const last = weeks.at(-1);
const owners = [...cumulative.entries()].sort((a, b) => (b[1] > a[1] ? 1 : -1)).slice(0, 15).map(([a, v]) => ({ address: a, cumulativePARB: fmtE(v), claimed: 0 }));
for (const o of owners) o.claimed = fmtE(await dist.claimed(o.address));
const out = {
  generatedAt: new Date().toISOString(),
  disclaimer: "SIMULATED. Local chain with hypothetical SCENARIO inputs, illustrative (unapproved) launch parameters, a mock of Hookr's tax hook, mock USDG and scenario P&L. Not actual financial performance, not a forecast, not proposed production values.",
  scenario: Object.fromEntries(Object.entries(SCENARIO).map(([k, v]) => [k, Array.isArray(v) ? v.map(String) : typeof v === "bigint" ? v.toString() : v])),
  launch: LAUNCH, start, weeks, timeline, priceSeries, ppsSeries, quoteRefusals, owners,
  conversions: [...ids, ...laterFees].map((x) => ({ id: x.id, feeEth: fmtE(x.fee), weiPerMillion: fmtE(x.weiPerMillion), minimumApplied: x.minimumApplied })),
  reconciliation: {
    ethVolume: fmtE(ethVolume), taxBuy: fmtE(buyTax), taxSell: fmtE(sellTax), hookrCommission: fmtE(hookrPaid), projectTax: fmtE(projPaid),
    taxToBurner: fmtE(tb), taxToTeam: fmtE(tt), taxToArbitrage: fmtE(ta),
    entryFees: fmtE(await conv.totalEntryFeesWei()), entryToArbitrage: fmtE(await entrySplit.totalAllocated(ZERO, routerPayee)), entryToTeam: fmtE(await entrySplit.totalAllocated(ZERO, D.teamSafe)),
    royalties: fmtE(await royaltySplit.totalDistributed(ZERO)), lpFeesEth: fmtE(await collector.totalEthForwarded()), lpFeesMilord: fmtE(await collector.totalMilordBurned()),
    burnBudgetEth: fmtE(await burner.totalEthReceived()), burnSpentEth: fmtE(await burner.totalEthSpent()), milordBurnedByBuyback: fmtE(buyBurns), milordBurnedOnExit: fmtE(exitBurns),
    arbitrageEth: fmtE(await sumAlloc(routerPayee)), ethSwappedToUsdg: fmtE(await arbRouter.totalEthSwapped()), routerHeldEth: fmtE(await provider.getBalance(routerPayee)), usdgDeposited: fmtU(await adapter.totalUsdcDeposited()),
    parbMinted: fmtE(await adapter.totalSharesMinted()), parbAllocated: fmtE(prevTotal), parbClaimed: fmtE(await dist.totalClaimed()), parbUnallocated: fmtE((await adapter.totalSharesMinted()) - prevTotal),
    supplyFinal: fmtE(supply),
  },
  keeperLog, rootChecks: rootChecks.map((x) => ({ ...x })), checks, deployments: D,
};
fs.mkdirSync(path.join(root, "sim/output"), { recursive: true });
fs.writeFileSync(path.join(root, "sim/output/simulation.json"), JSON.stringify(out, null, 2));
if (lastTree) {
  const claims = {};
  for (const [i, v] of lastTree.entries()) claims[v[0].toLowerCase()] = { cumulative: v[1], proof: lastTree.getProof(i) };
  fs.writeFileSync(path.join(root, "sim/output/epoch-latest.json"), JSON.stringify({ epoch, root: lastTree.root, total: prevTotal.toString(), claims }, null, 2));
}
{
  // the publisher's rewards.json for the frontend (earned / allocated / active / accruing per account)
  const inputs = await readInputs({ nft, adapter, provider, fromBlock: Number(D.startBlock ?? 0), toBlock: await provider.getBlockNumber() });
  const status = rewardsStatus({ inputs, genesis: T0, now: await now(), settlementGrace: GRACE, excluded: [D.MilordConverter], active: await dist.active(), pending: await dist.pending() });
  fs.writeFileSync(path.join(root, "sim/output/rewards.json"), JSON.stringify(status, null, 2));
}
console.log(`checks: ${checks.filter((c) => c.pass).length}/${checks.length} passed`);
if (checks.some((c) => !c.pass)) process.exit(1);
