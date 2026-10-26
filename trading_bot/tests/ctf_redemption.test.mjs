import test from 'node:test';
import assert from 'node:assert/strict';
import {Interface,ZeroHash,ZeroAddress} from 'ethers';
import {ADDR,ABI,payout,v2Ids,preparePlan,receiptPayout,observePlan,redemptionCall,approvalCall} from '../ctf_redemption.mjs';

const I=Object.fromEntries(Object.entries(ABI).map(([k,v])=>[k,new Interface(v)]));
const safe='0x'+'11'.repeat(20);
const cid32='0x'+'33'.repeat(32);
const WCOL='0x'+'77'.repeat(20);
// V2 binary-module condition: module byte 0x01, outcome byte dropped.
const cid31='0x01'+'ab'.repeat(30);
const v2yes=BigInt(cid31+'00');
const log=(addr,iface,name,args)=>({address:addr,...I[iface].encodeEventLog(I[iface].getEvent(name),args)});

// Fake chain reader: a resolved market where the Safe holds exactly the tracked side.
function reader(o={}) {
 const s={decimals:6n,cash:100n,denom:1n,nums:[1n,0n],bal:{},approved:true,slots:2n,hasResult:true,module:ADDR.binaryModule,...o};
 const ctfId=(col,set)=>BigInt(col)*10n+BigInt(set); // stand-in for getCollectionId/getPositionId
 return {s,ctfId,
  pusdDecimals:async()=>s.decimals, pusdBalance:async()=>s.cash,
  ctfSlots:async()=>s.slots, ctfDenominator:async()=>s.denom, ctfNumerator:async(c,i)=>s.nums[i],
  ctfPositionId:async(col,c,set)=>ctfId(col,set), ctfBalance:async(a,id)=>s.bal[id]??0n, ctfApproved:async()=>s.approved,
  adapterConfig:async(addr,nr)=>({conditionalTokens:ADDR.ctf,collateral:ADDR.pusd,usdce:ADDR.usdce,...(nr?{negRiskAdapter:ADDR.negRiskAdapter,wrapped:WCOL}:{}),...s.adapter}),
  negRiskWcol:async()=>WCOL,
  v2ModuleById:async()=>s.module, pmCollateral:async()=>ADDR.pusd, routerConfig:async()=>({positionManager:ADDR.positionManager,collateral:ADDR.pusd}),
  v2HasResult:async()=>s.hasResult, v2Payout:async(id,amt)=>amt*s.nums[Number(id&0xffn)]/1n,
  pmBalance:async(a,id)=>s.bal[id]??0n, pmApproved:async()=>s.approved,
 };
}
const yesCtf=r=>r.ctfId(ADDR.usdce,1).toString();

test('integer payouts: winner, loser, fractional and rounded',()=>{
 assert.equal(payout(100n,1n,1n),100n);assert.equal(payout(100n,0n,1n),0n);
 assert.equal(payout(101n,1n,2n),50n);assert.throws(()=>payout(1n,1n,0n));assert.throws(()=>payout(1n,3n,2n));
});

test('V2 ids are derived from the position id, never assumed equal to CTF ids',()=>{
 const ids=v2Ids(v2yes);
 assert.equal(ids.conditionId,cid31);assert.equal(ids.moduleId,1);assert.equal(ids.no,v2yes|1n);
 assert.throws(()=>v2Ids(v2yes|1n),/outcome byte 0/);
});

test('standard CTF: YES winner plans an adapter redemption paying pUSD',async()=>{
 const r=reader();r.s.bal[r.ctfId(ADDR.usdce,1)]=10n;
 const plan=await preparePlan(r,{system:'ctf',conditionId:cid32,outcome:0,yesTokenId:yesCtf(r),sharesRaw:'10'},safe);
 assert.equal(plan.payoutRaw,'10');assert.equal(plan.operator,ADDR.ctfAdapter);assert.equal(plan.positionCollateral,ADDR.usdce);
 assert.equal(plan.cashBefore,'100');assert.equal(plan.approved,true);assert.equal(plan.approvalTx,undefined);
 const call=redemptionCall(plan);
 assert.equal(call.to,ADDR.ctfAdapter);
 assert.deepEqual([...I.adapter.decodeFunctionData('redeemPositions',call.data)].map(String),[ADDR.pusd,ZeroHash,cid32,'1,2']);
});

test('neg-risk CTF ids use the wrapped collateral; a plain-CTF declaration is refused',async()=>{
 const r=reader();const yes=r.ctfId(WCOL,1).toString();r.s.bal[r.ctfId(WCOL,2)]=7n;r.s.nums=[0n,1n];
 const plan=await preparePlan(r,{system:'ctf_negrisk',conditionId:cid32,outcome:1,yesTokenId:yes,sharesRaw:'7'},safe);
 assert.equal(plan.operator,ADDR.negRiskCtfAdapter);assert.equal(plan.payoutRaw,'7');assert.equal(plan.indexSet,2);
 await assert.rejects(preparePlan(r,{system:'ctf',conditionId:cid32,outcome:1,yesTokenId:yes,sharesRaw:'7'},safe),/does not match tracked YES/);
});

test('V2: Router.redeem with the exact amount and outcome index',async()=>{
 const r=reader();r.s.bal[v2yes|1n]=5n;r.s.nums=[0n,1n];
 const plan=await preparePlan(r,{system:'v2',conditionId:cid31,outcome:1,yesTokenId:v2yes.toString(),sharesRaw:'5'},safe);
 assert.equal(plan.module,ADDR.binaryModule);assert.equal(plan.payoutRaw,'5');assert.equal(plan.tokenId,(v2yes|1n).toString());
 const call=redemptionCall(plan);assert.equal(call.to,ADDR.router);
 assert.deepEqual([...I.router.decodeFunctionData('redeem',call.data)].map(String),[cid31,'1','5']);
});

test('V2 refusals: CTF condition id, combinatorial module, unexpected module routing',async()=>{
 const r=reader();r.s.bal[v2yes]=5n;
 const cfg={system:'v2',conditionId:cid31,outcome:0,yesTokenId:v2yes.toString(),sharesRaw:'5'};
 await assert.rejects(preparePlan(r,{...cfg,conditionId:cid32},safe),/31 bytes/);
 const combo=BigInt('0x03'+'ab'.repeat(30)+'00');
 await assert.rejects(preparePlan(r,{...cfg,conditionId:'0x03'+'ab'.repeat(30),yesTokenId:combo.toString()},safe),/not a supported binary module/);
 r.s.module='0x'+'99'.repeat(20);
 await assert.rejects(preparePlan(r,cfg,safe),/unexpected module/);
});

test('unresolved markets wait; nothing is planned',async()=>{
 const r=reader({denom:0n});
 assert.deepEqual(await preparePlan(r,{system:'ctf',conditionId:cid32,outcome:0,yesTokenId:yesCtf(r),sharesRaw:'1'},safe),{pending:true});
 const v=reader({hasResult:false});
 assert.deepEqual(await preparePlan(v,{system:'v2',conditionId:cid31,outcome:0,yesTokenId:v2yes.toString(),sharesRaw:'1'},safe),{pending:true});
});

test('zero payout loser is planned explicitly',async()=>{
 const r=reader({nums:[0n,1n]});r.s.bal[r.ctfId(ADDR.usdce,1)]=10n;
 const plan=await preparePlan(r,{system:'ctf',conditionId:cid32,outcome:0,yesTokenId:yesCtf(r),sharesRaw:'10'},safe);
 assert.equal(plan.payoutRaw,'0');assert.equal(plan.zeroPayout,true);
});

test('partial, already-redeemed, migrated and shared holdings are refused',async()=>{
 const base=r=>({system:'ctf',conditionId:cid32,outcome:0,yesTokenId:yesCtf(r),sharesRaw:'10'});
 let r=reader();r.s.bal[r.ctfId(ADDR.usdce,1)]=6n;
 await assert.rejects(preparePlan(r,base(r),safe),/differs from tracked shares/);
 r=reader();await assert.rejects(preparePlan(r,base(r),safe),/differs from tracked shares/);
 r=reader();r.s.bal[r.ctfId(ADDR.usdce,1)]=10n;r.s.bal[r.ctfId(ADDR.usdce,2)]=1n;
 await assert.rejects(preparePlan(r,base(r),safe),/opposite outcome/);
});

test('configuration and system checks fail closed',async()=>{
 const r=reader();r.s.bal[r.ctfId(ADDR.usdce,1)]=10n;
 const cfg={system:'ctf',conditionId:cid32,outcome:0,yesTokenId:yesCtf(r),sharesRaw:'10'};
 await assert.rejects(preparePlan(r,{...cfg,system:'legacy'},safe),/unsupported position system/);
 await assert.rejects(preparePlan(r,{...cfg,sharesRaw:'0'},safe),/positive/);
 r.s.adapter={collateral:ADDR.usdce};await assert.rejects(preparePlan(r,cfg,safe),/adapter configuration changed/);
 const s=reader({slots:3n});s.s.bal[s.ctfId(ADDR.usdce,1)]=10n;await assert.rejects(preparePlan(s,cfg,safe),/binary/);
 const d=reader({decimals:18n});await assert.rejects(preparePlan(d,cfg,safe),/six-decimal/);
});

test('missing approval is reported with the exact one-time call, not bundled',async()=>{
 const r=reader({approved:false});r.s.bal[r.ctfId(ADDR.usdce,1)]=10n;
 const plan=await preparePlan(r,{system:'ctf',conditionId:cid32,outcome:0,yesTokenId:yesCtf(r),sharesRaw:'10'},safe);
 assert.equal(plan.approved,false);assert.deepEqual(plan.approvalTx,approvalCall(plan));
 assert.equal(plan.approvalTx.to,ADDR.ctf);
 assert.deepEqual([...I.ctf.decodeFunctionData('setApprovalForAll',plan.approvalTx.data)],[ADDR.ctfAdapter,true]);
 const v={system:'v2',operator:ADDR.router};assert.equal(approvalCall(v).to,ADDR.positionManager);
});

// Receipts shaped like the deployed contracts' logs.
const ctfPlan={system:'ctf',safe,conditionId:cid32,outcome:0,tokenId:'101',otherTokenId:'102',sharesRaw:'10',payoutRaw:'10',operator:ADDR.ctfAdapter,cashBefore:'100'};
function ctfReceipt(p=ctfPlan,{paid=BigInt(p.payoutRaw),values=p.outcome===0?[10n,0n]:[0n,10n],extra=[]}={}) {
 const ids=p.outcome===0?[p.tokenId,p.otherTokenId]:[p.otherTokenId,p.tokenId];
 const logs=[log(ADDR.ctf,'ctf','TransferBatch',[p.operator,p.safe,p.operator,ids,values])];
 if(p.system==='ctf') logs.push(log(ADDR.ctf,'ctf','PayoutRedemption',[p.operator,ADDR.usdce,ZeroHash,p.conditionId,[1,2],paid]));
 else logs.push(log(ADDR.negRiskAdapter,'negRiskAdapter','PayoutRedemption',[p.operator,p.conditionId,values,paid]));
 logs.push(log(ADDR.pusd,'erc20','Transfer',[ZeroAddress,p.safe,paid]),...extra);
 return {status:1,hash:'0x'+'ab'.repeat(32),blockNumber:1,logs};
}
const v2Plan={system:'v2',safe,conditionId:cid31,outcome:1,outcomeIndex:1,tokenId:(v2yes|1n).toString(),otherTokenId:v2yes.toString(),sharesRaw:'5',payoutRaw:'5',module:ADDR.binaryModule,operator:ADDR.router,cashBefore:'100'};
function v2Receipt(p=v2Plan,{paid=BigInt(p.payoutRaw),amount=BigInt(p.sharesRaw)}={}) {
 return {status:1,hash:'0x'+'cd'.repeat(32),blockNumber:2,logs:[
  log(ADDR.positionManager,'pm','TransferSingle',[ADDR.router,p.safe,p.module,p.tokenId,amount]),
  log(ADDR.pusd,'erc20','Transfer',[ZeroAddress,p.safe,paid]),
  log(ADDR.positionManager,'pm','TransferSingle',[p.module,p.module,ZeroAddress,p.tokenId,amount]),
  log(p.module,'module','PositionRedeemed',[ADDR.router,p.tokenId,p.safe,amount,paid]),
  log(ADDR.router,'router','RouterPositionRedeemed',[p.safe,p.tokenId,amount]),
 ]};
}

test('receipt evidence: standard, neg-risk and V2 winners and a zero payout',()=>{
 assert.equal(receiptPayout(ctfReceipt(),ctfPlan),10n);
 const nr={...ctfPlan,system:'ctf_negrisk',operator:ADDR.negRiskCtfAdapter,outcome:1};
 assert.equal(receiptPayout(ctfReceipt(nr),nr),10n);
 assert.equal(receiptPayout(v2Receipt(),v2Plan),5n);
 const zero={...ctfPlan,payoutRaw:'0'};assert.equal(receiptPayout(ctfReceipt(zero),zero),0n);
 const zr=ctfReceipt(zero);zr.logs=zr.logs.filter(l=>l.address!==ADDR.pusd);assert.equal(receiptPayout(zr,zero),0n);
});

test('receipt evidence rejects mismatches',()=>{
 assert.throws(()=>receiptPayout({...ctfReceipt(),status:0},ctfPlan),/reverted/);
 assert.throws(()=>receiptPayout(ctfReceipt(ctfPlan,{paid:9n}),ctfPlan),/mismatch/);           // partial payout
 assert.throws(()=>receiptPayout(ctfReceipt(ctfPlan,{values:[10n,3n]}),ctfPlan),/mismatch/);   // opposite side moved
 const dup=ctfReceipt();dup.logs.push(dup.logs[2]);assert.throws(()=>receiptPayout(dup,ctfPlan),/mismatch/); // second mint
 const out=ctfReceipt(ctfPlan,{extra:[log(ADDR.pusd,'erc20','Transfer',[safe,'0x'+'99'.repeat(20),1n])]});
 assert.throws(()=>receiptPayout(out,ctfPlan),/left the Safe/);
 const wrongCond=ctfReceipt({...ctfPlan,conditionId:'0x'+'44'.repeat(32)});assert.throws(()=>receiptPayout(wrongCond,ctfPlan),/mismatch/);
 const fakeToken=ctfReceipt();fakeToken.logs[2].address='0x'+'55'.repeat(20);assert.throws(()=>receiptPayout(fakeToken,ctfPlan),/mismatch/);
 // A neg-risk receipt cannot satisfy a standard plan and vice versa.
 const nr={...ctfPlan,system:'ctf_negrisk',operator:ADDR.negRiskCtfAdapter};
 assert.throws(()=>receiptPayout(ctfReceipt(nr),ctfPlan),/mismatch/);
 assert.throws(()=>receiptPayout(ctfReceipt(),nr),/mismatch/);
 assert.throws(()=>receiptPayout(v2Receipt(v2Plan,{amount:4n}),v2Plan),/mismatch/);
 assert.throws(()=>receiptPayout(v2Receipt({...v2Plan,module:ADDR.negRiskModule}),v2Plan),/mismatch/);
 assert.throws(()=>receiptPayout(ctfReceipt(),v2Plan),/mismatch/);
});

test('observation requires extinguished positions and exact cash',async()=>{
 const r=reader({cash:110n});
 assert.deepEqual(await observePlan(r,ctfPlan,ctfReceipt()),{payoutRaw:'10',cashRaw:'110',txHash:'0x'+'ab'.repeat(32),block:1});
 r.s.cash=111n;await assert.rejects(observePlan(r,ctfPlan,ctfReceipt()),/concurrently/);
 r.s.cash=110n;r.s.bal[101n]=1n;await assert.rejects(observePlan(r,ctfPlan,ctfReceipt()),/remaining position/);
 const v=reader({cash:105n});assert.equal((await observePlan(v,v2Plan,v2Receipt())).payoutRaw,'5');
});
