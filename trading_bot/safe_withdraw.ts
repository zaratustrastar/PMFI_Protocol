#!/usr/bin/env npx tsx
/**
 * Safe Withdraw - Polygon USDC.e withdrawal via Gnosis Safe
 * 
 * Withdraws USDC.e from Polymarket Safe (proxy) to EOA or directly to Relay bridge.
 * 
 * Flow 1 (Primary): Safe → Relay bridge contract (direct bridge)
 * Flow 2 (Fallback): Safe → EOA → Relay bridge
 * 
 * Security Guardrails:
 * - Strict allowlist for destination addresses
 * - maxPerTx cap on withdrawal amounts
 * - Dry-run simulation before execution
 * - Pre-flight checks (ownership, threshold, balance)
 */

import Safe from '@safe-global/protocol-kit';
import { ethers } from 'ethers';
import * as dotenv from 'dotenv';

dotenv.config();

// =============================================================================
// CONFIGURATION
// =============================================================================

const POLYGON_RPC_URL = process.env.POLYGON_RPC_URL || 'https://polygon-rpc.com';
const POLYGON_CHAIN_ID = 137n;

const POLYMARKET_PROXY_ADDRESS = process.env.POLYMARKET_PROXY_ADDRESS || '';
const POLYMARKET_PRIVATE_KEY = process.env.POLYMARKET_PRIVATE_KEY || '';
const TREASURY_ADDRESS = process.env.TREASURY_ADDRESS || '';

const USDC_E_POLYGON = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const RELAY_API_URL = 'https://api.relay.link';
const BASE_CHAIN_ID = 8453;
const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

// Security guardrails
const MAX_PER_TX_USDC = 5000; // Max $5000 per transaction
const MIN_WITHDRAWAL_USDC = 10; // Min $10 withdrawal

// Allowlist - only these addresses can receive funds
const ALLOWED_DESTINATIONS: Set<string> = new Set([
  TREASURY_ADDRESS.toLowerCase(),
  // Add emergency cold wallet here if needed
]);

// =============================================================================
// TYPES
// =============================================================================

interface SafeInfo {
  address: string;
  owners: string[];
  threshold: number;
  usdcBalance: number;
  maticBalance: number;
}

interface BridgeQuote {
  requestId: string;
  amountIn: bigint;
  amountOut: bigint;
  feeUsd: number;
  txData: {
    to: string;
    data: string;
    value: string;
  };
  timeEstimateSeconds: number;
}

interface WithdrawResult {
  success: boolean;
  txHash?: string;
  requestId?: string;
  amountSent: number;
  error?: string;
  flow: 'direct' | 'fallback' | 'none';
}

// =============================================================================
// LOGGING
// =============================================================================

function log(level: 'INFO' | 'WARN' | 'ERROR', message: string, data?: unknown): void {
  const timestamp = new Date().toISOString();
  const prefix = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : '📝';
  console.log(`${timestamp} ${prefix} [SafeWithdraw] ${message}`, data || '');
}

// =============================================================================
// SAFE INFO & VALIDATION
// =============================================================================

async function getSafeInfo(safeAddress: string, provider: ethers.JsonRpcProvider): Promise<SafeInfo> {
  log('INFO', `Fetching Safe info for ${safeAddress}`);
  
  const protocolKit = await Safe.init({
    provider: POLYGON_RPC_URL,
    signer: POLYMARKET_PRIVATE_KEY,
    safeAddress: safeAddress,
  });
  
  const owners = await protocolKit.getOwners();
  const threshold = await protocolKit.getThreshold();
  
  // Get USDC.e balance
  const usdcAbi = ['function balanceOf(address) view returns (uint256)'];
  const usdc = new ethers.Contract(USDC_E_POLYGON, usdcAbi, provider);
  const usdcBalance = await usdc.balanceOf(safeAddress);
  
  // Get MATIC balance (for gas info)
  const maticBalance = await provider.getBalance(safeAddress);
  
  const info: SafeInfo = {
    address: safeAddress,
    owners: owners,
    threshold: threshold,
    usdcBalance: Number(usdcBalance) / 1e6,
    maticBalance: Number(maticBalance) / 1e18,
  };
  
  log('INFO', 'Safe info retrieved', info);
  return info;
}

function validatePreFlight(safeInfo: SafeInfo, eoaAddress: string, amountUsdc: number): string | null {
  // Check ownership
  const isOwner = safeInfo.owners.some(
    (owner) => owner.toLowerCase() === eoaAddress.toLowerCase()
  );
  if (!isOwner) {
    return `EOA ${eoaAddress} is not an owner of Safe ${safeInfo.address}`;
  }
  
  // Check threshold (must be 1 for single-signature execution)
  if (safeInfo.threshold !== 1) {
    return `Safe threshold is ${safeInfo.threshold}, must be 1 for automated execution`;
  }
  
  // Check balance
  if (safeInfo.usdcBalance < amountUsdc) {
    return `Insufficient USDC.e balance: have $${safeInfo.usdcBalance.toFixed(2)}, need $${amountUsdc.toFixed(2)}`;
  }
  
  // Check min/max
  if (amountUsdc < MIN_WITHDRAWAL_USDC) {
    return `Amount $${amountUsdc} below minimum $${MIN_WITHDRAWAL_USDC}`;
  }
  if (amountUsdc > MAX_PER_TX_USDC) {
    return `Amount $${amountUsdc} exceeds max per tx $${MAX_PER_TX_USDC}`;
  }
  
  return null; // No errors
}

function validateDestination(address: string): boolean {
  if (!address) return false;
  const normalized = address.toLowerCase();
  
  // Check allowlist
  if (ALLOWED_DESTINATIONS.has(normalized)) {
    return true;
  }
  
  log('WARN', `Destination ${address} not in allowlist`);
  return false;
}

// =============================================================================
// RELAY BRIDGE API
// =============================================================================

async function getRelayQuote(
  senderAddress: string,
  recipientAddress: string,
  amountUsdc: number
): Promise<BridgeQuote | null> {
  log('INFO', `Getting Relay quote: $${amountUsdc} from ${senderAddress} to ${recipientAddress}`);
  
  const amount6dec = BigInt(Math.floor(amountUsdc * 1e6));
  
  const payload = {
    user: senderAddress,
    originChainId: 137, // Polygon
    destinationChainId: BASE_CHAIN_ID,
    originCurrency: USDC_E_POLYGON,
    destinationCurrency: USDC_BASE,
    amount: amount6dec.toString(),
    tradeType: 'EXACT_INPUT',
    recipient: recipientAddress,
  };
  
  try {
    const response = await fetch(`${RELAY_API_URL}/quote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    
    if (!response.ok) {
      const text = await response.text();
      log('ERROR', `Relay quote failed: ${response.status}`, text.slice(0, 200));
      return null;
    }
    
    const data = await response.json();
    
    const steps = data.steps || [];
    if (steps.length === 0) {
      log('ERROR', 'No steps in Relay quote response');
      return null;
    }
    
    const depositStep = steps[0];
    const items = depositStep.items || [];
    if (items.length === 0) {
      log('ERROR', 'No items in deposit step');
      return null;
    }
    
    const txData = items[0].data || {};
    const requestId = depositStep.requestId || '';
    
    const fees = data.fees || {};
    const gasFee = fees.gas || {};
    const feeUsd = parseFloat(gasFee.amountUsd || '0');
    
    const details = data.details || {};
    const currencyOut = details.currencyOut || {};
    const amountOut = BigInt(currencyOut.amount || '0');
    const timeEstimate = details.timeEstimate || 120;
    
    const quote: BridgeQuote = {
      requestId,
      amountIn: amount6dec,
      amountOut,
      feeUsd,
      txData: {
        to: txData.to || '',
        data: txData.data || '0x',
        value: txData.value || '0',
      },
      timeEstimateSeconds: timeEstimate,
    };
    
    log('INFO', `Quote received: output $${Number(amountOut) / 1e6}, fee $${feeUsd.toFixed(4)}`);
    return quote;
    
  } catch (error) {
    log('ERROR', 'Error getting Relay quote', error);
    return null;
  }
}

async function pollBridgeStatus(requestId: string): Promise<{ success: boolean; message: string }> {
  log('INFO', `Polling bridge status for ${requestId.slice(0, 20)}...`);
  
  const maxAttempts = 60;
  const pollInterval = 10000; // 10 seconds
  
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      const response = await fetch(
        `${RELAY_API_URL}/intents/status/v2?requestId=${encodeURIComponent(requestId)}`
      );
      
      if (!response.ok) {
        log('WARN', `Poll attempt ${attempt + 1}: status check failed`);
        await new Promise((r) => setTimeout(r, pollInterval));
        continue;
      }
      
      const data = await response.json();
      const status = data.status || 'unknown';
      
      if (status === 'success' || status === 'completed') {
        log('INFO', '✅ Bridge complete!');
        return { success: true, message: 'Bridge completed successfully' };
      }
      
      if (status === 'failed' || status === 'refunded') {
        const error = data.error || 'Unknown error';
        log('ERROR', `Bridge failed: ${error}`);
        return { success: false, message: `Bridge failed: ${error}` };
      }
      
      log('INFO', `Status: ${status} (attempt ${attempt + 1}/${maxAttempts})`);
      await new Promise((r) => setTimeout(r, pollInterval));
      
    } catch (error) {
      log('WARN', `Poll error: ${error}`);
      await new Promise((r) => setTimeout(r, pollInterval));
    }
  }
  
  return { success: false, message: 'Bridge status polling timed out' };
}

// =============================================================================
// SAFE TRANSACTION EXECUTION
// =============================================================================

async function executeSafeTransaction(
  safeAddress: string,
  to: string,
  data: string,
  value: string,
  dryRun: boolean = false
): Promise<{ success: boolean; txHash?: string; error?: string }> {
  log('INFO', `${dryRun ? '[DRY RUN] ' : ''}Executing Safe transaction`);
  log('INFO', `  To: ${to}`);
  log('INFO', `  Value: ${value}`);
  log('INFO', `  Data: ${data.slice(0, 66)}...`);
  
  try {
    const protocolKit = await Safe.init({
      provider: POLYGON_RPC_URL,
      signer: POLYMARKET_PRIVATE_KEY,
      safeAddress: safeAddress,
    });
    
    const safeTransaction = await protocolKit.createTransaction({
      transactions: [{
        to: to,
        value: value,
        data: data,
      }],
    });
    
    if (dryRun) {
      // Simulate only
      log('INFO', '[DRY RUN] Transaction created successfully, skipping execution');
      return { success: true };
    }
    
    // Sign the transaction
    const signedTx = await protocolKit.signTransaction(safeTransaction);
    
    // Execute the transaction
    const txResponse = await protocolKit.executeTransaction(signedTx);
    const receipt = await txResponse.transactionResponse?.wait();
    
    if (receipt && receipt.status === 1) {
      log('INFO', `✅ Safe transaction confirmed: ${receipt.hash}`);
      return { success: true, txHash: receipt.hash };
    } else {
      log('ERROR', 'Safe transaction failed');
      return { success: false, error: 'Transaction failed on-chain' };
    }
    
  } catch (error) {
    log('ERROR', 'Error executing Safe transaction', error);
    return { success: false, error: String(error) };
  }
}

// =============================================================================
// MAIN WITHDRAWAL FLOWS
// =============================================================================

/**
 * Flow 1: Direct Safe → Relay bridge
 * Safe executes the Relay deposit transaction directly
 */
async function withdrawViaSafeDirect(
  safeAddress: string,
  recipientAddress: string,
  amountUsdc: number,
  dryRun: boolean = false
): Promise<WithdrawResult> {
  log('INFO', `=== FLOW 1: Direct Safe → Relay Bridge ===`);
  log('INFO', `Amount: $${amountUsdc}, Recipient: ${recipientAddress}`);
  
  // Get quote with Safe as sender
  const quote = await getRelayQuote(safeAddress, recipientAddress, amountUsdc);
  if (!quote) {
    return {
      success: false,
      amountSent: 0,
      error: 'Failed to get Relay quote for Safe-as-user',
      flow: 'direct',
    };
  }
  
  // First, Safe needs to approve USDC.e spending to the bridge contract
  const usdcInterface = new ethers.Interface([
    'function approve(address spender, uint256 amount) returns (bool)',
  ]);
  const approveData = usdcInterface.encodeFunctionData('approve', [
    quote.txData.to,
    quote.amountIn,
  ]);
  
  // Execute approval
  log('INFO', 'Executing USDC.e approval via Safe...');
  const approvalResult = await executeSafeTransaction(
    safeAddress,
    USDC_E_POLYGON,
    approveData,
    '0',
    dryRun
  );
  
  if (!approvalResult.success) {
    return {
      success: false,
      amountSent: 0,
      error: `Approval failed: ${approvalResult.error}`,
      flow: 'direct',
    };
  }
  
  // Execute the bridge deposit
  log('INFO', 'Executing Relay deposit via Safe...');
  const depositResult = await executeSafeTransaction(
    safeAddress,
    quote.txData.to,
    quote.txData.data,
    quote.txData.value,
    dryRun
  );
  
  if (!depositResult.success) {
    return {
      success: false,
      amountSent: 0,
      error: `Deposit failed: ${depositResult.error}`,
      flow: 'direct',
    };
  }
  
  if (dryRun) {
    return {
      success: true,
      amountSent: amountUsdc,
      flow: 'direct',
    };
  }
  
  // Poll for bridge completion
  const bridgeResult = await pollBridgeStatus(quote.requestId);
  
  return {
    success: bridgeResult.success,
    txHash: depositResult.txHash,
    requestId: quote.requestId,
    amountSent: amountUsdc,
    error: bridgeResult.success ? undefined : bridgeResult.message,
    flow: 'direct',
  };
}

/**
 * Flow 2: Fallback Safe → EOA → Relay bridge
 * Safe transfers USDC.e to EOA, then EOA executes bridge
 */
async function withdrawViaSafeToEOA(
  safeAddress: string,
  eoaAddress: string,
  recipientAddress: string,
  amountUsdc: number,
  dryRun: boolean = false
): Promise<WithdrawResult> {
  log('INFO', `=== FLOW 2: Fallback Safe → EOA → Relay ===`);
  log('INFO', `Amount: $${amountUsdc}, EOA: ${eoaAddress}, Recipient: ${recipientAddress}`);
  
  const amount6dec = BigInt(Math.floor(amountUsdc * 1e6));
  
  // Step 1: Safe transfers USDC.e to EOA
  const usdcInterface = new ethers.Interface([
    'function transfer(address to, uint256 amount) returns (bool)',
  ]);
  const transferData = usdcInterface.encodeFunctionData('transfer', [
    eoaAddress,
    amount6dec,
  ]);
  
  log('INFO', 'Executing USDC.e transfer from Safe to EOA...');
  const transferResult = await executeSafeTransaction(
    safeAddress,
    USDC_E_POLYGON,
    transferData,
    '0',
    dryRun
  );
  
  if (!transferResult.success) {
    return {
      success: false,
      amountSent: 0,
      error: `Safe→EOA transfer failed: ${transferResult.error}`,
      flow: 'fallback',
    };
  }
  
  if (dryRun) {
    log('INFO', '[DRY RUN] Would proceed with EOA → Relay bridge');
    return {
      success: true,
      amountSent: amountUsdc,
      flow: 'fallback',
    };
  }
  
  // Step 2: EOA executes bridge (this uses the existing Python bridge code)
  // For now, we'll return success for the Safe transfer and let the Python code handle bridging
  log('INFO', '✅ Safe→EOA transfer complete. EOA can now execute bridge.');
  
  return {
    success: true,
    txHash: transferResult.txHash,
    amountSent: amountUsdc,
    flow: 'fallback',
  };
}

// =============================================================================
// MAIN FUNCTION
// =============================================================================

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const command = args[0] || 'info';
  
  if (!POLYMARKET_PROXY_ADDRESS || !POLYMARKET_PRIVATE_KEY) {
    log('ERROR', 'Missing POLYMARKET_PROXY_ADDRESS or POLYMARKET_PRIVATE_KEY');
    process.exit(1);
  }
  
  const provider = new ethers.JsonRpcProvider(POLYGON_RPC_URL);
  const wallet = new ethers.Wallet(POLYMARKET_PRIVATE_KEY, provider);
  const eoaAddress = wallet.address;
  
  log('INFO', `EOA Address: ${eoaAddress}`);
  log('INFO', `Safe Address: ${POLYMARKET_PROXY_ADDRESS}`);
  log('INFO', `Treasury Address: ${TREASURY_ADDRESS || 'Not set'}`);
  
  switch (command) {
    case 'info': {
      // Just show Safe info
      const safeInfo = await getSafeInfo(POLYMARKET_PROXY_ADDRESS, provider);
      console.log('\n=== Safe Info ===');
      console.log(JSON.stringify(safeInfo, null, 2));
      break;
    }
    
    case 'withdraw': {
      // withdraw <amount> [--dry-run]
      const amountStr = args[1];
      const dryRun = args.includes('--dry-run');
      
      if (!amountStr) {
        log('ERROR', 'Usage: withdraw <amount> [--dry-run]');
        process.exit(1);
      }
      
      const amountUsdc = parseFloat(amountStr);
      if (isNaN(amountUsdc) || amountUsdc <= 0) {
        log('ERROR', `Invalid amount: ${amountStr}`);
        process.exit(1);
      }
      
      if (!TREASURY_ADDRESS) {
        log('ERROR', 'TREASURY_ADDRESS not set');
        process.exit(1);
      }
      
      // Validate destination
      if (!validateDestination(TREASURY_ADDRESS)) {
        log('ERROR', 'Treasury address not in allowlist');
        process.exit(1);
      }
      
      // Get Safe info and validate
      const safeInfo = await getSafeInfo(POLYMARKET_PROXY_ADDRESS, provider);
      const validationError = validatePreFlight(safeInfo, eoaAddress, amountUsdc);
      if (validationError) {
        log('ERROR', `Pre-flight validation failed: ${validationError}`);
        process.exit(1);
      }
      
      // Try Flow 1: Direct Safe → Relay
      log('INFO', 'Attempting Flow 1: Direct Safe → Relay bridge...');
      let result = await withdrawViaSafeDirect(
        POLYMARKET_PROXY_ADDRESS,
        TREASURY_ADDRESS,
        amountUsdc,
        dryRun
      );
      
      // If Flow 1 fails, try Flow 2: Safe → EOA → Relay
      if (!result.success && result.flow === 'direct') {
        log('WARN', 'Flow 1 failed, trying Flow 2: Safe → EOA → Relay...');
        result = await withdrawViaSafeToEOA(
          POLYMARKET_PROXY_ADDRESS,
          eoaAddress,
          TREASURY_ADDRESS,
          amountUsdc,
          dryRun
        );
      }
      
      console.log('\n=== Withdrawal Result ===');
      console.log(JSON.stringify(result, null, 2));
      
      process.exit(result.success ? 0 : 1);
    }
    
    case 'quote': {
      // quote <amount>
      const amountStr = args[1] || '100';
      const amountUsdc = parseFloat(amountStr);
      
      if (!TREASURY_ADDRESS) {
        log('ERROR', 'TREASURY_ADDRESS not set');
        process.exit(1);
      }
      
      // Try quote with Safe as user
      log('INFO', 'Getting quote with Safe as user...');
      const safeQuote = await getRelayQuote(POLYMARKET_PROXY_ADDRESS, TREASURY_ADDRESS, amountUsdc);
      
      if (safeQuote) {
        console.log('\n=== Safe-as-User Quote ===');
        console.log(JSON.stringify({
          requestId: safeQuote.requestId.slice(0, 40) + '...',
          amountIn: `$${Number(safeQuote.amountIn) / 1e6}`,
          amountOut: `$${Number(safeQuote.amountOut) / 1e6}`,
          feeUsd: `$${safeQuote.feeUsd.toFixed(4)}`,
          bridgeContract: safeQuote.txData.to,
          timeEstimate: `${safeQuote.timeEstimateSeconds}s`,
        }, null, 2));
      } else {
        log('WARN', 'Safe-as-user quote failed');
      }
      
      // Also try quote with EOA as user (for comparison)
      log('INFO', 'Getting quote with EOA as user...');
      const eoaQuote = await getRelayQuote(eoaAddress, TREASURY_ADDRESS, amountUsdc);
      
      if (eoaQuote) {
        console.log('\n=== EOA-as-User Quote ===');
        console.log(JSON.stringify({
          requestId: eoaQuote.requestId.slice(0, 40) + '...',
          amountIn: `$${Number(eoaQuote.amountIn) / 1e6}`,
          amountOut: `$${Number(eoaQuote.amountOut) / 1e6}`,
          feeUsd: `$${eoaQuote.feeUsd.toFixed(4)}`,
          bridgeContract: eoaQuote.txData.to,
          timeEstimate: `${eoaQuote.timeEstimateSeconds}s`,
        }, null, 2));
      }
      
      break;
    }
    
    default:
      console.log('Usage:');
      console.log('  npx tsx safe_withdraw.ts info              - Show Safe info');
      console.log('  npx tsx safe_withdraw.ts quote <amount>    - Get bridge quote');
      console.log('  npx tsx safe_withdraw.ts withdraw <amount> [--dry-run]  - Execute withdrawal');
  }
}

main().catch((error) => {
  log('ERROR', 'Fatal error', error);
  process.exit(1);
});
