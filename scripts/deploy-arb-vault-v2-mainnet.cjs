/**
 * Deploy PMFI pARB Vault V2 (PMFIArbVaultV2) to Base Mainnet
 *
 * Key changes from V1:
 *   - No live NAV required for user deposit/redeem (async, report-based)
 *   - requestDeposit / claimDeposit replaces instant deposit()
 *   - requestRedeem / claimRedeem replaces requestWithdraw / claim()
 *   - Idle USDC buffer (~10%) kept in vault; not forwarded wholesale
 *   - 20% performance fee on realized net profits only (high-water-mark)
 *   - report() is the ONLY official accounting checkpoint
 *   - tend() is permissionless maintenance
 *
 * Constructor parameters:
 *   _usdc               — Base Mainnet USDC (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)
 *   _reportSigner       — Address whose private key (ARB_NAV_SIGNER_PRIVATE_KEY) signs ReportDataV2
 *   _feeRecipient       — Receives 20% performance fee shares
 *   _arbServicerWallet  — Receives deployable capital via tend()
 *   _maxTotal           — Maximum total USDC cap
 *
 * Required environment variables:
 *   METAMASK_PRIVATE_KEY      — Deployer private key (must have ETH on Base for gas)
 *   ARB_REPORT_SIGNER         — Address of the report signer (ARB_NAV_SIGNER_PRIVATE_KEY on bot)
 *   ARB_FEE_RECIPIENT         — Address to receive 20% performance fee shares
 *   ARB_SERVICER_WALLET       — Dedicated pArb servicer wallet address
 *   BASE_RPC_URL              — (optional) Custom Base RPC, defaults to https://mainnet.base.org
 *   BASESCAN_API_KEY          — (optional) For auto-verification on BaseScan
 *   ARB_MAX_TOTAL_USDC        — (optional) Total deposit cap in USDC, defaults to 10000
 *
 * Usage:
 *   npx hardhat run scripts/deploy-arb-vault-v2-mainnet.cjs --network base
 *
 * After deployment, set ARB_VAULT_V2_ADDRESS in your bot .env.
 * Run scripts/seed-arb-vault-v2-report.cjs to send the initial report() (nonce=1, assets=0).
 */

const hre = require("hardhat");
const fs  = require("fs");
const path = require("path");

async function main() {
    console.log("=".repeat(60));
    console.log("🚀 Deploying PMFI pARB Vault V2 to Base Mainnet");
    console.log("=".repeat(60));

    const [deployer] = await hre.ethers.getSigners();
    console.log(`\n📋 Deployer: ${deployer.address}`);

    const balance = await hre.ethers.provider.getBalance(deployer.address);
    console.log(`   Balance: ${hre.ethers.formatEther(balance)} ETH`);

    if (balance < hre.ethers.parseEther("0.002")) {
        throw new Error("Insufficient ETH for gas. Need at least 0.002 ETH on Base.");
    }

    // ── Base Mainnet USDC ──────────────────────────────────────────────────
    const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

    // ── Report Signer: address whose private key signs ReportDataV2 ────────
    const ARB_REPORT_SIGNER = process.env.ARB_REPORT_SIGNER || process.env.ARB_NAV_SIGNER;
    if (!ARB_REPORT_SIGNER) {
        throw new Error(
            "ARB_REPORT_SIGNER not set.\n" +
            "This is the address whose private key (ARB_NAV_SIGNER_PRIVATE_KEY on the bot)\n" +
            "signs ReportDataV2 payloads for the report() function."
        );
    }

    // ── Fee Recipient: receives 20% performance fee shares ─────────────────
    const ARB_FEE_RECIPIENT = process.env.ARB_FEE_RECIPIENT || deployer.address;
    console.log(`   Fee Recipient: ${ARB_FEE_RECIPIENT} ${ARB_FEE_RECIPIENT === deployer.address ? "(deployer)" : ""}`);

    // ── Servicer Wallet: receives deployable capital via tend() ────────────
    const ARB_SERVICER_WALLET = process.env.ARB_SERVICER_WALLET;
    if (!ARB_SERVICER_WALLET) {
        throw new Error(
            "ARB_SERVICER_WALLET not set.\n" +
            "This wallet receives capital deployed by tend() and places arb trades."
        );
    }

    // ── Deposit cap ────────────────────────────────────────────────────────
    const maxTotalUsdc = process.env.ARB_MAX_TOTAL_USDC || "10000";
    const MAX_TOTAL    = hre.ethers.parseUnits(maxTotalUsdc, 6);

    console.log(`\n📋 Deployment Parameters:`);
    console.log(`   USDC:              ${USDC_ADDRESS}`);
    console.log(`   Report Signer:     ${ARB_REPORT_SIGNER}`);
    console.log(`   Fee Recipient:     ${ARB_FEE_RECIPIENT}`);
    console.log(`   Servicer Wallet:   ${ARB_SERVICER_WALLET}`);
    console.log(`   Max Total Deposit: $${maxTotalUsdc} USDC`);
    console.log(`   Domain Salt:       PMFIArbVaultV2.v1`);
    console.log(`   Token:             pARB`);
    console.log(`   Min Deposit:       $10 USDC`);
    console.log(`   Performance Fee:   20% on realized profits`);
    console.log(`   Target Idle:       10% of backing assets`);
    console.log(`   Tend Cooldown:     60s`);
    console.log(`   Report Cooldown:   3600s (1 hour)`);

    // ── Deploy ─────────────────────────────────────────────────────────────
    console.log(`\n⏳ Deploying PMFIArbVaultV2...`);

    const Factory = await hre.ethers.getContractFactory("PMFIArbVaultV2");
    const vault   = await Factory.deploy(
        USDC_ADDRESS,
        ARB_REPORT_SIGNER,
        ARB_FEE_RECIPIENT,
        ARB_SERVICER_WALLET,
        MAX_TOTAL
    );

    await vault.waitForDeployment();
    const vaultAddress = await vault.getAddress();
    console.log(`\n✅ PMFIArbVaultV2 deployed at: ${vaultAddress}`);

    // ── Verify constructor params ──────────────────────────────────────────
    const [
        officialPPS,
        totalSupply,
        idleBalance,
        lastReportedBacking,
        lossCarryforward,
        pendingDepositAssets,
        claimableRedeemAssets,
        pendingRedeemShares,
        lastReportTs,
        lastReportNonce,
        paused,
        shutdown_,
    ] = await vault.getVaultState();

    console.log(`\n📊 Post-deployment state:`);
    console.log(`   officialPPS:      ${hre.ethers.formatUnits(officialPPS, 6)} USDC/share`);
    console.log(`   totalSupply:      ${hre.ethers.formatEther(totalSupply)} pARB`);
    console.log(`   idleBalance:      ${hre.ethers.formatUnits(idleBalance, 6)} USDC`);
    console.log(`   lastReportNonce:  ${lastReportNonce}`);
    console.log(`   paused:           ${paused}`);
    console.log(`   shutdown:         ${shutdown_}`);

    // ── Verify constants ───────────────────────────────────────────────────
    const domainSalt = await vault.DOMAIN_SALT();
    const perfFeeBps = await vault.PERF_FEE_BPS();
    console.log(`\n🔐 Constants:`);
    console.log(`   DOMAIN_SALT:      ${domainSalt}`);
    console.log(`   PERF_FEE_BPS:     ${perfFeeBps} (${Number(perfFeeBps)/100}%)`);

    // ── Save deployment info ───────────────────────────────────────────────
    const deploymentInfo = {
        contract:        "PMFIArbVaultV2",
        network:         "base-mainnet",
        address:         vaultAddress,
        deployer:        deployer.address,
        reportSigner:    ARB_REPORT_SIGNER,
        feeRecipient:    ARB_FEE_RECIPIENT,
        arbServicerWallet: ARB_SERVICER_WALLET,
        maxTotalDeposits: maxTotalUsdc,
        deployedAt:      new Date().toISOString(),
        chainId:         8453,
        domainSalt:      "PMFIArbVaultV2.v1",
        v1Contract:      process.env.ARB_VAULT_ADDRESS || "(set in env)",
        notes:           "V2: async vault, request/claim flows, 20% perf fee, 10% idle buffer",
    };

    const deployPath = path.join(__dirname, "../deployments");
    if (!fs.existsSync(deployPath)) fs.mkdirSync(deployPath, { recursive: true });
    const deployFile = path.join(deployPath, "arb-vault-v2-mainnet.json");
    fs.writeFileSync(deployFile, JSON.stringify(deploymentInfo, null, 2));
    console.log(`\n📄 Deployment info saved to: ${deployFile}`);

    // ── Next steps ─────────────────────────────────────────────────────────
    console.log(`\n${"=".repeat(60)}`);
    console.log(`📋 NEXT STEPS:`);
    console.log(`${"=".repeat(60)}`);
    console.log(`\n1. Add to bot .env:`);
    console.log(`   ARB_VAULT_V2_ADDRESS=${vaultAddress}`);
    console.log(`\n2. Verify on BaseScan:`);
    console.log(`   npx hardhat verify --network base ${vaultAddress} \\`);
    console.log(`     ${USDC_ADDRESS} \\`);
    console.log(`     ${ARB_REPORT_SIGNER} \\`);
    console.log(`     ${ARB_FEE_RECIPIENT} \\`);
    console.log(`     ${ARB_SERVICER_WALLET} \\`);
    console.log(`     ${MAX_TOTAL.toString()}`);
    console.log(`\n3. Send initial seed report (nonce=1, reportedAssets=0):`);
    console.log(`   ARB_VAULT_V2_ADDRESS=${vaultAddress} npx tsx scripts/seed-arb-vault-v2-report.ts`);
    console.log(`\n4. Update frontend config:`);
    console.log(`   ARB_VAULT_V2_ADDRESS = "${vaultAddress}"`);
    console.log(`\n5. Keep V1 running until all V1 users have redeemed.`);
    console.log(`   V1 contract: ${process.env.ARB_VAULT_ADDRESS || "(ARB_VAULT_ADDRESS env var)"}`);
    console.log(`\n6. Once V1 is drained, you can pause it (V1 setPaused(true)).`);
    console.log(`${"=".repeat(60)}`);
}

main()
    .then(() => process.exit(0))
    .catch((err) => {
        console.error("❌ Deployment failed:", err);
        process.exit(1);
    });
