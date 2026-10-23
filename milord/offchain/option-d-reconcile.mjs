// node offchain/option-d-reconcile.mjs <config.json>   ->   <outDir>/option-d-split.json (Safe batch) + option-d-report.{json,md}
//
// Option D (Anti-Snipe candidate, not an approved launch setting): reconciles the Anti-Snipe window from on-chain
// events and encodes the Safe's split. READ-ONLY: it calls eth_getLogs / eth_getBalance / eth_call only, reads no
// keys, signs nothing and sends nothing. The Safe signers review the report and the batch.
//
// Config (JSON): { rpcUrl, chainId, poolManager, launcher, familyLock, helper, safe, teamSafe, burner, milord,
//   familyId, poolId, launchBlock, guardEndBlock, baseFeePips: 3000, outDir, maxBlockRange?: 10000 }
// The handover is the FamilyTransferred(familyId, HookrFamilyLock) event; the run refuses until it exists, until the
// block holding it is final, and while the helper still holds fees (run MilordAtomicLaunch.sweep() first).
import fs from "node:fs";
import path from "node:path";
import { JsonRpcProvider, Interface, id, getAddress, zeroPadValue } from "ethers";
import { reconcileOptionD, optionDBatch } from "./lib/option-d.mjs";

const cfg = JSON.parse(fs.readFileSync(process.argv[2] ?? "deploy/option-d.config.json", "utf8"));
const provider = new JsonRpcProvider(cfg.rpcUrl, cfg.chainId, { staticNetwork: true });
const pmI = new Interface(["event Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)"]);
const lI = new Interface([
  "event LPFeesCollected(bytes32 indexed familyId, uint8 member, bytes32 indexed poolId, address indexed owner, address recipient, uint256 amount0, uint256 amount1)",
  "event FamilyTransferred(bytes32 indexed familyId, address indexed owner)",
]);
const iface = { erc20: new Interface(["function transfer(address,uint256)", "function balanceOf(address) view returns (uint256)"]), burner: new Interface(["function burnHeldTokens()"]) };

async function logs(filter, from, to) {
  const out = [];
  const step = cfg.maxBlockRange ?? 10_000;
  for (let a = from; a <= to; a += step) out.push(...(await provider.getLogs({ ...filter, fromBlock: a, toBlock: Math.min(to, a + step - 1) })));
  return out;
}

const net = await provider.getNetwork();
if (Number(net.chainId) !== cfg.chainId) throw new Error(`RPC is chain ${net.chainId}, config says ${cfg.chainId}`);
const head = (await provider.getBlock("finalized").catch(() => null)) ?? (await provider.getBlock("latest"));
const to = head.number;

const handovers = (await logs({ address: cfg.launcher, topics: [id("FamilyTransferred(bytes32,address)"), cfg.familyId, zeroPadValue(cfg.familyLock, 32)] }, cfg.launchBlock, to));
if (handovers.length !== 1) throw new Error(`expected one finalized handover to HookrFamilyLock, found ${handovers.length}; nothing generated`);
const handover = { blockNumber: handovers[0].blockNumber, logIndex: handovers[0].index };
if (handover.blockNumber < cfg.guardEndBlock) throw new Error(`handover at block ${handover.blockNumber} is before the guard end ${cfg.guardEndBlock}: Snipe after it went to the collector`);

const helperEth = await provider.getBalance(cfg.helper);
const helperMilord = BigInt(await provider.call({ to: cfg.milord, data: iface.erc20.encodeFunctionData("balanceOf", [cfg.helper]) }));
if (helperEth !== 0n || helperMilord !== 0n) throw new Error(`MilordAtomicLaunch still holds ${helperEth} wei / ${helperMilord} MILORD: run sweep() first`);

const swaps = (await logs({ address: cfg.poolManager, topics: [id("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"), cfg.poolId] }, cfg.launchBlock, handover.blockNumber))
  .map((l) => { const e = pmI.parseLog(l).args; return { blockNumber: l.blockNumber, logIndex: l.index, txHash: l.transactionHash, amount0: e.amount0, amount1: e.amount1, fee: e.fee }; });
const collections = (await logs({ address: cfg.launcher, topics: [id("LPFeesCollected(bytes32,uint8,bytes32,address,address,uint256,uint256)"), cfg.familyId] }, cfg.launchBlock, handover.blockNumber))
  .map((l) => { const e = lI.parseLog(l).args; return { blockNumber: l.blockNumber, logIndex: l.index, txHash: l.transactionHash, recipient: e.recipient, amount0: e.amount0, amount1: e.amount1 }; });

const split = reconcileOptionD(swaps, collections, {
  baseFeePips: cfg.baseFeePips, guardEndBlock: cfg.guardEndBlock, handover, recipients: [cfg.helper, cfg.safe],
});
const safeEth = await provider.getBalance(cfg.safe);
const safeMilord = BigInt(await provider.call({ to: cfg.milord, data: iface.erc20.encodeFunctionData("balanceOf", [cfg.safe]) }));
if (safeEth < split.collectedEth || safeMilord < split.collectedMilord) throw new Error(`the Safe holds less than it collected (${safeEth} wei, ${safeMilord} MILORD): stop and investigate`);

const batch = optionDBatch({ chainId: cfg.chainId, safe: getAddress(cfg.safe), teamSafe: getAddress(cfg.teamSafe), burner: getAddress(cfg.burner), milord: getAddress(cfg.milord), split, iface });
const outDir = cfg.outDir ?? "deploy/out";
fs.mkdirSync(outDir, { recursive: true });
const json = (o) => JSON.stringify(o, (_, v) => (typeof v === "bigint" ? v.toString() : v), 2);
fs.writeFileSync(path.join(outDir, "option-d-split.json"), json(batch));
fs.writeFileSync(path.join(outDir, "option-d-report.json"), json({ generatedAtBlock: to, handover, ...split }));
const eth = (w) => (Number(w) / 1e18).toFixed(6);
fs.writeFileSync(path.join(outDir, "option-d-report.md"), [
  `# Option D window split (finalized block ${to})`, "",
  `Handover to HookrFamilyLock: block ${handover.blockNumber}. Guard end: block ${cfg.guardEndBlock}. ${split.swaps} swaps (${split.guardSwaps} in the guard).`, "",
  "| | wei | ETH |", "|---|---|---|",
  `| LP fees collected by the helper and the Safe | ${split.collectedEth} | ${eth(split.collectedEth)} |`,
  `| of which post-commission Snipe LP part | ${split.surcharge} | ${eth(split.surcharge)} |`,
  `| Team Safe (10% of the Snipe LP part) | ${split.toTeam} | ${eth(split.toTeam)} |`,
  `| BuybackBurner (the rest) | ${split.toBurner} | ${eth(split.toBurner)} |`,
  `| MILORD LP fees to burn | ${split.milordToBurn} | |`, "",
  "Checks:", ...split.checks.map((c) => `- ${c.ok ? "OK" : "FAILED"}: ${c.what}`), "",
  "| block | tx | side | pool input | fee pips | LP fee | Snipe LP part |", "|---|---|---|---|---|---|---|",
  ...split.rows.map((r) => `| ${r.block} | ${r.tx} | ${r.side} | ${r.poolIn} | ${r.fee} | ${r.lpFee} | ${r.snipeLp} |`),
].join("\n") + "\n");
console.log(`wrote ${outDir}/option-d-split.json and option-d-report.{json,md}: team ${split.toTeam} wei, burner ${split.toBurner} wei, burn ${split.milordToBurn} MILORD`);
