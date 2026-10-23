// Shared by the withdrawal CLI and offline tests. All accounting uses raw USDC units.
export function requestedRaw(amount) {
  const text=String(amount);
  if (!/^[0-9]+(?:\.[0-9]{1,6})?$/.test(text)) throw Error('amount must be a decimal with at most six places');
  const [whole,frac='']=text.split('.');
  const raw=BigInt(whole)*1000000n+BigInt(frac.padEnd(6,'0'));
  if(raw<5000000n || raw>5000000000n) throw Error('amount outside $5–$5000 bounds');
  return raw;
}
export function bridgeAmount(requested,proxyBalance,before,after) {
  if(requested<=0n || proxyBalance<requested) throw Error('insufficient proxy funds');
  if(after-before!==requested) throw Error('EOA delta does not match withdrawal; reconcile before retry');
  return requested;
}
export function validateApproval(tx,token,spender,amount) {
  if(tx.to.toLowerCase()!==token.toLowerCase() || BigInt(tx.value || 0)!==0n) throw Error('invalid approval target/value');
  const data=tx.data.toLowerCase();
  if(!/^0x095ea7b3[0-9a-f]{128}$/.test(data) || data.slice(10,34)!=='0'.repeat(24)) throw Error('invalid approval calldata');
  if('0x'+data.slice(34,74)!==spender.toLowerCase() || BigInt('0x'+data.slice(74))!==amount) throw Error('approval exceeds or differs from requested amount/spender');
}
