// End-to-end local simulation on anvil against the real contracts.
//
//   anvil &
//   forge script script/DeployLocal.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
//   node sim/simulate.mjs
//
// Every number below marked SCENARIO is a hypothetical input chosen to exercise the
// mechanics. None of it is a forecast, a measured profit, or real share-price data.
import fs from "node:fs";
import path from "node:path";
import { JsonRpcProvider, Wallet, Contract, parseEther, formatEther, formatUnits, keccak256, toUtf8Bytes, hexlify, randomBytes, id } from "ethers";
import { ownershipIntervals, eligibility, allocate, accumulate, buildTree, proofFor, DAY, WEEK } from "../offchain/lib/eligibility.mjs";
import { issueQuote } from "../offchain/lib/quote.mjs";
import { signReport } from "../offchain/lib/report.mjs";

// ───────────── SCENARIO inputs (hypothetical) ─────────────
const SCENARIO = {
  weiPerMillionEarly: parseEther("0.03"),  // ETH value of 1M MILORD for the first 50 conversions
  weiPerMillionLater: parseEther("0.012"), // later price, low enough that the 0.002 ETH minimum applies
  ethUsd: 2500n,                           // used only by the mock ETH -> USDG swap on Robinhood Chain
  royaltySaleEth: parseEther("0.1"),       // one secondary sale that pays royalty
  tradingTaxEthWeek1: parseEther("0.15"),  // illustrative tax inflow; tax mechanism pending decision
  pnlBpsWeek2: 200,                        // +2% on deployed capital
  pnlBpsWeek3: -300,                       // -3% on deployed capital
};

const RPC = process.env.RPC_URL ?? "http://127.0.0.1:8545";
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const D = JSON.parse(fs.readFileSync(path.join(root, "sim/deployments.local.json"), "utf8"));
const abi = (file, name = file) => JSON.parse(fs.readFileSync(path.join(root, `out/${file}.sol/${name}.json`), "utf8")).abi;

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
const usdc = C(D.USDG, abi("MockUSDG")); // vault asset: USDG (mock), same chain as everything else
const vault = C(D.pARBVault, abi("PMFIArbVaultV3"));
const dist = C(D.ShareDistributor, abi("ShareDistributor"));
const adapter = C(D.ParbFundingAdapter, abi("ParbFundingAdapter"));
const chainId = Number((await provider.getNetwork()).chainId);
const servicer = arb; // local deploy uses the arbitrage Safe as the vault's servicer wallet

// ───────────── chain helpers ─────────────
const now = async () => (await provider.getBlock("latest")).timestamp;
const setTime = async (t) => { t = Math.max(Math.floor(t), (await now()) + 1); await provider.send("evm_setNextBlockTimestamp", [t]); await provider.send("evm_mine", []); };
const send = async (p) => (await p).wait();
const fmtE = (v) => Number(formatEther(v));
const fmtU = (v) => Number(formatUnits(v, 6));

// ───────────── actors ─────────────
const holders = Array.from({ length: 40 }, (_, i) => Wallet.createRandom().connect(provider));
const newcomers = Array.from({ length: 12 }, () => Wallet.createRandom().connect(provider));
const ALICE = holders[0], CAROL = holders[2], BUYER = newcomers[11];
for (const w of [...holders, ...newcomers]) await provider.send("anvil_setBalance", [w.address, "0x56BC75E2D63100000"]); // 100 ETH

async function giveMilord(w, amount) {
  await send(token.transfer(w.address, amount));
  await send(token.connect(w).approve(D.MilordConverter, 2n ** 255n));
}

async function convertFor(w, weiPerMillion) {
  const t = await now();
  const { quote, signature } = await issueQuote({
    signer: quoteSigner, chainId, converter: D.MilordConverter, account: w.address,
    priceSource: async () => ({ weiPerMillion, observedAt: t }), now: t, randomNonce: hexlify(randomBytes(32)),
  });
  const fee = await conv.entryFeeFor(weiPerMillion);
  const r = await send(conv.connect(w).convert(quote, signature, fee, { value: fee }));
  const ev = r.logs.map((l) => { try { return conv.interface.parseLog(l); } catch { return null; } }).find((e) => e?.name === "Converted");
  return { id: Number(ev.args.tokenId), fee };
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
async function report() {
  const t = await now();
  const reportedAssets = (await usdc.balanceOf(D.pARBVault)) + (await usdc.balanceOf(servicer.address));
  const data = { reportedAssets, timestamp: t, deadline: t + 600, nonce: ++reportNonce };
  const sig = await signReport(reportSigner, { ...data, vault: D.pARBVault, chainId });
  await send(vault.report(data, sig, 0, 0));
  return reportedAssets;
}
async function applyPnl(bps) {
  const deployed = await usdc.balanceOf(servicer.address);
  const delta = (deployed * BigInt(Math.abs(bps))) / 10000n;
  if (bps >= 0) await send(usdc.mint(servicer.address, delta));
  else await send(usdc.connect(servicer).transfer("0x000000000000000000000000000000000000dEaD", delta));
  return bps >= 0 ? delta : -delta;
}

// ───────────── weekly close ─────────────
let cumulative = new Map();
let prevTotal = 0n;
let epoch = 0;
let lastTree = null;
const weeks = [];
const arbAlloc = async () => (await entrySplit.totalAllocated(ZERO, D.arbitrageSafe)) + (await royaltySplit.totalAllocated(ZERO, D.arbitrageSafe)) + (await taxSplit.totalAllocated(ZERO, D.arbitrageSafe));
const ZERO = "0x0000000000000000000000000000000000000000";
let arbSwapped = 0n;

async function closeWeek(label, start, end, pnlBps, notes) {
  await setTime(end);
  // 1. split all ETH revenue (permissionless)
  for (const s of [entrySplit, royaltySplit, taxSplit]) await send(s.distribute(ZERO));
  // 2. buyback-and-burn with whatever budget the burner holds
  const budget = await burner.budget();
  let bought = 0n;
  if (budget >= parseEther("0.01")) {
    const minOut = (budget * 20_000_000n * 99n) / 100n; // mock rate 20M/ETH, 1% slippage bound
    const r = await send(burner.connect(operator).buybackAndBurn(budget, minOut, end + 600));
    bought = (await burner.totalBurnedFromBuybacks());
  }
  // 3. mock swap on Robinhood Chain: ETH that reached the arbitrage Safe this week -> USDG (no bridge)
  const arbTotal = await arbAlloc();
  const ethToSwap = arbTotal - arbSwapped;
  arbSwapped = arbTotal;
  const usdcIn = (ethToSwap * SCENARIO.ethUsd) / 10n ** 12n; // wei * $/ETH -> 6-dec USDG
  // 4. trading result on capital already deployed, then report + settle
  const pnl = pnlBps ? await applyPnl(pnlBps) : 0n;
  let batchId = null, shares = 0n;
  if (usdcIn >= 10_000_000n) {
    await send(usdc.mint(D.ParbFundingAdapter, usdcIn));
    const ref = id(`milord-fees:${label}:${ethToSwap}`);
    const r = await send(adapter.connect(operator).fund(ref, usdcIn));
    batchId = Number(await adapter.batchCount()) - 1;
  }
  const reported = await report();
  if (batchId !== null) {
    await send(adapter.settle(batchId));
    shares = (await adapter.batch(batchId)).sharesMinted;
  }
  await provider.send("evm_increaseTime", [61]);
  await send(vault.tend());
  const pps = await vault.officialPPS();

  // 5. eligibility for [start, end), allocate everything unallocated (incl. carried dust)
  const iv = ownershipIntervals(await transfers());
  const { weights, daily } = eligibility(iv, { start, end, excluded: [D.MilordConverter] });
  const available = (await vault.balanceOf(D.ShareDistributor)) + (await dist.totalClaimed()) - prevTotal;
  const { allocations, dust } = allocate(available, weights);
  const byPayout = allocations; // same chain: allocations go to the holder address itself
  cumulative = accumulate(cumulative, byPayout);
  const built = buildTree(cumulative);
  let rootHex = null;
  if (built && built.total > prevTotal) {
    await send(dist.connect(operator).proposeRoot(built.root, built.total, ++epoch, keccak256(toUtf8Bytes(JSON.stringify(built.values)))));
    await provider.send("evm_increaseTime", [3601]);
    await send(dist.activateRoot());
    prevTotal = built.total;
    lastTree = built.tree;
    rootHex = built.root;
  }
  // 6. some holders claim (others wait; cumulative leaves mean they lose nothing)
  let claimedNow = 0n;
  if (lastTree) {
    let i = 0;
    for (const [acct] of cumulative) {
      if (i++ % 2) continue;
      const p = proofFor(lastTree, acct);
      if (p && p.cumulative > (await dist.claimed(acct))) {
        await send(dist.claim(acct, p.cumulative, p.proof));
        claimedNow += 1n;
      }
    }
  }

  const active = Number(await nft.activeSupply());
  const owners = new Set();
  for (const [, list] of iv) { const last = list[list.length - 1]; if (last && last.end === Infinity && last.owner !== D.MilordConverter.toLowerCase()) owners.add(last.owner); }
  const w = {
    label, start, end, notes,
    activeNFTs: active, uniqueHolders: owners.size, inventory: Number(await conv.inventorySize()), totalMinted: Number(await nft.totalMinted()),
    conversionsTotal: Number(await conv.totalConversions()), redemptionsTotal: Number(await conv.totalRedemptions()),
    ethEntryFeesTotal: fmtE(await conv.totalEntryFeesWei()),
    ethToArbitrageSafeTotal: fmtE(arbTotal), ethToTeamSafeTotal: fmtE((await entrySplit.totalAllocated(ZERO, D.teamSafe)) + (await royaltySplit.totalAllocated(ZERO, D.teamSafe)) + (await taxSplit.totalAllocated(ZERO, D.teamSafe))),
    ethSwappedThisWeek: fmtE(ethToSwap), usdgDepositedThisWeek: fmtU(usdcIn), pnlUsdgThisWeek: fmtU(pnl),
    parbMintedThisWeek: fmtE(shares), parbMintedTotal: fmtE(await adapter.totalSharesMinted()),
    ppsUsdg: fmtU(pps), reportedAssetsUsdg: fmtU(reported),
    parbAllocatedThisWeek: fmtE([...allocations.values()].reduce((a, b) => a + b, 0n)), dustCarried: fmtE(dust),
    eligibleWallets: weights.size, cumulativeAllocated: fmtE(prevTotal), claimedTotal: fmtE(await dist.totalClaimed()),
    milordSupply: fmtE(await token.totalSupply()), escrowMilord: fmtE(await token.balanceOf(D.MilordConverter)),
    burnedOnExitTotal: fmtE(await conv.totalMilordBurnedOnExit()), burnedByBuybackTotal: fmtE(await burner.totalBurnedFromBuybacks()),
    merkleRoot: rootHex,
    tracked: {},
  };
  for (const [name, wal] of Object.entries(TRACK)) {
    const a = wal.toLowerCase();
    w.tracked[name] = { weightSeconds: Number(weights.get(a) ?? 0n), allocatedThisWeek: fmtE(byPayout.get(a) ?? 0n), cumulative: fmtE(cumulative.get(a) ?? 0n), nftsHeld: Number(await nft.balanceOf(wal)) };
  }
  weeks.push(w);
  console.log(`${label}: active=${w.activeNFTs} usdgIn=${w.usdgDepositedThisWeek} pARB+${w.parbMintedThisWeek} PPS=${w.ppsUsdg} allocated=${w.parbAllocatedThisWeek}`);
  return w;
}

// ───────────── scenario ─────────────
const TRACK = { seller_alice: ALICE.address, buyer: BUYER.address, redeemer_carol: CAROL.address };
const t0raw = await now();
const T0 = t0raw - ((t0raw - 4 * DAY) % WEEK) + WEEK; // next Monday 00:00 UTC
await setTime(T0 - 3600);
const snapshot0 = { activeNFTs: Number(await nft.activeSupply()), holders: 0, milordSupply: fmtE(await token.totalSupply()) };
console.log("start: zero holders, zero NFTs");

// Week 1: first 100 NFTs, fees, a royalty-paying sale, a sale without royalty, illustrative tax inflow
for (const w of holders) await giveMilord(w, parseEther("3000000"));
const ids = [];
for (let i = 0; i < 100; i++) {
  await setTime(T0 + i * 3000);
  const h = holders[i % holders.length];
  ids.push({ ...(await convertFor(h, i < 50 ? SCENARIO.weiPerMillionEarly : SCENARIO.weiPerMillionLater)), holder: h });
}
await setTime(T0 + 4 * DAY);
{ // secondary sale through a royalty-honouring marketplace (holders[5] -> newcomers[0])
  const s = ids.find((x) => x.holder === holders[5]);
  await send(nft.connect(holders[5]).transferFrom(holders[5].address, newcomers[0].address, s.id));
  const [recv, amt] = await nft.royaltyInfo(s.id, SCENARIO.royaltySaleEth);
  await send(owner.sendTransaction({ to: recv, value: amt }));
  // and one private transfer that pays no royalty (royalties are not guaranteed)
  const s2 = ids.find((x) => x.holder === holders[6]);
  await send(nft.connect(holders[6]).transferFrom(holders[6].address, newcomers[1].address, s2.id));
}
await send(owner.sendTransaction({ to: D.TradingTaxSplitter, value: SCENARIO.tradingTaxEthWeek1 }));
await closeWeek("week 1", T0, T0 + WEEK, 0, "first 100 Milords; one royalty-paying sale, one without; illustrative tax inflow");

// Week 2: redemption on day 1.5, ownership change on day 3.5, 10 more conversions, +2%
const W2 = T0 + WEEK;
await setTime(W2 + 1.5 * DAY);
const carolId = ids.find((x) => x.holder === CAROL).id;
await send(nft.connect(CAROL).approve(D.MilordConverter, carolId)).catch(() => {});
await send(conv.connect(CAROL).redeem(carolId));
await setTime(W2 + 2 * DAY);
for (let i = 0; i < 10; i++) {
  await giveMilord(newcomers[2 + (i % 9)], parseEther("1000000"));
  await setTime(W2 + 2 * DAY + i * 600);
  await convertFor(newcomers[2 + (i % 9)], SCENARIO.weiPerMillionLater);
}
await setTime(W2 + 3.5 * DAY);
const aliceIds = ids.filter((x) => x.holder === ALICE).map((x) => x.id);
await send(nft.connect(ALICE).transferFrom(ALICE.address, BUYER.address, aliceIds[0]));
await closeWeek("week 2", W2, W2 + WEEK, SCENARIO.pnlBpsWeek2, "one redemption (day 1.5, reissued later from inventory), one sale mid-week (day 3.5), 10 new conversions, +2% scenario P&L");

// Week 3: -3%, add a characteristic and a strategy without replacing the collection
const W3 = W2 + WEEK;
await setTime(W3 + DAY);
const nftAddrBefore = D.Milords;
await send(registry.addCharacteristic("Characteristic 9"));
await send(registry.addStrategy("Future strategy (placeholder)", chainId, ZERO, ZERO, false));
const sid = Number(await registry.strategyCount());
await send(registry.setPairStrategy(1, 9, sid));
await send(registry.setStrategyEnabled(sid, true));
const selector = holders[7];
const selId = ids.find((x) => x.holder === selector).id;
await send(registry.connect(selector).select(selId, 9, 1));
const pref = await registry.currentPreference(selId);
for (let i = 0; i < 5; i++) {
  const w = newcomers[2 + i];
  await giveMilord(w, parseEther("1000000"));
  await setTime(W3 + 2 * DAY + i * 3600);
  await convertFor(w, SCENARIO.weiPerMillionEarly);
}
await closeWeek("week 3", W3, W3 + WEEK, SCENARIO.pnlBpsWeek3, "-3% scenario P&L; 5 new conversions; characteristic 9 and strategy 2 added; one holder selected the new pair");

// ───────────── checks ─────────────
const checks = [];
const ok = (name, cond) => { checks.push({ name, pass: !!cond }); if (!cond) console.error("CHECK FAILED:", name); };
ok("escrow == backedCount x 1M", (await token.balanceOf(D.MilordConverter)) === (await conv.backedCount()) * parseEther("1000000"));
ok("distributor solvent", (await vault.balanceOf(D.ShareDistributor)) + (await dist.totalClaimed()) >= prevTotal);
ok("shares recorded once", (await vault.balanceOf(D.ShareDistributor)) + (await dist.totalClaimed()) === (await adapter.totalSharesMinted()));
ok("seller keeps week-1 allocation after selling", weeks[2].tracked.seller_alice.cumulative >= weeks[0].tracked.seller_alice.cumulative && weeks[0].tracked.seller_alice.cumulative > 0);
ok("buyer earns only from day 3.5 of week 2", weeks[0].tracked.buyer.cumulative === 0 && Math.abs(weeks[1].tracked.buyer.weightSeconds - 3.5 * DAY) <= 60);
ok("redeemer earns only while holding", weeks[1].tracked.redeemer_carol.weightSeconds < weeks[0].tracked.redeemer_carol.weightSeconds);
ok("PPS rose in the positive week", weeks[1].ppsUsdg > weeks[0].ppsUsdg);
ok("PPS fell in the negative week (not reset to $1)", weeks[2].ppsUsdg < weeks[1].ppsUsdg && weeks[2].ppsUsdg !== 1);
ok("collection unchanged when strategy added", D.Milords === nftAddrBefore && Number(pref.strategyId) === sid && pref.personal);
ok("reissued ID came from inventory", Number(await nft.activationCount(carolId)) === 2);
ok("no performance fee shares", (await vault.balanceOf(D.teamSafe)) === 0n);

const out = { generatedAt: new Date().toISOString(), disclaimer: "Local single-chain simulation (as on Robinhood Chain) with hypothetical SCENARIO inputs and mock USDG, swap and quote price. Not performance data, not a forecast.", scenario: Object.fromEntries(Object.entries(SCENARIO).map(([k, v]) => [k, typeof v === "bigint" ? v.toString() : v])), start: snapshot0, weeks, checks, deployments: D };
fs.writeFileSync(path.join(root, "sim/output/simulation.json"), JSON.stringify(out, null, 2));
if (lastTree) {
  const claims = {};
  for (const [i, v] of lastTree.entries()) claims[v[0].toLowerCase()] = { cumulative: v[1], proof: lastTree.getProof(i) };
  fs.writeFileSync(path.join(root, "sim/output/epoch-latest.json"), JSON.stringify({ epoch, root: lastTree.root, total: prevTotal.toString(), claims }, null, 2));
}
console.log(`checks: ${checks.filter((c) => c.pass).length}/${checks.length} passed`);
if (checks.some((c) => !c.pass)) process.exit(1);
