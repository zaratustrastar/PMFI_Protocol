import hre from "hardhat";

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  
  const STRATEGY_ADDRESS = "0x6bD7138e87Ed9F4e15eA09A5551C90789A373DB2";
  const VAULT_ADDRESS = "0x5E9Cfd99Cb55cB7981E8C7E8ceC73dC68e60e6C9";
  
  console.log("Configuring strategy...");
  console.log("Deployer:", deployer.address);
  
  const strategy = await hre.ethers.getContractAt("MockSniperStrategy", STRATEGY_ADDRESS);
  
  console.log("\nSetting vault address...");
  const tx1 = await strategy.setVault(VAULT_ADDRESS);
  await tx1.wait();
  console.log("✅ Vault set");
  
  console.log("\nSetting keeper...");
  const tx2 = await strategy.setKeeper(deployer.address);
  await tx2.wait();
  console.log("✅ Keeper set to:", deployer.address);
  
  console.log("\n✅ Strategy configured successfully!");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
