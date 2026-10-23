// MILORD-only Polymarket redemption transport (B21). No order submission, no wallet creation, no USDG route.
// prepare/observe are read-only; submit is disabled unless route verification is explicitly configured.
//
// Polymarket collateral is pUSD. Resolved positions are redeemed through the contract that owns them
// (docs.polymarket.com/trading/positions/manage; sources: Polymarket/ctf-exchange-v2 src/adapters,
// Polymarket/polymarket-v2-external src/routers/Router.sol, src/modules):
//   ctf          CTF tokens collateralised by USDC.e   -> CtfCollateralAdapter.redeemPositions
//   ctf_negrisk  CTF tokens collateralised by the NegRiskAdapter's wrapped collateral
//                                                       -> NegRiskCtfCollateralAdapter.redeemPositions
//   v2           PositionManager tokens (Protocol V2)  -> Router.redeem(bytes31 conditionId, outcomeIndex, amount)
// The two CTF adapters pull the caller's WHOLE balance of both outcomes and pay pUSD; Router.redeem burns an
// exact amount of one outcome and mints pUSD. Position ids, condition ids, methods and events differ by system,
// so the system is declared per pair and every id is re-derived on chain before anything is sent.
import {JsonRpcProvider,Contract,Interface,getAddress,ZeroHash,ZeroAddress} from 'ethers';
import fs from 'node:fs';

export const ADDR={
 ctf:getAddress('0x4d97dcd97ec945f40cf65f87097ace5ea0476045'),
 pusd:getAddress('0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb'),
 usdce:getAddress('0x2791bca1f2de4661ed88a30c99a7a9449aa84174'),
 ctfAdapter:getAddress('0xada100db00ca00073811820692005400218fce1f'),
 negRiskCtfAdapter:getAddress('0xada2005600dec949baf300f4c6120000bdb6eaab'),
 negRiskAdapter:getAddress('0xd91e80cf2e7be2e162c6513ced06f1dd0da35296'),
 positionManager:getAddress('0x006f54f7f9a22e0000cc2ab60031000000ae9fef'),
 router:getAddress('0x12121212006e4cd160d18e3f00711da5c3372600'),
 binaryModule:getAddress('0x1000008dd9001b968442c1000017eae6e0da00ba'),
 negRiskModule:getAddress('0x200000900045e3b6259600682756002200028933'),
};
export const SYSTEMS=['ctf','ctf_negrisk','v2'];
const V2_MODULES={1:ADDR.binaryModule,2:ADDR.negRiskModule}; // 3 = combinatorial: not a binary pair, refused

export const ABI={
 ctf:['function payoutDenominator(bytes32) view returns(uint256)','function payoutNumerators(bytes32,uint256) view returns(uint256)','function getOutcomeSlotCount(bytes32) view returns(uint256)','function getCollectionId(bytes32,bytes32,uint256) view returns(bytes32)','function getPositionId(address,bytes32) pure returns(uint256)','function balanceOf(address,uint256) view returns(uint256)','function isApprovedForAll(address,address) view returns(bool)','function setApprovalForAll(address,bool)',
  'event PayoutRedemption(address indexed redeemer,address indexed collateralToken,bytes32 indexed parentCollectionId,bytes32 conditionId,uint256[] indexSets,uint256 payout)',
  'event TransferBatch(address indexed operator,address indexed from,address indexed to,uint256[] ids,uint256[] values)',
  'event TransferSingle(address indexed operator,address indexed from,address indexed to,uint256 id,uint256 value)'],
 adapter:['function redeemPositions(address,bytes32,bytes32,uint256[])','function CONDITIONAL_TOKENS() view returns(address)','function COLLATERAL_TOKEN() view returns(address)','function USDCE() view returns(address)','function NEG_RISK_ADAPTER() view returns(address)','function WRAPPED_COLLATERAL() view returns(address)'],
 negRiskAdapter:['function wcol() view returns(address)','event PayoutRedemption(address indexed redeemer,bytes32 indexed conditionId,uint256[] amounts,uint256 payout)'],
 pm:['function balanceOf(address,uint256) view returns(uint256)','function isApprovedForAll(address,address) view returns(bool)','function setApprovalForAll(address,bool)','function moduleById(uint256) view returns(address)','function getPayout(uint256,uint256) view returns(uint256)','function COLLATERAL_TOKEN() view returns(address)',
  'event TransferSingle(address indexed operator,address indexed from,address indexed to,uint256 id,uint256 value)'],
 module:['function hasResult(bytes31) view returns(bool)','function getResult(bytes31) view returns(uint256[])',
  'event PositionRedeemed(address indexed initiator,uint256 indexed positionId,address indexed recipient,uint256 amount,uint256 payout)'],
 router:['function redeem(bytes31,uint256,uint256)','function POSITION_MANAGER() view returns(address)','function COLLATERAL_TOKEN() view returns(address)',
  'event RouterPositionRedeemed(address indexed initiator,uint256 indexed positionId,uint256 amount)'],
 erc20:['function balanceOf(address) view returns(uint256)','function decimals() view returns(uint8)','event Transfer(address indexed from,address indexed to,uint256 value)'],
};
const I=Object.fromEntries(Object.entries(ABI).map(([k,v])=>[k,new Interface(v)]));
const same=(a,b)=>String(a).toLowerCase()===String(b).toLowerCase();
const B32=/^0x[0-9a-fA-F]{64}$/, B31=/^0x[0-9a-fA-F]{62}$/;

export function payout(balance,numerator,denominator) {
 if(balance<0n || denominator<=0n || numerator<0n || numerator>denominator) throw Error('invalid resolved payout');
 return balance*numerator/denominator;
}

// V2 position id = conditionId (31 bytes) << 8 | outcomeIndex; module id = top byte (polymarket-v2-external Ids.sol).
export function v2Ids(yesTokenId) {
 const yes=BigInt(yesTokenId);
 if(yes<=0n || yes>=(1n<<256n) || (yes&0xffn)!==0n) throw Error('V2 YES position id must have outcome byte 0');
 return {conditionId:'0x'+(yes>>8n).toString(16).padStart(62,'0'),moduleId:Number(yes>>248n),yes,no:yes|1n};
}

// Which contract a plan calls, and the exact call. Only these three shapes are ever produced.
export function redemptionCall(plan) {
 if(plan.system==='v2') return {to:ADDR.router,value:'0',data:I.router.encodeFunctionData('redeem',[plan.conditionId,plan.outcomeIndex,BigInt(plan.sharesRaw)])};
 const target=plan.system==='ctf'?ADDR.ctfAdapter:ADDR.negRiskCtfAdapter;
 // The adapters ignore collateral/parent/indexSets (kept for interface compatibility) and redeem both outcomes.
 return {to:target,value:'0',data:I.adapter.encodeFunctionData('redeemPositions',[ADDR.pusd,ZeroHash,plan.conditionId,[1,2]])};
}
export function approvalCall(plan) {
 const [ledger,iface]=plan.system==='v2'?[ADDR.positionManager,I.pm]:[ADDR.ctf,I.ctf];
 return {to:ledger,value:'0',data:iface.encodeFunctionData('setApprovalForAll',[plan.operator,true])};
}

// Read-only plan. `r` is the chain reader (makeReader below; a fake in offline tests).
export async function preparePlan(r,cfg,safe) {
 const {system,conditionId,outcome,yesTokenId,sharesRaw}=cfg;
 if(!SYSTEMS.includes(system)) throw Error(`unsupported position system ${system}; declare ctf, ctf_negrisk or v2`);
 if(![0,1].includes(outcome)) throw Error('outcome must be 0 (YES) or 1 (NO)');
 const shares=BigInt(sharesRaw);
 if(shares<=0n) throw Error('positive tracked shares required');
 if(!same(await r.pusdDecimals(),6)) throw Error('six-decimal pUSD required');
 let plan;
 if(system==='v2') {
  if(!B31.test(conditionId)) throw Error('V2 condition id must be 31 bytes');
  const ids=v2Ids(yesTokenId);
  if(!same(ids.conditionId,conditionId)) throw Error('condition does not match tracked YES position id');
  const module=V2_MODULES[ids.moduleId];
  if(!module) throw Error(`V2 module ${ids.moduleId} is not a supported binary module`);
  if(!same(await r.v2ModuleById(ids.moduleId),module)) throw Error('PositionManager routes this id to an unexpected module');
  const rc=await r.routerConfig();
  if(!same(rc.positionManager,ADDR.positionManager) || !same(rc.collateral,ADDR.pusd) || !same(await r.pmCollateral(),ADDR.pusd)) throw Error('Router/PositionManager configuration changed');
  const [tokenId,otherId]=outcome===0?[ids.yes,ids.no]:[ids.no,ids.yes];
  plan={system,safe,conditionId,outcome,outcomeIndex:outcome,yesTokenId:String(yesTokenId),tokenId:tokenId.toString(),otherTokenId:otherId.toString(),module,operator:ADDR.router,ledger:ADDR.positionManager};
  if(!await r.v2HasResult(module,conditionId)) return {pending:true};
  plan.balance=await r.pmBalance(safe,tokenId); plan.otherBalance=await r.pmBalance(safe,otherId);
  plan.payoutBig=await r.v2Payout(tokenId,shares);
  plan.approved=await r.pmApproved(safe,ADDR.router);
 } else {
  if(!B32.test(conditionId)) throw Error('CTF condition id must be 32 bytes');
  const adapter=system==='ctf'?ADDR.ctfAdapter:ADDR.negRiskCtfAdapter;
  const ac=await r.adapterConfig(adapter,system==='ctf_negrisk');
  if(!same(ac.conditionalTokens,ADDR.ctf) || !same(ac.collateral,ADDR.pusd) || !same(ac.usdce,ADDR.usdce)) throw Error('collateral adapter configuration changed');
  let collateral=ADDR.usdce;
  if(system==='ctf_negrisk') {
   if(!same(ac.negRiskAdapter,ADDR.negRiskAdapter)) throw Error('neg-risk adapter configuration changed');
   collateral=await r.negRiskWcol();
   if(!same(ac.wrapped,collateral)) throw Error('wrapped collateral mismatch');
  }
  if(await r.ctfSlots(conditionId)!==2n) throw Error('not a binary CTF condition');
  const yes=await r.ctfPositionId(collateral,conditionId,1), no=await r.ctfPositionId(collateral,conditionId,2);
  if(yes.toString()!==String(yesTokenId)) throw Error(`condition does not match tracked YES token under ${system} collateral`);
  const [tokenId,otherId]=outcome===0?[yes,no]:[no,yes];
  plan={system,safe,conditionId,outcome,indexSet:outcome+1,yesTokenId:String(yesTokenId),tokenId:tokenId.toString(),otherTokenId:otherId.toString(),positionCollateral:getAddress(collateral),operator:adapter,ledger:ADDR.ctf};
  const denom=await r.ctfDenominator(conditionId);
  if(denom===0n) return {pending:true};
  plan.balance=await r.ctfBalance(safe,tokenId); plan.otherBalance=await r.ctfBalance(safe,otherId);
  plan.payoutBig=payout(shares,await r.ctfNumerator(conditionId,outcome),denom);
  plan.approved=await r.ctfApproved(safe,adapter);
 }
 if(plan.balance!==shares) throw Error('position balance differs from tracked shares; already redeemed, partial, migrated or shared holdings require reconciliation');
 // The CTF adapters redeem both outcomes; V2 is kept to the same isolation rule so one model covers all three.
 if(plan.otherBalance!==0n) throw Error('opposite outcome is also held; shared holdings require reconciliation');
 if(plan.payoutBig<0n || plan.payoutBig>shares) throw Error('implausible resolved payout');
 const out={...plan,sharesRaw:shares.toString(),payoutRaw:plan.payoutBig.toString(),zeroPayout:plan.payoutBig===0n,cashBefore:(await r.pusdBalance(safe)).toString()};
 delete out.balance; delete out.otherBalance; delete out.payoutBig;
 if(!out.approved) out.approvalTx=approvalCall(out);
 return out;
}

// Receipt evidence. Every check is exact; anything unexpected throws and leaves the journal for a person.
export function receiptPayout(receipt,plan) {
 if(Number(receipt.status)!==1) throw Error('redemption reverted');
 const parsed=[];
 for(const log of receipt.logs) for(const [name,iface] of Object.entries(I)) {
  let e;try {e=iface.parseLog(log);}catch {continue;}
  if(e) {parsed.push({addr:log.address,src:name,e});break;}
 }
 const at=(addr,name)=>parsed.filter(p=>same(p.addr,addr) && p.e.name===name).map(p=>p.e.args);
 // pUSD: exactly the expected amount minted to the Safe, and nothing leaves the Safe.
 const pusd=at(ADDR.pusd,'Transfer');
 if(pusd.some(t=>same(t.from,plan.safe))) throw Error('pUSD left the Safe in the redemption transaction');
 const mints=pusd.filter(t=>same(t.from,ZeroAddress) && same(t.to,plan.safe));
 const minted=mints.reduce((s,t)=>s+t.value,0n);
 const expected=BigInt(plan.payoutRaw);
 if(mints.length>1 || minted!==expected || (expected>0n && mints.length!==1)) throw Error('pUSD payout evidence mismatch');
 const shares=BigInt(plan.sharesRaw), token=BigInt(plan.tokenId);
 if(plan.system==='v2') {
  const burned=at(ADDR.positionManager,'TransferSingle').filter(t=>same(t.from,plan.safe));
  if(burned.length!==1 || !same(burned[0].operator,ADDR.router) || !same(burned[0].to,plan.module) || burned[0].id!==token || burned[0].value!==shares) throw Error('position transfer evidence mismatch');
  const red=at(plan.module,'PositionRedeemed');
  if(red.length!==1 || !same(red[0].initiator,ADDR.router) || red[0].positionId!==token || !same(red[0].recipient,plan.safe) || red[0].amount!==shares || red[0].payout!==expected) throw Error('module redemption evidence mismatch');
  const rr=at(ADDR.router,'RouterPositionRedeemed');
  if(rr.length!==1 || !same(rr[0].initiator,plan.safe) || rr[0].positionId!==token || rr[0].amount!==shares) throw Error('router redemption evidence mismatch');
  return expected;
 }
 const adapter=plan.operator;
 const moved=at(ADDR.ctf,'TransferBatch').filter(t=>same(t.from,plan.safe));
 if(moved.length!==1 || !same(moved[0].operator,adapter) || !same(moved[0].to,adapter)) throw Error('position transfer evidence mismatch');
 const ids=[...moved[0][3]].map(BigInt), values=[...moved[0][4]].map(BigInt); // positional: Result.values is a method
 const yesFirst=plan.outcome===0?[shares,0n]:[0n,shares];
 const [yesId,noId]=plan.outcome===0?[token,BigInt(plan.otherTokenId)]:[BigInt(plan.otherTokenId),token];
 if(ids.length!==2 || ids[0]!==yesId || ids[1]!==noId || values[0]!==yesFirst[0] || values[1]!==yesFirst[1]) throw Error('position transfer evidence mismatch');
 if(plan.system==='ctf') {
  const ev=at(ADDR.ctf,'PayoutRedemption').filter(e=>same(e.redeemer,adapter));
  if(ev.length!==1 || !same(ev[0].collateralToken,ADDR.usdce) || ev[0].parentCollectionId!==ZeroHash || !same(ev[0].conditionId,plan.conditionId) || ev[0].indexSets.map(Number).join()!=='1,2' || ev[0].payout!==expected) throw Error('CTF redemption evidence mismatch');
 } else {
  const ev=at(ADDR.negRiskAdapter,'PayoutRedemption').filter(e=>same(e.redeemer,adapter));
  if(ev.length!==1 || !same(ev[0].conditionId,plan.conditionId) || ev[0].amounts.map(BigInt).join()!==yesFirst.join() || ev[0].payout!==expected) throw Error('neg-risk redemption evidence mismatch');
 }
 return expected;
}

// Post-receipt state: position extinguished, opposite side untouched, cash exactly before + payout.
export async function observePlan(r,plan,receipt) {
 const paid=receiptPayout(receipt,plan);
 const bal=plan.system==='v2'?id=>r.pmBalance(plan.safe,id):id=>r.ctfBalance(plan.safe,id);
 if(await bal(BigInt(plan.tokenId))!==0n) throw Error('remaining position balance after redemption');
 if(await bal(BigInt(plan.otherTokenId))!==0n) throw Error('opposite outcome balance appeared; reconciliation required');
 const cash=await r.pusdBalance(plan.safe);
 if(cash!==BigInt(plan.cashBefore)+paid) throw Error('cash changed concurrently; reconciliation required');
 return {payoutRaw:paid.toString(),cashRaw:cash.toString(),txHash:receipt.hash,block:receipt.blockNumber};
}

export function makeReader(p,blockTag) {
 const at={blockTag};
 const c=(addr,abi)=>new Contract(addr,ABI[abi],p);
 const ctf=c(ADDR.ctf,'ctf'), pm=c(ADDR.positionManager,'pm'), pusd=c(ADDR.pusd,'erc20');
 return {
  pusdDecimals:()=>pusd.decimals(at), pusdBalance:a=>pusd.balanceOf(a,at),
  ctfSlots:cid=>ctf.getOutcomeSlotCount(cid,at), ctfDenominator:cid=>ctf.payoutDenominator(cid,at), ctfNumerator:(cid,i)=>ctf.payoutNumerators(cid,i,at),
  ctfPositionId:async(col,cid,set)=>ctf.getPositionId(col,await ctf.getCollectionId(ZeroHash,cid,set,at),at),
  ctfBalance:(a,id)=>ctf.balanceOf(a,id,at), ctfApproved:(a,o)=>ctf.isApprovedForAll(a,o,at),
  adapterConfig:async(addr,negRisk)=>{const a=c(addr,'adapter');return {conditionalTokens:await a.CONDITIONAL_TOKENS(at),collateral:await a.COLLATERAL_TOKEN(at),usdce:await a.USDCE(at),...(negRisk?{negRiskAdapter:await a.NEG_RISK_ADAPTER(at),wrapped:await a.WRAPPED_COLLATERAL(at)}:{})};},
  negRiskWcol:()=>c(ADDR.negRiskAdapter,'negRiskAdapter').wcol(at),
  v2ModuleById:id=>pm.moduleById(id,at), pmCollateral:()=>pm.COLLATERAL_TOKEN(at),
  routerConfig:async()=>{const rt=c(ADDR.router,'router');return {positionManager:await rt.POSITION_MANAGER(at),collateral:await rt.COLLATERAL_TOKEN(at)};},
  v2HasResult:(m,cid)=>c(m,'module').hasResult(cid,at), v2Payout:(id,amt)=>pm.getPayout(id,amt,at),
  pmBalance:(a,id)=>pm.balanceOf(a,id,at), pmApproved:(a,o)=>pm.isApprovedForAll(a,o,at),
 };
}

async function main() {
 const input=JSON.parse(fs.readFileSync(0,'utf8'));
 const {action,plan:given,...cfg}=input;
 if(!['prepare','submit','observe'].includes(action)) throw Error('unsupported action');
 const confirmations=Number(process.env.ARB_CTF_CONFIRMATIONS || 20);
 if(!Number.isInteger(confirmations) || confirmations<1) throw Error('positive confirmation depth required');
 const safe=getAddress(process.env.MILORD_POLY_SAFE_ADDRESS || '');
 if(safe===ZeroAddress || safe.toLowerCase()!==(process.env.POLY_PROXY_ADDRESS || '').toLowerCase()) throw Error('isolated MILORD Safe must match configured Polymarket funder');
 const p=new JsonRpcProvider(process.env.POLYGON_RPC_URL);
 try {
  if(Number((await p.getNetwork()).chainId)!==137) throw Error('Polygon required');
  if(action==='observe') {
   if(!same(given.safe,safe)) throw Error('Safe changed');
   const rc=await p.getTransactionReceipt(input.txHash);
   if(!rc || await rc.confirmations()<confirmations) {console.log(JSON.stringify({pending:true}));return;}
   if((await p.getBlock(rc.blockNumber)).hash!==rc.blockHash) throw Error('receipt reorg');
   console.log(JSON.stringify(await observePlan(makeReader(p,await p.getBlockNumber()),given,rc)));return;
  }
  const plan=await preparePlan(makeReader(p,await p.getBlockNumber()),action==='submit'?given:cfg,safe);
  if(action==='prepare') {console.log(JSON.stringify(plan));return;}
  if(process.env.ARB_CTF_ROUTE_VERIFIED!=='true') throw Error('live redemption route disabled');
  if(JSON.stringify(plan)!==JSON.stringify(given)) throw Error('redemption state changed since preparation');
  if(!plan.approved) throw Error('position approval missing; grant it separately before redemption');
  const tx=redemptionCall(plan);
  await p.call({...tx,from:safe}); // fail before submission on contract refusal
  const {RelayClient,RelayerTxType}=await import('@polymarket/builder-relayer-client');
  const {deriveSafe}=await import('@polymarket/builder-relayer-client/dist/builder/derive.js');
  const {BuilderConfig}=await import('@polymarket/builder-signing-sdk');
  const {createWalletClient,http}=await import('viem');
  const {polygon}=await import('viem/chains');
  const {privateKeyToAccount}=await import('viem/accounts');
  const account=privateKeyToAccount(process.env.MILORD_POLY_PRIVATE_KEY);
  const wallet=createWalletClient({account,chain:polygon,transport:http(process.env.POLYGON_RPC_URL)});
  const client=new RelayClient('https://relayer-v2.polymarket.com',137,wallet,new BuilderConfig({localBuilderCreds:{key:process.env.POLY2_API_KEY,secret:process.env.POLY2_API_SECRET,passphrase:process.env.POLY2_API_PASSPHRASE}}),RelayerTxType.SAFE);
  if(deriveSafe(account.address,client.contractConfig.SafeContracts.SafeFactory).toLowerCase()!==safe.toLowerCase()) throw Error('signer does not control configured Safe');
  const response=await client.execute([tx],`MILORD ${plan.system} redemption`);
  const result=await response.wait();
  if(!result?.transactionHash) throw Error('submission outcome unknown; reconcile journal');
  console.log(JSON.stringify({txHash:result.transactionHash}));
 } finally {p.destroy();}
}
if(process.argv[1] && new URL(import.meta.url).pathname===process.argv[1]) main().catch(e=>{console.error(e.message);process.exitCode=1;});
