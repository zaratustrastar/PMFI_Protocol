/**
 * Deploy PMFI pARB Vault (PredictFiArbVaultV1) to Base Mainnet
 *
 * Contract: PredictFiArbVaultV1
 * Token:    pARB
 * Venue:    Polymarket × Kalshi × Opinion Labs cross-venue arb vault
 *
 * Constructor parameters:
 *   _usdc              — Base Mainnet USDC (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)
 *   _navSigner         — Wallet that signs NAV data (ARB_NAV_SIGNER address)
 *   _taxCollector      — Receives withdrawal tax (deployer by default)
 *   _arbServicerWallet — pArb servicer wallet that receives forwarded deposits
 *   _maxTotal          — Maximum total USDC deposits allowed (e.g. $5000 for initial test)
 *
 * Required environment variables:
 *   METAMASK_PRIVATE_KEY      — Deployer private key (must have ETH on Base for gas)
 *   ARB_SERVICER_WALLET       — Dedicated pArb servicer wallet address
 *   ARB_NAV_SIGNER            — Address whose private key signs NAV data (ARB_NAV_SIGNER_PRIVATE_KEY on bot)
 *   BASE_RPC_URL              — (optional) Custom Base RPC, defaults to https://mainnet.base.org
 *   BASESCAN_API_KEY          — (optional) For auto-verification on BaseScan
 *   ARB_MAX_TOTAL_USDC        — (optional) Total deposit cap in USDC, defaults to 5000
 *
 * Usage:
 *   npx hardhat run scripts/deploy-arb-vault-v1-mainnet.cjs --network base
 */

const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
    console.log("=".repeat(60));
    console.log("🚀 Deploying PMFI pARB Vault to Base Mainnet");
    console.log("=".repeat(60));

    const [deployer] = await hre.ethers.getSigners();
    console.log(`\n📋 Deployer: ${deployer.address}`);

    const balance = await hre.ethers.provider.getBalance(deployer.address);
    console.log(`   Balance: ${hre.ethers.formatEther(balance)} ETH`);

    if (balance < hre.ethers.parseEther("0.001")) {
        throw new Error("Insufficient ETH for gas. Need at least 0.001 ETH on Base.");
    }

    // ── Base Mainnet USDC ──────────────────────────────────────────────────
    const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

    // ── NAV Signer: address whose private key (ARB_NAV_SIGNER_PRIVATE_KEY) signs NAV data ──
    const ARB_NAV_SIGNER = process.env.ARB_NAV_SIGNER || deployer.address;

    // ── Tax Collector: receives any withdrawal tax (deployer by default) ──
    const TAX_COLLECTOR = deployer.address;

    // ── pArb Servicer Wallet: receives forwarded USDC deposits, places arb trades ──
    const ARB_SERVICER_WALLET = process.env.ARB_SERVICER_WALLET;
    if (!ARB_SERVICER_WALLET) {
        throw new Error(
            "ARB_SERVICER_WALLET not set.\n" +
            "This is the dedicated pArb servicer wallet that receives forwarded deposits.\n" +
            "Set it to the wallet you use for arb trading on Polymarket/Kalshi."
        );
    }

    // ── Deposit cap: start small for initial testing ──────────────────────
    const maxTotalUsdc = process.env.ARB_MAX_TOTAL_USDC || "5000";
    const MAX_TOTAL = hre.ethers.parseUnits(maxTotalUsdc, 6);

    console.log(`\n📋 Deployment Parameters:`);
    console.log(`   USDC:              ${USDC_ADDRESS}`);
    console.log(`   NAV Signer:        ${ARB_NAV_SIGNER}`);
    console.log(`   Tax Collector:     ${TAX_COLLECTOR}`);
    console.log(`   Servicer Wallet:   ${ARB_SERVICER_WALLET}`);
    console.log(`   Max Total Deposit: $${maxTotalUsdc} USDC`);
    console.log(`   Domain Salt:       PredictFiArbVaultV1.v1`);
    console.log(`   Token:             pARB`);
    console.log(`   Min Deposit:       $10 USDC`);
    console.log(`   Withdrawal Expiry: 7 days`);

    // ── Deploy ────────────────────────────────────────────────────────────
    console.log(`\n⏳ Deploying contract...`);

    const VaultFactory = await hre.ethers.getContractFactory("PredictFiArbVaultV1");
    const vault = await VaultFactory.deploy(
        USDC_ADDRESS,
        ARB_NAV_SIGNER,
        TAX_COLLECTOR,
        ARB_SERVICER_WALLET,
        MAX_TOTAL
    );

    await vault.waitForDeployment();
    const vaultAddress = await vault.getAddress();

    console.log(`\n✅ PredictFiArbVaultV1 deployed!`);
    console.log(`   Address: ${vaultAddress}`);
    console.log(`   Block:   ${await hre.ethers.provider.getBlockNumber()}`);

    // ── Save deployment info ──────────────────────────────────────────────
    const deploymentInfo = {
        contract: "PredictFiArbVaultV1",
        token: "pARB",
        address: vaultAddress,
        network: "base-mainnet",
        chainId: 8453,
        deployer: deployer.address,
        navSigner: ARB_NAV_SIGNER,
        taxCollector: TAX_COLLECTOR,
        arbServicerWallet: ARB_SERVICER_WALLET,
        usdcAddress: USDC_ADDRESS,
        maxTotalUsdc: maxTotalUsdc,
        maxTotalRaw: MAX_TOTAL.toString(),
        minDepositUsdc: "10",
        domainSalt: "PredictFiArbVaultV1.v1",
        timestamp: new Date().toISOString(),
    };

    const deploymentsDir = path.join(__dirname, "..", "deployments");
    if (!fs.existsSync(deploymentsDir)) {
        fs.mkdirSync(deploymentsDir, { recursive: true });
    }
    const deploymentPath = path.join(deploymentsDir, "arb-vault-v1-mainnet.json");
    fs.writeFileSync(deploymentPath, JSON.stringify(deploymentInfo, null, 2));
    console.log(`\n📁 Deployment info saved to: ${deploymentPath}`);

    // ── Next steps ────────────────────────────────────────────────────────
    console.log(`\n🔧 Next Steps:`);
    console.log(`   1. Set ARB_VAULT_V1_ADDRESS=${vaultAddress} on VPS`);
    console.log(`   2. Set ARB_NAV_SIGNER_PRIVATE_KEY=<key for ${ARB_NAV_SIGNER}> on VPS`);
    console.log(`   3. Set ODDPOOL_API_KEY on VPS`);
    console.log(`   4. Restart bot: sudo systemctl restart psniper-bot`);
    console.log(`   5. Verify: https://basescan.org/address/${vaultAddress}`);
    console.log(`   6. Test deposit $10 USDC to verify NAV signing works`);

    // ── Auto-verify on BaseScan ────────────────────────────────────────────
    if (process.env.BASESCAN_API_KEY) {
        console.log(`\n⏳ Verifying on BaseScan...`);
        try {
            await hre.run("verify:verify", {
                address: vaultAddress,
                constructorArguments: [
                    USDC_ADDRESS,
                    ARB_NAV_SIGNER,
                    TAX_COLLECTOR,
                    ARB_SERVICER_WALLET,
                    MAX_TOTAL,
                ],
            });
            console.log(`✅ Verified on BaseScan`);
        } catch (e) {
            console.log(`⚠️ Verification failed (do it manually): ${e.message}`);
        }
    } else {
        console.log(`\n💡 To verify on BaseScan manually:`);
        console.log(`   npx hardhat verify --network base ${vaultAddress} \\`);
        console.log(`     "${USDC_ADDRESS}" \\`);
        console.log(`     "${ARB_NAV_SIGNER}" \\`);
        console.log(`     "${TAX_COLLECTOR}" \\`);
        console.log(`     "${ARB_SERVICER_WALLET}" \\`);
        console.log(`     "${MAX_TOTAL.toString()}"`);
    }

    console.log(`\n${"=".repeat(60)}`);
    console.log(`✅ Deployment Complete!`);
    console.log(`   pARB Vault: ${vaultAddress}`);
    console.log(`${"=".repeat(60)}`);
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error("❌ Deployment failed:", error);
        process.exit(1);
    });
