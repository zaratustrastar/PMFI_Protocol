// Integrated launch rehearsal flows, called by fork/rehearse-full-launch.sh on a PRIVATE local Anvil fork of
// Robinhood Chain (4663). Never run it against anything else: it refuses any endpoint that is not a loopback Anvil
// fork, and every key below is a public Anvil test key.
//
//   node fork/rehearse-flows.mjs launch  <deployments.json> <MilordAtomicLaunch>
//       Safe batches from offchain/safe-launch-batch.mjs: Team and Arbitrage Safe ownership, then safe-1-launch
//       (the atomic launch); offchain/record-launch.mjs with the official StateView, which must report "complete".
//   node fork/rehearse-flows.mjs operate <deployments.json> <MilordAtomicLaunch> <V4SwapAdapter> <HookrLockFeeCollector>
//       safe-2-accept, safe-3-lock (fork only: irreversible on mainnet and NOT approved there), trading through the
//       Hookr pool, keeper passes with the services' own code (tax queues, splitters, LP fees, buyback, ETH -> USDG
//       router, deposit settlement), an NFT conversion with a TWAP-signed quote, a signed vault report, a weekly
//       pARB root (publisher, checker, activation) and a claim. Writes $REHEARSAL_TMP/summary.json.
//
// Env: REHEARSAL_RPC (http://127.0.0.1:<port>), REHEARSAL_TMP (scratch dir), STATE_VIEW. Exit 1 on any failure.
import fs from "node:fs";
import path from "node:path";
import { execFileSync, spawnSync } from "node:child_process";
import { JsonRpcProvider, JsonRpcSigner, Wallet, Contract, ContractFactory, NonceManager, parseEther, formatEther, formatUnits, getAddress, hexlify, randomBytes } from "ethers";

const [mode, depFile, HELPER, ADAPTER, COLLECTOR] = process.argv.slice(2);
const RPC = process.env.REHEARSAL_RPC;
const TMP = process.env.REHEARSAL_TMP;
const STATE_VIEW = process.env.STATE_VIEW;
if (!["launch", "operate"].includes(mode) || !depFile || !HELPER || !RPC || !TMP || !STATE_VIEW) {
  console.error("usage: REHEARSAL_RPC=... REHEARSAL_TMP=... STATE_VIEW=... node fork/rehearse-flows.mjs launch|operate <deployments.json> <helper> [adapter collector]");
  process.exit(1);
}
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const ZERO = "0x0000000000000000000000000000000000000000";
const FOREVER = (1n << 64n) - 1n;
const KEYS = { // public Anvil test keys (never hold real funds)
  deployer: "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80", // #0
  quoteSigner: "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a", // #2
  reportSigner: "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6", // #3
  keeper: "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a", // #4
  publisher: "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba", // #5
  trader: "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356", // #7
};

const fail = (m) => { console.error(`REHEARSAL FAILED: ${m}`); process.exit(1); };
const checks = [];
const ok = (cond, what, detail) => {
  checks.push({ ok: !!cond, what, ...(detail !== undefined ? { detail: String(detail) } : {}) });
  console.log(`${cond ? "ok  " : "FAIL"} ${what}${detail !== undefined ? ` (${detail})` : ""}`);
  if (!cond) fail(what);
};

// ── 0. the endpoint must be a loopback Anvil fork of 4663 ──
const url = new URL(RPC);
if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) fail(`refusing non-loopback RPC ${url.hostname}`);
const provider = new JsonRpcProvider(RPC, 4663, { staticNetwork: true, pollingInterval: 50, cacheTimeout: -1 });
const client = await provider.send("web3_clientVersion", []);
if (!/^anvil/i.test(client)) fail(`not Anvil: ${client}`);
const info = await provider.send("anvil_nodeInfo", []);
if (!info?.forkConfig?.forkBlockNumber && !info?.forkBlockNumber && !JSON.stringify(info).includes("forkBlockNumber")) fail("Anvil is not a fork");
if (Number((await provider.getNetwork()).chainId) !== 4663) fail("fork is not chain 4663");

const art = (file, name = file) => JSON.parse(fs.readFileSync(path.join(root, `out/${file}.sol/${name}.json`), "utf8"));
const abi = (file, name) => art(file, name).abi;
const readD = () => JSON.parse(fs.readFileSync(depFile, "utf8"));
const writeD = (D) => fs.writeFileSync(depFile, JSON.stringify(D, null, 2) + "\n");
let D = readD();
const TEAM = getAddress(D.teamSafe);
const ARB = getAddress(D.arbitrageSafe);
if (getAddress(D.owner) !== TEAM) fail("the rehearsal expects MILORD_OWNER_SAFE = MILORD_TEAM_SAFE (one Team Safe)");
const now = async () => (await provider.getBlock("latest")).timestamp;
const mine = async (secs) => { await provider.send("evm_increaseTime", [secs]); await provider.send("evm_mine", []); };
const receipt = async (txp, what) => {
  const r = await (await txp).wait();
  if (!r || r.status !== 1) fail(`${what}: reverted`);
  return r;
};

/** Execute one Safe Transaction Builder batch from an impersonated stand-in Safe (fork only), in order. */
async function runBatch(file, from) {
  const b = JSON.parse(fs.readFileSync(file, "utf8"));
  ok(String(b.chainId) === "4663", `${path.basename(file)} targets chain 4663`);
  const safe = new JsonRpcSigner(provider, from);
  for (const [i, t] of b.transactions.entries()) {
    await receipt(safe.sendTransaction({ to: t.to, data: t.data, value: BigInt(t.value) }), `${path.basename(file)} tx ${i} (${t.description})`);
  }
  console.log(`executed ${path.basename(file)} from ${from}: ${b.transactions.length} transactions`);
  return b;
}

/** safe-launch-batch.mjs config from the deployments file; batches land in <TMP>/launch/out/. */
async function generateBatches(extra = {}) {
  const helper = new Contract(HELPER, abi("MilordAtomicLaunch"), provider);
  const hc = await helper.config();
  const owned = { teamSafe: [], arbitrageSafe: [] };
  for (const [k, v] of Object.entries(D)) {
    if (typeof v !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(v) || (await provider.getCode(v)) === "0x") continue;
    if (["owner", "teamSafe", "arbitrageSafe", "USDG", "WETH", "WethUsdgPool", "FundingVenue", "TwapReference"].includes(k)) continue;
    try {
      const p = getAddress(await new Contract(v, ["function pendingOwner() view returns (address)"], provider).pendingOwner());
      if (p === TEAM && !owned.teamSafe.includes(v)) owned.teamSafe.push(v);
      if (p === ARB && !owned.arbitrageSafe.includes(v)) owned.arbitrageSafe.push(v);
    } catch {} // not Ownable2Step
  }
  const cfg = {
    chainId: 4663,
    atomicLaunch: {
      helper: HELPER, predictedToken: await helper.predictedToken(), launchFeeWei: String(await helper.launchFee()),
      maxLaunchFeeWei: String(hc.maxLaunchFee), lockBlocks: Number(hc.lockBlocks), antiSnipe: "off",
    },
    hookr: { launcher: hc.launcher, familyLock: D.HookrFamilyLock ?? "0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b", root: hc.root, rules: hc.rules, familyId: D.MilordFamilyId ?? null },
    poolManager: D.PoolManager ?? null,
    tradingTaxSplitter: D.TradingTaxSplitter, lockFeeCollector: COLLECTOR ?? null, buybackBurner: D.BuybackBurner,
    v4SwapAdapter: ADAPTER ?? null, arbitrageFundingRouter: D.ArbitrageFundingRouter,
    ownable2StepContracts: owned,
    deadline: (await now()) + 3600,
    ...extra,
  };
  const dir = path.join(TMP, "launch");
  fs.mkdirSync(dir, { recursive: true });
  const cfgPath = path.join(dir, "robinhood-launch.config.json");
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  execFileSync(process.execPath, [path.join(root, "offchain/safe-launch-batch.mjs"), cfgPath], { cwd: root, stdio: "inherit" });
  return { dir: path.join(dir, "out"), owned };
}

const summary = fs.existsSync(path.join(TMP, "summary.json")) ? JSON.parse(fs.readFileSync(path.join(TMP, "summary.json"), "utf8")) : { checks: [] };
const save = () => fs.writeFileSync(path.join(TMP, "summary.json"), JSON.stringify({ ...summary, checks: [...summary.checks, ...checks] }, (_, v) => (typeof v === "bigint" ? v.toString() : v), 2));
process.on("exit", save);

if (mode === "launch") {
  // ── 1. Safe ownership, then the atomic launch ──
  const { dir, owned } = await generateBatches();
  ok(owned.teamSafe.length >= 9, "Team Safe has pending ownership of every Ownable2Step contract", owned.teamSafe.length);
  ok(owned.arbitrageSafe.length === 1 && getAddress(owned.arbitrageSafe[0]) === getAddress(D.ArbitrageFundingRouter), "Arbitrage Safe has pending ownership of the router only");
  await runBatch(path.join(dir, "safe-team-setup.json"), TEAM);
  await runBatch(path.join(dir, "safe-arbitrage-setup.json"), ARB);
  for (const a of owned.teamSafe) ok(getAddress(await new Contract(a, ["function owner() view returns (address)"], provider).owner()) === TEAM, `owner() = Team Safe at ${a}`);
  ok(getAddress(await new Contract(D.ArbitrageFundingRouter, ["function owner() view returns (address)"], provider).owner()) === ARB, "router owner() = Arbitrage Safe");
  ok(getAddress(await new Contract(D.pARBVault, ["function owner() view returns (address)"], provider).owner()) === getAddress(D.owner), "vault owner() = owner Safe");

  const milord = new Contract(D.MILORD, abi("MILORDToken"), provider);
  ok((await provider.getCode(D.MILORD)) === "0x", "MILORD has no code before the launch (no pre-emption window)");
  await runBatch(path.join(dir, "safe-1-launch.json"), TEAM);
  const helper = new Contract(HELPER, abi("MilordAtomicLaunch"), provider);
  ok(await helper.launched(), "MilordAtomicLaunch.launched()");
  ok(getAddress(await helper.token()) === getAddress(D.MILORD), "launched token = predicted MILORD the protocol was deployed against");
  const supply = await milord.totalSupply();
  ok(supply > parseEther("6766999999") && supply <= parseEther("6767000000"), "supply 6,767,000,000 MILORD less rounding dust", formatEther(supply));
  const pm = await new Contract("0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4", ["function poolManager() view returns (address)"], provider).poolManager();
  ok((await milord.balanceOf(pm)) === supply, "every MILORD is in the PoolManager (founding position)");
  ok((await milord.balanceOf(TEAM)) === 0n && (await milord.balanceOf(HELPER)) === 0n, "no MILORD held by the Team Safe or the helper");

  // ── 2. record-launch.mjs with the official StateView: must be complete (exit 0) ──
  const r = spawnSync(process.execPath, [path.join(root, "offchain/record-launch.mjs"), depFile, HELPER, STATE_VIEW], { cwd: root, env: { ...process.env, RPC_URL: RPC }, encoding: "utf8" });
  process.stdout.write(r.stdout ?? "");
  process.stderr.write(r.stderr ?? "");
  ok(r.status === 0, "record-launch.mjs: launch validated and service configuration complete (exit 0)", `exit ${r.status}`);
  D = readD();
  ok(D.launchValidation?.complete === true && D.lpFee === 8388608 && D.tickSpacing === 60, "deployments carry the recorded launch (dynamic-fee key, tick spacing 60)");
  summary.launch = { MILORD: D.MILORD, familyId: D.MilordFamilyId, poolId: D.MilordPoolId, buyQueue: D.HookrBuyTaxQueue, sellQueue: D.HookrSellTaxQueue, principalLockedUntil: D.principalLockedUntil, supply: formatEther(supply) };
  console.log("LAUNCH PHASE PASSED");
  process.exit(0);
}

// ════════════════════════════ operate ════════════════════════════
if (!ADAPTER || !COLLECTOR) fail("operate needs <V4SwapAdapter> <HookrLockFeeCollector>");
D = { ...readD(), V4SwapAdapter: getAddress(ADAPTER), LockFeeCollector: getAddress(COLLECTOR) };
writeD(D);
const LOCK = D.HookrFamilyLock;
const launcher = new Contract(D.HookrLauncher, [
  "function familyOwner(bytes32) view returns (address)",
  "function position(bytes32,uint8) view returns (tuple(tuple(address currency0,address currency1,uint24 fee,int24 tickSpacing,address hooks) key,int24 tickLower,int24 tickUpper,uint128 liquidity))",
], provider);
const lock = new Contract(LOCK, ["function lockOf(bytes32) view returns (address beneficiary,uint64 unlockBlock)"], provider);
const milord = new Contract(D.MILORD, abi("MILORDToken"), provider);
const burner = new Contract(D.BuybackBurner, abi("BuybackBurner"), provider);
const taxSplit = new Contract(D.TradingTaxSplitter, abi("RevenueSplitter"), provider);
const entrySplit = new Contract(D.EntryFeeSplitter, abi("RevenueSplitter"), provider);
const router = new Contract(D.ArbitrageFundingRouter, abi("ArbitrageFundingRouter"), provider);
const adapter = new Contract(D.ParbFundingAdapter, abi("ParbFundingAdapter"), provider);
const vault = new Contract(D.pARBVault, abi("PMFIArbVaultV3"), provider);
const dist = new Contract(D.ShareDistributor, abi("ShareDistributor"), provider);
const nft = new Contract(D.Milords, abi("MilordsNFT"), provider);
const usdg = new Contract(D.USDG, ["function balanceOf(address) view returns (uint256)"], provider);
const collector = new Contract(COLLECTOR, abi("HookrLockFeeCollector"), provider);
const swap = new Contract(ADAPTER, abi("V4SwapAdapter"), provider);

// ── 3. accept the family, set the buyback route, permanent lock (fork only) ──
{
  const { dir } = await generateBatches();
  await runBatch(path.join(dir, "safe-2-accept.json"), TEAM);
  ok(getAddress(await launcher.familyOwner(D.MilordFamilyId)) === TEAM, "Team Safe owns the MILORD family");
  ok(getAddress(await burner.adapter()) === getAddress(ADAPTER), "BuybackBurner routes through V4SwapAdapter");
  const b3 = JSON.parse(fs.readFileSync(path.join(dir, "safe-3-lock-DO-NOT-EXECUTE.json"), "utf8"));
  ok(b3.irreversible === true && b3.executed === false && b3.transactions.length === 2, "lock batch is marked irreversible and not executed");
  await runBatch(path.join(dir, "safe-3-lock-DO-NOT-EXECUTE.json"), TEAM); // PRIVATE FORK ONLY
  const l = await lock.lockOf(D.MilordFamilyId);
  ok(getAddress(l.beneficiary) === getAddress(COLLECTOR) && BigInt(l.unlockBlock) === FOREVER, "HookrFamilyLock: beneficiary = HookrLockFeeCollector, unlockBlock = forever");
  ok(getAddress(await launcher.familyOwner(D.MilordFamilyId)) === getAddress(LOCK), "HookrFamilyLock owns the family");
}

// ── 4. trading through the Hookr pool (Anvil #7) ──
const trader = new NonceManager(new Wallet(KEYS.trader, provider));
const traderAddr = await trader.getAddress();
await provider.send("anvil_setBalance", [traderAddr, "0x" + parseEther("1000").toString(16)]);
const pos = await launcher.position(D.MilordFamilyId, 0);
const key = { currency0: pos.key.currency0, currency1: pos.key.currency1, fee: pos.key.fee, tickSpacing: pos.key.tickSpacing, hooks: pos.key.hooks };
const ra = art("RehearsalTrader");
const seller = await new ContractFactory(ra.abi, ra.bytecode.object, trader).deploy(D.PoolManager);
await seller.waitForDeployment();
const supply0 = await milord.totalSupply();
const t0 = await now();
const buys = [1, 2, 5, 10, 5].map((x) => parseEther(String(x)));
for (const eth of buys) {
  await receipt(swap.connect(trader).swapExactETHForToken(D.MILORD, 1n, traderAddr, (await now()) + 600, { value: eth }), `buy ${formatEther(eth)} ETH`);
  await mine(120);
}
const held = await milord.balanceOf(traderAddr);
ok(held > 0n, "buys through V4SwapAdapter delivered MILORD", formatEther(held));
await receipt(milord.connect(trader).approve(await seller.getAddress(), held / 10n), "approve sell");
const ethBefore = await provider.getBalance(traderAddr);
await receipt(seller.connect(trader).sell(key, held / 10n, traderAddr), "sell 10% of holdings");
ok((await provider.getBalance(traderAddr)) > ethBefore - parseEther("0.01"), "sell paid ETH back");
// small trades for 35 minutes so the 30-minute TWAP converges on spot (the buyback and the quote refuse >10% gaps)
for (let i = 0; i < 7; i++) {
  await mine(300);
  await receipt(swap.connect(trader).swapExactETHForToken(D.MILORD, 1n, traderAddr, (await now()) + 600, { value: parseEther("0.01") }), "small buy");
}
summary.trading = { buysEth: buys.map(formatEther), sold: formatEther(held / 10n), minutes: Math.round(((await now()) - t0) / 60) };

// ── 5. keeper pass with the services' own code (offchain/services/lib) ──
process.env.KEEPER_PRIVATE_KEY = KEYS.keeper;
process.env.ALLOW_MAINNET = "yes"; // services' 4663 guard; this process verified a loopback Anvil fork above
const { loadConfig, connect, resolveTaxQueues } = await import("../offchain/services/lib/config.mjs");
const { keeperTick, readInputs, publishEpoch, checkRoot } = await import("../offchain/services/lib/jobs.mjs");
const { entryPriceSource } = await import("../offchain/lib/entry-price.mjs");
const { issueQuote } = await import("../offchain/lib/quote.mjs");
const { signReport } = await import("../offchain/lib/report.mjs");
const { proofFor } = await import("../offchain/lib/eligibility.mjs");
const cfg = loadConfig({ ...process.env, DEPLOYMENTS_FILE: depFile, RPC_URL: RPC, CHAIN_ID: "4663" });
ok((await resolveTaxQueues(cfg.D, provider)) !== null, "tax queues agree with HookrDirectionalTax");
const svc = connect(cfg, "KEEPER_PRIVATE_KEY");
let chainNow = await now();
const price = entryPriceSource(svc.provider, cfg.D, { maxHeadLag: Infinity, clock: () => chainNow });
const keeperOpts = {
  now: async () => (chainNow = await now()),
  taxQueue: { expect: { assetRecipient: D.TradingTaxSplitter, protocolShareBps: 2000 }, minPayoutWei: 0n },
  buyback: {
    minBudgetWei: cfg.minBuybackWei,
    quoteMinOut: async (budget) => {
      chainNow = await now();
      const { weiPerMillion } = await price.price();
      return ((budget * 10n ** 24n) / weiPerMillion) * BigInt(10_000 - cfg.buybackMaxBelowTwapBps) / 10_000n;
    },
  },
  alerter: { alert: async (level, k, text) => console.log(`alert ${level} ${k}: ${text}`) },
};
const keeperLog = [];
async function keeper(label, mustDo = []) {
  const r = await keeperTick(svc.contracts, keeperOpts);
  keeperLog.push({ label, steps: r.results.map((x) => `${x.step}:${x.status}${x.error ? ` (${x.error})` : ""}`) });
  console.log(`keeper ${label}: ${keeperLog.at(-1).steps.join(", ")}`);
  ok(r.failed === 0, `keeper ${label}: no failed steps`);
  for (const s of mustDo) ok(r.results.some((x) => x.step === s && x.status === "done"), `keeper ${label}: ${s} done`);
  return r;
}
const teamBefore = await provider.getBalance(TEAM);
await keeper("pass 1", ["hookr-tax-buy", "hookr-tax-sell", "lp-fees-0", "buyback", "router"]);
const taxed = await taxSplit.totalDistributed(ZERO);
ok(taxed > 0n, "TradingTaxSplitter received and distributed the 80% creator share", formatEther(taxed));
const third = taxed / 3n; // payees 3334 / 3333 / 3333 bps
for (const [who, name] of [[TEAM, "Team Safe"], [D.BuybackBurner, "BuybackBurner"], [D.ArbitrageFundingRouter, "router"]]) {
  const got = await taxSplit.totalAllocated(ZERO, who);
  ok(got >= third - taxed / 5000n && got <= third + taxed / 5000n, `tax third to ${name}`, formatEther(got));
}
ok((await provider.getBalance(TEAM)) > teamBefore, "Team Safe received ETH");
ok((await collector.totalEthForwarded()) > 0n || (await collector.totalMilordBurned()) > 0n, "LP fees collected from the locked position", `${formatEther(await collector.totalEthForwarded())} ETH, ${formatEther(await collector.totalMilordBurned())} MILORD burned`);
ok((await burner.totalBurnedFromBuybacks()) > 0n, "buyback burned MILORD", formatEther(await burner.totalBurnedFromBuybacks()));
ok((await milord.totalSupply()) < supply0, "MILORD supply fell");
ok((await router.totalEthSwapped()) > 0n && (await router.totalAssetFunded()) > 0n, "router swapped ETH -> USDG on the real WETH/USDG pool", `${formatEther(await router.totalEthSwapped())} ETH -> ${formatUnits(await router.totalAssetFunded(), 6)} USDG`);
ok((await adapter.batchCount()) === 1n, "one funding batch requested from the vault");

// ── 6. NFT conversion with a TWAP-signed quote (Anvil #2 signs) ──
const genesis = await now(); // epoch 1 starts here, before the first NFT exists
const quoteSigner = new Wallet(KEYS.quoteSigner, provider);
const conv = new Contract(D.MilordConverter, abi("MilordConverter"), trader);
await receipt(milord.connect(trader).approve(D.MilordConverter, parseEther("1000000")), "approve converter");
let conversion;
for (let attempt = 0; !conversion; attempt++) {
  chainNow = await now();
  try {
    const { quote, signature } = await issueQuote({
      signer: quoteSigner, chainId: 4663, converter: D.MilordConverter, account: traderAddr,
      priceSource: () => price.price(), now: chainNow, maxPriceAge: 120, randomNonce: hexlify(randomBytes(32)),
    });
    const fee = await conv.entryFeeFor(quote.weiPerMillion);
    const expect = (quote.weiPerMillion * 1000n) / 10_000n > parseEther("0.002") ? (quote.weiPerMillion * 1000n) / 10_000n : parseEther("0.002");
    ok(fee === expect, "entry fee = max(0.002 ETH, 10% of the value of 1M MILORD)", `${formatEther(fee)} ETH at ${formatEther(quote.weiPerMillion)} ETH per 1M`);
    const r = await receipt(conv.convert(quote, signature, fee, { value: fee }), "convert");
    const ev = r.logs.map((l) => { try { return conv.interface.parseLog(l); } catch { return null; } }).find((e) => e?.name === "Converted");
    conversion = { tokenId: Number(ev.args.tokenId), fee, weiPerMillion: quote.weiPerMillion, minimumApplied: fee === parseEther("0.002") };
  } catch (e) {
    if (!/entry waits/.test(String(e?.message)) || attempt >= 6) throw e;
    console.log(`quote refused (${e.message}); waiting 10 minutes for the TWAP`);
    await mine(600);
  }
}
ok(getAddress(await nft.ownerOf(conversion.tokenId)) === getAddress(traderAddr), "NFT issued to the converter's caller", `#${conversion.tokenId}`);
summary.conversion = conversion;

// ── 7. signed vault report (Anvil #3) settles the deposit; keeper mints pARB to the distributor ──
const reportSigner = new Wallet(KEYS.reportSigner, provider);
const t = await now();
const data = {
  reportedAssets: (await usdg.balanceOf(D.pARBVault)) + (await usdg.balanceOf(D.servicer)),
  timestamp: t, deadline: t + 600, nonce: Number(await vault.lastReportNonce()) + 1,
};
const sig = await signReport(reportSigner, { ...data, vault: D.pARBVault, chainId: 4663 });
await receipt(vault.connect(new Wallet(KEYS.deployer, provider)).report(data, sig, 0, 0), "vault report");
await mine(60);
await keeper("pass 2", ["settle", "split-0"]);
const entryTotal = await entrySplit.totalDistributed(ZERO);
ok(entryTotal === conversion.fee, "entry fee distributed by EntryFeeSplitter", formatEther(entryTotal));
ok((await entrySplit.totalAllocated(ZERO, D.ArbitrageFundingRouter)) === (entryTotal * 7000n) / 10_000n, "entry fee 70% to the arbitrage router, 30% to the Team Safe");
const parbHeld = await vault.balanceOf(D.ShareDistributor);
ok(parbHeld > 0n, "pARB minted to ShareDistributor", formatEther(parbHeld));

// ── 8. weekly root: publisher (Anvil #5), independent checker, activation by the keeper, claim ──
await provider.send("evm_setNextBlockTimestamp", [genesis + 7 * 86400 + cfg.settlementGrace + 60]);
await provider.send("evm_mine", []);
const toBlock = await provider.getBlockNumber();
const inputs = await readInputs({ nft: svc.contracts.nft, adapter: svc.contracts.adapter, provider, fromBlock: Number(D.startBlock ?? 0), toBlock, logStep: cfg.logStep });
let epoch = null;
const publisher = new NonceManager(new Wallet(KEYS.publisher, provider));
const pub = await publishEpoch({ distributor: dist.connect(publisher), inputs, genesis, now: await now(), settlementGrace: cfg.settlementGrace, excluded: [D.MilordConverter], save: (e) => { epoch = e; } });
ok(pub.action === "proposed" && pub.epoch === 1, "publisher proposed the epoch 1 root", pub.reason ?? pub.root);
const chk = await checkRoot({ distributor: dist, parb: vault, inputs, genesis, settlementGrace: cfg.settlementGrace, excluded: [D.MilordConverter] });
ok(chk.length > 0 && chk.every((x) => x.match), "independent checker recomputes the same root");
await mine(Number(await dist.activationDelay()) + 1);
await keeper("pass 3", ["activate-root"]);
const p = proofFor(epoch.tree, traderAddr);
ok(p && p.cumulative > 0n, "the NFT holder has a leaf", p && formatEther(p.cumulative));
const parbBefore = await vault.balanceOf(traderAddr);
await receipt(dist.connect(trader).claim(traderAddr, p.cumulative, p.proof), "claim");
ok((await vault.balanceOf(traderAddr)) - parbBefore === p.cumulative, "holder claimed exactly its cumulative pARB", formatEther(p.cumulative));
let replay = false;
try { await (await dist.connect(trader).claim(traderAddr, p.cumulative, p.proof)).wait(); replay = true; } catch {}
ok(!replay, "a repeated claim pays nothing");

summary.operate = {
  keeper: keeperLog,
  taxDistributedEth: formatEther(taxed),
  lpFeesEth: formatEther(await collector.totalEthForwarded()), lpFeesMilordBurned: formatEther(await collector.totalMilordBurned()),
  buybackBurned: formatEther(await burner.totalBurnedFromBuybacks()), supplyAfter: formatEther(await milord.totalSupply()),
  routerEthSwapped: formatEther(await router.totalEthSwapped()), usdgFunded: formatUnits(await router.totalAssetFunded(), 6),
  parbMinted: formatEther(parbHeld), epoch: { root: pub.root, total: formatEther(pub.total) }, claimed: formatEther(p.cumulative),
};
console.log("OPERATE PHASE PASSED");
process.exit(0);
