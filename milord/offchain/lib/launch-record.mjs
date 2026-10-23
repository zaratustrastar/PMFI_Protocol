import { AbiCoder, keccak256, getAddress } from "ethers";
export async function validateLaunch(D, helperAddr, stateViewArg, {contract,code}) {
const KEY = "tuple(address currency0,address currency1,uint24 fee,int24 tickSpacing,address hooks)";
const helper = contract(helperAddr, [
  "function predictedToken() view returns (address)",
  "function launched() view returns (bool)", "function token() view returns (address)", "function familyId() view returns (bytes32)",
  "function poolId() view returns (bytes32)", "function buyQueue() view returns (address)", "function sellQueue() view returns (address)",
  "function guardEndBlock() view returns (uint256)", "function lockEndBlock() view returns (uint256)",
  "function config() view returns (tuple(address launcher,address root,address rules,address tax,address owner,bytes32 salt,uint256 maxLaunchFee,uint32 lockBlocks,uint24 snipePips,uint16 snipeSeconds,uint32 guardBlocks))",
]);
if (!(await helper.launched())) throw new Error("MilordAtomicLaunch has not launched yet; nothing recorded");
const [token, familyId, poolId, buyQueue, sellQueue, guardEnd, cfg] = await Promise.all([
  helper.token(), helper.familyId(), helper.poolId(), helper.buyQueue(), helper.sellQueue(), helper.guardEndBlock(), helper.config(),
]);
const launcher = contract(cfg.launcher, [
  `function position(bytes32,uint8) view returns (tuple(${KEY} key,int24 tickLower,int24 tickUpper,uint128 liquidity))`,
  "function poolManager() view returns (address)", "function principalLockedUntil(bytes32) view returns (uint256)", "function familyOwner(bytes32) view returns (address)",
]);
const [pos, pm, lockedUntil, owner] = await Promise.all([launcher.position(familyId, 0), launcher.poolManager(), launcher.principalLockedUntil(familyId), launcher.familyOwner(familyId)]);
const k = pos.key;
const id = keccak256(AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], [k.currency0, k.currency1, k.fee, k.tickSpacing, k.hooks]));
const errs = [];
const zero = '0x' + '0'.repeat(40);
if (getAddress(await helper.predictedToken()) !== getAddress(token)) errs.push('token differs from predicted address');
if (Number(k.fee) !== 8388608 || k.currency0 !== zero || Number(k.tickSpacing) !== 60 || Number(pos.tickLower) !== 134220 || Number(pos.tickUpper) !== 203280 || pos.liquidity <= 0n) errs.push('unexpected pool position');
if (lockedUntil < await helper.lockEndBlock()) errs.push('principal lock shorter than helper lock');
if (![helperAddr,cfg.owner,D.HookrFamilyLock].filter(Boolean).some(a=>getAddress(a) === getAddress(owner))) errs.push('unexpected family owner');
if (getAddress(buyQueue) === getAddress(sellQueue)) errs.push('queues must be distinct');
for (const a of [helperAddr,token,cfg.launcher,cfg.root,cfg.tax,cfg.rules,pm,buyQueue,sellQueue]) {
  if (getAddress(a) === zero || await code(a) === '0x') errs.push(`missing contract code: ${a}`);
}
if (id !== poolId) errs.push(`pool id ${poolId} != key hash ${id}`);
if (getAddress(k.currency1) !== getAddress(token)) errs.push(`pool currency1 ${k.currency1} is not the launched token ${token}`);
if (getAddress(k.hooks) !== getAddress(cfg.root)) errs.push(`pool hook ${k.hooks} is not HookrRoot ${cfg.root}`);
if (D.MILORD && getAddress(D.MILORD) !== getAddress(token)) errs.push(`deployments MILORD ${D.MILORD} != launched token ${token} (protocol deployed against another predicted address)`);
const dt = contract(cfg.tax, ["function terms(address,bytes32) view returns (tuple(address buyQueue,uint24 buyTaxPips,bool bound,bool tiered,bool decaying,address sellQueue,uint24 sellTaxPips))"]);
const t = await dt.terms(cfg.root, poolId);
if (!t.bound || t.tiered || t.decaying || getAddress(t.buyQueue) !== getAddress(buyQueue) || getAddress(t.sellQueue) !== getAddress(sellQueue)) errs.push("HookrDirectionalTax terms disagree with the helper's queues");
if (Number(t.buyTaxPips) !== 15000 || Number(t.sellTaxPips) !== 15000) errs.push(`tax ${t.buyTaxPips}/${t.sellTaxPips} pips, expected 15000/15000`);
for (const [address,isBuy] of [[buyQueue,true],[sellQueue,false]]) {
  const q = await contract(address,['function terms() view returns (tuple(address root,address rules,bytes32 poolId,address quote,bool isBuy,address protocolRecipient,uint16 protocolShareBps,bytes32 routeId,address assetRecipient,address recoveryRecipient,uint32 staleRecoveryDelay))']).terms();
  if (getAddress(q.root) !== getAddress(cfg.root) || getAddress(q.rules) !== getAddress(cfg.rules) || q.poolId !== poolId || q.quote !== zero || q.isBuy !== isBuy || Number(q.protocolShareBps) !== 2000 || q.routeId !== '0x'+'0'.repeat(64)) errs.push('unexpected tax queue terms');
  if (D.TradingTaxSplitter && getAddress(q.assetRecipient) !== getAddress(D.TradingTaxSplitter)) errs.push('wrong tax recipient');
}
const stateView = stateViewArg || D.StateView;
const missing = [];
if (!stateView) missing.push("StateView (argument 3): the quote server refuses to start and the keeper skips buybacks without it");
else {
  const sv = contract(stateView, ["function getSlot0(bytes32) view returns (uint160,int24,uint24,uint24)"]);
  try {
    if (await code(stateView) === '0x') throw Error('no code');
    if (getAddress(await contract(stateView,['function poolManager() view returns(address)']).poolManager()) !== getAddress(pm)) throw Error('wrong PoolManager');
    const slot = await sv.getSlot0(poolId);
    if (slot[0] === 0n || Number(slot[3]) !== 3000) throw Error('uninitialized pool or unexpected LP fee');
  } catch (e) { errs.push(`StateView ${stateView} cannot read the pool: ${e.shortMessage ?? e.message}`); }
}
if (errs.length) {
  throw new Error(errs.join("; "));
}
const added = {
  MILORD: getAddress(token), AtomicLaunch: getAddress(helperAddr), HookrLauncher: cfg.launcher, HookrRoot: cfg.root, HookrRules: cfg.rules,
  HookrDirectionalTax: cfg.tax, HookrFamilyLock: D.HookrFamilyLock ?? "0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b",
  MilordFamilyId: familyId, MilordPoolId: poolId, HookrBuyTaxQueue: buyQueue, HookrSellTaxQueue: sellQueue,
  PoolManager: pm, StateView: stateView, lpFee: Number(k.fee), tickSpacing: Number(k.tickSpacing),
  principalLockedUntil: Number(lockedUntil), guardEndBlock: Number(guardEnd), familyOwnerAtRecord: owner,
};
const required = ['Milords','MilordConverter','EntryFeeSplitter','RoyaltySplitter','TradingTaxSplitter','BuybackBurner','pARBVault','ShareDistributor','ParbFundingAdapter','ArbitrageFundingRouter','USDG'];
for (const name of required) {
  if (!D[name]) missing.push(name);
  else if (getAddress(D[name]) === zero || await code(D[name]) === '0x') throw Error(`invalid service contract: ${name}`);
}
if (!Number.isSafeInteger(D.startBlock) || D.startBlock < 0) missing.push('startBlock');
return {added, validated:true, complete:missing.length === 0, missing};
}
