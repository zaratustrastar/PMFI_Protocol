/**
 * seed-arb-vault-v2-report.ts
 * ─────────────────────────────────────────────────────────────────────────
 * Initialise the PMFIArbVaultV2 contract with its first report().
 *
 * WHY THIS IS NEEDED
 *   The constructor sets officialPPS = 1.000000 USDC, but `lastReportNonce = 0`
 *   and `lastReportedBackingAssets = 0`.  The first user deposit can be queued
 *   via requestDeposit() immediately, but `claimDeposit()` can only be called
 *   AFTER a report() has processed that request.  Running this seed script
 *   immediately after deploy (before any user deposits) sets nonce = 1 and
 *   confirms the clean-slate backing so the first real report() cycles cleanly.
 *
 * WHAT IT DOES
 *   Calls report(reportData, sig) with:
 *     - reportedAssets = 0  (no real USDC deployed yet — clean start)
 *     - nonce = 1           (strictly > lastReportNonce which is 0 on deploy)
 *     - timestamp / deadline based on current block time
 *
 * REQUIRED ENV VARS
 *   ARB_VAULT_V2_ADDRESS       — deployed V2 contract address on Base
 *   ARB_NAV_SIGNER_PRIVATE_KEY — private key whose address == contract.reportSigner
 *   BASE_RPC_URL               — (optional) Base RPC, default https://mainnet.base.org
 *
 * OPTIONAL ENV VARS
 *   ARB_REPORTED_ASSETS_USDC   — override reported assets in USDC (default 0)
 *   ARB_SEED_NONCE             — override nonce (default reads lastReportNonce+1)
 *
 * USAGE
 *   npx tsx scripts/seed-arb-vault-v2-report.ts
 */

import { ethers } from "ethers";
import * as dotenv from "dotenv";
dotenv.config();

// ── Config ────────────────────────────────────────────────────────────────

const BASE_RPC_URL      = process.env.BASE_RPC_URL ?? "https://mainnet.base.org";
const VAULT_ADDRESS     = process.env.ARB_VAULT_V2_ADDRESS ?? "";
const SIGNER_PRIV_KEY   = process.env.ARB_NAV_SIGNER_PRIVATE_KEY ?? "";
const CHAIN_ID          = 8453n;

if (!VAULT_ADDRESS)   throw new Error("ARB_VAULT_V2_ADDRESS not set");
if (!SIGNER_PRIV_KEY) throw new Error("ARB_NAV_SIGNER_PRIVATE_KEY not set");

// ── Minimal ABI (only what we need) ──────────────────────────────────────

const VAULT_ABI = [
  // State readers
  "function officialPPS() view returns (uint256)",
  "function lastReportNonce() view returns (uint256)",
  "function lastReportedBackingAssets() view returns (uint256)",
  "function reportSigner() view returns (address)",
  "function totalPendingDepositAssets() view returns (uint256)",
  "function totalPendingRedeemShares() view returns (uint256)",
  "function REPORT_TYPEHASH() view returns (bytes32)",
  "function DOMAIN_SALT() view returns (bytes32)",
  "function paused() view returns (bool)",
  "function shutdown() view returns (bool)",
  "function reportCooldown() view returns (uint256)",
  "function lastReportTimestamp() view returns (uint256)",
  // report() — deployed ABI: 4-field struct (vault/chainId/domainSalt hardcoded inside contract)
  "function report(tuple(uint256 reportedAssets, uint256 timestamp, uint256 deadline, uint256 nonce) data, bytes signature, uint256 maxDeposits, uint256 maxRedeems)",
];

// ── Helpers ───────────────────────────────────────────────────────────────

function fmt6(raw: bigint): string {
  return (Number(raw) / 1e6).toFixed(6);
}
function fmt18(raw: bigint): string {
  return (Number(raw) / 1e18).toFixed(6);
}

async function buildAndSignReport(
  wallet: ethers.Wallet,
  vault: ethers.Contract,
  reportedAssetsUsdc: bigint,
  nonce: bigint,
  reportTypehash: string,
  domainSalt: string,
): Promise<{ callData: object; signature: string; structHash: string }> {
  const now      = BigInt(Math.floor(Date.now() / 1000));
  const deadline = now + 3600n; // valid for 1 hour

  // 4-field struct passed to report() — vault/chainId/domainSalt are
  // added internally by the contract during signature verification
  const callData = {
    reportedAssets: reportedAssetsUsdc,
    timestamp:      now,
    deadline,
    nonce,
  };

  // structHash = keccak256(abi.encode(TYPEHASH, ...7 fields))
  // The contract's _verifyReportSignature appends address(this), block.chainid,
  // DOMAIN_SALT to the struct fields before hashing — so we must match exactly.
  const structHash = ethers.keccak256(
    ethers.AbiCoder.defaultAbiCoder().encode(
      ["bytes32", "uint256", "uint256", "uint256", "uint256", "address", "uint256", "bytes32"],
      [
        reportTypehash,
        callData.reportedAssets,
        callData.timestamp,
        callData.deadline,
        callData.nonce,
        VAULT_ADDRESS,      // address(this) in contract
        CHAIN_ID,           // block.chainid
        domainSalt,         // DOMAIN_SALT constant
      ],
    ),
  );

  // digest = keccak256("\x19Ethereum Signed Message:\n32" || structHash)
  // wallet.signMessage(bytes32) applies the EIP-191 prefix automatically
  const signature = await wallet.signMessage(ethers.getBytes(structHash));

  return { callData, signature, structHash };
}

// ── Main ─────────────────────────────────────────────────────────────────

async function main() {
  const provider = new ethers.JsonRpcProvider(BASE_RPC_URL);
  const signerWallet = new ethers.Wallet(SIGNER_PRIV_KEY, provider);

  console.log(`\n🌱 pARB Vault V2 — Seed Report`);
  console.log(`   Vault:      ${VAULT_ADDRESS}`);
  console.log(`   Signer:     ${signerWallet.address}`);
  console.log(`   Chain:      Base (${CHAIN_ID})`);
  console.log(`   RPC:        ${BASE_RPC_URL}\n`);

  const vault = new ethers.Contract(VAULT_ADDRESS, VAULT_ABI, signerWallet);

  // ── Pre-flight checks ────────────────────────────────────────────────
  const [
    reportSignerAddr,
    currentPPS,
    lastNonce,
    lastBacking,
    pendingDepositAssets,
    pendingRedeemShares,
    reportTypehash,
    domainSalt,
    isPaused,
    isShutdown,
    reportCooldown,
    lastReportTimestamp,
  ] = await Promise.all([
    vault.reportSigner(),
    vault.officialPPS(),
    vault.lastReportNonce(),
    vault.lastReportedBackingAssets(),
    vault.totalPendingDepositAssets(),
    vault.totalPendingRedeemShares(),
    vault.REPORT_TYPEHASH(),
    vault.DOMAIN_SALT(),
    vault.paused(),
    vault.shutdown(),
    vault.reportCooldown(),
    vault.lastReportTimestamp(),
  ]);

  const now = BigInt(Math.floor(Date.now() / 1000));
  const cooldownReady = now >= lastReportTimestamp + reportCooldown;

  console.log(`📊 Current vault state:`);
  console.log(`   officialPPS:               ${fmt6(currentPPS)} USDC/share`);
  console.log(`   lastReportNonce:           ${lastNonce}`);
  console.log(`   lastReportedBackingAssets: ${fmt6(lastBacking)} USDC`);
  console.log(`   totalPendingDepositAssets: ${fmt6(pendingDepositAssets)} USDC`);
  console.log(`   totalPendingRedeemShares:  ${fmt18(pendingRedeemShares)} shares`);
  console.log(`   reportSigner:              ${reportSignerAddr}`);
  console.log(`   paused:                    ${isPaused}`);
  console.log(`   shutdown:                  ${isShutdown}`);
  console.log(`   reportCooldown:            ${reportCooldown}s  lastReportTimestamp: ${lastReportTimestamp}`);
  console.log(`   cooldown ready:            ${cooldownReady} (now=${now})`);

  if (isPaused)   throw new Error("Vault is paused — call setPaused(false) from owner first");
  if (isShutdown) throw new Error("Vault is shut down — cannot call report() on a shut-down vault");

  if (signerWallet.address.toLowerCase() !== reportSignerAddr.toLowerCase()) {
    throw new Error(
      `MISMATCH: ARB_NAV_SIGNER_PRIVATE_KEY derives ${signerWallet.address} ` +
      `but contract.reportSigner = ${reportSignerAddr}. ` +
      `Update ARB_NAV_SIGNER_PRIVATE_KEY or redeploy.`
    );
  }

  // Determine nonce and reportedAssets
  const nonce = process.env.ARB_SEED_NONCE
    ? BigInt(process.env.ARB_SEED_NONCE)
    : lastNonce + 1n;

  const reportedAssetsUsdc = process.env.ARB_REPORTED_ASSETS_USDC
    ? BigInt(Math.round(Number(process.env.ARB_REPORTED_ASSETS_USDC) * 1e6))
    : 0n;

  console.log(`\n📝 Seed report params:`);
  console.log(`   nonce:          ${nonce}`);
  console.log(`   reportedAssets: ${fmt6(reportedAssetsUsdc)} USDC`);

  if (nonce <= lastNonce) {
    throw new Error(`nonce ${nonce} must be > lastReportNonce ${lastNonce}. Set ARB_SEED_NONCE env var.`);
  }

  // ── Build & sign ─────────────────────────────────────────────────────
  console.log(`\n🔐 Signing report data…`);
  const { callData, signature, structHash } = await buildAndSignReport(
    signerWallet,
    vault,
    reportedAssetsUsdc,
    nonce,
    reportTypehash,
    domainSalt,
  );

  console.log(`   structHash: ${structHash}`);
  console.log(`   signature:  ${signature.slice(0, 20)}…`);

  const MAX_DEPOSITS = 100n;
  const MAX_REDEEMS  = 100n;

  // Alchemy strips revert data from eth_estimateGas — skip estimation,
  // send with a fixed gas limit. If the tx reverts on-chain the hash is
  // printed so the exact reason can be read on Basescan.
  const gasLimit = 600_000n;
  const feeData  = await provider.getFeeData();
  const gasPrice = feeData.gasPrice ?? 1_000_000n;
  console.log(`\n⛽ Using fixed gas limit: ${gasLimit}`);

  // ── Send transaction ──────────────────────────────────────────────────
  console.log(`\n📤 Sending report() transaction…`);
  const tx = await vault.report(callData, signature, MAX_DEPOSITS, MAX_REDEEMS, { gasLimit });
  console.log(`   tx hash: ${tx.hash}`);
  console.log(`   Waiting for confirmation…`);

  // Wait for the tx — ethers v6 throws on revert; Alchemy strips revert data.
  // On revert we replay via a public Base RPC (not Alchemy) which DOES return
  // full revert data, then decode the Solidity reason string.
  const PUBLIC_BASE_RPC = "https://mainnet.base.org";

  let receipt: Awaited<ReturnType<typeof tx.wait>>;
  try {
    receipt = await tx.wait(1);
  } catch (waitErr: unknown) {
    const errAny   = waitErr as Record<string, unknown>;
    const blockNum = (errAny.receipt as Record<string, unknown>)?.blockNumber as number | undefined;

    console.error(`\n💥 Tx reverted on-chain: ${tx.hash}`);
    console.log(`   Basescan: https://basescan.org/tx/${tx.hash}`);

    if (blockNum) {
      console.log(`   Replaying at block ${blockNum} via public RPC to decode revert reason…`);
      // Use public RPC — Alchemy strips revert data even from eth_call
      const pubProvider = new ethers.JsonRpcProvider(PUBLIC_BASE_RPC);
      const txCallData = tx.data;

      try {
        await pubProvider.call({ to: VAULT_ADDRESS, from: signerWallet.address, data: txCallData }, blockNum);
        console.log(`   Replay did not revert — may be block-sensitive`);
      } catch (replayErr: unknown) {
        const re     = replayErr as Record<string, unknown>;
        const reason = re.reason ?? re.shortMessage ?? re.message ?? String(replayErr);
        const raw    = re.data   ?? "(no data)";
        console.error(`   Revert reason: ${reason}`);
        console.error(`   Revert data:   ${raw}`);
      }

    } else {
      console.log(`   (no block number available — check Basescan link above)`);
    }
    throw new Error(`Transaction reverted — see revert reason above`);
  }

  if (!receipt || receipt.status === 0) {
    throw new Error(`Transaction status=0 (unexpected path): ${tx.hash}`);
  }

  console.log(`\n✅ Seed report confirmed!`);
  console.log(`   Block:   ${receipt.blockNumber}`);
  console.log(`   Gas used: ${receipt.gasUsed}`);

  // ── Verify final state ────────────────────────────────────────────────
  const [newPPS, newNonce, newBacking] = await Promise.all([
    vault.officialPPS(),
    vault.lastReportNonce(),
    vault.lastReportedBackingAssets(),
  ]);

  console.log(`\n📊 New vault state:`);
  console.log(`   officialPPS:               ${fmt6(newPPS)} USDC/share`);
  console.log(`   lastReportNonce:           ${newNonce}`);
  console.log(`   lastReportedBackingAssets: ${fmt6(newBacking)} USDC`);
  console.log(`\n🎉 Vault is seeded and ready for user deposits!`);
  console.log(`   Users can now requestDeposit() — shares will be minted on next report()\n`);
}

main().catch((err) => {
  console.error(`\n❌ Seed failed:`, err.message ?? err);
  process.exit(1);
});
