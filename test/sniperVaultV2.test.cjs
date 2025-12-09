const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("PredictFiSniperVaultV2", function () {
  let vault, usdc, strategy;
  let owner, user1, user2, feeCollector;
  
  const USDC_DECIMALS = 6;
  const MAX_TOTAL_DEPOSITS = ethers.parseUnits("10000", USDC_DECIMALS); // 10,000 USDC
  const WALLET_DEPOSIT_CAP = ethers.parseUnits("100", USDC_DECIMALS); // 100 USDC per wallet
  const PERFORMANCE_FEE_BPS = 500; // 5%
  const BUFFER_BPS = 2000; // 20%

  beforeEach(async function () {
    [owner, user1, user2, feeCollector] = await ethers.getSigners();

    // Deploy TestUSDC
    const TestUSDC = await ethers.getContractFactory("TestUSDC");
    usdc = await TestUSDC.deploy();
    await usdc.waitForDeployment();

    // Deploy MockSniperStrategy
    const MockSniperStrategy = await ethers.getContractFactory("MockSniperStrategy");
    strategy = await MockSniperStrategy.deploy(await usdc.getAddress());
    await strategy.waitForDeployment();

    // Deploy PredictFiSniperVaultV2
    const VaultV2 = await ethers.getContractFactory("PredictFiSniperVaultV2");
    vault = await VaultV2.deploy(
      await usdc.getAddress(),
      await strategy.getAddress(),
      feeCollector.address,
      PERFORMANCE_FEE_BPS,
      BUFFER_BPS,
      MAX_TOTAL_DEPOSITS,
      WALLET_DEPOSIT_CAP
    );
    await vault.waitForDeployment();

    // Set vault in strategy
    await strategy.setVault(await vault.getAddress());

    // Mint USDC to users for testing
    await usdc.mint(user1.address, ethers.parseUnits("500", USDC_DECIMALS));
    await usdc.mint(user2.address, ethers.parseUnits("500", USDC_DECIMALS));
  });

  describe("Deployment", function () {
    it("should set correct wallet deposit cap", async function () {
      expect(await vault.walletDepositCap()).to.equal(WALLET_DEPOSIT_CAP);
    });

    it("should set correct max total deposits", async function () {
      expect(await vault.maxTotalDeposits()).to.equal(MAX_TOTAL_DEPOSITS);
    });

    it("should have correct token name and symbol", async function () {
      expect(await vault.name()).to.equal("PredictFi Sniper Vault V2");
      expect(await vault.symbol()).to.equal("pSNIPERv2");
    });
  });

  describe("Per-Wallet Deposit Cap", function () {
    it("should allow deposit up to wallet cap", async function () {
      const depositAmount = ethers.parseUnits("100", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), depositAmount);
      await vault.connect(user1).deposit(depositAmount, user1.address);

      expect(await vault.walletDeposited(user1.address)).to.equal(depositAmount);
      expect(await vault.balanceOf(user1.address)).to.be.gt(0);
    });

    it("should revert when deposit exceeds wallet cap", async function () {
      const overCapAmount = ethers.parseUnits("101", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), overCapAmount);
      
      await expect(
        vault.connect(user1).deposit(overCapAmount, user1.address)
      ).to.be.revertedWith("wallet cap reached");
    });

    it("should revert when second deposit pushes over wallet cap", async function () {
      const firstDeposit = ethers.parseUnits("80", USDC_DECIMALS);
      const secondDeposit = ethers.parseUnits("30", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), firstDeposit + secondDeposit);
      
      // First deposit succeeds
      await vault.connect(user1).deposit(firstDeposit, user1.address);
      expect(await vault.walletDeposited(user1.address)).to.equal(firstDeposit);

      // Second deposit exceeds cap
      await expect(
        vault.connect(user1).deposit(secondDeposit, user1.address)
      ).to.be.revertedWith("wallet cap reached");
    });

    it("should allow different users to deposit independently", async function () {
      const depositAmount = ethers.parseUnits("100", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), depositAmount);
      await usdc.connect(user2).approve(await vault.getAddress(), depositAmount);
      
      await vault.connect(user1).deposit(depositAmount, user1.address);
      await vault.connect(user2).deposit(depositAmount, user2.address);

      expect(await vault.walletDeposited(user1.address)).to.equal(depositAmount);
      expect(await vault.walletDeposited(user2.address)).to.equal(depositAmount);
    });
  });

  describe("Global Deposit Cap", function () {
    it("should enforce global cap", async function () {
      // Set wallet cap very high to test global cap
      await vault.setWalletDepositCap(ethers.parseUnits("100000", USDC_DECIMALS));
      
      // Set low global cap
      await vault.setMaxTotalDeposits(ethers.parseUnits("150", USDC_DECIMALS));
      
      const depositAmount = ethers.parseUnits("100", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), depositAmount);
      await usdc.connect(user2).approve(await vault.getAddress(), depositAmount);
      
      // First deposit succeeds
      await vault.connect(user1).deposit(depositAmount, user1.address);
      
      // Second deposit exceeds global cap
      await expect(
        vault.connect(user2).deposit(depositAmount, user2.address)
      ).to.be.revertedWith("cap reached");
    });
  });

  describe("InvestIdle and Buffer", function () {
    it("should invest idle funds above buffer target", async function () {
      const depositAmount = ethers.parseUnits("100", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), depositAmount);
      await vault.connect(user1).deposit(depositAmount, user1.address);

      // Check initial idle balance
      const idleBefore = await usdc.balanceOf(await vault.getAddress());
      expect(idleBefore).to.equal(depositAmount);

      // Call investIdle
      await vault.investIdle();

      // Buffer is 20%, so 80 USDC should be invested
      const idleAfter = await usdc.balanceOf(await vault.getAddress());
      const expectedIdle = ethers.parseUnits("20", USDC_DECIMALS); // 20% of 100
      
      expect(idleAfter).to.equal(expectedIdle);
    });
  });

  describe("Owner Functions", function () {
    it("should allow owner to update wallet deposit cap", async function () {
      const newCap = ethers.parseUnits("200", USDC_DECIMALS);
      
      await vault.setWalletDepositCap(newCap);
      expect(await vault.walletDepositCap()).to.equal(newCap);
    });

    it("should not allow non-owner to update wallet deposit cap", async function () {
      const newCap = ethers.parseUnits("200", USDC_DECIMALS);
      
      await expect(
        vault.connect(user1).setWalletDepositCap(newCap)
      ).to.be.revertedWithCustomError(vault, "OwnableUnauthorizedAccount");
    });
  });

  describe("Withdraw after wallet cap reached", function () {
    it("should allow withdraw but not increase deposit cap", async function () {
      const depositAmount = ethers.parseUnits("100", USDC_DECIMALS);
      
      await usdc.connect(user1).approve(await vault.getAddress(), depositAmount);
      await vault.connect(user1).deposit(depositAmount, user1.address);

      // Get shares
      const shares = await vault.balanceOf(user1.address);
      
      // Withdraw half
      const withdrawAmount = ethers.parseUnits("50", USDC_DECIMALS);
      await vault.connect(user1).withdraw(withdrawAmount, user1.address, user1.address);

      // walletDeposited should still be at cap (100)
      expect(await vault.walletDeposited(user1.address)).to.equal(depositAmount);

      // Cannot deposit more
      const newDeposit = ethers.parseUnits("10", USDC_DECIMALS);
      await usdc.connect(user1).approve(await vault.getAddress(), newDeposit);
      
      await expect(
        vault.connect(user1).deposit(newDeposit, user1.address)
      ).to.be.revertedWith("wallet cap reached");
    });
  });
});
