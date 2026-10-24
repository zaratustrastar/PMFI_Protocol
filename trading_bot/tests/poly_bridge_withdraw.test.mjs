import test from 'node:test';
import assert from 'node:assert/strict';
import { Interface } from 'ethers';
import { amountRaw, quoteRequest, validateQuote, withdrawAddress, transferTx, statusOf, PUSD, USDG } from '../poly_bridge_withdraw.mjs';

const R = '0x' + '11'.repeat(20);
const B = '0x' + '22'.repeat(20);

test('amount is exact raw units within the engine bounds', () => {
  assert.equal(amountRaw('5000000'), 5_000_000n);
  for (const bad of ['4999999', '5000000001', '5.5', '-1', '1e9', '']) assert.throws(() => amountRaw(bad));
});

test('quote request is pUSD on Polygon to USDG on Robinhood Chain', () => {
  const q = quoteRequest(10_000_000n, R);
  assert.deepEqual([q.fromChainId, q.fromTokenAddress, q.toChainId, q.toTokenAddress, q.fromAmountBaseUnit], ['137', PUSD, '4663', USDG, '10000000']);
  assert.throws(() => quoteRequest(1n, 'nope'));
});

test('quote fee bound and plausibility', () => {
  assert.equal(validateQuote({ estToTokenBaseUnit: '9990000' }, 10_000_000n, 50).feeBps, 10);
  assert.throws(() => validateQuote({ estToTokenBaseUnit: '9940000' }, 10_000_000n, 50));
  assert.throws(() => validateQuote({}, 10_000_000n, 50));
  assert.throws(() => validateQuote({ estToTokenBaseUnit: '20000000' }, 10_000_000n, 50));
});

test('withdraw address must be a fresh evm address', () => {
  assert.equal(withdrawAddress({ address: { evm: B } }).toLowerCase(), B);
  assert.throws(() => withdrawAddress({ address: { svm: 'x' } }));
  assert.throws(() => withdrawAddress({ address: { evm: R } }, [R]));
  assert.throws(() => withdrawAddress({ address: { evm: PUSD } }));
});

test('the Safe transaction is one exact pUSD transfer', () => {
  const tx = transferTx(B, 7_000_000n);
  assert.equal(tx.to, PUSD);
  assert.equal(tx.value, '0');
  const [to, amt] = new Interface(['function transfer(address,uint256)']).decodeFunctionData('transfer', tx.data);
  assert.equal(to.toLowerCase(), B);
  assert.equal(amt, 7_000_000n);
});

test('status needs exactly one COMPLETED USDG delivery on 4663', () => {
  assert.equal(statusOf({ transactions: [] }).state, 'none');
  assert.equal(statusOf({ transactions: [{ status: 'PROCESSING' }] }).state, 'pending');
  assert.equal(statusOf({ transactions: [{ status: 'FAILED' }] }).state, 'failed');
  assert.equal(statusOf({ transactions: [{ status: 'COMPLETED', toChainId: '4663', toTokenAddress: USDG, txHash: '0xab' }] }).state, 'completed');
  assert.equal(statusOf({ transactions: [{ status: 'COMPLETED', toChainId: '8453', toTokenAddress: USDG }] }).state, 'wrong_route');
  assert.equal(statusOf({ transactions: [{}, {}] }).state, 'ambiguous');
});
