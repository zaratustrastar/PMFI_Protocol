#!/usr/bin/env npx tsx
/**
 * Safe Wallet USDC.e Withdrawal to Treasury
 * 
 * Withdraws USDC.e from the MetaMask Safe wallet on Polygon to the Treasury address
 * via the Polymarket Builder Relayer.
 * 
 * Usage:
 *   npx tsx safe_withdraw.ts info          # Show wallet info and balances
 *   npx tsx safe_withdraw.ts withdraw 100  # Withdraw 100 USDC.e to treasury
 *   npx tsx safe_withdraw.ts withdraw 100 --dry-run  # Simulate withdrawal
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

const USDC_E_ADDRESS = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174';
const USDC_DECIMALS = 6;
const RELAYER_URL = 'https://relayer-v2.polymarket.com/';
const POLYGON_CHAIN_ID = 137;

const MAX_SINGLE_WITHDRAW_USDC = 1000.0;
const MIN_WITHDRAW_USDC = 1.0;

interface WithdrawResult {
  success: boolean;
  txHash?: string;
  amount?: number;
  safeBalance?: number;
  error?: string;
}

interface WalletInfo {
  eoaAddress: string;
  safeAddress: string;
  treasuryAddress: string;
  safeUsdcBalance: number;
  eoaUsdcBalance: number;
}

async function getWalletInfo(): Promise<WalletInfo | null> {
  const builderKey = process.env.METAMASK_BUILDER_API_KEY;
  const builderSecret = process.env.METAMASK_BUILDER_SECRET;
  const builderPassphrase = process.env.METAMASK_BUILDER_PASSPHRASE;
  const privateKey = process.env.METAMASK_PRIVATE_KEY;
  const treasuryAddress = process.env.TREASURY_ADDRESS;
  const polygonRpc = process.env.POLYGON_RPC || 'https://polygon-rpc.com';

  if (!builderKey || !builderSecret || !builderPassphrase || !privateKey || !treasuryAddress) {
    console.error('Missing required environment variables');
    return null;
  }

  const provider = new JsonRpcProvider(polygonRpc);
  const wallet = new Wallet(privateKey, provider);

  const builderCreds: BuilderApiKeyCreds = {
    key: builderKey,
    secret: builderSecret,
    passphrase: builderPassphrase,
  };
  const builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });
  const client = new RelayClient(RELAYER_URL, POLYGON_CHAIN_ID, wallet, builderConfig, RelayerTxType.SAFE);

  const usdcContract = new Contract(
    USDC_E_ADDRESS,
    ['function balanceOf(address) view returns (uint256)'],
    provider
  );

  let safeAddress = '';
  try {
    const relayPayload = await client.getRelayPayload(wallet.address, 'SAFE');
    safeAddress = relayPayload.address;
  } catch (err) {
    console.error('Failed to get Safe address:', err instanceof Error ? err.message : err);
    return null;
  }

  let safeBalance = 0;
  let eoaBalance = 0;
  try {
    const safeBal = await usdcContract.balanceOf(safeAddress);
    safeBalance = Number(safeBal.toString()) / 1e6;
    const eoaBal = await usdcContract.balanceOf(wallet.address);
    eoaBalance = Number(eoaBal.toString()) / 1e6;
  } catch (err) {
    console.error('Failed to get balances:', err instanceof Error ? err.message : err);
  }

  return {
    eoaAddress: wallet.address,
    safeAddress,
    treasuryAddress,
    safeUsdcBalance: safeBalance,
    eoaUsdcBalance: eoaBalance,
  };
}

async function withdrawToTreasury(amountUsdc: number, dryRun: boolean = false): Promise<WithdrawResult> {
  const builderKey = process.env.METAMASK_BUILDER_API_KEY;
  const builderSecret = process.env.METAMASK_BUILDER_SECRET;
  const builderPassphrase = process.env.METAMASK_BUILDER_PASSPHRASE;
  const privateKey = process.env.METAMASK_PRIVATE_KEY;
  const treasuryAddress = process.env.TREASURY_ADDRESS;
  const polygonRpc = process.env.POLYGON_RPC || 'https://polygon-rpc.com';

  if (!builderKey || !builderSecret || !builderPassphrase || !privateKey || !treasuryAddress) {
    return { success: false, error: 'Missing required environment variables' };
  }

  if (amountUsdc < MIN_WITHDRAW_USDC) {
    return { success: false, error: `Amount ${amountUsdc} below minimum ${MIN_WITHDRAW_USDC} USDC` };
  }

  if (amountUsdc > MAX_SINGLE_WITHDRAW_USDC) {
    return { success: false, error: `Amount ${amountUsdc} exceeds max ${MAX_SINGLE_WITHDRAW_USDC} USDC per transaction` };
  }

  const provider = new JsonRpcProvider(polygonRpc);
  const wallet = new Wallet(privateKey, provider);

  const builderCreds: BuilderApiKeyCreds = {
    key: builderKey,
    secret: builderSecret,
    passphrase: builderPassphrase,
  };
  const builderConfig = new BuilderConfig({ localBuilderCreds: builderCreds });
  const client = new RelayClient(RELAYER_URL, POLYGON_CHAIN_ID, wallet, builderConfig, RelayerTxType.SAFE);

  const usdcContract = new Contract(
    USDC_E_ADDRESS,
    ['function balanceOf(address) view returns (uint256)'],
    provider
  );

  let safeAddress = '';
  try {
    const relayPayload = await client.getRelayPayload(wallet.address, 'SAFE');
    safeAddress = relayPayload.address;
  } catch (err) {
    return { success: false, error: `Failed to get Safe address: ${err instanceof Error ? err.message : err}` };
  }

  let safeBalance = 0;
  try {
    const safeBal = await usdcContract.balanceOf(safeAddress);
    safeBalance = Number(safeBal.toString()) / 1e6;
  } catch (err) {
    return { success: false, error: `Failed to get Safe balance: ${err instanceof Error ? err.message : err}` };
  }

  if (safeBalance < amountUsdc) {
    return { 
      success: false, 
      error: `Insufficient Safe balance: ${safeBalance.toFixed(2)} < ${amountUsdc} USDC`,
      safeBalance 
    };
  }

  console.log(`Safe wallet: ${safeAddress}`);
  console.log(`Safe balance: ${safeBalance.toFixed(2)} USDC.e`);
  console.log(`Withdrawing: ${amountUsdc} USDC.e`);
  console.log(`To treasury: ${treasuryAddress}`);

  if (dryRun) {
    console.log('[DRY RUN] Would submit transaction');
    return { success: true, amount: amountUsdc, safeBalance };
  }

  const amountInBaseUnits = Math.floor(amountUsdc * Math.pow(10, USDC_DECIMALS));
  const amountBN = BigNumber.from(amountInBaseUnits);

  const erc20Interface = new Interface([
    'function transfer(address to, uint256 amount) returns (bool)'
  ]);
  const calldata = erc20Interface.encodeFunctionData('transfer', [treasuryAddress, amountBN]);

  const tx: Transaction = {
    to: USDC_E_ADDRESS,
    data: calldata,
    value: '0',
  };

  try {
    console.log('Submitting to relayer...');
    const response = await client.execute([tx]);
    console.log(`Transaction ID: ${response.transactionID}`);
    console.log(`Initial state: ${response.state}`);

    console.log('Polling for confirmation...');
    const result = await client.pollUntilState(
      response.transactionID,
      ['CONFIRMED', 'MINED'],
      'FAILED',
      60,
      2000
    );

    if (result && (result.state === 'CONFIRMED' || result.state === 'MINED')) {
      const txHash = result.transactionHash || '';
      console.log(`SUCCESS: ${txHash || 'confirmed without hash'}`);
      return {
        success: true,
        txHash: txHash,
        amount: amountUsdc,
        safeBalance: safeBalance - amountUsdc,
      };
    } else {
      const errorState = result?.state || 'UNKNOWN';
      return { success: false, error: `Transaction failed: state=${errorState}`, safeBalance };
    }
  } catch (err) {
    return { 
      success: false, 
      error: `Relayer error: ${err instanceof Error ? err.message : err}`,
      safeBalance 
    };
  }
}

async function main() {
  const args = process.argv.slice(2);
  const command = args[0] || 'info';

  if (command === 'info') {
    const info = await getWalletInfo();
    if (info) {
      console.log(JSON.stringify(info));
    } else {
      console.log(JSON.stringify({ error: 'Failed to get wallet info' }));
      process.exit(1);
    }
  } else if (command === 'withdraw') {
    const amountStr = args[1];
    if (!amountStr) {
      console.error('Usage: safe_withdraw.ts withdraw <amount> [--dry-run]');
      process.exit(1);
    }

    const amount = parseFloat(amountStr);
    if (isNaN(amount) || amount <= 0) {
      console.error('Invalid amount:', amountStr);
      process.exit(1);
    }

    const dryRun = args.includes('--dry-run');
    const result = await withdrawToTreasury(amount, dryRun);
    console.log(JSON.stringify(result));

    if (!result.success) {
      process.exit(1);
    }
  } else {
    console.error('Unknown command:', command);
    console.error('Usage: safe_withdraw.ts [info|withdraw <amount>]');
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
