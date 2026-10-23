// MILORD-only Safe/CTF transport. No order submission, no wallet creation, no USDG route.
// prepare/observe are read-only; submit is disabled unless route verification is explicitly configured.
import {JsonRpcProvider,Contract,Interface,getAddress,ZeroHash,ZeroAddress} from 'ethers';
import fs from 'node:fs';
const ABI=[
 'function payoutDenominator(bytes32) view returns(uint256)',
 'function payoutNumerators(bytes32,uint256) view returns(uint256)',
 'function getOutcomeSlotCount(bytes32) view returns(uint256)',
 'function getCollectionId(bytes32,bytes32,uint256) view returns(bytes32)',
 'function getPositionId(address,bytes32) pure returns(uint256)',
 'function balanceOf(address,uint256) view returns(uint256)',
 'function redeemPositions(address,bytes32,bytes32,uint256[])',
 'event PayoutRedemption(address indexed redeemer,address indexed collateralToken,bytes32 indexed parentCollectionId,bytes32 conditionId,uint256[] indexSets,uint256 payout)',
];
const ERC=['function balanceOf(address) view returns(uint256)','function decimals() view returns(uint8)','event Transfer(address indexed from,address indexed to,uint256 value)'];
const CTF='0x4D97DCd97eC945f40cF65F87097ACe5EA0476045';
export function payout(balance,numerator,denominator) {
 if(balance<0n || denominator<=0n || numerator<0n || numerator>denominator) throw Error('invalid resolved payout');
 return balance*numerator/denominator;
}
export function receiptPayout(receipt,plan) {
 if(Number(receipt.status)!==1) throw Error('redemption reverted');
 const ci=new Interface(ABI),ei=new Interface(ERC);
 const events=[];let transferred=0n;
 for(const log of receipt.logs) {
  if(log.address.toLowerCase()===CTF.toLowerCase()) {
   let e;try {e=ci.parseLog(log);}catch {continue;}
   if(e?.name==='PayoutRedemption' && e.args.redeemer.toLowerCase()===plan.safe.toLowerCase() && e.args.collateralToken.toLowerCase()===plan.collateral.toLowerCase() && e.args.parentCollectionId===ZeroHash && e.args.conditionId===plan.conditionId && e.args.indexSets.length===1 && Number(e.args.indexSets[0])===plan.indexSet) events.push(e.args.payout);
  }
  if(log.address.toLowerCase()===plan.collateral.toLowerCase()) {
   let e;try {e=ei.parseLog(log);}catch {continue;}
   if(e?.name==='Transfer' && e.args.from.toLowerCase()===CTF.toLowerCase() && e.args.to.toLowerCase()===plan.safe.toLowerCase()) transferred+=e.args.value;
  }
 }
 if(events.length!==1 || events[0]!==BigInt(plan.payoutRaw) || transferred!==events[0]) throw Error('receipt payout evidence mismatch');
 return events[0];
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
  const c=new Contract(CTF,ABI,p);
  if(action==='observe') {
   if(given.safe.toLowerCase()!==safe.toLowerCase()) throw Error('Safe changed');
   const r=await p.getTransactionReceipt(input.txHash);
   if(!r || await r.confirmations()<confirmations) {console.log(JSON.stringify({pending:true}));return;}
   if((await p.getBlock(r.blockNumber)).hash!==r.blockHash) throw Error('receipt reorg');
   const paid=receiptPayout(r,given);
   const token=new Contract(given.collateral,ERC,p);
   const head=await p.getBlockNumber();
   if(await c.balanceOf(safe,given.tokenId,{blockTag:head})!==0n) throw Error('remaining conditional token balance');
   const cash=await token.balanceOf(safe,{blockTag:head});
   if(cash!==BigInt(given.cashBefore)+paid) throw Error('cash changed concurrently; reconciliation required');
   console.log(JSON.stringify({payoutRaw:paid.toString(),cashRaw:cash.toString(),txHash:r.hash,block:r.blockNumber}));return;
  }
  const planInput=action==='submit'?given:cfg;
  if(!/^0x[0-9a-fA-F]{64}$/.test(planInput.conditionId) || ![1,2].includes(planInput.indexSet)) throw Error('binary CTF condition/index required');
  const collateral=getAddress(planInput.collateral);
  if(collateral.toLowerCase()!==(process.env.MILORD_CTF_COLLATERAL || '').toLowerCase()) throw Error('collateral route not configured');
  const block=await p.getBlockNumber(), at={blockTag:block};
  if(await c.getOutcomeSlotCount(planInput.conditionId,at)!==2n) throw Error('not a binary CTF condition');
  const denom=await c.payoutDenominator(planInput.conditionId,at);
  if(denom===0n) {console.log(JSON.stringify({pending:true}));return;}
  const yesCollection=await c.getCollectionId(ZeroHash,planInput.conditionId,1,at);
  if((await c.getPositionId(collateral,yesCollection)).toString()!==planInput.yesTokenId) throw Error('condition does not match tracked YES token');
  const collection=await c.getCollectionId(ZeroHash,planInput.conditionId,planInput.indexSet,at);
  const tokenId=await c.getPositionId(collateral,collection);
  const balance=await c.balanceOf(safe,tokenId,at);
  if(balance!==BigInt(planInput.sharesRaw) || balance===0n) throw Error('CTF balance differs from tracked shares; already redeemed/partial/shared holdings require reconciliation');
  const num=await c.payoutNumerators(planInput.conditionId,planInput.indexSet===1?0:1,at);
  const token=new Contract(collateral,ERC,p);
  if(await token.decimals(at)!==6n) throw Error('six-decimal collateral required');
  const plan={safe,collateral,conditionId:planInput.conditionId,indexSet:planInput.indexSet,yesTokenId:planInput.yesTokenId,sharesRaw:balance.toString(),tokenId:tokenId.toString(),payoutRaw:payout(balance,num,denom).toString(),cashBefore:(await token.balanceOf(safe,at)).toString()};
  if(action==='prepare') {console.log(JSON.stringify(plan));return;}
  if(action!=='submit' || process.env.ARB_CTF_ROUTE_VERIFIED!=='true') throw Error('live redemption route disabled');
  if(JSON.stringify(plan)!==JSON.stringify(given)) throw Error('redemption state changed since preparation');
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
  const tx={to:CTF,value:'0',data:c.interface.encodeFunctionData('redeemPositions',[collateral,ZeroHash,plan.conditionId,[plan.indexSet]])};
  await p.call({...tx,from:safe}); // fail before submission on contract refusal
  const response=await client.execute([tx],'MILORD CTF redemption');
  const result=await response.wait();
  if(!result?.transactionHash) throw Error('submission outcome unknown; reconcile journal');
  console.log(JSON.stringify({txHash:result.transactionHash}));
 } finally {p.destroy();}
}
if(process.argv[1] && new URL(import.meta.url).pathname===process.argv[1]) main().catch(e=>{console.error(e.message);process.exitCode=1;});
