// B21 Polygon fork test: the pUSD redemption transport against Polymarket's DEPLOYED contracts, on a private Anvil
// fork only (run it through fork/polygon-redemption-fork.sh, which starts and verifies the fork).
//
// Each case creates its own market on the fork, so no real holder, API or key is involved:
//   ctf          ConditionalTokens condition (oracle = the test account), split through CtfCollateralAdapter
//   ctf_negrisk  NegRiskAdapter market + question (permissionless prepare), split through NegRiskCtfCollateralAdapter
//   v2           BinaryModule condition, split through Router; the test account gets the resolver role from the
//                module owner (impersonated on the fork only)
// pUSD for the splits is minted by impersonating BinaryModule, which holds pUSD's minter role.
// The holder is a fresh impersonated address standing in for the Safe; no key exists. Transactions go to the fork only.
//
// For every system: unresolved -> pending; missing approval -> reported with the exact call; resolved -> planned
// payout; redemption transaction from the transport's own calldata; receipt evidence + balances + cash checked by
// the transport's own observePlan; a second prepare is refused (already redeemed). Plus a zero-payout loser and a
// shared-holdings refusal on standard CTF.
import {waitReceipt} from './polygon-receipts.mjs';
import {JsonRpcProvider,FetchRequest,Interface,keccak256,toUtf8Bytes,ZeroHash,getAddress} from 'ethers';

const {ADDR,preparePlan,observePlan,redemptionCall,makeReader}=await import(process.env.CTF_TRANSPORT);
const RPC=process.env.FORK_URL;
// Every RPC request is bounded (a stalled upstream fetch inside Anvil surfaces as an error, not a hang).
const RPC_TIMEOUT_MS=Number(process.env.FORK_RPC_TIMEOUT_MS || 120_000);
const RECEIPT_TIMEOUT_MS=Number(process.env.FORK_RECEIPT_TIMEOUT_MS || 120_000);
const CASE_TIMEOUT_MS=Number(process.env.FORK_CASE_TIMEOUT_MS || 600_000);
const req=new FetchRequest(RPC);req.timeout=RPC_TIMEOUT_MS;
const p=new JsonRpcProvider(req,undefined,{staticNetwork:true,cacheTimeout:-1});
const t0=Date.now();
let stage='startup';
const progress=msg=>console.log(`[+${((Date.now()-t0)/1000).toFixed(1)}s] ${msg}`);
const step=label=>{stage=label;progress(label);};
// A fresh address per run (impersonated on the fork), so no mainnet history, approvals or balances leak in.
const HOLDER=getAddress('0x'+keccak256(toUtf8Bytes(`milord-b21-holder-${Date.now()}-${Math.random()}`)).slice(26));
const DEAD='0x000000000000000000000000000000000000dEaD';
const AMT=10_000_000n; // 10 pUSD
const results=[];
const approvedOperators=new Set(); // operators this run's HOLDER has approved
let failed=0;

const iface={
 ctf:new Interface(['function prepareCondition(address,bytes32,uint256)','function getConditionId(address,bytes32,uint256) view returns(bytes32)','function reportPayouts(bytes32,uint256[])','function safeTransferFrom(address,address,uint256,uint256,bytes)']),
 erc20:new Interface(['function approve(address,uint256) returns(bool)','function mint(address,uint256)','function balanceOf(address) view returns(uint256)']),
 adapter:new Interface(['function splitPosition(address,bytes32,bytes32,uint256[],uint256)']),
 nra:new Interface(['function prepareMarket(uint256,bytes) returns(bytes32)','function prepareQuestion(bytes32,bytes) returns(bytes32)','function getConditionId(bytes32) view returns(bytes32)','function getPositionId(bytes32,bool) view returns(uint256)','function reportOutcome(bytes32,bool)']),
 module:new Interface(['function getConditionId(bytes) view returns(bytes31)','function reportResult(bytes31,uint256[])','function owner() view returns(address)','function grantRoles(address,uint256)']),
 router:new Interface(['function split(bytes31,uint256)']),
 pm:new Interface(['function safeTransferFrom(address,address,uint256,uint256,bytes)']),
};

const receiptOf=(hash,label)=>waitReceipt(p,hash,{label,timeoutMs:RECEIPT_TIMEOUT_MS});
async function send(from,to,data,label) {
 step(`${label}: sending from ${from} to ${to}`);
 const hash=await p.send('eth_sendTransaction',[{from,to,data,gas:'0x7a1200'}]);
 progress(`${label}: tx ${hash}`);
 const r=await receiptOf(hash,label);
 progress(`${label}: block ${r.blockNumber} status ${r.status} gas ${r.gasUsed}`);
 if(Number(r.status)!==1) throw Error(`${label} reverted (tx ${hash}, block ${r.blockNumber})`);
 return r;
}
async function call(from,to,data) {step(`eth_call ${to} ${data.slice(0,10)}`);return p.send('eth_call',[{from,to,data},'latest']);}
async function impersonate(addr,fn) {
 await p.send('anvil_impersonateAccount',[addr]);
 await p.send('anvil_setBalance',[addr,'0x56bc75e2d63100000']);
 try {return await fn();} finally {await p.send('anvil_stopImpersonatingAccount',[addr]);}
}
const reader=async()=>makeReader(p,await p.getBlockNumber());
const prepare=async(cfg,what)=>{step(`${cfg.system}: prepare (read-only) ${what}`);const out=await preparePlan(await reader(),cfg,HOLDER);progress(`${cfg.system}: plan ${out.pending?'pending':`payout ${out.payoutRaw} approved ${out.approved}`}`);return out;};
const nonce=label=>keccak256(toUtf8Bytes(`milord-b21-fork-${label}-${Date.now()}-${Math.random()}`));

async function mintPusd(amount) {
 await impersonate(ADDR.binaryModule,()=>send(ADDR.binaryModule,ADDR.pusd,iface.erc20.encodeFunctionData('mint',[HOLDER,amount]),'pUSD mint'));
}

async function expectRefusal(fn,re,label) {
 try {await fn();} catch(e) {if(re.test(e.message)) return; throw Error(`${label}: wrong refusal: ${e.message}`);}
 throw Error(`${label}: was not refused`);
}

// Redeem a resolved position end to end through the transport's own plan, calldata and checks.
async function redeemAndObserve(cfg,expectedPayout,label) {
 let plan=await prepare(cfg,'after resolution');
 if(plan.pending) throw Error(`${label}: still pending after resolution`);
 // One HOLDER runs every case, and setApprovalForAll persists: the first case per operator (adapter or Router)
 // must see a genuinely missing approval and grant it; later cases on the same operator must see it as granted.
 let approval='reused';
 if(!approvedOperators.has(plan.operator)) {
  if(plan.approved || !plan.approvalTx) throw Error(`${label}: a fresh account must require approval for ${plan.operator}`);
  if(plan.approvalTx.to!==plan.ledger) throw Error(`${label}: approval targets ${plan.approvalTx.to}, expected ${plan.ledger}`);
  await send(HOLDER,plan.approvalTx.to,plan.approvalTx.data,`${label} approval`);
  approvedOperators.add(plan.operator);
  approval='granted';
  plan=await prepare(cfg,'after approval');
 } else if(!plan.approved || plan.approvalTx) throw Error(`${label}: approval for ${plan.operator} was granted earlier in this run but is not seen`);
 if(!plan.approved || plan.payoutRaw!==expectedPayout.toString()) throw Error(`${label}: plan ${JSON.stringify(plan)}`);
 const tx=redemptionCall(plan);
 step(`${label}: simulate redemption`);
 await p.call({...tx,from:HOLDER});
 const receipt=await send(HOLDER,tx.to,tx.data,`${label} redemption`);
 step(`${label}: observe receipt, balances and cash`);
 const seen=await observePlan(await reader(),plan,receipt);
 if(seen.payoutRaw!==expectedPayout.toString()) throw Error(`${label}: observed ${seen.payoutRaw}`);
 await expectRefusal(async()=>preparePlan(await reader(),cfg,HOLDER),/differs from tracked shares/,`${label} second redemption`);
 return {payoutRaw:seen.payoutRaw,approval,cashRaw:seen.cashRaw,gasUsed:receipt.gasUsed.toString(),tx:receipt.hash};
}

async function ctfCase(label,keep,payouts,{shared=false}={}) {
 const qid=nonce(label);
 await send(HOLDER,ADDR.ctf,iface.ctf.encodeFunctionData('prepareCondition',[HOLDER,qid,2]),'prepareCondition');
 const cid=iface.ctf.decodeFunctionResult('getConditionId',await call(HOLDER,ADDR.ctf,iface.ctf.encodeFunctionData('getConditionId',[HOLDER,qid,2])))[0];
 await mintPusd(AMT);
 await send(HOLDER,ADDR.pusd,iface.erc20.encodeFunctionData('approve',[ADDR.ctfAdapter,AMT]),'approve pUSD');
 await send(HOLDER,ADDR.ctfAdapter,iface.adapter.encodeFunctionData('splitPosition',[ADDR.pusd,ZeroHash,cid,[1,2],AMT]),'adapter split');
 const r=await reader();
 const yes=await r.ctfPositionId(ADDR.usdce,cid,1), no=await r.ctfPositionId(ADDR.usdce,cid,2);
 if(await r.ctfBalance(HOLDER,yes)!==AMT) throw Error(`${label}: split did not mint USDC.e-collateral CTF ids (position id derivation differs)`);
 if(!shared) await send(HOLDER,ADDR.ctf,iface.ctf.encodeFunctionData('safeTransferFrom',[HOLDER,DEAD,keep===0?no:yes,AMT,'0x']),'drop other side');
 const cfg={system:'ctf',conditionId:cid,outcome:keep,yesTokenId:yes.toString(),sharesRaw:AMT.toString()};
 const pending=await prepare(cfg,'before resolution');
 if(!pending.pending) throw Error(`${label}: unresolved condition was not pending`);
 await send(HOLDER,ADDR.ctf,iface.ctf.encodeFunctionData('reportPayouts',[qid,payouts]),'reportPayouts');
 if(shared) {await expectRefusal(async()=>preparePlan(await reader(),cfg,HOLDER),/opposite outcome/,label);return {refused:'shared holdings'};}
 return redeemAndObserve(cfg,AMT*BigInt(payouts[keep])/BigInt(payouts[0]+payouts[1]),label);
}

async function negRiskCase(label) {
 const meta=nonce(label); // prepareMarket ids depend on the caller and metadata
 const mid=iface.nra.decodeFunctionResult('prepareMarket',await call(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('prepareMarket',[0,meta])))[0];
 await send(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('prepareMarket',[0,meta]),'prepareMarket');
 const qid=iface.nra.decodeFunctionResult('prepareQuestion',await call(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('prepareQuestion',[mid,'0x'])))[0];
 await send(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('prepareQuestion',[mid,'0x']),'prepareQuestion');
 const cid=iface.nra.decodeFunctionResult('getConditionId',await call(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('getConditionId',[qid])))[0];
 const yesNra=iface.nra.decodeFunctionResult('getPositionId',await call(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('getPositionId',[qid,true])))[0];
 await mintPusd(AMT);
 await send(HOLDER,ADDR.pusd,iface.erc20.encodeFunctionData('approve',[ADDR.negRiskCtfAdapter,AMT]),'approve pUSD');
 await send(HOLDER,ADDR.negRiskCtfAdapter,iface.adapter.encodeFunctionData('splitPosition',[ADDR.pusd,ZeroHash,cid,[1,2],AMT]),'neg-risk adapter split');
 const r=await reader(), wcol=await r.negRiskWcol();
 const yes=await r.ctfPositionId(wcol,cid,1), no=await r.ctfPositionId(wcol,cid,2);
 if(yes!==yesNra) throw Error(`${label}: wrapped-collateral YES id differs from NegRiskAdapter.getPositionId`);
 if(await r.ctfBalance(HOLDER,yes)!==AMT) throw Error(`${label}: split did not mint wrapped-collateral ids`);
 await send(HOLDER,ADDR.ctf,iface.ctf.encodeFunctionData('safeTransferFrom',[HOLDER,DEAD,no,AMT,'0x']),'drop NO');
 const cfg={system:'ctf_negrisk',conditionId:cid,outcome:0,yesTokenId:yes.toString(),sharesRaw:AMT.toString()};
 if(!(await prepare(cfg,'before resolution')).pending) throw Error(`${label}: unresolved question was not pending`);
 await expectRefusal(async()=>preparePlan(await reader(),{...cfg,system:'ctf'},HOLDER),/does not match tracked YES/,`${label} declared as plain CTF`);
 await send(HOLDER,ADDR.negRiskAdapter,iface.nra.encodeFunctionData('reportOutcome',[qid,true]),'reportOutcome');
 return redeemAndObserve(cfg,AMT,label);
}

async function v2Case(label) {
 const mod=ADDR.binaryModule;
 const owner=iface.module.decodeFunctionResult('owner',await call(HOLDER,mod,iface.module.encodeFunctionData('owner',[])))[0];
 await impersonate(owner,()=>send(owner,mod,iface.module.encodeFunctionData('grantRoles',[HOLDER,1n<<4n]),'grant resolver role (fork only)'));
 const cid=iface.module.decodeFunctionResult('getConditionId',await call(HOLDER,mod,iface.module.encodeFunctionData('getConditionId',[nonce(label)])))[0];
 await mintPusd(AMT);
 await send(HOLDER,ADDR.pusd,iface.erc20.encodeFunctionData('approve',[ADDR.router,AMT]),'approve pUSD');
 await send(HOLDER,ADDR.router,iface.router.encodeFunctionData('split',[cid,AMT]),'router split');
 const yes=BigInt(cid+'00');
 const r=await reader();
 if(await r.pmBalance(HOLDER,yes)!==AMT) throw Error(`${label}: V2 YES id is not conditionId<<8`);
 await send(HOLDER,ADDR.positionManager,iface.pm.encodeFunctionData('safeTransferFrom',[HOLDER,DEAD,yes,AMT,'0x']),'drop YES');
 const cfg={system:'v2',conditionId:cid,outcome:1,yesTokenId:yes.toString(),sharesRaw:AMT.toString()};
 if(!(await prepare(cfg,'before resolution')).pending) throw Error(`${label}: unresolved V2 condition was not pending`);
 await send(HOLDER,mod,iface.module.encodeFunctionData('reportResult',[cid,[0n,1_000_000n]]),'reportResult');
 return redeemAndObserve(cfg,AMT,label);
}

const cases=[
 ['ctf YES winner',()=>ctfCase('ctf-win',0,[1,0])],
 ['ctf YES loser (zero payout)',()=>ctfCase('ctf-lose',0,[0,1])],
 ['ctf NO split 1:1 (fractional)',()=>ctfCase('ctf-half',1,[1,1])],
 ['ctf shared holdings refused',()=>ctfCase('ctf-shared',0,[1,0],{shared:true})],
 ['ctf_negrisk YES winner',()=>negRiskCase('negrisk-win')],
 ['v2 NO winner',()=>v2Case('v2-win')],
];
await p.send('anvil_impersonateAccount',[HOLDER]);
await p.send('anvil_setBalance',[HOLDER,'0x56bc75e2d63100000']);
console.log(`fork ${RPC} block ${await p.getBlockNumber()} holder ${HOLDER}`);
const withDeadline=(name,promise)=>{let timer;return Promise.race([promise.finally(()=>clearTimeout(timer)),new Promise((_,rej)=>{timer=setTimeout(()=>rej(Error(`timed out after ${CASE_TIMEOUT_MS} ms at stage: ${stage}`)),CASE_TIMEOUT_MS);})]);};
for(const [name,fn] of cases) {
 step(`case ${name}: start`);
 try {const out=await withDeadline(name,fn());results.push({name,ok:true,...out});console.log(`PASS ${name} ${JSON.stringify(out)}`);}
 catch(e) {failed++;results.push({name,ok:false,stage,error:e.message});console.log(`FAIL ${name} at stage '${stage}': ${e.message}`);}
}
console.log(JSON.stringify({passed:results.length-failed,failed,results}));
p.destroy();
process.exitCode=failed?1:0;
