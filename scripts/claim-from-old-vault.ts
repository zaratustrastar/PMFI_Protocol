#!/usr/bin/env npx tsx
/**
 * Claim Pending Withdrawal from Old V7 Contract
 * 
 * This script fetches signed NAV data from the bot and claims your pending withdrawal
 * from the old V7 contract (0xfcfa01291d1e75f71e97c4EE53f675D7622988b4)
 */

import { createPublicClient, createWalletClient, http, formatUnits } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { base } from 'viem/chains';
import * as dotenv from 'dotenv';

dotenv.config();

const OLD_VAULT_ADDRESS = '0xfcfa01291d1e75f71e97c4EE53f675D7622988b4';
const BOT_API_URL = 'http://147.182.206.35:8080';
const BASE_RPC = 'https://mainnet.base.org';

const PRIVATE_KEY = process.env.METAMASK_PRIVATE_KEY || process.env.POLYMARKET_PRIVATE_KEY;
if (!PRIVATE_KEY) {
  throw new Error('METAMASK_PRIVATE_KEY or POLYMARKET_PRIVATE_KEY required in .env');
}

const OLD_VAULT_ABI = [
  {
    name: 'claim',
    type: 'function',
    inputs: [
      { name: 'requestId', type: 'uint256' },
      {
        name: 'navData',
        type: 'tuple',
        components: [
          { name: 'totalAssets', type: 'uint256' },
          { name: 'creditedCash', type: 'uint256' },
          { name: 'creditedPositions', type: 'uint256' },
          { name: 'pendingCredit', type: 'uint256' },
          { name: 'inFlightOnChain', type: 'uint256' },
          { name: 'timestamp', type: 'uint256' },
          { name: 'deadline', type: 'uint256' },
          { name: 'roundId', type: 'uint256' },
        ],
      },
      { name: 'signature', type: 'bytes' },
    ],
    outputs: [],
    stateMutability: 'nonpayable',
  },
  {
    name: 'getUserWithdrawals',
    type: 'function',
    inputs: [{ name: 'user', type: 'address' }],
    outputs: [{ type: 'uint256[]' }],
    stateMutability: 'view',
  },
  {
    name: 'getWithdrawalRequest',
    type: 'function',
    inputs: [{ name: 'requestId', type: 'uint256' }],
    outputs: [
      { name: 'user', type: 'address' },
      { name: 'shares', type: 'uint256' },
      { name: 'requestTime', type: 'uint256' },
      { name: 'claimed', type: 'bool' },
      { name: 'expired', type: 'bool' },
    ],
    stateMutability: 'view',
  },
  {
    name: 'balanceOf',
    type: 'function',
    inputs: [{ name: 'account', type: 'address' }],
    outputs: [{ type: 'uint256' }],
    stateMutability: 'view',
  },
] as const;

const USDC_ABI = [
  {
    name: 'balanceOf',
    type: 'function',
    inputs: [{ name: 'account', type: 'address' }],
    outputs: [{ type: 'uint256' }],
    stateMutability: 'view',
  },
] as const;

const USDC_ADDRESS = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';

async function main() {
  console.log('='.repeat(60));
  console.log('🔓 Claim Pending Withdrawal from Old V7 Contract');
  console.log('='.repeat(60));

  const account = privateKeyToAccount(PRIVATE_KEY as `0x${string}`);
  console.log(`\n📋 Your wallet: ${account.address}`);

  const publicClient = createPublicClient({
    chain: base,
    transport: http(BASE_RPC),
  });

  const walletClient = createWalletClient({
    account,
    chain: base,
    transport: http(BASE_RPC),
  });

  // Check USDC balance in old vault
  const vaultUsdcBalance = await publicClient.readContract({
    address: USDC_ADDRESS,
    abi: USDC_ABI,
    functionName: 'balanceOf',
    args: [OLD_VAULT_ADDRESS],
  });
  console.log(`\n💰 Old vault USDC balance: $${formatUnits(vaultUsdcBalance, 6)}`);

  if (vaultUsdcBalance === 0n) {
    console.log('\n⚠️  Old vault has no USDC! Cannot claim.');
    console.log('   You may need to bridge funds back to the vault first.');
    return;
  }

  // Get user's withdrawal requests
  const requestIds = await publicClient.readContract({
    address: OLD_VAULT_ADDRESS,
    abi: OLD_VAULT_ABI,
    functionName: 'getUserWithdrawals',
    args: [account.address],
  });

  console.log(`\n📝 Your withdrawal request IDs: ${requestIds.join(', ') || 'None'}`);

  if (requestIds.length === 0) {
    console.log('\n✅ No pending withdrawal requests found!');
    return;
  }

  // Check each request
  const pendingRequests: { id: bigint; shares: bigint; requestTime: bigint }[] = [];
  
  for (const requestId of requestIds) {
    const [user, shares, requestTime, claimed, expired] = await publicClient.readContract({
      address: OLD_VAULT_ADDRESS,
      abi: OLD_VAULT_ABI,
      functionName: 'getWithdrawalRequest',
      args: [requestId],
    });

    console.log(`\n   Request #${requestId}:`);
    console.log(`     Shares: ${formatUnits(shares, 18)}`);
    console.log(`     Claimed: ${claimed}`);
    console.log(`     Expired: ${expired}`);

    if (!claimed && !expired) {
      pendingRequests.push({ id: requestId, shares, requestTime });
    }
  }

  if (pendingRequests.length === 0) {
    console.log('\n✅ All requests are already claimed or expired!');
    return;
  }

  console.log(`\n🔄 Found ${pendingRequests.length} pending request(s) to claim`);

  // Fetch signed NAV from bot
  console.log(`\n📡 Fetching signed NAV from bot at ${BOT_API_URL}...`);
  
  let signedNav;
  try {
    const response = await fetch(`${BOT_API_URL}/sign-nav`);
    if (!response.ok) {
      throw new Error(`Bot returned ${response.status}: ${await response.text()}`);
    }
    signedNav = await response.json();
    console.log('✅ Got signed NAV data');
    console.log(`   Total Assets: $${formatUnits(BigInt(signedNav.navData.totalAssets), 6)}`);
    console.log(`   Round ID: ${signedNav.navData.roundId}`);
  } catch (error: any) {
    console.error(`\n❌ Failed to fetch NAV from bot: ${error.message}`);
    console.log('\n💡 Make sure the bot is running on the VPS:');
    console.log('   ssh to VPS and run: python bot/bot_v7.py');
    return;
  }

  // Build navData tuple
  const navData = {
    totalAssets: BigInt(signedNav.navData.totalAssets),
    creditedCash: BigInt(signedNav.navData.creditedCash),
    creditedPositions: BigInt(signedNav.navData.creditedPositions),
    pendingCredit: BigInt(signedNav.navData.pendingCredit),
    inFlightOnChain: BigInt(signedNav.navData.inFlightOnChain),
    timestamp: BigInt(signedNav.navData.timestamp),
    deadline: BigInt(signedNav.navData.deadline),
    roundId: BigInt(signedNav.navData.roundId),
  };

  const signature = signedNav.signature as `0x${string}`;

  // Claim each pending request
  for (const req of pendingRequests) {
    console.log(`\n🚀 Claiming request #${req.id}...`);
    
    try {
      const hash = await walletClient.writeContract({
        address: OLD_VAULT_ADDRESS,
        abi: OLD_VAULT_ABI,
        functionName: 'claim',
        args: [req.id, navData, signature],
      });

      console.log(`   TX Hash: ${hash}`);
      console.log('   Waiting for confirmation...');

      const receipt = await publicClient.waitForTransactionReceipt({ hash });
      
      if (receipt.status === 'success') {
        console.log(`   ✅ Claimed successfully!`);
      } else {
        console.log(`   ❌ Transaction failed`);
      }
    } catch (error: any) {
      console.error(`   ❌ Claim failed: ${error.message}`);
      
      if (error.message.includes('Insufficient buffer')) {
        console.log('   💡 Not enough USDC in vault buffer. Need to bridge funds back first.');
      } else if (error.message.includes('RoundId must increase')) {
        console.log('   💡 NAV round ID issue. The bot may need to generate a fresh NAV.');
      } else if (error.message.includes('conservation')) {
        console.log('   💡 Conservation bound issue. NAV data may be stale or incorrect.');
      }
    }
  }

  // Final balance check
  const finalUsdcBalance = await publicClient.readContract({
    address: USDC_ADDRESS,
    abi: USDC_ABI,
    functionName: 'balanceOf',
    args: [account.address],
  });
  console.log(`\n💰 Your USDC balance: $${formatUnits(finalUsdcBalance, 6)}`);

  console.log('\n' + '='.repeat(60));
  console.log('Done!');
}

main().catch(console.error);
