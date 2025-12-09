import hre from "hardhat";

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  
  console.log("Deploying PredictFiSniperVaultV2 contracts with account:", deployer.address);
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

  // Deploy PredictFiSniperVaultV2
  console.log("\nDeploying PredictFiSniperVaultV2...");
  const PredictFiSniperVaultV2 = await hre.ethers.getContractFactory("PredictFiSniperVaultV2");
  
  const performanceFeeBps = 500;  // 5%
  const bufferBps = 2000;         // 20%
  const maxTotalDeposits = 10_000n * 1_000_000n; // 10,000 USDC (6 decimals)
  const walletDepositCap = 100n * 1_000_000n;    // 100 USDC per wallet (6 decimals)

  const vault = await PredictFiSniperVaultV2.deploy(
    testUSDCAddress,
    mockStrategyAddress,
    deployer.address,  // feeCollector
    performanceFeeBps,
    bufferBps,
    maxTotalDeposits,
    walletDepositCap
  );
  await vault.waitForDeployment();
  const vaultAddress = await vault.getAddress();
  console.log("✅ PredictFiSniperVaultV2 deployed to:", vaultAddress);

  // Set vault address in strategy
  console.log("\nSetting vault address in MockSniperStrategy...");
  await mockStrategy.setVault(vaultAddress);
  console.log("✅ Vault address set in strategy");

  // Summary
  console.log("\n" + "=".repeat(60));
  console.log("V2 DEPLOYMENT SUMMARY");
  console.log("=".repeat(60));
  console.log("TestUSDC:                ", testUSDCAddress);
  console.log("MockSniperStrategy:      ", mockStrategyAddress);
  console.log("PredictFiSniperVaultV2:  ", vaultAddress);
  console.log("Fee Collector:           ", deployer.address);
  console.log("Performance Fee:         ", performanceFeeBps / 100, "%");
  console.log("Buffer:                  ", bufferBps / 100, "%");
  console.log("Max Total Deposits:      ", "10,000 USDC");
  console.log("Wallet Deposit Cap:      ", "100 USDC");
  console.log("=".repeat(60));

  // Verification commands
  console.log("\nVERIFICATION COMMANDS:");
  console.log("=".repeat(60));
  console.log(`npx hardhat verify --network baseSepolia ${testUSDCAddress}`);
  console.log(`npx hardhat verify --network baseSepolia ${mockStrategyAddress} "${testUSDCAddress}"`);
  console.log(`npx hardhat verify --network baseSepolia ${vaultAddress} "${testUSDCAddress}" "${mockStrategyAddress}" "${deployer.address}" ${performanceFeeBps} ${bufferBps} ${maxTotalDeposits} ${walletDepositCap}`);
  console.log("=".repeat(60));
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
