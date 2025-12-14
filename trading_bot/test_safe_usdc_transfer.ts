#!/usr/bin/env npx tsx
/**
 * PoC: USDC.e transfer from Polymarket MetaMask Safe wallet via Builder Relayer
 * 
 * VERBOSE DEBUG VERSION - shows every step of encoding
 * 
 * Usage:
 *   npx tsx trading_bot/test_safe_usdc_transfer.ts
 */

import { Wallet } from '@ethersproject/wallet';
import { JsonRpcProvider } from '@ethersproject/providers';
import { Interface } from '@ethersproject/abi';
import { Contract } from '@ethersproject/contracts';
import { BigNumber } from '@ethersproject/bignumber';
import { RelayClient, RelayerTxType, Transaction } from '@polymarket/builder-relayer-client';
import { BuilderConfig, BuilderApiKeyCreds } from '@polymarket/builder-signing-sdk';
import * as dotenv from 'dotenv';

dotenv.config();

// ============ CONSTANTS ============
const USDC_E_ADDRESS = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';  // USDC.e on Polygon (6 decimals)
const USDC_DECIMALS = 6;  // USDC.e has 6 decimals, NOT 18
const RELAYER_URL = 'https://relayer-v2.polymarket.com/';
const POLYGON_CHAIN_ID = 137;

// ============ POC LIMITS ============
const MAX_USDC_PER_TX = 1.0;        // Max 1 USDC for PoC safety
const POC_AMOUNT_USDC = 0.01;       // Send 0.01 USDC.e

async function main() {
  console.log('╔════════════════════════════════════════════════════════════════╗');
  console.log('║  VERBOSE DEBUG: Safe USDC.e Transfer PoC                       ║');
  console.log('╚════════════════════════════════════════════════════════════════╝\n');

  // ============ STEP 1: Load Environment Variables ============
  console.log('═══ STEP 1: Loading Environment Variables ═══\n');
  
  const builderKey = process.env.METAMASK_BUILDER_API_KEY;
  const builderSecret = process.env.METAMASK_BUILDER_SECRET;
  const builderPassphrase = process.env.METAMASK_BUILDER_PASSPHRASE;
  const privateKey = process.env.METAMASK_PRIVATE_KEY;
  const treasuryAddress = process.env.TREASURY_ADDRESS;
  const polygonRpc = process.env.POLYGON_RPC || 'https://polygon-rpc.com';

  console.log('METAMASK_BUILDER_API_KEY:', builderKey ? `✓ (${builderKey.substring(0, 8)}...)` : '✗ MISSING');
  console.log('METAMASK_BUILDER_SECRET:', builderSecret ? '✓ (hidden)' : '✗ MISSING');
  console.log('METAMASK_BUILDER_PASSPHRASE:', builderPassphrase ? '✓ (hidden)' : '✗ MISSING');
  console.log('METAMASK_PRIVATE_KEY:', privateKey ? '✓ (hidden)' : '✗ MISSING');
  console.log('TREASURY_ADDRESS:', treasuryAddress || '✗ MISSING');
  console.log('POLYGON_RPC:', polygonRpc);

  if (!builderKey || !builderSecret || !builderPassphrase || !privateKey || !treasuryAddress) {
    console.error('\n❌ Missing required environment variables');
    process.exit(1);
  }

  // ============ STEP 2: Initialize Wallet ============
  console.log('\n═══ STEP 2: Initialize Wallet ═══\n');
  
  const provider = new JsonRpcProvider(polygonRpc);
  const wallet = new Wallet(privateKey, provider);
  
  console.log('EOA Address (from private key):', wallet.address);
  console.log('Treasury Address:', treasuryAddress);

  // ============ STEP 3: Calculate Amount (CRITICAL - DECIMAL HANDLING) ============
  console.log('\n═══ STEP 3: Calculate Amount (DECIMAL HANDLING) ═══\n');
  
  console.log('USDC.e contract:', USDC_E_ADDRESS);
  console.log('USDC.e decimals:', USDC_DECIMALS, '(NOT 18!)');
  console.log('');
  console.log('Amount we want to send:', POC_AMOUNT_USDC, 'USDC.e');
  console.log('');
  console.log('Calculation:');
  console.log('  amountInBaseUnits = POC_AMOUNT_USDC * (10 ** USDC_DECIMALS)');
  console.log('  amountInBaseUnits =', POC_AMOUNT_USDC, '* (10 **', USDC_DECIMALS, ')');
  console.log('  amountInBaseUnits =', POC_AMOUNT_USDC, '*', Math.pow(10, USDC_DECIMALS));
  
  const amountInBaseUnits = POC_AMOUNT_USDC * Math.pow(10, USDC_DECIMALS);
  console.log('  amountInBaseUnits =', amountInBaseUnits);
  
  // Use BigNumber to avoid floating point issues
  const amountBN = BigNumber.from(Math.floor(amountInBaseUnits));
  console.log('');
  console.log('As BigNumber:', amountBN.toString());
  console.log('As hex:', amountBN.toHexString());
  console.log('');
  
  // Verify: if this were wrong (e.g., 18 decimals), it would be:
  const wrongAmount18Decimals = POC_AMOUNT_USDC * Math.pow(10, 18);
  console.log('⚠️  IF we mistakenly used 18 decimals, amount would be:', wrongAmount18Decimals.toExponential());
  console.log('⚠️  That would be:', wrongAmount18Decimals / Math.pow(10, 6), 'USDC.e worth!');
  console.log('');
  console.log('✓ We are using 6 decimals, so amount is:', amountBN.toString(), 'base units =', 
              Number(amountBN.toString()) / Math.pow(10, 6), 'USDC.e');

  // Safety check
  if (POC_AMOUNT_USDC > MAX_USDC_PER_TX) {
    console.error(`\n❌ Amount ${POC_AMOUNT_USDC} exceeds PoC max of ${MAX_USDC_PER_TX} USDC`);
    process.exit(1);
  }

  // ============ STEP 4: Build Calldata (ERC20 transfer) ============
  console.log('\n═══ STEP 4: Build ERC20 transfer() Calldata ═══\n');
  
  const erc20Interface = new Interface([
    'function transfer(address to, uint256 amount) returns (bool)'
  ]);
  
  console.log('Function signature: transfer(address,uint256)');
  console.log('Function selector:', erc20Interface.getSighash('transfer'));
  console.log('');
  console.log('Parameters:');
  console.log('  to (address):', treasuryAddress);
  console.log('  amount (uint256):', amountBN.toString(), '(decimal)');
  console.log('  amount (uint256):', amountBN.toHexString(), '(hex)');
  console.log('');
  
  const calldata = erc20Interface.encodeFunctionData('transfer', [
    treasuryAddress,
    amountBN
  ]);
  
  console.log('Encoded calldata (full):');
  console.log(calldata);
  console.log('');
  console.log('Calldata breakdown:');
  console.log('  Bytes 0-3 (selector):', calldata.substring(0, 10));
  console.log('  Bytes 4-35 (address):', '0x' + calldata.substring(10, 74));
  console.log('  Bytes 36-67 (amount):', '0x' + calldata.substring(74, 138));
  console.log('');
  
  // Decode to verify
  const decodedAmount = BigNumber.from('0x' + calldata.substring(74, 138));
  console.log('Verification - decoded amount from calldata:');
  console.log('  As decimal:', decodedAmount.toString());
  console.log('  As USDC.e:', Number(decodedAmount.toString()) / Math.pow(10, 6));
  
  if (!decodedAmount.eq(amountBN)) {
    console.error('\n❌ ENCODING ERROR: Decoded amount does not match input!');
    process.exit(1);
  }
  console.log('  ✓ Encoding verified correct');

  // ============ STEP 5: Check Safe Wallet Balance ============
  console.log('\n═══ STEP 5: Check Balances ═══\n');
  
  const usdcContract = new Contract(
    USDC_E_ADDRESS,
    ['function balanceOf(address) view returns (uint256)'],
    provider
  );

  // We need to know the Safe address to check its balance
  // The Safe is derived from the EOA - let's get it from the relay payload
  
  // ============ STEP 6: Initialize Relayer Client ============
  console.log('\n═══ STEP 6: Initialize Relayer Client ═══\n');

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
      RelayerTxType.SAFE
    );
    console.log('✓ RelayClient initialized');
    console.log('  Relayer URL:', RELAYER_URL);
    console.log('  Chain ID:', POLYGON_CHAIN_ID);
    console.log('  TX Type: SAFE (for MetaMask wallets)');
  } catch (err) {
    console.error('❌ RelayClient error:', err instanceof Error ? err.message : err);
    process.exit(1);
  }

  // Get Safe address from relay payload
  console.log('\n--- Getting Safe Address ---');
  let safeAddress: string | undefined;
  try {
    const relayPayload = await client.getRelayPayload(wallet.address, 'SAFE');
    safeAddress = relayPayload.address;
    console.log('Relay payload received:');
    console.log('  Safe Address:', safeAddress);
    console.log('  Nonce:', relayPayload.nonce);
  } catch (err) {
    console.warn('⚠️ Could not get relay payload:', err instanceof Error ? err.message : err);
  }

  // Now check balances
  console.log('\n--- Checking USDC.e Balances ---');
  try {
    const eoaBalance = await usdcContract.balanceOf(wallet.address);
    console.log('EOA USDC.e balance:', (Number(eoaBalance.toString()) / 1e6).toFixed(6), 'USDC.e');
  } catch (err) {
    console.warn('⚠️ Could not check EOA balance');
  }

  if (safeAddress) {
    try {
      const safeBalance = await usdcContract.balanceOf(safeAddress);
      const safeBalanceUsdc = Number(safeBalance.toString()) / 1e6;
      console.log('Safe USDC.e balance:', safeBalanceUsdc.toFixed(6), 'USDC.e');
      console.log('');
      
      if (safeBalanceUsdc < POC_AMOUNT_USDC) {
        console.error(`❌ Insufficient Safe balance: ${safeBalanceUsdc} < ${POC_AMOUNT_USDC}`);
        console.error('   The Safe wallet needs more USDC.e to complete this transfer.');
        process.exit(1);
      }
      console.log('✓ Safe has sufficient balance for transfer');
    } catch (err) {
      console.warn('⚠️ Could not check Safe balance');
    }
  }

  // ============ STEP 7: Build Transaction Object ============
  console.log('\n═══ STEP 7: Build Transaction Object ═══\n');

  const tx: Transaction = {
    to: USDC_E_ADDRESS,
    data: calldata,
    value: '0',
  };

  console.log('Transaction object:');
  console.log(JSON.stringify(tx, null, 2));
  console.log('');
  console.log('This transaction will:');
  console.log('  1. Be submitted to the Polymarket relayer');
  console.log('  2. The relayer will call execTransaction() on the Safe');
  console.log('  3. The Safe will call USDC.e.transfer(treasury, amount)');
  console.log('  4. Treasury receives', POC_AMOUNT_USDC, 'USDC.e');

  // ============ STEP 8: Submit to Relayer ============
  console.log('\n═══ STEP 8: Submit to Relayer ═══\n');

  try {
    console.log('📤 Calling client.execute([tx])...\n');
    const response = await client.execute([tx]);
    
    console.log('📋 Relayer Response:');
    console.log('  Transaction ID:', response.transactionID);
    console.log('  State:', response.state);
    console.log('  TX Hash:', response.transactionHash || 'pending');

    console.log('\n⏳ Polling for confirmation (max 60 attempts, 2s interval)...');
    const result = await client.pollUntilState(
      response.transactionID,
      ['CONFIRMED', 'MINED'],
      'FAILED',
      60,
      2000
    );
    
    console.log('\n📋 Final Result:');
    if (result) {
      console.log('  TX Hash:', result.transactionHash);
      console.log('  State:', result.state);

      if (result.transactionHash && result.state !== 'FAILED') {
        console.log('\n╔════════════════════════════════════════════════════════════════╗');
        console.log('║  ✅ SUCCESS! Transaction confirmed.                            ║');
        console.log('╚════════════════════════════════════════════════════════════════╝');
        console.log(`\nPolygonscan: https://polygonscan.com/tx/${result.transactionHash}`);
      } else {
        console.log('\n❌ Transaction failed on-chain');
        console.log('Check Polygonscan for revert reason');
      }
    } else {
      console.log('\n⚠️ Polling timed out - transaction may still be pending');
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
