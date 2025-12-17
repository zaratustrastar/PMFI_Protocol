/**
 * Deploy PredictFiSniperVaultV7 to Base Mainnet
 * 
 * V7 Features:
 * - 100% forwarding to Polymarket (no buffer split)
 * - 3-state asset tracking (in-flight, pending, credited)
 * - Conservation bounds (replace 5% NAV change limit)
 * - Extended NavData with full asset breakdown
 * 
 * Usage:
 *   npx hardhat run scripts/deploy-v7-mainnet.cjs --network base
 */

const hre = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
    console.log("=" .repeat(60));
    console.log("🚀 Deploying PredictFiSniperVaultV7 to Base Mainnet");
    console.log("=" .repeat(60));

    const [deployer] = await hre.ethers.getSigners();
    console.log(`\n📋 Deployer: ${deployer.address}`);
    
    const balance = await hre.ethers.provider.getBalance(deployer.address);
    console.log(`   Balance: ${hre.ethers.formatEther(balance)} ETH`);

    // Base Mainnet USDC
    const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
    
    // Oracle signer (same as deployer for simplicity)
    const NAV_SIGNER = deployer.address;
    
    // Tax collector (deployer)
    const TAX_COLLECTOR = deployer.address;
    
    // Polymarket trading wallet
    const POLYMARKET_WALLET = process.env.POLYMARKET_PROXY_ADDRESS;
    if (!POLYMARKET_WALLET) {
        throw new Error("POLYMARKET_PROXY_ADDRESS not set in .env");
    }
    
    // Caps
    const MAX_PER_WALLET = hre.ethers.parseUnits("100", 6);     // 100 USDC per wallet
    const MAX_TOTAL = hre.ethers.parseUnits("100000", 6);       // 100,000 USDC total
    
    // Max loss allowed (10% = 1000 bps)
    const MAX_LOSS_BPS = 1000;

    console.log(`\n📋 Deployment Parameters:`);
    console.log(`   USDC: ${USDC_ADDRESS}`);
    console.log(`   NAV Signer: ${NAV_SIGNER}`);
    console.log(`   Tax Collector: ${TAX_COLLECTOR}`);
    console.log(`   Polymarket Wallet: ${POLYMARKET_WALLET}`);
    console.log(`   Max Per Wallet: $${hre.ethers.formatUnits(MAX_PER_WALLET, 6)}`);
    console.log(`   Max Total: $${hre.ethers.formatUnits(MAX_TOTAL, 6)}`);
    console.log(`   Max Loss: ${MAX_LOSS_BPS / 100}%`);

    // Deploy
    console.log(`\n⏳ Deploying contract...`);
    
    const VaultFactory = await hre.ethers.getContractFactory("PredictFiSniperVaultV7");
    const vault = await VaultFactory.deploy(
        USDC_ADDRESS,
        NAV_SIGNER,
        TAX_COLLECTOR,
        POLYMARKET_WALLET,
        MAX_PER_WALLET,
        MAX_TOTAL,
        MAX_LOSS_BPS
    );

    await vault.waitForDeployment();
    const vaultAddress = await vault.getAddress();

    console.log(`\n✅ PredictFiSniperVaultV7 deployed!`);
    console.log(`   Address: ${vaultAddress}`);
    console.log(`   Block: ${await hre.ethers.provider.getBlockNumber()}`);

    // Save deployment info
    const deploymentInfo = {
        contract: "PredictFiSniperVaultV7",
        address: vaultAddress,
        network: "base-mainnet",
        chainId: 8453,
        deployer: deployer.address,
        navSigner: NAV_SIGNER,
        taxCollector: TAX_COLLECTOR,
        polymarketWallet: POLYMARKET_WALLET,
        usdcAddress: USDC_ADDRESS,
        maxPerWallet: MAX_PER_WALLET.toString(),
        maxTotal: MAX_TOTAL.toString(),
        maxLossBps: MAX_LOSS_BPS,
        timestamp: new Date().toISOString(),
        polymarketBaseDeposit: "0xa76a91208FC7CB88420070AF978D12F440cab2F0",
    };

    const deploymentsDir = path.join(__dirname, "..", "deployments");
    if (!fs.existsSync(deploymentsDir)) {
        fs.mkdirSync(deploymentsDir, { recursive: true });
    }

    const deploymentPath = path.join(deploymentsDir, "v7-mainnet.json");
    fs.writeFileSync(deploymentPath, JSON.stringify(deploymentInfo, null, 2));
    console.log(`\n📁 Deployment info saved to: ${deploymentPath}`);

    // Copy ABI to frontend
    const artifactPath = path.join(__dirname, "..", "artifacts", "contracts", "PredictFiSniperVaultV7.sol", "PredictFiSniperVaultV7.json");
    const frontendAbiPath = path.join(__dirname, "..", "frontend", "abis", "vault.json");
    
    if (fs.existsSync(artifactPath)) {
        const artifact = JSON.parse(fs.readFileSync(artifactPath, 'utf8'));
        fs.writeFileSync(frontendAbiPath, JSON.stringify(artifact.abi, null, 2));
        console.log(`📁 ABI copied to: ${frontendAbiPath}`);
    }

    console.log(`\n🔧 Next Steps:`);
    console.log(`   1. Update VAULT_V7_ADDRESS in .env: ${vaultAddress}`);
    console.log(`   2. Update frontend/main.js VAULT_ADDRESS`);
    console.log(`   3. Deploy bot_v7.py to VPS`);
    console.log(`   4. Start bot: python bot/bot_v7.py`);

    console.log(`\n📊 Verify on BaseScan:`);
    console.log(`   https://basescan.org/address/${vaultAddress}`);

    // Verify contract (optional)
    if (process.env.BASESCAN_API_KEY) {
        console.log(`\n⏳ Verifying contract on BaseScan...`);
        try {
            await hre.run("verify:verify", {
                address: vaultAddress,
                constructorArguments: [
                    USDC_ADDRESS,
                    NAV_SIGNER,
                    TAX_COLLECTOR,
                    POLYMARKET_WALLET,
                    MAX_PER_WALLET,
                    MAX_TOTAL,
                    MAX_LOSS_BPS,
                ],
            });
            console.log(`✅ Contract verified!`);
        } catch (error) {
            console.log(`⚠️ Verification failed: ${error.message}`);
        }
    }

    console.log(`\n${"=".repeat(60)}`);
    console.log(`✅ Deployment Complete!`);
    console.log(`${"=".repeat(60)}`);
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });
