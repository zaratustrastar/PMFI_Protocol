const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("PredictFiSniperVault", function () {
  let testUSDC;
  let mockStrategy;
  let vault;
  let deployer;
  let user;
  
  const DECIMALS = 6;
  const ONE_USDC = 10n ** BigInt(DECIMALS); // 1e6
  const INITIAL_USER_BALANCE = 10000n * ONE_USDC; // 10,000 USDC
  const DEPOSIT_AMOUNT = 1000n * ONE_USDC; // 1,000 USDC
  const MAX_DEPOSITS = 10000n * ONE_USDC; // 10,000 USDC
  const PERFORMANCE_FEE_BPS = 500n; // 5%
  const BUFFER_BPS = 2000n; // 20%

  beforeEach(async function () {
    [deployer, user] = await ethers.getSigners();

    // Deploy TestUSDC
    const TestUSDC = await ethers.getContractFactory("TestUSDC");
    testUSDC = await TestUSDC.deploy();
    await testUSDC.waitForDeployment();

    // Mint tUSDC to user
    await testUSDC.mint(user.address, INITIAL_USER_BALANCE);

    // Deploy MockSniperStrategy
    const MockSniperStrategy = await ethers.getContractFactory("MockSniperStrategy");
    mockStrategy = await MockSniperStrategy.deploy(await testUSDC.getAddress());
    await mockStrategy.waitForDeployment();

    // Deploy PredictFiSniperVault
    const PredictFiSniperVault = await ethers.getContractFactory("PredictFiSniperVault");
    vault = await PredictFiSniperVault.deploy(
      await testUSDC.getAddress(),
      await mockStrategy.getAddress(),
      deployer.address, // feeCollector
      PERFORMANCE_FEE_BPS,
      BUFFER_BPS,
      MAX_DEPOSITS
    );
    await vault.waitForDeployment();

    // Set vault address in strategy
    await mockStrategy.setVault(await vault.getAddress());
  });

  describe("Deployment", function () {
    it("Should set the correct asset", async function () {
      expect(await vault.asset()).to.equal(await testUSDC.getAddress());
    });

    it("Should set the correct strategy", async function () {
      expect(await vault.strategy()).to.equal(await mockStrategy.getAddress());
    });

    it("Should set the correct fee collector", async function () {
      expect(await vault.feeCollector()).to.equal(deployer.address);
    });

    it("Should set the correct performance fee", async function () {
      expect(await vault.performanceFeeBps()).to.equal(PERFORMANCE_FEE_BPS);
    });

    it("Should set the correct buffer", async function () {
      expect(await vault.bufferBps()).to.equal(BUFFER_BPS);
    });

    it("Should set the correct max deposits", async function () {
      expect(await vault.maxTotalDeposits()).to.equal(MAX_DEPOSITS);
    });
  });

  describe("Deposits", function () {
    it("User can approve and deposit 1000 tUSDC", async function () {
      // Approve vault to spend user's USDC
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      
      // Deposit
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      // Check user received shares
      const userShares = await vault.balanceOf(user.address);
      expect(userShares).to.be.gt(0);
      
      // Check vault received USDC
      const vaultBalance = await testUSDC.balanceOf(await vault.getAddress());
      expect(vaultBalance).to.equal(DEPOSIT_AMOUNT);
    });

    it("totalAssets increases by deposit amount", async function () {
      const totalAssetsBefore = await vault.totalAssets();
      
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      const totalAssetsAfter = await vault.totalAssets();
      expect(totalAssetsAfter - totalAssetsBefore).to.equal(DEPOSIT_AMOUNT);
    });

    it("Should reject deposits exceeding max total deposits", async function () {
      // First deposit at max
      await testUSDC.connect(user).approve(await vault.getAddress(), MAX_DEPOSITS);
      await vault.connect(user).deposit(MAX_DEPOSITS, user.address);
      
      // Mint more to user and try to deposit again
      await testUSDC.mint(user.address, ONE_USDC);
      await testUSDC.connect(user).approve(await vault.getAddress(), ONE_USDC);
      
      await expect(
        vault.connect(user).deposit(ONE_USDC, user.address)
      ).to.be.revertedWith("Exceeds max deposits");
    });
  });

  describe("Invest Idle", function () {
    it("investIdle moves USDC from vault to strategy, leaving 20% buffer", async function () {
      // Deposit 1000 USDC
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      // Check initial vault balance
      const vaultBalanceBefore = await testUSDC.balanceOf(await vault.getAddress());
      expect(vaultBalanceBefore).to.equal(DEPOSIT_AMOUNT);
      
      // Call investIdle
      await vault.investIdle();
      
      // After investIdle:
      // - totalAssets = 1000 USDC
      // - targetIdle = 1000 * 20% = 200 USDC
      // - toInvest = 1000 - 200 = 800 USDC
      const vaultBalanceAfter = await testUSDC.balanceOf(await vault.getAddress());
      const strategyValue = await mockStrategy.totalStrategyValue();
      
      // Vault should have 20% = 200 USDC
      expect(vaultBalanceAfter).to.equal(200n * ONE_USDC);
      
      // Strategy should have 80% = 800 USDC
      expect(strategyValue).to.equal(800n * ONE_USDC);
      
      // Total assets should still be 1000 USDC
      expect(await vault.totalAssets()).to.equal(DEPOSIT_AMOUNT);
    });
  });

  describe("Withdrawals", function () {
    it("withdraw 200 tUSDC works if enough idle in vault", async function () {
      // Deposit 1000 USDC
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      // investIdle leaves 200 USDC in vault
      await vault.investIdle();
      
      // User balance before withdrawal
      const userBalanceBefore = await testUSDC.balanceOf(user.address);
      
      // Withdraw 200 USDC (exactly what's idle)
      const withdrawAmount = 200n * ONE_USDC;
      await vault.connect(user).withdraw(withdrawAmount, user.address, user.address);
      
      // User should have received 200 USDC
      const userBalanceAfter = await testUSDC.balanceOf(user.address);
      expect(userBalanceAfter - userBalanceBefore).to.equal(withdrawAmount);
    });

    it("withdraw pulls from strategy if not enough idle", async function () {
      // Deposit 1000 USDC
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      // investIdle leaves 200 USDC in vault
      await vault.investIdle();
      
      // Try to withdraw 500 USDC (more than idle 200)
      const withdrawAmount = 500n * ONE_USDC;
      await vault.connect(user).withdraw(withdrawAmount, user.address, user.address);
      
      // User should have received 500 USDC
      const userBalance = await testUSDC.balanceOf(user.address);
      expect(userBalance).to.equal(INITIAL_USER_BALANCE - DEPOSIT_AMOUNT + withdrawAmount);
    });
  });

  describe("Harvest Performance Fees", function () {
    it("harvest charges performance fee after strategy gains", async function () {
      // Deposit 1000 USDC
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      // investIdle
      await vault.investIdle();
      
      // Record fee collector's shares before
      const feeSharesBefore = await vault.balanceOf(deployer.address);
      
      // Simulate strategy profit by increasing totalStrategyValue
      // Current strategy value: 800 USDC, let's add 100 USDC profit
      const profit = 100n * ONE_USDC;
      const newStrategyValue = 800n * ONE_USDC + profit;
      await mockStrategy.setTotalStrategyValue(newStrategyValue);
      
      // Also mint extra USDC to strategy to cover the "profit"
      await testUSDC.mint(await mockStrategy.getAddress(), profit);
      
      // Harvest
      await vault.harvest();
      
      // Fee collector should have received new shares
      const feeSharesAfter = await vault.balanceOf(deployer.address);
      expect(feeSharesAfter).to.be.gt(feeSharesBefore);
      
      // The fee should be 5% of 100 USDC profit = 5 USDC worth of shares
      // Let's just verify it's non-zero (exact calculation depends on share price)
      const newShares = feeSharesAfter - feeSharesBefore;
      expect(newShares).to.be.gt(0);
    });

    it("harvest does not charge fee if no profit", async function () {
      // Deposit 1000 USDC
      await testUSDC.connect(user).approve(await vault.getAddress(), DEPOSIT_AMOUNT);
      await vault.connect(user).deposit(DEPOSIT_AMOUNT, user.address);
      
      // investIdle
      await vault.investIdle();
      
      // Record fee collector's shares before
      const feeSharesBefore = await vault.balanceOf(deployer.address);
      
      // Harvest without any profit
      await vault.harvest();
      
      // Fee collector should have same shares
      const feeSharesAfter = await vault.balanceOf(deployer.address);
      expect(feeSharesAfter).to.equal(feeSharesBefore);
    });
  });
});
