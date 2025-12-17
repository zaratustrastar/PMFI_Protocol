const hre = require("hardhat");

async function main() {
  console.log("Deploying PredictFiSniperVaultV6 to Base Mainnet...\n");

  const [deployer] = await hre.ethers.getSigners();
  console.log("Deployer address:", deployer.address);
  
  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log("Deployer ETH balance:", hre.ethers.formatEther(balance), "ETH\n");

  // Base Mainnet USDC
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  
  // NAV signer = deployer (signs NAV data off-chain)
  const NAV_SIGNER = deployer.address;
  
  // Tax collector = deployer (receives 1% withdrawal tax)
  const TAX_COLLECTOR = deployer.address;
  
  // Polymarket trading wallet (your PM account, for tracking/refills)
  // This should be the wallet that holds PM positions
  const POLYMARKET_WALLET = process.env.POLYMARKET_PROXY_ADDRESS || deployer.address;
  
  // Initial NAV = $1.00 (1e6 precision - matches USDC decimals for correct share math)
  // Contract formula: sharesToMint = usdcAmount * 1e18 / nav
  // With nav=1e6: 4 USDC (4e6) * 1e18 / 1e6 = 4e18 shares = 4.0 pSNIPER
  const INITIAL_NAV = hre.ethers.parseUnits("1", 6);
  
  // Caps
  const MAX_PER_WALLET = hre.ethers.parseUnits("100", 6);   // 100 USDC per wallet
  const MAX_TOTAL = hre.ethers.parseUnits("10000", 6);      // 10,000 USDC total
  
  // Buffer percentage: 1000 = 10% (10% stays in vault, 90% goes to Polymarket)
  // Min 5% (500), Max 50% (5000)
  const BUFFER_PERCENT_BPS = 1000;

  console.log("Deployment Parameters:");
  console.log("  USDC:", USDC_ADDRESS);
  console.log("  NAV Signer:", NAV_SIGNER);
  console.log("  Tax Collector:", TAX_COLLECTOR);
  console.log("  Polymarket Wallet:", POLYMARKET_WALLET);
  console.log("  Initial NAV: $1.00");
  console.log("  Max per Wallet:", hre.ethers.formatUnits(MAX_PER_WALLET, 6), "USDC");
  console.log("  Max Total:", hre.ethers.formatUnits(MAX_TOTAL, 6), "USDC");
  console.log("  Buffer Percent:", BUFFER_PERCENT_BPS / 100, "% (min 5%, max 50%)");
  console.log("  Polymarket Base Deposit: 0xa76a91208FC7CB88420070AF978D12F440cab2F0 (hardcoded)\n");

  const VaultV6 = await hre.ethers.getContractFactory("PredictFiSniperVaultV6");
  
  const vault = await VaultV6.deploy(
    USDC_ADDRESS,
    NAV_SIGNER,
    TAX_COLLECTOR,
    POLYMARKET_WALLET,
    INITIAL_NAV,
    MAX_PER_WALLET,
    MAX_TOTAL,
    BUFFER_PERCENT_BPS
  );

  await vault.waitForDeployment();
  const vaultAddress = await vault.getAddress();

  console.log("\n✅ PredictFiSniperVaultV6 deployed!");
  console.log("   Address:", vaultAddress);
  console.log("\n📋 Next Steps:");
  console.log("   1. Update VAULT_V6_ADDRESS in .env");
  console.log("   2. Update frontend/main-v6.js with new address");
  console.log("   3. Update bot/bot_v6.py with new address");
  console.log("   4. Verify on BaseScan:");
  console.log(`      npx hardhat verify --network base ${vaultAddress} \\`);
  console.log(`        ${USDC_ADDRESS} \\`);
  console.log(`        ${NAV_SIGNER} \\`);
  console.log(`        ${TAX_COLLECTOR} \\`);
  console.log(`        ${POLYMARKET_WALLET} \\`);
  console.log(`        ${INITIAL_NAV} \\`);
  console.log(`        ${MAX_PER_WALLET} \\`);
  console.log(`        ${MAX_TOTAL} \\`);
  console.log(`        ${BUFFER_PERCENT_BPS}`);

  // Save deployment info
  const fs = require("fs");
  const deploymentInfo = {
    network: "base",
    contract: "PredictFiSniperVaultV6",
    address: vaultAddress,
    deployer: deployer.address,
    timestamp: new Date().toISOString(),
    parameters: {
      usdc: USDC_ADDRESS,
      navSigner: NAV_SIGNER,
      taxCollector: TAX_COLLECTOR,
      polymarketWallet: POLYMARKET_WALLET,
      initialNav: INITIAL_NAV.toString(),
      maxPerWallet: MAX_PER_WALLET.toString(),
      maxTotal: MAX_TOTAL.toString(),
      bufferPercentBps: BUFFER_PERCENT_BPS,
      polymarketBaseDeposit: "0xa76a91208FC7CB88420070AF978D12F440cab2F0"
    }
  };
  
  fs.writeFileSync(
    "deployments/v6-mainnet.json",
    JSON.stringify(deploymentInfo, null, 2)
  );
  console.log("\n📁 Deployment info saved to deployments/v6-mainnet.json");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
