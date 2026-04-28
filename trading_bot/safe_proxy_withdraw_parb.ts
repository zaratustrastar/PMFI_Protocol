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

import { createWalletClient, createPublicClient, http, encodeFunctionData, parseUnits, formatUnits, type Hex } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import { RelayClient, RelayerTxType } from '@polymarket/builder-relayer-client';
import { BuilderConfig } from '@polymarket/builder-signing-sdk';
import * as dotenv from 'dotenv';

dotenv.config();

// =============================================================================
// CONFIGURATION
// =============================================================================

const POLYGON_RPC_URL = process.env.POLYGON_RPC_URL || 'https://polygon-rpc.com';
const POLYGON_CHAIN_ID = 137;

const POLYMARKET_PROXY_ADDRESS = process.env.POLY_PROXY_ADDRESS || '';
const POLYMARKET_PRIVATE_KEY = process.env.POLY_PRIVATE_KEY || '';
const TREASURY_ADDRESS = process.env.TREASURY_ADDRESS || '';

// Builder API credentials for Polymarket
// Builder API credentials for Polymarket
const BUILDER_API_KEY = process.env.POLY2_API_KEY || '';
const BUILDER_SECRET = process.env.POLY2_API_SECRET || '';
const BUILDER_PASSPHRASE = process.env.POLY2_API_PASSPHRASE || '';

// Contracts
// Bridged USDC (PoS) on Polygon - this is what Polymarket uses
const USDC_E_POLYGON = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const RELAY_API_URL = 'https://api.relay.link';
const BASE_CHAIN_ID = 8453;
const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

// Relayer URL
const RELAYER_URL = 'https://relayer-v2.polymarket.com';

// Security guardrails
const MAX_PER_TX_USDC = 5000;
const MIN_WITHDRAWAL_USDC = 5;

// Bot API URL for updating withdrawal tracker
const BOT_API_URL = process.env.BOT_API_URL || 'http://localhost:8080';

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

async function notifyBotWithdrawalBack(amountUsdc: number): Promise<boolean> {
  const amountRaw = Math.round(amountUsdc * 1e6);
  log('INFO', `Notifying bot of withdrawal: $${amountUsdc.toFixed(2)} (${amountRaw} raw)`);
  
  // Retry up to 3 times
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const response = await fetch(`${BOT_API_URL}/admin/record-withdrawal-back`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount_usdc: amountRaw }),
      });
      
      if (response.ok) {
        const data = await response.json();
        log('INFO', `✅ Bot notified - withdrawn_back now: $${data.withdrawn_back?.toFixed(2) || 'unknown'}`);
        return true;
      } else {
        log('WARN', `Attempt ${attempt}/3: Bot returned ${response.status} ${response.statusText}`);
      }
    } catch (error) {
      log('WARN', `Attempt ${attempt}/3: Could not reach bot API at ${BOT_API_URL}`, error);
    }
    
    if (attempt < 3) {
      await new Promise(r => setTimeout(r, 2000)); // Wait 2s between retries
    }
  }
  
  log('ERROR', '❌ IMPORTANT: Failed to notify bot after 3 attempts!');
  log('ERROR', `   Run this manually to sync: curl -X POST ${BOT_API_URL}/admin/record-withdrawal-back -H "Content-Type: application/json" -d '{"amount": ${amountRaw}}'`);
  return false;
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
      undefined,
      RelayerTxType.SAFE
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
    RelayerTxType.SAFE
  );

  return client;
}

// =============================================================================
// BALANCE CHECKING
// =============================================================================

async function getProxyBalanceRaw(): Promise<bigint> {
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
  
  return BigInt(data.result);
}

async function getProxyBalance(): Promise<number> {
  const balanceRaw = await getProxyBalanceRaw();
  return Number(formatUnits(balanceRaw, 6));
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

interface StepTxData {
  to: string;
  data: string;
  value: string;
}

interface BridgeQuote {
  requestId: string;
  amountIn: bigint;
  amountOut: bigint;
  feeUsd: number;
  approveTx: StepTxData | null;
  depositTx: StepTxData;
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
    if (!steps.length) {
      log('ERROR', 'Invalid quote response: no steps');
      return null;
    }

    log('INFO', `Relay quote returned ${steps.length} step(s): ${steps.map((s: { id?: string }) => s.id || 'unknown').join(', ')}`);

    let approveTx: StepTxData | null = null;
    let depositTx: StepTxData | null = null;
    let requestId = '';

    const APPROVE_STEP_IDS = ['approve', 'signature', 'permit'];
    const DEPOSIT_STEP_IDS = ['deposit', 'bridge', 'swap', 'transaction'];

    for (const step of steps) {
      const items = step.items || [];
      if (!items.length) continue;
      const txData = items[0].data || {};
      const stepTx: StepTxData = {
        to: txData.to || '',
        data: txData.data || '0x',
        value: txData.value || '0',
      };
      const stepId = (step.id || '').toLowerCase();

      if (APPROVE_STEP_IDS.includes(stepId) || step.kind === 'signature') {
        approveTx = stepTx;
        log('INFO', `  Step "${step.id}" (kind: ${step.kind || 'n/a'}): classified as APPROVE → ${stepTx.to.slice(0, 10)}...`);
      } else if (DEPOSIT_STEP_IDS.includes(stepId)) {
        depositTx = stepTx;
        requestId = step.requestId || requestId;
        log('INFO', `  Step "${step.id}" (kind: ${step.kind || 'n/a'}): classified as DEPOSIT → ${stepTx.to.slice(0, 10)}...`);
      } else {
        log('WARN', `  Step "${step.id}" (kind: ${step.kind || 'n/a'}): UNKNOWN step type, treating as deposit candidate → ${stepTx.to.slice(0, 10)}...`);
        depositTx = stepTx;
        requestId = step.requestId || requestId;
      }
    }

    if (!depositTx) {
      if (steps.length === 1 && steps[0].items?.length) {
        const txData = steps[0].items[0].data || {};
        depositTx = {
          to: txData.to || '',
          data: txData.data || '0x',
          value: txData.value || '0',
        };
        requestId = steps[0].requestId || '';
        log('INFO', `  Single-step quote: treating as deposit to ${depositTx.to.slice(0, 10)}...`);
      } else {
        log('ERROR', 'Could not find deposit step in Relay quote. Raw steps:');
        for (const step of steps) {
          log('ERROR', `  id=${step.id}, kind=${step.kind}, requestId=${step.requestId}, items=${(step.items || []).length}`);
        }
        return null;
      }
    }

    return {
      requestId,
      amountIn: amount6dec,
      amountOut: BigInt(data.details?.currencyOut?.amount || '0'),
      feeUsd: parseFloat(data.fees?.gas?.amountUsd || '0'),
      approveTx,
      depositTx,
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
  amountRaw: bigint,
  dryRun: boolean
): Promise<{ success: boolean; txHash?: string; error?: string }> {
  const amountUsdc = Number(formatUnits(amountRaw, 6));
  log('INFO', `Withdrawing $${amountUsdc.toFixed(6)} (${amountRaw} raw) from Safe proxy to EOA ${eoaAddress}`);
  
  // Encode USDC transfer from proxy to EOA using exact raw amount
  const transferData = encodeFunctionData({
    abi: ERC20_ABI,
    functionName: 'transfer',
    args: [eoaAddress as Hex, amountRaw],
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

      if (
        POLYMARKET_PROXY_ADDRESS &&
        result.proxyAddress &&
        result.proxyAddress.toLowerCase() !== POLYMARKET_PROXY_ADDRESS.toLowerCase()
      ) {
        return {
          success: false,
          error: `Relay used wrong proxy ${result.proxyAddress}; expected ${POLYMARKET_PROXY_ADDRESS}`
        };
      }

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
// APPROVE HELPER: Use Relay-provided approve tx or build manual one
// =============================================================================

async function executeApproveTx(
  wallet: any,
  publicClient: ReturnType<typeof createPublicClient>,
  quote: BridgeQuote
): Promise<{ success: boolean; error?: string }> {
  if (quote.approveTx) {
    log('INFO', 'Using Relay-provided approve transaction...');
    try {
      const approveHash = await wallet.sendTransaction({
        to: quote.approveTx.to as Hex,
        data: quote.approveTx.data as Hex,
        value: BigInt(quote.approveTx.value || '0'),
        gas: BigInt(100_000),
      });
      log('INFO', `Approval tx sent: ${approveHash}`);

      log('INFO', 'Waiting for approval to be mined...');
      const receipt = await publicClient.waitForTransactionReceipt({
        hash: approveHash,
        timeout: 120_000,
        pollingInterval: 3_000,
      });

      if (receipt.status === 'reverted') {
        log('ERROR', 'Approval transaction reverted on-chain');
        return { success: false, error: 'Approval transaction reverted' };
      }

      log('INFO', `✅ Approval confirmed in block ${receipt.blockNumber} (gas used: ${receipt.gasUsed})`);
      return { success: true };
    } catch (error: unknown) {
      const errMsg = error instanceof Error ? error.message : String(error);
      log('ERROR', 'Approval failed', errMsg);
      return { success: false, error: `Approval failed: ${errMsg}` };
    }
  } else {
    log('INFO', 'No Relay approve step — building manual approve for deposit contract...');
    const approveData = encodeFunctionData({
      abi: ERC20_ABI,
      functionName: 'approve',
      args: [quote.depositTx.to as Hex, quote.amountIn],
    });

    try {
      const approveHash = await wallet.sendTransaction({
        to: USDC_E_POLYGON as Hex,
        data: approveData as Hex,
        gas: BigInt(100_000),
      });
      log('INFO', `Manual approval tx sent: ${approveHash}`);

      log('INFO', 'Waiting for approval to be mined...');
      const receipt = await publicClient.waitForTransactionReceipt({
        hash: approveHash,
        timeout: 120_000,
        pollingInterval: 3_000,
      });

      if (receipt.status === 'reverted') {
        log('ERROR', 'Manual approval transaction reverted on-chain');
        return { success: false, error: 'Manual approval transaction reverted' };
      }

      log('INFO', `✅ Manual approval confirmed in block ${receipt.blockNumber} (gas used: ${receipt.gasUsed})`);
      return { success: true };
    } catch (error: unknown) {
      const errMsg = error instanceof Error ? error.message : String(error);
      log('ERROR', 'Manual approval failed', errMsg);
      return { success: false, error: `Manual approval failed: ${errMsg}` };
    }
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
  log('INFO', `Requested amount: $${amountUsdc}`);
  log('INFO', `Destination: ${TREASURY_ADDRESS}`);
  log('INFO', `Dry run: ${dryRun}`);
  
  // Validation
  if (amountUsdc < MIN_WITHDRAWAL_USDC) {
    return { success: false, error: `Amount $${amountUsdc} below minimum $${MIN_WITHDRAWAL_USDC}` };
  }
  
  if (amountUsdc > MAX_PER_TX_USDC) {
    return { success: false, error: `Amount $${amountUsdc} exceeds max $${MAX_PER_TX_USDC}` };
  }
  
  // Get exact raw balance (no floating point issues)
  const proxyBalanceRaw = await getProxyBalanceRaw();
  const proxyBalance = Number(formatUnits(proxyBalanceRaw, 6));
  log('INFO', `Proxy USDC balance: $${proxyBalance.toFixed(6)} (${proxyBalanceRaw} raw)`);
  
  // Convert requested amount to raw (6 decimals)
  const requestedRaw = parseUnits(amountUsdc.toFixed(6), 6);
  
  // Cap at available balance minus small dust buffer (100 units = 0.0001 USDC)
  const DUST_BUFFER = BigInt(100);
  const maxAvailable = proxyBalanceRaw > DUST_BUFFER ? proxyBalanceRaw - DUST_BUFFER : proxyBalanceRaw;
  const sendRaw = requestedRaw <= maxAvailable ? requestedRaw : maxAvailable;
  const sendUsdc = Number(formatUnits(sendRaw, 6));
  
  if (sendRaw <= BigInt(0)) {
    return { success: false, error: `Insufficient balance: have $${proxyBalance.toFixed(6)}` };
  }
  
  if (sendRaw < requestedRaw) {
    log('INFO', `⚠️ Capping transfer: requested $${amountUsdc}, sending $${sendUsdc.toFixed(6)} (available minus dust)`);
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
  const withdrawResult = await withdrawFromProxyToEOA(client, eoaAddress, sendRaw, dryRun);
  
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
  
  // Check EOA balance - use ACTUAL balance for bridging (may include prior transfers)
  const eoaBalance = await getEOABalance(eoaAddress);
  log('INFO', `EOA USDC.e balance: $${eoaBalance.toFixed(6)}`);
  
  // Minimum bridge amount - Relay requires ~$5 minimum
  const MIN_BRIDGE_AMOUNT = 5.0;
  
  if (eoaBalance < MIN_BRIDGE_AMOUNT) {
    return { success: false, error: `EOA balance $${eoaBalance.toFixed(2)} below minimum bridge amount ($${MIN_BRIDGE_AMOUNT}). Accumulate more before bridging.` };
  }
  
  // Use the FULL EOA balance for bridging (not just what Step 1 transferred)
  // This handles cases where multiple transfers accumulated or prior Step 1s succeeded
  const bridgeAmount = eoaBalance;
  log('INFO', `Will bridge full EOA balance: $${bridgeAmount.toFixed(6)}`);
  
  // Get bridge quote using actual EOA balance
  const quote = await getRelayQuote(eoaAddress, TREASURY_ADDRESS, bridgeAmount);
  if (!quote) {
    return { success: false, error: 'Failed to get Relay bridge quote' };
  }
  
  log('INFO', `Bridge quote: in=$${formatUnits(quote.amountIn, 6)}, out=$${formatUnits(quote.amountOut, 6)}, fee=$${quote.feeUsd.toFixed(4)}`);
  
  const wallet = createWalletClient({
    account,
    chain: polygon,
    transport: http(POLYGON_RPC_URL),
  });

  const publicClient = createPublicClient({
    chain: polygon,
    transport: http(POLYGON_RPC_URL),
  });

  // Step 2a: Execute approve (use Relay-provided approve tx if available, else manual)
  const approveResult = await executeApproveTx(wallet, publicClient, quote);
  if (!approveResult.success) {
    return { success: false, error: approveResult.error };
  }
  
  // Step 2b: Execute bridge deposit (with retry)
  const MAX_BRIDGE_ATTEMPTS = 2;
  
  for (let attempt = 1; attempt <= MAX_BRIDGE_ATTEMPTS; attempt++) {
    log('INFO', `Executing Relay bridge deposit (attempt ${attempt}/${MAX_BRIDGE_ATTEMPTS})...`);
    
    try {
      let currentQuote = quote;
      
      if (attempt > 1) {
        log('INFO', 'Getting fresh bridge quote for retry...');
        const freshQuote = await getRelayQuote(eoaAddress, TREASURY_ADDRESS, bridgeAmount);
        if (!freshQuote) {
          log('ERROR', 'Failed to get fresh quote for retry');
          continue;
        }
        currentQuote = freshQuote;
        log('INFO', `Fresh quote: in=$${formatUnits(currentQuote.amountIn, 6)}, out=$${formatUnits(currentQuote.amountOut, 6)}`);
        
        if (currentQuote.approveTx) {
          log('INFO', 'Fresh quote includes new approve step, executing...');
          const retryApprove = await executeApproveTx(wallet, publicClient, currentQuote);
          if (!retryApprove.success) {
            log('ERROR', `Retry approve failed: ${retryApprove.error}`);
            continue;
          }
        }
      }
      
      const bridgeTxHash = await wallet.sendTransaction({
        to: currentQuote.depositTx.to as Hex,
        data: currentQuote.depositTx.data as Hex,
        value: BigInt(currentQuote.depositTx.value || '0'),
        gas: BigInt(300_000),
      });
      log('INFO', `Bridge tx sent: ${bridgeTxHash}`);
      
      log('INFO', 'Waiting for bridge tx to be mined...');
      const bridgeReceipt = await publicClient.waitForTransactionReceipt({
        hash: bridgeTxHash,
        timeout: 120_000,
        pollingInterval: 3_000,
      });
      
      if (bridgeReceipt.status === 'reverted') {
        log('ERROR', `Bridge tx reverted on-chain (attempt ${attempt})`);
        if (attempt < MAX_BRIDGE_ATTEMPTS) {
          log('INFO', 'Will retry with fresh quote in 5s...');
          await new Promise(r => setTimeout(r, 5000));
          continue;
        }
        return { success: false, error: 'Bridge transaction reverted after all retries' };
      }
      
      log('INFO', `✅ Bridge tx confirmed in block ${bridgeReceipt.blockNumber} (gas used: ${bridgeReceipt.gasUsed})`);
      
      log('INFO', 'Polling for cross-chain bridge completion...');
      const bridgeResult = await pollBridgeStatus(currentQuote.requestId);
      
      if (bridgeResult.success) {
        log('INFO', '\n✅ FULL WITHDRAWAL COMPLETE');
        log('INFO', `   Step 1 (Safe → EOA): ${withdrawResult.txHash}`);
        log('INFO', `   Step 2 (EOA → Base): ${bridgeTxHash}`);
        log('INFO', `   Bridged: $${bridgeAmount.toFixed(2)} USDC`);
        log('INFO', `   Destination: ${TREASURY_ADDRESS}`);
        
        await notifyBotWithdrawalBack(bridgeAmount);
        
        return {
          success: true,
          txHash: bridgeTxHash,
          bridgeRequestId: currentQuote.requestId,
        };
      } else {
        log('WARN', `Bridge status: ${bridgeResult.message}`);
        log('INFO', 'Bridge may still be in progress - check Relay dashboard');
        return {
          success: true,
          txHash: bridgeTxHash,
          bridgeRequestId: currentQuote.requestId,
        };
      }
    } catch (error: unknown) {
      const errMsg = error instanceof Error ? error.message : String(error);
      log('ERROR', `Bridge deposit failed (attempt ${attempt}): ${errMsg}`);
      if (attempt < MAX_BRIDGE_ATTEMPTS) {
        log('INFO', 'Will retry with fresh quote in 5s...');
        await new Promise(r => setTimeout(r, 5000));
        continue;
      }
      return { success: false, error: `Bridge deposit failed after ${MAX_BRIDGE_ATTEMPTS} attempts: ${errMsg}` };
    }
  }
  
  return { success: false, error: 'Bridge failed after all attempts' };
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
    
    case 'bridge': {
      // Bridge EOA balance directly to vault (skip Step 1)
      log('INFO', '=== Bridge EOA → Base Vault ===');
      
      if (!TREASURY_ADDRESS) {
        log('ERROR', 'TREASURY_ADDRESS not set');
        process.exit(1);
      }
      
      let cleanKey = POLYMARKET_PRIVATE_KEY.trim();
      if (!cleanKey.startsWith('0x')) {
        cleanKey = '0x' + cleanKey;
      }
      const account = privateKeyToAccount(cleanKey as Hex);
      const eoaAddress = account.address;
      
      log('INFO', `EOA: ${eoaAddress}`);
      log('INFO', `Destination: ${TREASURY_ADDRESS}`);
      
      // Check EOA balance
      const eoaBalance = await getEOABalance(eoaAddress);
      log('INFO', `EOA USDC.e balance: $${eoaBalance.toFixed(6)}`);
      
      const MIN_BRIDGE_AMOUNT = 5.0;
      if (eoaBalance < MIN_BRIDGE_AMOUNT) {
        log('ERROR', `EOA balance $${eoaBalance.toFixed(2)} below minimum bridge amount ($${MIN_BRIDGE_AMOUNT})`);
        process.exit(1);
      }
      
      const bridgeAmount = eoaBalance;
      log('INFO', `Will bridge: $${bridgeAmount.toFixed(6)}`);
      
      // Get bridge quote
      const quote = await getRelayQuote(eoaAddress, TREASURY_ADDRESS, bridgeAmount);
      if (!quote) {
        log('ERROR', 'Failed to get Relay bridge quote');
        process.exit(1);
      }
      
      log('INFO', `Bridge quote: in=$${formatUnits(quote.amountIn, 6)}, out=$${formatUnits(quote.amountOut, 6)}, fee=$${quote.feeUsd.toFixed(4)}`);
      
      const wallet = createWalletClient({
        account,
        chain: polygon,
        transport: http(POLYGON_RPC_URL),
      });

      const publicClient = createPublicClient({
        chain: polygon,
        transport: http(POLYGON_RPC_URL),
      });

      // Execute approve (Relay-provided or manual)
      const approveResult = await executeApproveTx(wallet, publicClient, quote);
      if (!approveResult.success) {
        log('ERROR', `Approval failed: ${approveResult.error}`);
        process.exit(1);
      }
      
      // Execute bridge with retry
      const BRIDGE_MAX_ATTEMPTS = 2;
      let bridgeSuccess = false;
      
      for (let attempt = 1; attempt <= BRIDGE_MAX_ATTEMPTS; attempt++) {
        log('INFO', `Executing Relay bridge deposit (attempt ${attempt}/${BRIDGE_MAX_ATTEMPTS})...`);
        
        try {
          let currentQuote = quote;
          
          if (attempt > 1) {
            log('INFO', 'Getting fresh bridge quote for retry...');
            const freshQuote = await getRelayQuote(eoaAddress, TREASURY_ADDRESS, bridgeAmount);
            if (!freshQuote) {
              log('ERROR', 'Failed to get fresh quote for retry');
              continue;
            }
            currentQuote = freshQuote;
            
            if (currentQuote.approveTx) {
              log('INFO', 'Fresh quote includes new approve step, executing...');
              const retryApprove = await executeApproveTx(wallet, publicClient, currentQuote);
              if (!retryApprove.success) {
                log('ERROR', `Retry approve failed: ${retryApprove.error}`);
                continue;
              }
            }
          }
          
          const bridgeTxHash = await wallet.sendTransaction({
            to: currentQuote.depositTx.to as Hex,
            data: currentQuote.depositTx.data as Hex,
            value: BigInt(currentQuote.depositTx.value || '0'),
            gas: BigInt(300_000),
          });
          log('INFO', `Bridge tx sent: ${bridgeTxHash}`);
          
          log('INFO', 'Waiting for bridge tx to be mined...');
          const bridgeReceipt = await publicClient.waitForTransactionReceipt({
            hash: bridgeTxHash,
            timeout: 120_000,
            pollingInterval: 3_000,
          });
          
          if (bridgeReceipt.status === 'reverted') {
            log('ERROR', `Bridge tx reverted on-chain (attempt ${attempt})`);
            if (attempt < BRIDGE_MAX_ATTEMPTS) {
              log('INFO', 'Will retry with fresh quote in 5s...');
              await new Promise(r => setTimeout(r, 5000));
              continue;
            }
            process.exit(1);
          }
          
          log('INFO', `✅ Bridge tx confirmed in block ${bridgeReceipt.blockNumber} (gas used: ${bridgeReceipt.gasUsed})`);
          
          log('INFO', 'Polling for cross-chain bridge completion...');
          const bridgeResult = await pollBridgeStatus(currentQuote.requestId);
          
          if (bridgeResult.success) {
            console.log('\n✅ BRIDGE COMPLETE');
            console.log(`   TX: ${bridgeTxHash}`);
            console.log(`   Bridged: $${bridgeAmount.toFixed(2)} USDC`);
            console.log(`   Destination: ${TREASURY_ADDRESS}`);
            
            await notifyBotWithdrawalBack(bridgeAmount);
          } else {
            log('WARN', `Bridge status: ${bridgeResult.message}`);
            console.log('Bridge may still be in progress - check Relay dashboard');
          }
          bridgeSuccess = true;
          break;
        } catch (error: unknown) {
          const errMsg = error instanceof Error ? error.message : String(error);
          log('ERROR', `Bridge deposit failed (attempt ${attempt}): ${errMsg}`);
          if (attempt < BRIDGE_MAX_ATTEMPTS) {
            log('INFO', 'Will retry with fresh quote in 5s...');
            await new Promise(r => setTimeout(r, 5000));
            continue;
          }
          process.exit(1);
        }
      }
      
      if (!bridgeSuccess) {
        log('ERROR', 'Bridge failed after all attempts');
        process.exit(1);
      }
      break;
    }
    
    default:
      console.log('Usage: safe_proxy_withdraw.ts <command> [options]');
      console.log('Commands:');
      console.log('  info                    Show proxy info and balances');
      console.log('  withdraw <amount>       Withdraw to vault (--dry-run for simulation)');
      console.log('  bridge                  Bridge EOA balance directly to vault (skip Step 1)');
      console.log('  test                    Test RelayClient connection');
  }
}

main().catch((error) => {
  log('ERROR', 'Unhandled error', error);
  process.exit(1);
});
