// RPC_URL=<rpc> node offchain/record-launch.mjs <deployments.json> <MilordAtomicLaunch address> [StateView address]
//
// READ-ONLY (eth_call only, no key): after the Team Safe's launch transaction, reads the result from the
// MilordAtomicLaunch helper and Hookr, cross-checks it, and adds the keys the services need to the deployments file
// written by DeployRobinhood.s.sol: MILORD (the launched token), HookrLauncher, HookrRoot, HookrDirectionalTax,
// HookrFamilyLock, MilordFamilyId, MilordPoolId, HookrBuyTaxQueue, HookrSellTaxQueue, PoolManager, StateView, lpFee,
// tickSpacing, principalLockedUntil, guardEndBlock, AtomicLaunch. Refuses on any disagreement (nothing written).
import fs from "node:fs";
import { JsonRpcProvider, Contract, AbiCoder, keccak256, getAddress } from "ethers";

const [file, helperAddr, stateViewArg] = process.argv.slice(2);
if (!process.env.RPC_URL || !file || !helperAddr) throw new Error("usage: RPC_URL=... node offchain/record-launch.mjs <deployments.json> <MilordAtomicLaunch> [StateView]");
const D = JSON.parse(fs.readFileSync(file, "utf8"));
const p = new JsonRpcProvider(process.env.RPC_URL);
const chainId = Number((await p.getNetwork()).chainId);
if (D.chainId !== undefined && Number(D.chainId) !== chainId) throw new Error(`deployments file is for chain ${D.chainId}, RPC is chain ${chainId}`);

const KEY = "tuple(address currency0,address currency1,uint24 fee,int24 tickSpacing,address hooks)";
const helper = new Contract(helperAddr, [
  "function launched() view returns (bool)", "function token() view returns (address)", "function familyId() view returns (bytes32)",
  "function poolId() view returns (bytes32)", "function buyQueue() view returns (address)", "function sellQueue() view returns (address)",
  "function guardEndBlock() view returns (uint256)", "function lockEndBlock() view returns (uint256)",
  "function config() view returns (tuple(address launcher,address root,address rules,address tax,address owner,bytes32 salt,uint256 maxLaunchFee,uint32 lockBlocks,uint24 snipePips,uint16 snipeSeconds,uint32 guardBlocks))",
], p);
if (!(await helper.launched())) throw new Error("MilordAtomicLaunch has not launched yet; nothing recorded");
const [token, familyId, poolId, buyQueue, sellQueue, guardEnd, cfg] = await Promise.all([
  helper.token(), helper.familyId(), helper.poolId(), helper.buyQueue(), helper.sellQueue(), helper.guardEndBlock(), helper.config(),
]);
const launcher = new Contract(cfg.launcher, [
  `function position(bytes32,uint8) view returns (tuple(${KEY} key,int24 tickLower,int24 tickUpper,uint128 liquidity))`,
  "function poolManager() view returns (address)", "function principalLockedUntil(bytes32) view returns (uint256)", "function familyOwner(bytes32) view returns (address)",
], p);
const [pos, pm, lockedUntil, owner] = await Promise.all([launcher.position(familyId, 0), launcher.poolManager(), launcher.principalLockedUntil(familyId), launcher.familyOwner(familyId)]);
const k = pos.key;
const id = keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], [k.currency0, k.currency1, k.fee, k.tickSpacing, k.hooks]));
const errs = [];
if (id !== poolId) errs.push(`pool id ${poolId} != key hash ${id}`);
if (getAddress(k.currency1) !== getAddress(token)) errs.push(`pool currency1 ${k.currency1} is not the launched token ${token}`);
if (getAddress(k.hooks) !== getAddress(cfg.root)) errs.push(`pool hook ${k.hooks} is not HookrRoot ${cfg.root}`);
if (D.MILORD && getAddress(D.MILORD) !== getAddress(token)) errs.push(`deployments MILORD ${D.MILORD} != launched token ${token} (protocol deployed against another predicted address)`);
const dt = new Contract(cfg.tax, ["function terms(address,bytes32) view returns (tuple(address buyQueue,uint24 buyTaxPips,bool bound,bool tiered,bool decaying,address sellQueue,uint24 sellTaxPips))"], p);
const t = await dt.terms(cfg.root, poolId);
if (!t.bound || getAddress(t.buyQueue) !== getAddress(buyQueue) || getAddress(t.sellQueue) !== getAddress(sellQueue)) errs.push("HookrDirectionalTax terms disagree with the helper's queues");
if (Number(t.buyTaxPips) !== 15000 || Number(t.sellTaxPips) !== 15000) errs.push(`tax ${t.buyTaxPips}/${t.sellTaxPips} pips, expected 15000/15000`);
const stateView = stateViewArg ?? D.StateView;
const missing = [];
if (!stateView) missing.push("StateView (argument 3): the quote server refuses to start and the keeper skips buybacks without it");
else {
  const sv = new Contract(stateView, ["function getSlot0(bytes32) view returns (uint160,int24,uint24,uint24)"], p);
  try { await sv.getSlot0(poolId); } catch (e) { errs.push(`StateView ${stateView} cannot read the pool: ${e.shortMessage ?? e.message}`); }
}
if (errs.length) {
  console.error(JSON.stringify({ refused: errs }, null, 2));
  process.exit(1);
}
const added = {
  MILORD: getAddress(token), AtomicLaunch: getAddress(helperAddr), HookrLauncher: cfg.launcher, HookrRoot: cfg.root, HookrRules: cfg.rules,
  HookrDirectionalTax: cfg.tax, HookrFamilyLock: D.HookrFamilyLock ?? "0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b",
  MilordFamilyId: familyId, MilordPoolId: poolId, HookrBuyTaxQueue: buyQueue, HookrSellTaxQueue: sellQueue,
  PoolManager: pm, StateView: stateView, lpFee: Number(k.fee), tickSpacing: Number(k.tickSpacing),
  principalLockedUntil: Number(lockedUntil), guardEndBlock: Number(guardEnd), familyOwnerAtRecord: owner,
};
fs.writeFileSync(file, JSON.stringify({ ...D, ...added }, null, 2) + "\n");
if (!stateView) delete added.StateView;
console.log(JSON.stringify({ recorded: file, ...added, ...(missing.length ? { missing } : {}) }, null, 2));
