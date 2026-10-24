// MILORD (ARB_FUNDING_CHAIN=robinhood) Polymarket recall through Polymarket's own bridge:
//   Safe pUSD (Polygon) --transfer--> per-request bridge address --Polymarket bridge--> USDG on Robinhood Chain (4663)
//
// Why: since the CLOB V2 upgrade (2026-04-28) Polymarket collateral is pUSD, not USDC.e. The legacy recall
// (safe_proxy_withdraw_parb.ts: Safe USDC.e -> EOA -> Relay) moves a token the Safe no longer trades with.
// Official docs: docs.polymarket.com/trading/bridge/withdraw (POST /withdraw, send pUSD to the returned evm address),
// /quote, /supported-assets (lists 4663 USDG 0x5fc5...1d168), /status/{address}.
//
// Actions (stdin JSON, one JSON line on stdout):
//   quote    {amountRaw}                        read-only: POST /quote, checks route and fee
//   withdraw {amountRaw}                        DISABLED unless ARB_POLY_BRIDGE_ROUTE_VERIFIED=true (set only after an
//                                               owner-approved small withdrawal proved the route): quote, POST /withdraw,
//                                               then ONE Safe transaction pUSD.transfer(bridgeAddress, amountRaw)
//   status   {depositAddress}                   read-only: GET /status/{address}
// Not retried: a submitted transfer whose outcome is unknown is reported, never resent.
import { getAddress, Interface } from 'ethers';

export const BRIDGE_API = 'https://bridge.polymarket.com';
export const POLYGON = 137;
export const PUSD = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'; // docs.polymarket.com/trading/ctf/redeem, /supported-assets
export const ROBINHOOD = 4663;
export const USDG = '0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168';
export const MIN_RAW = 5_000_000n; // $5 (supported-assets says $2; keep the existing engine floor)
export const MAX_RAW = 5_000_000_000n; // $5,000 per request (existing engine cap; split larger recalls)
const erc20 = new Interface(['function transfer(address,uint256) returns (bool)']);

const lc = (a) => String(a).toLowerCase();
const isAddr = (a) => /^0x[0-9a-fA-F]{40}$/.test(String(a));

export function amountRaw(v) {
  const s = String(v);
  if (!/^[0-9]+$/.test(s)) throw Error('amountRaw must be a positive integer string (6-decimal units)');
  const n = BigInt(s);
  if (n < MIN_RAW || n > MAX_RAW) throw Error(`amount ${n} outside ${MIN_RAW}..${MAX_RAW} raw`);
  return n;
}

export function quoteRequest(amount, recipient) {
  if (!isAddr(recipient)) throw Error('recipient must be an address');
  return {
    fromAmountBaseUnit: amount.toString(), fromChainId: String(POLYGON), fromTokenAddress: PUSD,
    recipientAddress: getAddress(recipient), toChainId: String(ROBINHOOD), toTokenAddress: USDG,
  };
}

/** Bridge output must be USDG with at most maxFeeBps below the pUSD input (both 6 decimals). */
export function validateQuote(q, amount, maxFeeBps) {
  if (!q || typeof q !== 'object') throw Error('empty quote');
  const out = BigInt(String(q.estToTokenBaseUnit ?? '-1'));
  if (out <= 0n) throw Error('quote has no output');
  if (out * 10_000n < amount * BigInt(10_000 - maxFeeBps)) throw Error(`quote output ${out} is more than ${maxFeeBps} bps below ${amount}`);
  if (out > amount * 101n / 100n) throw Error(`quote output ${out} implausibly above input ${amount}`);
  return { out, feeBps: Number(((amount - out) * 1_000_000n) / amount) / 100, quoteId: q.quoteId ?? null, etaMs: q.estCheckoutTimeMs ?? null };
}

/** POST /withdraw returns per-request addresses; only the evm one is used (the Safe lives on Polygon). */
export function withdrawAddress(resp, forbidden = []) {
  const a = resp?.address?.evm;
  if (!isAddr(a)) throw Error('withdraw response has no evm address');
  if ([PUSD, USDG, ...forbidden].filter(Boolean).some((x) => lc(x) === lc(a))) throw Error(`bridge address ${a} collides with a known address`);
  return getAddress(a);
}

export function transferTx(to, amount) {
  return { to: PUSD, value: '0', data: erc20.encodeFunctionData('transfer', [to, amount]) };
}

/** /status/{address}: completed only when a COMPLETED transaction delivers USDG on 4663. */
export function statusOf(resp) {
  const txs = Array.isArray(resp?.transactions) ? resp.transactions : [];
  if (!txs.length) return { state: 'none' };
  if (txs.length !== 1) return { state: 'ambiguous', count: txs.length };
  const t = txs[0];
  if (t.status === 'FAILED') return { state: 'failed' };
  if (t.status !== 'COMPLETED') return { state: 'pending', status: t.status };
  if (String(t.toChainId) !== String(ROBINHOOD) || lc(t.toTokenAddress) !== lc(USDG)) return { state: 'wrong_route' };
  return { state: 'completed', txHash: t.txHash ?? null };
}

async function api(method, path, body) {
  const r = await fetch(BRIDGE_API + path, {
    method, headers: { 'content-type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(20_000),
  });
  const text = await r.text();
  if (!r.ok) throw Error(`${method} ${path} HTTP ${r.status}: ${text.slice(0, 200)}`);
  return JSON.parse(text);
}

async function main() {
  const input = JSON.parse((await import('node:fs')).readFileSync(0, 'utf8'));
  const maxFeeBps = Number(process.env.ARB_BRIDGE_MAX_FEE_BPS || 50);
  const safe = process.env.POLY_PROXY_ADDRESS || '';
  const recipient = process.env.ARB_SERVICER_WALLET || '';
  if (input.action === 'status') {
    if (!isAddr(input.depositAddress)) throw Error('depositAddress required');
    console.log(JSON.stringify(statusOf(await api('GET', `/status/${input.depositAddress}`))));
    return;
  }
  if (!['quote', 'withdraw'].includes(input.action)) throw Error('unsupported action');
  if (!isAddr(safe) || !isAddr(recipient)) throw Error('POLY_PROXY_ADDRESS and ARB_SERVICER_WALLET required');
  const amount = amountRaw(input.amountRaw);
  const q = validateQuote(await api('POST', '/quote', quoteRequest(amount, recipient)), amount, maxFeeBps);
  if (input.action === 'quote') { console.log(JSON.stringify({ quoted: true, amountRaw: amount.toString(), outRaw: q.out.toString(), feeBps: q.feeBps, etaMs: q.etaMs })); return; }

  if (process.env.ARB_POLY_BRIDGE_ROUTE_VERIFIED !== 'true') throw Error('Polymarket bridge withdrawal disabled until an approved small withdrawal verifies the route');
  const resp = await api('POST', '/withdraw', { address: getAddress(safe), toChainId: String(ROBINHOOD), toTokenAddress: USDG, recipientAddr: getAddress(recipient) });
  const to = withdrawAddress(resp, [safe, recipient]);
  const { RelayClient, RelayerTxType } = await import('@polymarket/builder-relayer-client');
  const { deriveSafe } = await import('@polymarket/builder-relayer-client/dist/builder/derive.js');
  const { BuilderConfig } = await import('@polymarket/builder-signing-sdk');
  const { createWalletClient, http } = await import('viem');
  const { polygon } = await import('viem/chains');
  const { privateKeyToAccount } = await import('viem/accounts');
  const key = (process.env.POLY_PRIVATE_KEY || '').trim();
  const account = privateKeyToAccount(key.startsWith('0x') ? key : `0x${key}`);
  const wallet = createWalletClient({ account, chain: polygon, transport: http(process.env.POLYGON_RPC_URL) });
  const client = new RelayClient('https://relayer-v2.polymarket.com', POLYGON, wallet,
    new BuilderConfig({ localBuilderCreds: { key: process.env.POLY2_API_KEY, secret: process.env.POLY2_API_SECRET, passphrase: process.env.POLY2_API_PASSPHRASE } }),
    RelayerTxType.SAFE);
  if (lc(deriveSafe(account.address, client.contractConfig.SafeContracts.SafeFactory)) !== lc(safe)) throw Error('signer does not control POLY_PROXY_ADDRESS');
  // The bridge address is printed BEFORE submission so a crash after this line is still reconcilable via /status.
  console.error(JSON.stringify({ bridgeAddress: to, amountRaw: amount.toString() }));
  let result;
  try {
    result = await (await client.execute([transferTx(to, amount)], 'MILORD pUSD -> USDG (4663) recall')).wait();
  } catch (e) {
    console.log(JSON.stringify({ unknown: true, bridgeAddress: to, error: String(e?.message ?? e).slice(0, 300) }));
    process.exitCode = 3;
    return;
  }
  if (!result?.transactionHash) { console.log(JSON.stringify({ unknown: true, bridgeAddress: to })); process.exitCode = 3; return; }
  if (result.proxyAddress && lc(result.proxyAddress) !== lc(safe)) {
    // submitted, but not from the expected Safe: a person must reconcile it; never reported as a failure-to-send
    console.log(JSON.stringify({ unknown: true, bridgeAddress: to, txHash: null, error: `relayer used ${result.proxyAddress}` }));
    process.exitCode = 3;
    return;
  }
  console.log(JSON.stringify({ txHash: result.transactionHash, bridgeAddress: to, amountRaw: amount.toString(), outRaw: q.out.toString() }));
}

if (process.argv[1] && new URL(import.meta.url).pathname === process.argv[1]) main().catch((e) => { console.log(JSON.stringify({ error: e.message })); process.exitCode = 1; });
