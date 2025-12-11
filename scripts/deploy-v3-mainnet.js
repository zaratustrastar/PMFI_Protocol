const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  
  console.log("Deploying V3 contracts to Base Mainnet with account:", deployer.address);
  console.log("Account balance:", hre.ethers.formatEther(await hre.ethers.provider.getBalance(deployer.address)), "ETH");

  // Base Mainnet USDC address
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  
  // Your deployer wallet (receives both performance fees and withdrawal tax)
  const DEPLOYER_WALLET = "0x32320BF36D31f3B430D5BC91f572987F019E0Fa9";

  // V3 Parameters:
  // - 5% performance fee (500 bps)
  // - 1% withdrawal tax (100 bps)
  // - 10% buffer (1000 bps)
  // - 100,000 USDC max total deposits (scalable)
  // - 100 USDC per-wallet cap
  const PERFORMANCE_FEE_BPS = 500;
  const WITHDRAWAL_TAX_BPS = 100;
  const BUFFER_BPS = 1000;
  const MAX_TOTAL_DEPOSITS = hre.ethers.parseUnits("100000", 6); // 100k USDC
  const WALLET_DEPOSIT_CAP = hre.ethers.parseUnits("100", 6); // 100 USDC

  // Deploy Strategy first
  console.log("\n1. Deploying MockSniperStrategy...");
  const Strategy = await hre.ethers.getContractFactory("MockSniperStrategy");
  const strategy = await Strategy.deploy(
    USDC_ADDRESS,
    hre.ethers.ZeroAddress // Vault address set later
  );
  await strategy.waitForDeployment();
  const strategyAddress = await strategy.getAddress();
  console.log("   Strategy deployed to:", strategyAddress);

  // Deploy V3 Vault
  console.log("\n2. Deploying PredictFiSniperVaultV3...");
  const VaultV3 = await hre.ethers.getContractFactory("PredictFiSniperVaultV3");
  const vaultV3 = await VaultV3.deploy(
    USDC_ADDRESS,
    strategyAddress,
    DEPLOYER_WALLET, // feeCollector (performance fees)
    DEPLOYER_WALLET, // taxCollector (1% withdrawal tax)
    PERFORMANCE_FEE_BPS,
    WITHDRAWAL_TAX_BPS,
    BUFFER_BPS,
    MAX_TOTAL_DEPOSITS,
    WALLET_DEPOSIT_CAP
  );
  await vaultV3.waitForDeployment();
  const vaultAddress = await vaultV3.getAddress();
  console.log("   Vault V3 deployed to:", vaultAddress);

  // Configure strategy
  console.log("\n3. Configuring strategy...");
  await strategy.setVault(vaultAddress);
  console.log("   Set vault on strategy");
  
  await strategy.setKeeper(DEPLOYER_WALLET);
  console.log("   Set keeper to deployer wallet");

  // Summary
  console.log("\n========================================");
  console.log("DEPLOYMENT COMPLETE - V3 MAINNET");
  console.log("========================================");
  console.log("Network: Base Mainnet");
  console.log("USDC:", USDC_ADDRESS);
  console.log("Strategy:", strategyAddress);
  console.log("Vault V3:", vaultAddress);
  console.log("Fee/Tax Collector:", DEPLOYER_WALLET);
  console.log("");
  console.log("Parameters:");
  console.log("  - Performance Fee: 5%");
  console.log("  - Withdrawal Tax: 1%");
  console.log("  - Buffer: 10%");
  console.log("  - Max Total Deposits: 100,000 USDC");
  console.log("  - Per-Wallet Cap: 100 USDC");
  console.log("========================================");
  
  // Return for verification
  return {
    strategy: strategyAddress,
    vault: vaultAddress
  };
}

main()
  .then((addresses) => {
    console.log("\nSave these addresses for verification!");
    process.exit(0);
  })
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
