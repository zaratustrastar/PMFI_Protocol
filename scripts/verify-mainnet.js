import hre from "hardhat";

async function main() {
  const VAULT_ADDRESS = "0x5E9Cfd99Cb55cB7981E8C7E8ceC73dC68e60e6C9";
  const STRATEGY_ADDRESS = "0x6bD7138e87Ed9F4e15eA09A5551C90789A373DB2";
  const USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  const FEE_COLLECTOR = "0x32320BF36D31f3B430D5BC91f572987F019E0Fa9";
  
  console.log("=".repeat(60));
  console.log("VERIFYING CONTRACTS ON BASESCAN");
  console.log("=".repeat(60));

  console.log("\n1. Verifying MockSniperStrategy...");
  try {
    await hre.run("verify:verify", {
      address: STRATEGY_ADDRESS,
      constructorArguments: [
        USDC_ADDRESS,
        "0x0000000000000000000000000000000000000000"
      ],
    });
    console.log("✅ MockSniperStrategy verified!");
  } catch (e) {
    if (e.message.includes("Already Verified")) {
      console.log("✅ MockSniperStrategy already verified");
    } else {
      console.log("❌ MockSniperStrategy verification failed:", e.message);
    }
  }

  console.log("\n2. Verifying PredictFiSniperVaultV2...");
  try {
    await hre.run("verify:verify", {
      address: VAULT_ADDRESS,
      constructorArguments: [
        USDC_ADDRESS,
        STRATEGY_ADDRESS,
        FEE_COLLECTOR,
        500,
        2000,
        10000000000n,
        100000000n
      ],
    });
    console.log("✅ PredictFiSniperVaultV2 verified!");
  } catch (e) {
    if (e.message.includes("Already Verified")) {
      console.log("✅ PredictFiSniperVaultV2 already verified");
    } else {
      console.log("❌ PredictFiSniperVaultV2 verification failed:", e.message);
    }
  }

  console.log("\n" + "=".repeat(60));
  console.log("VERIFICATION COMPLETE");
  console.log("=".repeat(60));
  console.log("View on BaseScan:");
  console.log(`Strategy: https://basescan.org/address/${STRATEGY_ADDRESS}#code`);
  console.log(`Vault: https://basescan.org/address/${VAULT_ADDRESS}#code`);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
