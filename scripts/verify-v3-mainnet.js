const hre = require("hardhat");

async function main() {
  // V3 Contract addresses - UPDATE THESE after deployment
  const VAULT_ADDRESS = process.env.V3_VAULT_ADDRESS || "0x0000000000000000000000000000000000000000";
  const STRATEGY_ADDRESS = process.env.V3_STRATEGY_ADDRESS || "0x0000000000000000000000000000000000000000";
  
  console.log("Verifying V3 contracts on BaseScan...\n");

  // Base Mainnet USDC address
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  const DEPLOYER_WALLET = "0x32320BF36D31f3B430D5BC91f572987F019E0Fa9";

  // V3 Parameters
  const PERFORMANCE_FEE_BPS = 500;
  const WITHDRAWAL_TAX_BPS = 100;
  const BUFFER_BPS = 1000;
  const MAX_TOTAL_DEPOSITS = hre.ethers.parseUnits("100000", 6);
  const WALLET_DEPOSIT_CAP = hre.ethers.parseUnits("100", 6);

  // Verify Strategy
  console.log("1. Verifying MockSniperStrategy...");
  try {
    await hre.run("verify:verify", {
      address: STRATEGY_ADDRESS,
      constructorArguments: [
        USDC_ADDRESS,
        hre.ethers.ZeroAddress
      ],
    });
    console.log("   Strategy verified!");
  } catch (e) {
    console.log("   Strategy verification:", e.message);
  }

  // Verify Vault V3
  console.log("\n2. Verifying PredictFiSniperVaultV3...");
  try {
    await hre.run("verify:verify", {
      address: VAULT_ADDRESS,
      constructorArguments: [
        USDC_ADDRESS,
        STRATEGY_ADDRESS,
        DEPLOYER_WALLET, // feeCollector
        DEPLOYER_WALLET, // taxCollector
        PERFORMANCE_FEE_BPS,
        WITHDRAWAL_TAX_BPS,
        BUFFER_BPS,
        MAX_TOTAL_DEPOSITS,
        WALLET_DEPOSIT_CAP
      ],
    });
    console.log("   Vault V3 verified!");
  } catch (e) {
    console.log("   Vault V3 verification:", e.message);
  }

  console.log("\n========================================");
  console.log("VERIFICATION COMPLETE");
  console.log("========================================");
  console.log("Strategy:", STRATEGY_ADDRESS);
  console.log("Vault V3:", VAULT_ADDRESS);
  console.log("========================================");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
