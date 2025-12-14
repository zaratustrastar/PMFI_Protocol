#!/usr/bin/env npx tsx
/**
 * Test USDC.e transfer from Polymarket Magic proxy via Builder Relayer
 * 
 * This PoC tests if we can execute arbitrary ERC20 transfers through
 * Polymarket's relayer infrastructure for Magic/email wallet users.
 * 
 * Usage:
 *   npx tsx test_proxy_usdc_transfer.ts 0.01 --dry-run   # Dry run
 *   npx tsx test_proxy_usdc_transfer.ts 0.01             # Execute
 */

import { ethers } from 'ethers';
import { RelayClient, RelayerTxType } from '@polymarket/builder-relayer-client';
import { BuilderConfig, BuilderApiKeyCreds } from '@polymarket/builder-signing-sdk';
import * as dotenv from 'dotenv';

dotenv.config();

const USDC_E = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const RELAYER_URL = 'https://relayer-v2.polymarket.com/';
const POLYGON_CHAIN_ID = 137;

async function main() {
  const privateKey = process.env.POLYMARKET_PRIVATE_KEY;
  const treasuryAddress = process.env.TREASURY_ADDRESS;
  const dryRun = process.argv.includes('--dry-run');
  const amountUsdc = parseFloat(process.argv[2] || '0.01');
  
  if (!privateKey) {
    console.error('❌ Missing POLYMARKET_PRIVATE_KEY');
    process.exit(1);
  }
  
  if (!treasuryAddress) {
    console.error('❌ Missing TREASURY_ADDRESS');
    process.exit(1);
  }
  
  const provider = new ethers.JsonRpcProvider('https://polygon-rpc.com');
  const wallet = new ethers.Wallet(privateKey, provider);
  
  console.log('=== Proxy USDC.e Transfer PoC ===');
  console.log('EOA:', wallet.address);
  console.log('Treasury:', treasuryAddress);
  console.log('Amount:', amountUsdc, 'USDC.e');
  console.log('Dry run:', dryRun);
  
  const apiKey = process.env.POLYMARKET_API_KEY || '';
  const apiSecret = process.env.POLYMARKET_API_SECRET || '';
  
  console.log('\nBuilder credentials:');
  console.log('  API key available:', !!apiKey);
  console.log('  API secret available:', !!apiSecret);
  
  if (!apiKey || !apiSecret) {
    console.error('❌ Missing POLYMARKET_API_KEY or POLYMARKET_API_SECRET');
    process.exit(1);
  }
  
  const builderCreds: BuilderApiKeyCreds = {
    key: apiKey,
    secret: apiSecret,
    passphrase: '', // May be empty for Polymarket
  };
  
  const builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });
  
  console.log('\nInitializing RelayClient with PROXY type...');
  
  let client: RelayClient;
  try {
    client = new RelayClient(
      RELAYER_URL,
      POLYGON_CHAIN_ID,
      wallet,
      builderConfig,
      RelayerTxType.PROXY
    );
    console.log('✅ RelayClient initialized');
  } catch (error) {
    console.error('❌ Failed to initialize RelayClient:', error);
    process.exit(1);
  }
  
  const erc20Interface = new ethers.Interface([
    'function transfer(address to, uint256 amount) returns (bool)'
  ]);
  const amount = BigInt(Math.floor(amountUsdc * 1e6));
  const transferData = erc20Interface.encodeFunctionData('transfer', [
    treasuryAddress, amount
  ]);
  
  const tx = {
    to: USDC_E,
    data: transferData,
    value: '0'
  };
  
  console.log('\nTransaction payload:');
  console.log('  to:', tx.to);
  console.log('  data:', tx.data);
  console.log('  value:', tx.value);
  console.log('  (transfer', amountUsdc, 'USDC.e to', treasuryAddress + ')');
  
  if (dryRun) {
    console.log('\n[DRY RUN] Would submit via relayer - exiting');
    console.log('Run without --dry-run to execute');
    return;
  }
  
  console.log('\n📤 Submitting to Polymarket relayer...');
  
  try {
    const response = await client.execute(tx);
    console.log('\n📝 Relayer response:', JSON.stringify(response, null, 2));
    
    console.log('\n⏳ Waiting for confirmation...');
    const result = await response.wait();
    console.log('\n📝 Result:', JSON.stringify(result, null, 2));
    
    if (result && result.transactionHash) {
      console.log('\n✅ SUCCESS!');
      console.log('TX Hash:', result.transactionHash);
      console.log('State:', result.state);
    } else {
      console.log('\n⚠️ Transaction submitted but no hash returned');
    }
  } catch (error: unknown) {
    console.error('\n❌ RELAYER ERROR:');
    if (error instanceof Error) {
      console.error('Message:', error.message);
      if ('response' in error) {
        console.error('Response:', (error as { response?: unknown }).response);
      }
    } else {
      console.error(error);
    }
    process.exit(1);
  }
}

main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});
