#!/usr/bin/env npx tsx
/**
 * PoC: USDC.e transfer from Polymarket Magic proxy via Builder Relayer
 * 
 * Proves we can move USDC.e out of the Magic proxy without UI by using
 * Polymarket's builder relayer in PROXY mode.
 * 
 * Usage:
 *   npx tsx trading_bot/test_proxy_usdc_transfer.ts
 */

import { Wallet } from '@ethersproject/wallet';
import { JsonRpcProvider } from '@ethersproject/providers';
import { Interface } from '@ethersproject/abi';
import { Contract } from '@ethersproject/contracts';
import { RelayClient, RelayerTxType, Transaction } from '@polymarket/builder-relayer-client';
import { BuilderConfig, BuilderApiKeyCreds } from '@polymarket/builder-signing-sdk';
import * as dotenv from 'dotenv';

dotenv.config();

const USDC_E_ADDRESS = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const RELAYER_URL = 'https://relayer-v2.polymarket.com/';
const POLYGON_CHAIN_ID = 137;
const DEFAULT_PROXY = '0x4eCD29f6b1d336dA51a90E835CC211eDEABFc9ff';

const MAX_USDC_PER_TX = 1.0;
const POC_AMOUNT_USDC = 0.01;

async function main() {
  console.log('=== Polymarket Proxy USDC.e Transfer PoC ===\n');

  const builderKey = process.env.POLYMARKET_BUILDER_API_KEY;
  const builderSecret = process.env.POLYMARKET_BUILDER_SECRET;
  const builderPassphrase = process.env.POLYMARKET_BUILDER_PASSPHRASE;
  const privateKey = process.env.POLYMARKET_PRIVATE_KEY;
  const proxyAddress = process.env.POLYMARKET_PROXY_ADDRESS || DEFAULT_PROXY;
  const treasuryAddress = process.env.TREASURY_ADDRESS;
  const polygonRpc = process.env.POLYGON_RPC || 'https://polygon-rpc.com';

  console.log('Environment check:');
  console.log('  POLYMARKET_BUILDER_API_KEY:', builderKey ? '✓ set' : '✗ missing');
  console.log('  POLYMARKET_BUILDER_SECRET:', builderSecret ? '✓ set' : '✗ missing');
  console.log('  POLYMARKET_BUILDER_PASSPHRASE:', builderPassphrase ? '✓ set' : '✗ missing');
  console.log('  POLYMARKET_PRIVATE_KEY:', privateKey ? '✓ set' : '✗ missing');
  console.log('  POLYMARKET_PROXY_ADDRESS:', proxyAddress);
  console.log('  TREASURY_ADDRESS:', treasuryAddress || '✗ missing');
  console.log('');

  if (!builderKey || !builderSecret || !builderPassphrase) {
    console.error('❌ Missing builder credentials. Need: POLYMARKET_BUILDER_API_KEY, POLYMARKET_BUILDER_SECRET, POLYMARKET_BUILDER_PASSPHRASE');
    process.exit(1);
  }

  if (!privateKey) {
    console.error('❌ Missing POLYMARKET_PRIVATE_KEY (EOA that controls the proxy)');
    process.exit(1);
  }

  if (!treasuryAddress) {
    console.error('❌ Missing TREASURY_ADDRESS');
    process.exit(1);
  }

  const provider = new JsonRpcProvider(polygonRpc);
  const wallet = new Wallet(privateKey, provider);

  console.log('Wallet (EOA):', wallet.address);
  console.log('Proxy:', proxyAddress);
  console.log('Treasury:', treasuryAddress);
  console.log('Amount:', POC_AMOUNT_USDC, 'USDC.e');
  console.log('');

  if (POC_AMOUNT_USDC > MAX_USDC_PER_TX) {
    console.error(`❌ Amount ${POC_AMOUNT_USDC} exceeds PoC max of ${MAX_USDC_PER_TX} USDC`);
    process.exit(1);
  }

  const usdcContract = new Contract(
    USDC_E_ADDRESS,
    ['function balanceOf(address) view returns (uint256)'],
    provider
  );

  try {
    const balance = await usdcContract.balanceOf(proxyAddress);
    const balanceUsdc = Number(balance.toString()) / 1e6;
    console.log('Proxy USDC.e balance:', balanceUsdc.toFixed(2));
    
    if (balanceUsdc < POC_AMOUNT_USDC) {
      console.error(`❌ Insufficient balance: ${balanceUsdc} < ${POC_AMOUNT_USDC}`);
      process.exit(1);
    }
  } catch (err) {
    console.warn('⚠️ Could not check balance, proceeding anyway');
  }

  console.log('\n--- Building Transaction ---');

  const erc20Interface = new Interface([
    'function transfer(address to, uint256 amount) returns (bool)'
  ]);
  
  const amountWei = Math.floor(POC_AMOUNT_USDC * 1e6);
  const calldata = erc20Interface.encodeFunctionData('transfer', [
    treasuryAddress,
    amountWei
  ]);

  console.log('Target contract:', USDC_E_ADDRESS, '(USDC.e)');
  console.log('Function: transfer(address,uint256)');
  console.log('Recipient:', treasuryAddress);
  console.log('Amount (wei):', amountWei.toString());
  console.log('Calldata:', calldata);

  console.log('\n--- Initializing Relayer Client ---');

  const builderCreds: BuilderApiKeyCreds = {
    key: builderKey,
    secret: builderSecret,
    passphrase: builderPassphrase,
  };

  let builderConfig: BuilderConfig;
  try {
    builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });
    console.log('✓ BuilderConfig initialized');
  } catch (err) {
    console.error('❌ BuilderConfig error:', err instanceof Error ? err.message : err);
    process.exit(1);
  }

  let client: RelayClient;
  try {
    client = new RelayClient(
      RELAYER_URL,
      POLYGON_CHAIN_ID,
      wallet,
      builderConfig,
      RelayerTxType.PROXY
    );
    console.log('✓ RelayClient initialized (PROXY mode)');
  } catch (err) {
    console.error('❌ RelayClient error:', err instanceof Error ? err.message : err);
    process.exit(1);
  }

  console.log('\n--- Submitting to Relayer ---');

  const tx: Transaction = {
    to: USDC_E_ADDRESS,
    data: calldata,
    value: '0',
  };

  try {
    console.log('📤 Sending transaction...');
    const response = await client.execute([tx]);
    
    console.log('\n📋 Relayer Response:');
    console.log('  Transaction ID:', response.transactionID || 'N/A');
    console.log('  Full response:', JSON.stringify(response, null, 2));

    console.log('\n⏳ Waiting for confirmation...');
    const result = await client.pollUntilState(
      response.transactionID,
      ['CONFIRMED', 'MINED'],
      'FAILED',
      60,
      2000
    );
    
    console.log('\n📋 Final Result:');
    if (result) {
      console.log('  TX Hash:', result.transactionHash || 'N/A');
      console.log('  State:', result.state || 'N/A');
      console.log('  Full result:', JSON.stringify(result, null, 2));

      if (result.transactionHash) {
        console.log('\n✅ SUCCESS! Transaction confirmed.');
        console.log(`   https://polygonscan.com/tx/${result.transactionHash}`);
      }
    } else {
      console.log('\n⚠️ Transaction may have failed or timed out');
    }

  } catch (err: unknown) {
    console.error('\n❌ RELAYER ERROR:');
    
    if (err instanceof Error) {
      console.error('Message:', err.message);
      
      if ('response' in err) {
        const response = (err as any).response;
        console.error('Response status:', response?.status);
        console.error('Response data:', JSON.stringify(response?.data, null, 2));
      }
      
      if ('body' in err) {
        console.error('Body:', (err as any).body);
      }
    } else {
      console.error('Error:', err);
    }
    
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
