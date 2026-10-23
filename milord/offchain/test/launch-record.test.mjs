import test from 'node:test';
import assert from 'node:assert/strict';
import { AbiCoder,keccak256 } from 'ethers';
import { validateLaunch } from '../lib/launch-record.mjs';
const a=n=>'0x'+n.toString(16).padStart(40,'0');
function fixture() {
 const key={currency0:a(0),currency1:a(2),fee:8388608,tickSpacing:60,hooks:a(4)};
 const id=keccak256(AbiCoder.defaultAbiCoder().encode(['address','address','uint24','int24','address'],Object.values(key)));
 const values={launched:true,predictedToken:a(2),token:a(2),familyId:'0x'+'11'.repeat(32),poolId:id,buyQueue:a(6),sellQueue:a(7),guardEndBlock:0n,lockEndBlock:100n,config:{launcher:a(3),root:a(4),rules:a(5),tax:a(8),owner:a(9)},position:{key,tickLower:134220,tickUpper:203280,liquidity:1n},poolManager:a(10),principalLockedUntil:100n,familyOwner:a(1),terms:{bound:true,buyQueue:a(6),sellQueue:a(7),buyTaxPips:15000,sellTaxPips:15000},getSlot0:[1n,0n,0n,3000n]};
 const D={startBlock:1,StateView:a(11)};
 for(const k of ['Milords','MilordConverter','EntryFeeSplitter','RoyaltySplitter','TradingTaxSplitter','BuybackBurner','pARBVault','ShareDistributor','ParbFundingAdapter','ArbitrageFundingRouter','USDG']) D[k]=a(12);
 return {D,values,read:{code:async()=> '0x01',contract:(addr)=>new Proxy({},{get:(_,m)=>async()=>m==='terms' && [a(6),a(7)].includes(addr) ? {root:a(4),rules:a(5),poolId:id,quote:a(0),isBuy:addr===a(6),protocolShareBps:2000,routeId:'0x'+'0'.repeat(64),assetRecipient:a(12)} : values[m]})}};
}
test('complete configuration',async()=>{const f=fixture();assert.equal((await validateLaunch(f.D,a(1),undefined,f.read)).complete,true);});
test('missing StateView is incomplete',async()=>{const f=fixture();delete f.D.StateView;const r=await validateLaunch(f.D,a(1),undefined,f.read);assert.equal(r.validated,true);assert.equal(r.complete,false);});
test('stand-ins are not a full service deployment',async()=>{const f=fixture();assert.equal((await validateLaunch({},a(1),undefined,f.read)).complete,false);});
for(const [k,v] of Object.entries({predictedToken:a(22),poolId:'0x'+'22'.repeat(32),buyQueue:a(7),principalLockedUntil:1n,familyOwner:a(99),getSlot0:[0n,0n,0n,3000n]})) test(`reject ${k}`,async()=>{const f=fixture();f.values[k]=v;await assert.rejects(validateLaunch(f.D,a(1),undefined,f.read));});
test('code outage refuses',async()=>{const f=fixture();f.read.code=async()=>{throw Error('RPC unavailable');};await assert.rejects(validateLaunch(f.D,a(1),undefined,f.read));});
