/**
 * Deploy PredictFiSniperVaultV4 to Base Mainnet
 * 
 * V4 uses signed NAV oracle pattern:
 * - No on-chain NAV updates needed (zero gas for oracle)
 * - Users include signed NAV data in deposit/withdraw transactions
 * - 1% withdrawal tax to deployer
 * - Per-wallet and total deposit caps
 * 
 * Usage:
 *   npx hardhat run scripts/deploy-v4-mainnet.js --network base
 */

const { ethers } = require("hardhat");

async function main() {
  console.log("\n" + "=".repeat(60));
  console.log("🚀 Deploying PredictFiSniperVaultV4 to Base Mainnet");
  console.log("=".repeat(60) + "\n");

  const [deployer] = await ethers.getSigners();
  console.log("📍 Deployer:", deployer.address);
  
  const balance = await ethers.provider.getBalance(deployer.address);
  console.log("💰 Balance:", ethers.formatEther(balance), "ETH\n");

  // Base Mainnet USDC
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  
  // Oracle signer (same as deployer - this address signs NAV data)
  const NAV_SIGNER = deployer.address;
  
  // Tax collector (receives 1% withdrawal tax)
  const TAX_COLLECTOR = deployer.address;
  
  // Initial NAV: 1.0 USDC per pSNIPE (in 1e18 format)
  const INITIAL_NAV = ethers.parseEther("1.0");
  
  // Caps
  const MAX_PER_WALLET = 100n * 1000000n;  // 100 USDC (6 decimals)
  const MAX_TOTAL = 10000n * 1000000n;     // 10,000 USDC (6 decimals) - can increase later

  console.log("📋 Configuration:");
  console.log("   USDC:", USDC_ADDRESS);
  console.log("   NAV Signer:", NAV_SIGNER);
  console.log("   Tax Collector:", TAX_COLLECTOR);
  console.log("   Initial NAV:", ethers.formatEther(INITIAL_NAV), "(1 USDC per pSNIPE)");
  console.log("   Max per wallet:", MAX_PER_WALLET / 1000000n, "USDC");
  console.log("   Max total:", MAX_TOTAL / 1000000n, "USDC\n");

  // Deploy Vault V4
  console.log("📦 Deploying PredictFiSniperVaultV4...");
  const VaultV4 = await ethers.getContractFactory("PredictFiSniperVaultV4");
  const vault = await VaultV4.deploy(
    USDC_ADDRESS,
    NAV_SIGNER,
    TAX_COLLECTOR,
    INITIAL_NAV,
    MAX_PER_WALLET,
    MAX_TOTAL
  );
  await vault.waitForDeployment();
  const vaultAddress = await vault.getAddress();
  console.log("✅ Vault V4 deployed:", vaultAddress);

  // Verify deployment
  console.log("\n📊 Verifying deployment...");
  const navSigner = await vault.navSigner();
  const taxCollector = await vault.taxCollector();
  const lastNav = await vault.lastNav();
  const maxPerWallet = await vault.maxDepositPerWallet();
  const maxTotal = await vault.maxTotalDeposits();

  console.log("   navSigner:", navSigner);
  console.log("   taxCollector:", taxCollector);
  console.log("   lastNav:", ethers.formatEther(lastNav));
  console.log("   maxDepositPerWallet:", maxPerWallet / 1000000n, "USDC");
  console.log("   maxTotalDeposits:", maxTotal / 1000000n, "USDC");

  console.log("\n" + "=".repeat(60));
  console.log("✅ DEPLOYMENT COMPLETE");
  console.log("=".repeat(60));
  console.log("\n📝 Add these to your .env file:\n");
  console.log(`VAULT_V4_ADDRESS=${vaultAddress}`);
  console.log(`ORACLE_PRIVATE_KEY=<your deployer private key>`);
  console.log("\n📝 Update frontend/main.js:");
  console.log(`const VAULT_ADDRESS = "${vaultAddress}";`);
  console.log("\n⚠️  IMPORTANT:");
  console.log("1. The deployer address is both the NAV signer AND tax collector");
  console.log("2. Start bot_v4.py to sign NAV data for deposits/withdrawals");
  console.log("3. Bot spends ZERO gas - users pay gas for their transactions");
  console.log("4. To increase caps: vault.setCaps(newPerWallet, newTotal)");
  console.log("\n" + "=".repeat(60) + "\n");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
