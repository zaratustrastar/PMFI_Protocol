// Regression for the B21 Polygon fork runner hang (2026-10-24): with Anvil automine no block follows the
// transaction's own, so ethers' waitForTransaction can wait forever. The runner polls receipts with a deadline.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {JsonRpcProvider} from 'ethers';
import {waitReceipt} from '../../fork/polygon-receipts.mjs';

const HASH='0x'+'ab'.repeat(32);
// A node that mined the transaction in block 101 and then mines nothing more, like Anvil automine.
async function anvilLike({receiptAfterPolls=0,blockNumbers=[101],never=false}={}) {
 let receiptPolls=0,blockCalls=0;
 const receipt={transactionHash:HASH,blockHash:'0x'+'cd'.repeat(32),blockNumber:'0x65',transactionIndex:'0x0',from:'0x'+'11'.repeat(20),to:'0x'+'22'.repeat(20),
  cumulativeGasUsed:'0x5208',gasUsed:'0x5208',effectiveGasPrice:'0x1',contractAddress:null,logs:[],logsBloom:'0x'+'00'.repeat(256),status:'0x1',type:'0x2'};
 const server=http.createServer((req,res)=>{let body='';req.on('data',c=>body+=c);req.on('end',()=>{
  const reply=m=>{const {id,method}=m;let result=null;
   if(method==='eth_chainId') result='0x89';
   else if(method==='eth_blockNumber') result='0x'+(blockNumbers[Math.min(blockCalls++,blockNumbers.length-1)]).toString(16);
   else if(method==='eth_getTransactionReceipt') result=!never && receiptPolls++>=receiptAfterPolls?receipt:null;
   else if(method==='eth_getTransactionByHash') result=null;
   return {jsonrpc:'2.0',id,result};};
  const m=JSON.parse(body);res.setHeader('content-type','application/json');res.end(JSON.stringify(Array.isArray(m)?m.map(reply):reply(m)));
 });});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const p=new JsonRpcProvider(`http://127.0.0.1:${server.address().port}`,137,{staticNetwork:true,cacheTimeout:-1,pollingInterval:50,batchMaxCount:1});
 return {p,close:()=>{p.destroy();server.close();}};
}

test('ethers waitForTransaction never resolves when no later block is mined (the observed hang)',async()=>{
 const n=await anvilLike({blockNumbers:[100,101]}); // read head 100, receipt in 101, nothing after
 try {await assert.rejects(n.p.waitForTransaction(HASH,1,1500),/timeout/);} finally {n.close();}
});

test('waitReceipt returns the mined receipt without a later block',async()=>{
 const n=await anvilLike({blockNumbers:[100,101]});
 try {const r=await waitReceipt(n.p,HASH,{timeoutMs:2000,intervalMs:20});assert.equal(r.blockNumber,101);assert.equal(r.status,1);}
 finally {n.close();}
});

test('waitReceipt keeps polling while the receipt is not yet visible',async()=>{
 const n=await anvilLike({receiptAfterPolls:3});
 try {assert.equal((await waitReceipt(n.p,HASH,{timeoutMs:2000,intervalMs:20})).status,1);} finally {n.close();}
});

test('waitReceipt fails with a diagnostic instead of hanging',async()=>{
 const n=await anvilLike({never:true});
 try {await assert.rejects(waitReceipt(n.p,HASH,{label:'adapter split',timeoutMs:200,intervalMs:20}),/adapter split: no receipt for 0xabab.* after 200 ms .*head 101, tx unknown to the node/);}
 finally {n.close();}
});
