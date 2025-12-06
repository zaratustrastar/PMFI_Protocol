const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  
  console.log("Deploying contracts with account:", deployer.address);
  console.log("Account balance:", (await hre.ethers.provider.getBalance(deployer.address)).toString());
  console.log("");

  // Deploy TestUSDC
  console.log("Deploying TestUSDC...");
  const TestUSDC = await hre.ethers.getContractFactory("TestUSDC");
  const testUSDC = await TestUSDC.deploy();
  await testUSDC.waitForDeployment();
  const testUSDCAddress = await testUSDC.getAddress();
  console.log("✅ TestUSDC deployed to:", testUSDCAddress);

  // Deploy MockSniperStrategy
  console.log("\nDeploying MockSniperStrategy...");
  const MockSniperStrategy = await hre.ethers.getContractFactory("MockSniperStrategy");
  const mockStrategy = await MockSniperStrategy.deploy(testUSDCAddress);
  await mockStrategy.waitForDeployment();
  const mockStrategyAddress = await mockStrategy.getAddress();
  console.log("✅ MockSniperStrategy deployed to:", mockStrategyAddress);

  // Deploy PredictFiSniperVault
  console.log("\nDeploying PredictFiSniperVault...");
  const PredictFiSniperVault = await hre.ethers.getContractFactory("PredictFiSniperVault");
  
  const performanceFeeBps = 500;  // 5%
  const bufferBps = 2000;         // 20%
  const maxTotalDeposits = 10_000n * 1_000_000n; // 10,000 USDC (6 decimals)

  const vault = await PredictFiSniperVault.deploy(
    testUSDCAddress,
    mockStrategyAddress,
    deployer.address,  // feeCollector
    performanceFeeBps,
    bufferBps,
    maxTotalDeposits
  );
  await vault.waitForDeployment();
  const vaultAddress = await vault.getAddress();
  console.log("✅ PredictFiSniperVault deployed to:", vaultAddress);

  // Set vault address in strategy
  console.log("\nSetting vault address in MockSniperStrategy...");
  await mockStrategy.setVault(vaultAddress);
  console.log("✅ Vault address set in strategy");

  // Summary
  console.log("\n" + "=".repeat(60));
  console.log("DEPLOYMENT SUMMARY");
  console.log("=".repeat(60));
  console.log("TestUSDC:            ", testUSDCAddress);
  console.log("MockSniperStrategy:  ", mockStrategyAddress);
  console.log("PredictFiSniperVault:", vaultAddress);
  console.log("Fee Collector:       ", deployer.address);
  console.log("Performance Fee:     ", performanceFeeBps / 100, "%");
  console.log("Buffer:              ", bufferBps / 100, "%");
  console.log("Max Deposits:        ", "10,000 USDC");
  console.log("=".repeat(60));
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
