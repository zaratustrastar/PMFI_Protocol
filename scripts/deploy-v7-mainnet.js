import hre from "hardhat";
import fs from "fs";

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  
  console.log("=".repeat(60));
  console.log("BASE MAINNET - VAULT V7 DEPLOYMENT");
  console.log("=".repeat(60));
  console.log("Deployer:", deployer.address);
  
  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log("ETH Balance:", hre.ethers.formatEther(balance), "ETH\n");

  // Base Mainnet addresses
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  const NAV_SIGNER = deployer.address;
  const TAX_COLLECTOR = deployer.address;
  const POLYMARKET_WALLET = "0x9cbc5577e6f1B3a097d579C6A28D0cDbd247B6A5";
  
  // Deposit caps (in USDC with 6 decimals)
  const MAX_PER_WALLET = 100_000n * 1_000_000n;   // $100,000 per wallet
  const MAX_TOTAL = 100_000_000n * 1_000_000n;    // $100M total
  const MAX_LOSS_BPS = 1000;                       // 10% max loss allowed

  console.log("Configuration:");
  console.log("  USDC:", USDC_ADDRESS);
  console.log("  NAV Signer:", NAV_SIGNER);
  console.log("  Tax Collector:", TAX_COLLECTOR);
  console.log("  Polymarket Wallet:", POLYMARKET_WALLET);
  console.log("  Max per wallet:", MAX_PER_WALLET.toString());
  console.log("  Max total:", MAX_TOTAL.toString());
  console.log("  Max loss BPS:", MAX_LOSS_BPS);

  // Deploy PredictFiSniperVaultV7
  console.log("\nDeploying PredictFiSniperVaultV7...");
  const PredictFiSniperVaultV7 = await hre.ethers.getContractFactory("PredictFiSniperVaultV7");
  
  const vault = await PredictFiSniperVaultV7.deploy(
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
  console.log("✅ PredictFiSniperVaultV7 deployed to:", vaultAddress);

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
    polymarketBaseDeposit: "0xa76a91208FC7CB88420070AF978D12F440cab2F0"
  };
  
  fs.writeFileSync(
    "deployments/v7-mainnet.json",
    JSON.stringify(deploymentInfo, null, 2)
  );
  console.log("✅ Deployment info saved to deployments/v7-mainnet.json");

  // Summary
  console.log("\n" + "=".repeat(60));
  console.log("BASE MAINNET V7 DEPLOYMENT COMPLETE");
  console.log("=".repeat(60));
  console.log("PredictFiSniperVaultV7:", vaultAddress);
  console.log("USDC:", USDC_ADDRESS);
  console.log("NAV Signer:", NAV_SIGNER);
  console.log("=".repeat(60));
  
  console.log("\n📋 UPDATE YOUR FILES WITH:");
  console.log(`VAULT_ADDRESS = "${vaultAddress}"`);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
