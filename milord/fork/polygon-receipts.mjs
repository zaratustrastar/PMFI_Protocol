// Receipt wait for the Polygon fork runner (fork/polygon-redemption.mjs).
//
// Not provider.waitForTransaction: ethers v6 resolves it only from a "block" event for a block AFTER the one it
// read when it started, and its block poller swallows the first block it sees. With Anvil automine no further block
// is mined while the runner waits, so a receipt that was not yet visible on the first check, or that landed in a
// block after the block number it read, leaves it waiting forever with Node idle in ep_poll. Polling the receipt
// itself needs no later block, and the deadline turns a stuck node into an explicit error.
export async function waitReceipt(provider,hash,{label='tx',timeoutMs=120_000,intervalMs=250}={}) {
 const deadline=Date.now()+timeoutMs;
 for(let polls=1;;polls++) {
  const r=await provider.getTransactionReceipt(hash);
  if(r) return r;
  if(Date.now()>=deadline) {
   const tx=await provider.getTransaction(hash).catch(e=>({error:e.message}));
   const head=await provider.getBlockNumber().catch(()=>'unknown');
   const known=tx==null?'unknown to the node':tx.error?`getTransaction failed: ${tx.error}`:`known, blockNumber ${tx.blockNumber??'pending'}`;
   throw Error(`${label}: no receipt for ${hash} after ${timeoutMs} ms (${polls} polls, head ${head}, tx ${known})`);
  }
  await new Promise(res=>setTimeout(res,intervalMs));
 }
}
