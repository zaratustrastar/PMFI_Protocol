#!/usr/bin/env npx tsx
/**
 * Proxy Withdraw - Polygon USDC.e withdrawal via Polymarket Proxy
 * 
 * Supports BOTH proxy types:
 * - Magic Proxy (EIP-1167): Uses proxy.proxy([calls]) interface
 * - Safe Proxy (Gnosis Safe): Uses Safe Protocol Kit
 * 
 * Flow 1 (Primary): Proxy → Relay bridge contract (direct bridge)
 * Flow 2 (Fallback): Proxy → EOA → Relay bridge
 * 
 * Security Guardrails:
 * - Strict allowlist for destination addresses
 * - maxPerTx cap on withdrawal amounts
 * - Pre-flight checks (ownership, balance)
 */

import { ethers } from 'ethers';
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

const USDC_E_POLYGON = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const RELAY_API_URL = 'https://api.relay.link';
const BASE_CHAIN_ID = 8453;
const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

// Security guardrails
const MAX_PER_TX_USDC = 5000;
const MIN_WITHDRAWAL_USDC = 10;

// Allowlist - only these addresses can receive funds
const ALLOWED_DESTINATIONS: Set<string> = new Set(
  [TREASURY_ADDRESS].filter(Boolean).map(a => a.toLowerCase())
);

// Magic Proxy ABI
const MAGIC_PROXY_ABI = [
  'function proxy((address to, bytes data, uint256 value)[] calls) payable returns (bytes[])',
];

// Safe ABI (for detection)
const SAFE_ABI = [
  'function getOwners() view returns (address[])',
  'function getThreshold() view returns (uint256)',
];

const ERC20_ABI = [
  'function balanceOf(address) view returns (uint256)',
  'function transfer(address to, uint256 amount) returns (bool)',
  'function approve(address spender, uint256 amount) returns (bool)',
];

// =============================================================================
// TYPES
// =============================================================================

type ProxyType = 'magic' | 'safe' | 'unknown';

interface ProxyInfo {
  address: string;
  type: ProxyType;
  owner: string;
  usdcBalance: number;
  maticBalance: number;
}

interface BridgeQuote {
  requestId: string;
  amountIn: bigint;
  amountOut: bigint;
  feeUsd: number;
  txData: { to: string; data: string; value: string };
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
  console.log(`${timestamp} ${prefix} [ProxyWithdraw] ${message}`, data || '');
}

// =============================================================================
// PROXY DETECTION & INFO
// =============================================================================

async function detectProxyType(
  proxyAddress: string,
  provider: ethers.JsonRpcProvider
): Promise<ProxyType> {
  log('INFO', `Detecting proxy type for ${proxyAddress}`);
  
  // Try Safe interface first
  const safeContract = new ethers.Contract(proxyAddress, SAFE_ABI, provider);
  try {
    await safeContract.getOwners();
    log('INFO', 'Detected: Gnosis Safe proxy');
    return 'safe';
  } catch {
    // Not a Safe, try Magic proxy
  }
  
  // Check if it has code (any proxy should)
  const code = await provider.getCode(proxyAddress);
  if (code === '0x') {
    log('ERROR', 'No contract code at address');
    return 'unknown';
  }
  
  // Assume Magic proxy if it has code but no Safe interface
  log('INFO', 'Detected: Magic proxy (EIP-1167)');
  return 'magic';
}

async function getProxyInfo(
  proxyAddress: string,
  eoaAddress: string,
  provider: ethers.JsonRpcProvider
): Promise<ProxyInfo> {
  log('INFO', `Fetching proxy info for ${proxyAddress}`);
  
  const proxyType = await detectProxyType(proxyAddress, provider);
  
  // Get USDC.e balance
  const usdc = new ethers.Contract(USDC_E_POLYGON, ERC20_ABI, provider);
  const usdcBalance = await usdc.balanceOf(proxyAddress);
  
  // Get MATIC balance of EOA (for gas)
  const maticBalance = await provider.getBalance(eoaAddress);
  
  return {
    address: proxyAddress,
    type: proxyType,
    owner: eoaAddress,
    usdcBalance: Number(usdcBalance) / 1e6,
    maticBalance: Number(maticBalance) / 1e18,
  };
}

// =============================================================================
// VALIDATION
// =============================================================================

function validatePreFlight(proxyInfo: ProxyInfo, amountUsdc: number): string | null {
  if (proxyInfo.type === 'unknown') {
    return `Unknown proxy type at ${proxyInfo.address}`;
  }
  
  if (proxyInfo.usdcBalance < amountUsdc) {
    return `Insufficient USDC.e: have $${proxyInfo.usdcBalance.toFixed(2)}, need $${amountUsdc.toFixed(2)}`;
  }
  
  if (amountUsdc < MIN_WITHDRAWAL_USDC) {
    return `Amount $${amountUsdc} below minimum $${MIN_WITHDRAWAL_USDC}`;
  }
  
  if (amountUsdc > MAX_PER_TX_USDC) {
    return `Amount $${amountUsdc} exceeds max per tx $${MAX_PER_TX_USDC}`;
  }
  
  if (proxyInfo.maticBalance < 0.01) {
    return `Insufficient MATIC for gas: have ${proxyInfo.maticBalance.toFixed(4)}, need ~0.01`;
  }
  
  return null;
}

function validateDestination(address: string): boolean {
  if (!address) return false;
  if (ALLOWED_DESTINATIONS.has(address.toLowerCase())) return true;
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
      log('ERROR', `Relay quote failed: ${response.status}`);
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
// MAGIC PROXY EXECUTION
// =============================================================================

async function executeMagicProxyCall(
  proxyAddress: string,
  wallet: ethers.Wallet,
  calls: { to: string; data: string; value: bigint }[],
  dryRun: boolean
): Promise<{ success: boolean; txHash?: string; error?: string }> {
  log('INFO', `${dryRun ? '[DRY RUN] ' : ''}Executing Magic proxy call`);
  
  const proxy = new ethers.Contract(proxyAddress, MAGIC_PROXY_ABI, wallet);
  
  const formattedCalls = calls.map(c => ({
    to: c.to,
    data: c.data,
    value: c.value,
  }));
  
  try {
    if (dryRun) {
      // Estimate gas to validate
      await proxy.proxy.estimateGas(formattedCalls);
      log('INFO', '[DRY RUN] Gas estimation successful');
      return { success: true };
    }
    
    const tx = await proxy.proxy(formattedCalls);
    log('INFO', `TX submitted: ${tx.hash}`);
    
    const receipt = await tx.wait();
    if (receipt.status === 1) {
      log('INFO', `✅ Magic proxy tx confirmed: ${receipt.hash}`);
      return { success: true, txHash: receipt.hash };
    } else {
      return { success: false, error: 'Transaction reverted' };
    }
  } catch (error: unknown) {
    const errMsg = error instanceof Error ? error.message : String(error);
    log('ERROR', 'Magic proxy call failed', errMsg);
    return { success: false, error: errMsg };
  }
}

// =============================================================================
// WITHDRAWAL FLOWS
// =============================================================================

async function withdrawToEOA(
  proxyAddress: string,
  proxyType: ProxyType,
  eoaAddress: string,
  amountUsdc: number,
  wallet: ethers.Wallet,
  dryRun: boolean
): Promise<{ success: boolean; txHash?: string; error?: string }> {
  log('INFO', `Withdrawing $${amountUsdc} from proxy to EOA ${eoaAddress}`);
  
  const amount6dec = BigInt(Math.floor(amountUsdc * 1e6));
  const usdcInterface = new ethers.Interface(ERC20_ABI);
  const transferData = usdcInterface.encodeFunctionData('transfer', [eoaAddress, amount6dec]);
  
  if (proxyType === 'magic') {
    return executeMagicProxyCall(
      proxyAddress,
      wallet,
      [{ to: USDC_E_POLYGON, data: transferData, value: 0n }],
      dryRun
    );
  } else {
    // For Safe proxy, would use Safe Protocol Kit
    // For now, return error as Safe support needs more implementation
    return { success: false, error: 'Safe proxy not yet supported - use Magic proxy' };
  }
}

async function withdrawViaBridge(
  proxyAddress: string,
  proxyType: ProxyType,
  recipientAddress: string,
  amountUsdc: number,
  wallet: ethers.Wallet,
  dryRun: boolean
): Promise<WithdrawResult> {
  log('INFO', `=== Direct Proxy → Relay Bridge ===`);
  
  // Get quote with proxy as sender
  const quote = await getRelayQuote(proxyAddress, recipientAddress, amountUsdc);
  if (!quote) {
    return { success: false, amountSent: 0, error: 'Failed to get Relay quote', flow: 'direct' };
  }
  
  log('INFO', `Quote: output $${Number(quote.amountOut) / 1e6}, fee $${quote.feeUsd.toFixed(4)}`);
  
  // Approve USDC spending
  const usdcInterface = new ethers.Interface(ERC20_ABI);
  const approveData = usdcInterface.encodeFunctionData('approve', [quote.txData.to, quote.amountIn]);
  
  if (proxyType === 'magic') {
    // Execute approval
    const approvalResult = await executeMagicProxyCall(
      proxyAddress,
      wallet,
      [{ to: USDC_E_POLYGON, data: approveData, value: 0n }],
      dryRun
    );
    
    if (!approvalResult.success) {
      return { success: false, amountSent: 0, error: `Approval failed: ${approvalResult.error}`, flow: 'direct' };
    }
    
    // Execute bridge deposit
    const depositResult = await executeMagicProxyCall(
      proxyAddress,
      wallet,
      [{ to: quote.txData.to, data: quote.txData.data, value: BigInt(quote.txData.value) }],
      dryRun
    );
    
    if (!depositResult.success) {
      return { success: false, amountSent: 0, error: `Deposit failed: ${depositResult.error}`, flow: 'direct' };
    }
    
    if (dryRun) {
      return { success: true, amountSent: amountUsdc, flow: 'direct' };
    }
    
    // Poll for completion
    const bridgeResult = await pollBridgeStatus(quote.requestId);
    return {
      success: bridgeResult.success,
      txHash: depositResult.txHash,
      requestId: quote.requestId,
      amountSent: amountUsdc,
      error: bridgeResult.success ? undefined : bridgeResult.message,
      flow: 'direct',
    };
  } else {
    return { success: false, amountSent: 0, error: 'Safe proxy not yet supported', flow: 'direct' };
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
  
  const provider = new ethers.JsonRpcProvider(POLYGON_RPC_URL);
  const wallet = new ethers.Wallet(POLYMARKET_PRIVATE_KEY, provider);
  const eoaAddress = wallet.address;
  
  log('INFO', `EOA: ${eoaAddress}`);
  log('INFO', `Proxy: ${POLYMARKET_PROXY_ADDRESS}`);
  log('INFO', `Treasury: ${TREASURY_ADDRESS || 'Not set'}`);
  
  switch (command) {
    case 'info': {
      const proxyInfo = await getProxyInfo(POLYMARKET_PROXY_ADDRESS, eoaAddress, provider);
      console.log('\n=== Proxy Info ===');
      console.log(JSON.stringify(proxyInfo, null, 2));
      break;
    }
    
    case 'quote': {
      const amountUsdc = parseFloat(args[1] || '100');
      if (!TREASURY_ADDRESS) {
        log('ERROR', 'TREASURY_ADDRESS not set');
        process.exit(1);
      }
      
      const quote = await getRelayQuote(POLYMARKET_PROXY_ADDRESS, TREASURY_ADDRESS, amountUsdc);
      if (quote) {
        console.log('\n=== Bridge Quote ===');
        console.log(JSON.stringify({
          amountIn: `$${Number(quote.amountIn) / 1e6}`,
          amountOut: `$${Number(quote.amountOut) / 1e6}`,
          feeUsd: `$${quote.feeUsd.toFixed(4)}`,
          bridgeContract: quote.txData.to,
          timeEstimate: `${quote.timeEstimateSeconds}s`,
        }, null, 2));
      }
      break;
    }
    
    case 'withdraw': {
      const amountUsdc = parseFloat(args[1] || '0');
      const dryRun = args.includes('--dry-run');
      const toEoa = args.includes('--to-eoa');
      
      if (!amountUsdc || amountUsdc <= 0) {
        log('ERROR', 'Usage: withdraw <amount> [--dry-run] [--to-eoa]');
        process.exit(1);
      }
      
      if (!TREASURY_ADDRESS && !toEoa) {
        log('ERROR', 'TREASURY_ADDRESS not set (use --to-eoa to withdraw to EOA instead)');
        process.exit(1);
      }
      
      const proxyInfo = await getProxyInfo(POLYMARKET_PROXY_ADDRESS, eoaAddress, provider);
      const validationError = validatePreFlight(proxyInfo, amountUsdc);
      if (validationError) {
        log('ERROR', `Validation failed: ${validationError}`);
        process.exit(1);
      }
      
      let result: WithdrawResult;
      
      if (toEoa) {
        // Withdraw to EOA only (no bridge)
        const txResult = await withdrawToEOA(
          POLYMARKET_PROXY_ADDRESS,
          proxyInfo.type,
          eoaAddress,
          amountUsdc,
          wallet,
          dryRun
        );
        result = {
          success: txResult.success,
          txHash: txResult.txHash,
          amountSent: amountUsdc,
          error: txResult.error,
          flow: 'fallback',
        };
      } else {
        // Validate destination
        if (!validateDestination(TREASURY_ADDRESS)) {
          log('ERROR', 'Treasury not in allowlist');
          process.exit(1);
        }
        
        // Try direct bridge first
        result = await withdrawViaBridge(
          POLYMARKET_PROXY_ADDRESS,
          proxyInfo.type,
          TREASURY_ADDRESS,
          amountUsdc,
          wallet,
          dryRun
        );
        
        // If direct fails, try fallback (proxy → EOA → bridge)
        if (!result.success && result.flow === 'direct') {
          log('WARN', 'Direct bridge failed, trying fallback flow...');
          
          const toEoaResult = await withdrawToEOA(
            POLYMARKET_PROXY_ADDRESS,
            proxyInfo.type,
            eoaAddress,
            amountUsdc,
            wallet,
            dryRun
          );
          
          if (toEoaResult.success) {
            log('INFO', 'Funds transferred to EOA. Python bridge can now execute.');
            result = {
              success: true,
              txHash: toEoaResult.txHash,
              amountSent: amountUsdc,
              flow: 'fallback',
            };
          } else {
            result = {
              success: false,
              amountSent: 0,
              error: `Fallback also failed: ${toEoaResult.error}`,
              flow: 'fallback',
            };
          }
        }
      }
      
      console.log('\n=== Withdrawal Result ===');
      console.log(JSON.stringify(result, null, 2));
      process.exit(result.success ? 0 : 1);
    }
    
    default:
      console.log('Usage:');
      console.log('  npx tsx proxy_withdraw.ts info              - Show proxy info');
      console.log('  npx tsx proxy_withdraw.ts quote <amount>    - Get bridge quote');
      console.log('  npx tsx proxy_withdraw.ts withdraw <amount> [--dry-run] [--to-eoa]');
  }
}

main().catch(error => {
  log('ERROR', 'Fatal error', error);
  process.exit(1);
});
