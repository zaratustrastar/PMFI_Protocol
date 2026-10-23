import test from 'node:test';
import assert from 'node:assert/strict';
import { checkFunding,FUNDING as F,meanTick,referencePrice } from '../lib/funding-check.mjs';
function reader(reverse=false, bad={}) {
 const tick=reverse ? 198505 : -198505;
 return {chainId:async()=>4663,code:async()=> '0x01',call:async(a,m,args=[])=>{
   if (m in bad) { if (bad[m] instanceof Error) throw bad[m]; return bad[m]; }
   return {token0:reverse?F.usdg:F.weth,token1:reverse?F.weth:F.usdg,fee:500n,factory:F.factory,getPool:F.pool,decimals:a===F.weth?18n:6n,deposit:undefined,withdraw:undefined,slot0:[1n,BigInt(tick),0n,23000n],observe:[[BigInt(-tick)*1800n,0n]],quoteExactInputSingle:[(m === "quoteExactInputSingle" ? args[0][2] : 0n)*referencePrice(tick,!reverse)/10n**18n*999n/1000n || 0n,1n,1n,1n]}[m];
 }};
}
for (const reverse of [false,true]) test(`quotes both token orderings ${reverse}`,async()=>{
 const r=await checkFunding(reader(reverse));assert.equal(r.validated,true);assert.equal(r.quotes.length,4);
});
for (const [name,bad] of Object.entries({wrongTokens:{token1:F.weth},wrongFactory:{factory:F.weth},wrongPool:{getPool:F.weth},history:{observe:Error('OLD')},quote:{quoteExactInputSingle:Error('revert')},slippage:{quoteExactInputSingle:[1n,1n,0n]},proxy:{deposit:Error('paused')}})) test(name,async()=>assert.equal((await checkFunding(reader(false,bad))).validated,false));
test('negative mean rounds down like Solidity',()=>assert.equal(meanTick(-1801n),-2));
