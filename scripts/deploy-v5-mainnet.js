/**
 * Deploy PredictFiSniperVaultV5 to Base Mainnet
 * 
 * V5 Features (Refined):
 * - Permissionless investIdle() for rebalancing
 * - Async withdrawal queue (requestWithdraw → claim)
 * - Claim-time NAV (simpler, no stored NAV)
 * - Signed NAV oracle (zero gas for protocol)
 * - 1% withdrawal tax
 * - Claim allowed even when paused
 * 
 * Usage:
 *   npx hardhat run scripts/deploy-v5-mainnet.js --network baseMainnet
 */

const hre = require("hardhat");

async function main() {
  console.log("========================================");
  console.log("  PSNIPER V5 DEPLOYMENT - BASE MAINNET");
  console.log("========================================\n");

  const [deployer] = await hre.ethers.getSigners();
  console.log("Deployer:", deployer.address);
  
  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log("Balance:", hre.ethers.formatEther(balance), "ETH\n");

  // Configuration
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"; // Base USDC
  const NAV_SIGNER = deployer.address; // Oracle key (same as deployer for MVP)
  const TAX_COLLECTOR = deployer.address; // 1% tax goes here
  const POLYMARKET_WALLET = process.env.POLYMARKET_PROXY_ADDRESS;
  
  if (!POLYMARKET_WALLET) {
    console.error("❌ Missing POLYMARKET_PROXY_ADDRESS environment variable");
    process.exit(1);
  }
  
  const INITIAL_NAV = hre.ethers.parseUnits("1", 18); // 1.0 NAV
  const MAX_PER_WALLET = hre.ethers.parseUnits("100", 6); // 100 USDC per wallet
  const MAX_TOTAL = hre.ethers.parseUnits("10000", 6); // 10,000 USDC total cap
  const TARGET_BUFFER = hre.ethers.parseUnits("1000", 6); // 1,000 USDC target buffer (~10%)
  const MAX_REBALANCE_PER_CALL = hre.ethers.parseUnits("1000", 6); // 1,000 USDC max per investIdle() call

  console.log("Configuration:");
  console.log("  USDC:", USDC_ADDRESS);
  console.log("  NAV Signer:", NAV_SIGNER);
  console.log("  Tax Collector:", TAX_COLLECTOR);
  console.log("  Polymarket Wallet:", POLYMARKET_WALLET);
  console.log("  Initial NAV:", "1.0");
  console.log("  Max per wallet:", "100 USDC");
  console.log("  Max total:", "10,000 USDC");
  console.log("  Target buffer:", "1,000 USDC");
  console.log("  Max rebalance per call:", "1,000 USDC");
  console.log("");

  // Deploy
  console.log("Deploying PredictFiSniperVaultV5...");
  
  const VaultV5 = await hre.ethers.getContractFactory("PredictFiSniperVaultV5");
  const vault = await VaultV5.deploy(
    USDC_ADDRESS,
    NAV_SIGNER,
    TAX_COLLECTOR,
    POLYMARKET_WALLET,
    INITIAL_NAV,
    MAX_PER_WALLET,
    MAX_TOTAL,
    TARGET_BUFFER,
    MAX_REBALANCE_PER_CALL
  );

  await vault.waitForDeployment();
  const vaultAddress = await vault.getAddress();

  console.log("\n✅ V5 Vault deployed to:", vaultAddress);

  // Verify state
  console.log("\nVerifying deployment...");
  const lastNav = await vault.lastNav();
  const maxPerWallet = await vault.maxDepositPerWallet();
  const maxTotal = await vault.maxTotalDeposits();
  const targetBuf = await vault.targetBuffer();
  const pmWallet = await vault.polymarketWallet();
  
  console.log("  lastNav:", hre.ethers.formatUnits(lastNav, 18));
  console.log("  maxDepositPerWallet:", hre.ethers.formatUnits(maxPerWallet, 6), "USDC");
  console.log("  maxTotalDeposits:", hre.ethers.formatUnits(maxTotal, 6), "USDC");
  console.log("  targetBuffer:", hre.ethers.formatUnits(targetBuf, 6), "USDC");
  console.log("  polymarketWallet:", pmWallet);

  console.log("\n========================================");
  console.log("  DEPLOYMENT COMPLETE");
  console.log("========================================");
  console.log("\nAdd to .env:");
  console.log(`VAULT_V5_ADDRESS=${vaultAddress}`);
  console.log("\nNext steps:");
  console.log("1. Start bot: python bot/bot_v5.py");
  console.log("2. Update frontend to use V5 ABI and address");
  console.log("3. Test deposit/withdrawal flow");
  console.log("4. Anyone can call investIdle() to rebalance");
  console.log("5. Verify on Basescan:");
  console.log(`   npx hardhat verify --network baseMainnet ${vaultAddress} "${USDC_ADDRESS}" "${NAV_SIGNER}" "${TAX_COLLECTOR}" "${POLYMARKET_WALLET}" "${INITIAL_NAV}" "${MAX_PER_WALLET}" "${MAX_TOTAL}" "${TARGET_BUFFER}" "${MAX_REBALANCE_PER_CALL}"`);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
