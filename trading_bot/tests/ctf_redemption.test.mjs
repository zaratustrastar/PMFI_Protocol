import test from 'node:test';
import assert from 'node:assert/strict';
import {Interface,ZeroHash} from 'ethers';
import {payout,receiptPayout} from '../ctf_redemption.mjs';
const ctf='0x4D97DCd97eC945f40cF65F87097ACe5EA0476045';
const plan={safe:'0x'+'11'.repeat(20),collateral:'0x'+'22'.repeat(20),conditionId:'0x'+'33'.repeat(32),indexSet:1,payoutRaw:'100'};
const ci=new Interface(['event PayoutRedemption(address indexed redeemer,address indexed collateralToken,bytes32 indexed parentCollectionId,bytes32 conditionId,uint256[] indexSets,uint256 payout)']);
const ei=new Interface(['event Transfer(address indexed from,address indexed to,uint256 value)']);
function receipt(p=plan,paid=100n) {
 const event=ci.encodeEventLog(ci.getEvent('PayoutRedemption'),[p.safe,p.collateral,ZeroHash,p.conditionId,[p.indexSet],paid]);
 const transfer=ei.encodeEventLog(ei.getEvent('Transfer'),[ctf,p.safe,paid]);
 return {status:1,logs:[{address:ctf,...event},...(paid===0n?[]:[{address:p.collateral,...transfer}])]};
}
test('winner, loser, fractional and rounded conditional payouts',()=>{
 assert.equal(payout(100n,1n,1n),100n);assert.equal(payout(100n,0n,1n),0n);
 assert.equal(payout(101n,1n,2n),50n);assert.throws(()=>payout(1n,1n,0n));
 assert.throws(()=>payout(1n,3n,2n));
});
test('confirmed winner and zero-payout loser require matching CTF event',()=>{
 assert.equal(receiptPayout(receipt(),plan),100n);
 assert.equal(receiptPayout(receipt(plan,0n),{...plan,payoutRaw:'0'}),0n);
 assert.throws(()=>receiptPayout({status:1,logs:[]},{...plan,payoutRaw:'0'}));
});
for(const field of ['safe','collateral','conditionId','indexSet']) test(`reject wrong ${field}`,()=>{
 const other={...plan,[field]:field==='indexSet'?2:'0x'+'44'.repeat(field==='conditionId'?32:20)};
 assert.throws(()=>receiptPayout(receipt(other),plan));
});
test('reject partial payout, missing transfer, duplicate redemption and revert',()=>{
 assert.throws(()=>receiptPayout(receipt(plan,50n),plan));
 const r=receipt();assert.throws(()=>receiptPayout({...r,logs:r.logs.slice(0,1)},plan));
 assert.throws(()=>receiptPayout({...r,logs:[...r.logs,r.logs[0]]},plan));
 assert.throws(()=>receiptPayout({...r,status:0},plan));
});
test('unrelated token transfers cannot prove payout',()=>{
 const r=receipt();r.logs[1].address='0x'+'55'.repeat(20);
 assert.throws(()=>receiptPayout(r,plan));
});
