import hre from "hardhat";

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  const STRATEGY_ADDRESS = "0x6bD7138e87Ed9F4e15eA09A5551C90789A373DB2";
  
  console.log("Setting keeper to:", deployer.address);
  
  const strategy = await hre.ethers.getContractAt("MockSniperStrategy", STRATEGY_ADDRESS);
  const tx = await strategy.setKeeper(deployer.address);
  await tx.wait();
  
  console.log("✅ Keeper set successfully!");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
