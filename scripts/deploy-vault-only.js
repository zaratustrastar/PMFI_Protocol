import hre from "hardhat";

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  
  console.log("=".repeat(60));
  console.log("BASE MAINNET - VAULT ONLY DEPLOYMENT");
  console.log("=".repeat(60));
  console.log("Deployer:", deployer.address);
  
  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log("ETH Balance:", hre.ethers.formatEther(balance), "ETH\n");

  // Base Mainnet USDC and already deployed strategy
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  const STRATEGY_ADDRESS = "0x6bD7138e87Ed9F4e15eA09A5551C90789A373DB2";
  
  console.log("Using USDC:", USDC_ADDRESS);
  console.log("Using Strategy:", STRATEGY_ADDRESS);

  // Deploy PredictFiSniperVaultV2
  console.log("\nDeploying PredictFiSniperVaultV2...");
  const PredictFiSniperVaultV2 = await hre.ethers.getContractFactory("PredictFiSniperVaultV2");
  
  const performanceFeeBps = 500;  // 5%
  const bufferBps = 2000;         // 20%
  const maxTotalDeposits = 10_000n * 1_000_000n; // 10,000 USDC (6 decimals)
  const walletDepositCap = 100n * 1_000_000n;    // 100 USDC per wallet

  const vault = await PredictFiSniperVaultV2.deploy(
    USDC_ADDRESS,
    STRATEGY_ADDRESS,
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
  const MockSniperStrategy = await hre.ethers.getContractFactory("MockSniperStrategy");
  const mockStrategy = MockSniperStrategy.attach(STRATEGY_ADDRESS);
  await mockStrategy.setVault(vaultAddress);
  console.log("✅ Vault address set in strategy");

  // Set keeper to deployer
  console.log("\nSetting keeper to deployer address...");
  await mockStrategy.setKeeper(deployer.address);
  console.log("✅ Keeper set to:", deployer.address);

  // Summary
  console.log("\n" + "=".repeat(60));
  console.log("BASE MAINNET DEPLOYMENT COMPLETE");
  console.log("=".repeat(60));
  console.log("USDC (Base):            ", USDC_ADDRESS);
  console.log("MockSniperStrategy:     ", STRATEGY_ADDRESS);
  console.log("PredictFiSniperVaultV2: ", vaultAddress);
  console.log("=".repeat(60));
  
  console.log("\n📋 UPDATE YOUR frontend/main.js WITH:");
  console.log(`const VAULT_ADDRESS = "${vaultAddress}";`);
  console.log(`const USDC_ADDRESS = "${USDC_ADDRESS}";`);
  console.log(`const BASE_MAINNET_RPC = "https://mainnet.base.org";`);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
