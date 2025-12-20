#!/usr/bin/env npx tsx
/**
 * Safe Proxy Withdraw - Polymarket Safe Wallet Withdrawal via RelayClient
 * 
 * Uses @polymarket/builder-relayer-client for proper Safe proxy support.
 * This script withdraws USDC.e from Polymarket Safe wallet to EOA on Polygon,
 * then bridges to Base vault via Relay.
 * 
 * Flow: PM Safe (Polygon) → EOA (Polygon) → Relay Bridge → Vault (Base)
 */

import { createWalletClient, http, encodeFunctionData, parseUnits, formatUnits, type Hex } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import { RelayClient } from '@polymarket/builder-relayer-client';
import { BuilderConfig } from '@polymarket/builder-signing-sdk';
import * as dotenv from 'dotenv';

dotenv.config();

// =============================================================================
// CONFIGURATION
// =============================================================================

const POLYGON_RPC_URL = process.env.POLYGON_RPC_URL || 'https://polygon-rpc.com';
const POLYGON_CHAIN_ID = 137;

const POLYMARKET_PROXY_ADDRESS = process.env.POLYMARKET_PROXY_ADDRESS || '';
const POLYMARKET_PRIVATE_KEY = process.env.POLYMARKET_PRIVATE_KEY || '';
const TREASURY_ADDRESS = process.env.TREASURY_ADDRESS || '';

// Builder API credentials for Polymarket
const BUILDER_API_KEY = process.env.POLYMARKET_BUILDER_API_KEY || '';
const BUILDER_SECRET = process.env.POLYMARKET_BUILDER_SECRET || '';
const BUILDER_PASSPHRASE = process.env.POLYMARKET_BUILDER_PASSPHRASE || '';

// Contracts
const USDC_E_POLYGON = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const RELAY_API_URL = 'https://api.relay.link';
const BASE_CHAIN_ID = 8453;
const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

// Relayer URL
const RELAYER_URL = 'https://relayer-v2.polymarket.com';

// Security guardrails
const MAX_PER_TX_USDC = 5000;
const MIN_WITHDRAWAL_USDC = 5;

// ERC20 ABI
const ERC20_ABI = [
  {
    name: 'transfer',
    type: 'function',
    inputs: [
      { name: 'to', type: 'address' },
      { name: 'amount', type: 'uint256' },
    ],
    outputs: [{ type: 'bool' }],
  },
  {
    name: 'balanceOf',
    type: 'function',
    inputs: [{ name: 'account', type: 'address' }],
    outputs: [{ type: 'uint256' }],
    stateMutability: 'view',
  },
  {
    name: 'approve',
    type: 'function',
    inputs: [
      { name: 'spender', type: 'address' },
      { name: 'amount', type: 'uint256' },
    ],
    outputs: [{ type: 'bool' }],
  },
] as const;

// =============================================================================
// LOGGING
// =============================================================================

function log(level: 'INFO' | 'WARN' | 'ERROR', message: string, data?: unknown): void {
  const timestamp = new Date().toISOString();
  const prefix = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : '📝';
  console.log(`${timestamp} ${prefix} [SafeProxyWithdraw] ${message}`, data || '');
}

// =============================================================================
// RELAY CLIENT SETUP
// =============================================================================

async function createRelayClient(): Promise<RelayClient> {
  if (!POLYMARKET_PRIVATE_KEY) {
    throw new Error('POLYMARKET_PRIVATE_KEY not set');
  }
  
  // Clean the private key
  let cleanKey = POLYMARKET_PRIVATE_KEY.trim();
  if (!cleanKey.startsWith('0x')) {
    cleanKey = '0x' + cleanKey;
  }
  
  const account = privateKeyToAccount(cleanKey as Hex);
  
  const wallet = createWalletClient({
    account,
    chain: polygon,
    transport: http(POLYGON_RPC_URL),
  });

  log('INFO', `EOA address: ${account.address}`);
  log('INFO', `Proxy address: ${POLYMARKET_PROXY_ADDRESS}`);

  // Check if we have builder credentials
  if (!BUILDER_API_KEY || !BUILDER_SECRET || !BUILDER_PASSPHRASE) {
    log('WARN', 'Builder API credentials not set - using direct EOA mode');
    // Create client without builder config (uses EOA-derived proxy)
    const client = new RelayClient(
      RELAYER_URL,
      POLYGON_CHAIN_ID,
      wallet,
    );
    return client;
  }

  log('INFO', 'Using Builder API credentials');
  
  const builderConfig = new BuilderConfig({
    localBuilderCreds: {
      key: BUILDER_API_KEY,
      secret: BUILDER_SECRET,
      passphrase: BUILDER_PASSPHRASE,
    },
  });

  const client = new RelayClient(
    RELAYER_URL,
    POLYGON_CHAIN_ID,
    wallet,
    builderConfig,
  );

  return client;
}

// =============================================================================
// BALANCE CHECKING
// =============================================================================

async function getProxyBalance(): Promise<number> {
  const response = await fetch(POLYGON_RPC_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'eth_call',
      params: [
        {
          to: USDC_E_POLYGON,
          data: encodeFunctionData({
            abi: ERC20_ABI,
            functionName: 'balanceOf',
            args: [POLYMARKET_PROXY_ADDRESS as Hex],
          }),
        },
        'latest',
      ],
    }),
  });
  
  const data = await response.json();
  if (data.error) {
    throw new Error(`RPC error: ${data.error.message}`);
  }
  
  const balance = BigInt(data.result);
  return Number(formatUnits(balance, 6));
}

async function getEOABalance(eoaAddress: string): Promise<number> {
  const response = await fetch(POLYGON_RPC_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'eth_call',
      params: [
        {
          to: USDC_E_POLYGON,
          data: encodeFunctionData({
            abi: ERC20_ABI,
            functionName: 'balanceOf',
            args: [eoaAddress as Hex],
          }),
        },
        'latest',
      ],
    }),
  });
  
  const data = await response.json();
  if (data.error) {
    throw new Error(`RPC error: ${data.error.message}`);
  }
  
  const balance = BigInt(data.result);
  return Number(formatUnits(balance, 6));
}

// =============================================================================
// RELAY BRIDGE
// =============================================================================

interface BridgeQuote {
  requestId: string;
  amountIn: bigint;
  amountOut: bigint;
  feeUsd: number;
  txData: { to: string; data: string; value: string };
  timeEstimateSeconds: number;
}

async function getRelayQuote(
  senderAddress: string,
  recipientAddress: string,
  amountUsdc: number
): Promise<BridgeQuote | null> {
  log('INFO', `Getting Relay quote: $${amountUsdc} from ${senderAddress} to ${recipientAddress}`);
  
  const amount6dec = parseUnits(amountUsdc.toFixed(6), 6);
  
  try {
    const response = await fetch(`${RELAY_API_URL}/quote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user: senderAddress,
        originChainId: POLYGON_CHAIN_ID,
        destinationChainId: BASE_CHAIN_ID,
        originCurrency: USDC_E_POLYGON,
        destinationCurrency: USDC_BASE,
        amount: amount6dec.toString(),
        tradeType: 'EXACT_INPUT',
        recipient: recipientAddress,
      }),
    });
    
    if (!response.ok) {
      const text = await response.text();
      log('ERROR', `Relay quote failed: ${response.status} - ${text}`);
      return null;
    }
    
    const data = await response.json();
    const steps = data.steps || [];
    if (!steps.length || !steps[0].items?.length) {
      log('ERROR', 'Invalid quote response structure');
      return null;
    }
    
    const txData = steps[0].items[0].data || {};
    return {
      requestId: steps[0].requestId || '',
      amountIn: amount6dec,
      amountOut: BigInt(data.details?.currencyOut?.amount || '0'),
      feeUsd: parseFloat(data.fees?.gas?.amountUsd || '0'),
      txData: { to: txData.to || '', data: txData.data || '0x', value: txData.value || '0' },
      timeEstimateSeconds: data.details?.timeEstimate || 120,
    };
  } catch (error) {
    log('ERROR', 'Error getting Relay quote', error);
    return null;
  }
}

async function pollBridgeStatus(requestId: string): Promise<{ success: boolean; message: string }> {
  log('INFO', `Polling bridge status for ${requestId.slice(0, 20)}...`);
  
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      const response = await fetch(
        `${RELAY_API_URL}/intents/status/v2?requestId=${encodeURIComponent(requestId)}`
      );
      
      if (!response.ok) {
        await new Promise(r => setTimeout(r, 10000));
        continue;
      }
      
      const data = await response.json();
      const status = data.status || 'unknown';
      
      if (status === 'success' || status === 'completed') {
        log('INFO', '✅ Bridge complete!');
        return { success: true, message: 'Bridge completed successfully' };
      }
      
      if (status === 'failed' || status === 'refunded') {
        return { success: false, message: `Bridge failed: ${data.error || 'Unknown'}` };
      }
      
      log('INFO', `Status: ${status} (${attempt + 1}/60)`);
      await new Promise(r => setTimeout(r, 10000));
    } catch {
      await new Promise(r => setTimeout(r, 10000));
    }
  }
  
  return { success: false, message: 'Bridge status polling timed out' };
}

// =============================================================================
// WITHDRAWAL VIA RELAY CLIENT
// =============================================================================

async function withdrawFromProxyToEOA(
  client: RelayClient,
  eoaAddress: string,
  amountUsdc: number,
  dryRun: boolean
): Promise<{ success: boolean; txHash?: string; error?: string }> {
  log('INFO', `Withdrawing $${amountUsdc} from Safe proxy to EOA ${eoaAddress}`);
  
  const amount6dec = parseUnits(amountUsdc.toFixed(6), 6);
  
  // Encode USDC transfer from proxy to EOA
  const transferData = encodeFunctionData({
    abi: ERC20_ABI,
    functionName: 'transfer',
    args: [eoaAddress as Hex, amount6dec],
  });
  
  const tx = {
    to: USDC_E_POLYGON,
    data: transferData,
    value: '0',
  };
  
  if (dryRun) {
    log('INFO', '[DRY RUN] Would execute transfer via RelayClient');
    log('INFO', `  To: ${tx.to}`);
    log('INFO', `  Data: ${tx.data.slice(0, 50)}...`);
    return { success: true };
  }
  
  try {
    log('INFO', 'Executing via Polymarket RelayClient...');
    const response = await client.execute([tx], `Withdraw $${amountUsdc} USDC to EOA`);
    
    log('INFO', 'Waiting for transaction confirmation...');
    const result = await response.wait();
    
    if (result) {
      log('INFO', `✅ Transfer complete!`);
      log('INFO', `   TX Hash: ${result.transactionHash}`);
      log('INFO', `   Proxy: ${result.proxyAddress}`);
      return { success: true, txHash: result.transactionHash };
    } else {
      return { success: false, error: 'Transaction result was null' };
    }
  } catch (error: unknown) {
    const errMsg = error instanceof Error ? error.message : String(error);
    log('ERROR', 'RelayClient execute failed', errMsg);
    return { success: false, error: errMsg };
  }
}

// =============================================================================
// MAIN FLOW: Safe → EOA → Bridge to Base
// =============================================================================

async function withdrawAndBridge(
  amountUsdc: number,
  dryRun: boolean
): Promise<{ success: boolean; txHash?: string; bridgeRequestId?: string; error?: string }> {
  log('INFO', '=== Starting Safe Proxy Withdrawal ===');
  log('INFO', `Amount: $${amountUsdc}`);
  log('INFO', `Destination: ${TREASURY_ADDRESS}`);
  log('INFO', `Dry run: ${dryRun}`);
  
  // Validation
  if (amountUsdc < MIN_WITHDRAWAL_USDC) {
    return { success: false, error: `Amount $${amountUsdc} below minimum $${MIN_WITHDRAWAL_USDC}` };
  }
  
  if (amountUsdc > MAX_PER_TX_USDC) {
    return { success: false, error: `Amount $${amountUsdc} exceeds max $${MAX_PER_TX_USDC}` };
  }
  
  // Check proxy balance
  const proxyBalance = await getProxyBalance();
  log('INFO', `Proxy USDC.e balance: $${proxyBalance.toFixed(2)}`);
  
  if (proxyBalance < amountUsdc) {
    return { success: false, error: `Insufficient balance: have $${proxyBalance.toFixed(2)}, need $${amountUsdc}` };
  }
  
  // Create RelayClient
  const client = await createRelayClient();
  
  // Clean the private key to get EOA address
  let cleanKey = POLYMARKET_PRIVATE_KEY.trim();
  if (!cleanKey.startsWith('0x')) {
    cleanKey = '0x' + cleanKey;
  }
  const account = privateKeyToAccount(cleanKey as Hex);
  const eoaAddress = account.address;
  
  // Step 1: Withdraw from Safe proxy to EOA
  log('INFO', '\n--- Step 1: Safe Proxy → EOA ---');
  const withdrawResult = await withdrawFromProxyToEOA(client, eoaAddress, amountUsdc, dryRun);
  
  if (!withdrawResult.success) {
    return { success: false, error: `Withdrawal failed: ${withdrawResult.error}` };
  }
  
  if (dryRun) {
    log('INFO', '[DRY RUN] Step 1 complete - would continue with bridge');
    return { success: true };
  }
  
  // Wait a bit for the transfer to settle
  log('INFO', 'Waiting 5s for transfer to settle...');
  await new Promise(r => setTimeout(r, 5000));
  
  // Step 2: Bridge from EOA to Base vault
  log('INFO', '\n--- Step 2: EOA → Relay Bridge → Base Vault ---');
  
  // Check EOA balance
  const eoaBalance = await getEOABalance(eoaAddress);
  log('INFO', `EOA USDC.e balance: $${eoaBalance.toFixed(2)}`);
  
  if (eoaBalance < amountUsdc * 0.99) { // Allow 1% slippage
    return { success: false, error: `EOA balance too low after transfer: $${eoaBalance.toFixed(2)}` };
  }
  
  // Get bridge quote
  const quote = await getRelayQuote(eoaAddress, TREASURY_ADDRESS, amountUsdc);
  if (!quote) {
    return { success: false, error: 'Failed to get Relay bridge quote' };
  }
  
  log('INFO', `Bridge quote: in=$${Number(quote.amountIn) / 1e6}, out=$${Number(quote.amountOut) / 1e6}, fee=$${quote.feeUsd.toFixed(4)}`);
  
  // Step 2a: Approve USDC.e for bridge contract
  log('INFO', 'Approving USDC.e for Relay bridge...');
  const approveData = encodeFunctionData({
    abi: ERC20_ABI,
    functionName: 'approve',
    args: [quote.txData.to as Hex, quote.amountIn],
  });
  
  const wallet = createWalletClient({
    account,
    chain: polygon,
    transport: http(POLYGON_RPC_URL),
  });
  
  try {
    const approveHash = await wallet.sendTransaction({
      to: USDC_E_POLYGON as Hex,
      data: approveData as Hex,
    });
    log('INFO', `Approval tx: ${approveHash}`);
    
    // Wait for approval to confirm
    log('INFO', 'Waiting for approval confirmation...');
    await new Promise(r => setTimeout(r, 5000));
  } catch (error: unknown) {
    const errMsg = error instanceof Error ? error.message : String(error);
    log('ERROR', 'Approval failed', errMsg);
    return { success: false, error: `Approval failed: ${errMsg}` };
  }
  
  // Step 2b: Execute bridge deposit
  log('INFO', 'Executing Relay bridge deposit...');
  
  try {
    const bridgeTxHash = await wallet.sendTransaction({
      to: quote.txData.to as Hex,
      data: quote.txData.data as Hex,
      value: BigInt(quote.txData.value || '0'),
    });
    log('INFO', `Bridge tx submitted: ${bridgeTxHash}`);
    
    // Wait for bridge tx to confirm
    log('INFO', 'Waiting for bridge tx confirmation...');
    await new Promise(r => setTimeout(r, 10000));
    
    // Poll for bridge completion
    log('INFO', 'Polling for bridge completion...');
    const bridgeResult = await pollBridgeStatus(quote.requestId);
    
    if (bridgeResult.success) {
      log('INFO', '\n✅ FULL WITHDRAWAL COMPLETE');
      log('INFO', `   Step 1 (Safe → EOA): ${withdrawResult.txHash}`);
      log('INFO', `   Step 2 (EOA → Base): ${bridgeTxHash}`);
      log('INFO', `   Amount: $${amountUsdc} USDC`);
      log('INFO', `   Destination: ${TREASURY_ADDRESS}`);
      
      return {
        success: true,
        txHash: bridgeTxHash,
        bridgeRequestId: quote.requestId,
      };
    } else {
      log('WARN', `Bridge status: ${bridgeResult.message}`);
      log('INFO', 'Bridge may still be in progress - check Relay dashboard');
      return {
        success: true, // Tx submitted, may still complete
        txHash: bridgeTxHash,
        bridgeRequestId: quote.requestId,
      };
    }
  } catch (error: unknown) {
    const errMsg = error instanceof Error ? error.message : String(error);
    log('ERROR', 'Bridge deposit failed', errMsg);
    return { success: false, error: `Bridge deposit failed: ${errMsg}` };
  }
}

// =============================================================================
// MAIN
// =============================================================================

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const command = args[0] || 'info';
  
  if (!POLYMARKET_PROXY_ADDRESS || !POLYMARKET_PRIVATE_KEY) {
    log('ERROR', 'Missing POLYMARKET_PROXY_ADDRESS or POLYMARKET_PRIVATE_KEY');
    process.exit(1);
  }
  
  switch (command) {
    case 'info': {
      log('INFO', '=== Safe Proxy Info ===');
      log('INFO', `Proxy: ${POLYMARKET_PROXY_ADDRESS}`);
      
      let cleanKey = POLYMARKET_PRIVATE_KEY.trim();
      if (!cleanKey.startsWith('0x')) {
        cleanKey = '0x' + cleanKey;
      }
      const account = privateKeyToAccount(cleanKey as Hex);
      log('INFO', `EOA: ${account.address}`);
      log('INFO', `Treasury: ${TREASURY_ADDRESS || 'Not set'}`);
      
      const balance = await getProxyBalance();
      log('INFO', `Proxy USDC.e: $${balance.toFixed(2)}`);
      
      const eoaBalance = await getEOABalance(account.address);
      log('INFO', `EOA USDC.e: $${eoaBalance.toFixed(2)}`);
      
      // Check builder credentials
      if (BUILDER_API_KEY && BUILDER_SECRET && BUILDER_PASSPHRASE) {
        log('INFO', '✅ Builder API credentials configured');
      } else {
        log('WARN', '⚠️ Builder API credentials not set - withdrawal may fail');
      }
      break;
    }
    
    case 'withdraw': {
      const amountUsdc = parseFloat(args[1] || '0');
      const dryRun = args.includes('--dry-run');
      
      if (!amountUsdc || amountUsdc <= 0) {
        log('ERROR', 'Usage: withdraw <amount> [--dry-run]');
        process.exit(1);
      }
      
      if (!TREASURY_ADDRESS) {
        log('ERROR', 'TREASURY_ADDRESS not set');
        process.exit(1);
      }
      
      const result = await withdrawAndBridge(amountUsdc, dryRun);
      
      if (result.success) {
        console.log('\n✅ SUCCESS');
        console.log(JSON.stringify(result, null, 2));
      } else {
        console.log('\n❌ FAILED');
        console.log(result.error);
        process.exit(1);
      }
      break;
    }
    
    case 'test': {
      // Quick test to verify RelayClient connection
      log('INFO', 'Testing RelayClient connection...');
      try {
        const client = await createRelayClient();
        log('INFO', '✅ RelayClient created successfully');
        log('INFO', `Client type: ${typeof client}`);
      } catch (error) {
        log('ERROR', 'Failed to create RelayClient', error);
        process.exit(1);
      }
      break;
    }
    
    default:
      console.log('Usage: safe_proxy_withdraw.ts <command> [options]');
      console.log('Commands:');
      console.log('  info                    Show proxy info and balances');
      console.log('  withdraw <amount>       Withdraw to vault (--dry-run for simulation)');
      console.log('  test                    Test RelayClient connection');
  }
}

main().catch((error) => {
  log('ERROR', 'Unhandled error', error);
  process.exit(1);
});
