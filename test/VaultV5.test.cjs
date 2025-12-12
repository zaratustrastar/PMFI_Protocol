const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("PredictFiSniperVaultV5 - Refined", function () {
  let vault;
  let usdc;
  let owner;
  let oracle;
  let user;
  let user2;
  let taxCollector;
  let polymarketWallet;
  
  const NAV_PRECISION = ethers.parseUnits("1", 18);
  const INITIAL_NAV = NAV_PRECISION;
  const MAX_PER_WALLET = ethers.parseUnits("100", 6);
  const MAX_TOTAL = ethers.parseUnits("10000", 6);
  const TARGET_BUFFER = ethers.parseUnits("100", 6);
  
  const NAV_TYPEHASH = ethers.keccak256(
    ethers.toUtf8Bytes("NavData(uint256 nav,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)")
  );

  async function signNavData(nav, timestamp, deadline, roundId, vaultAddress, signer) {
    const structHash = ethers.keccak256(
      ethers.AbiCoder.defaultAbiCoder().encode(
        ["bytes32", "uint256", "uint256", "uint256", "uint256", "address"],
        [NAV_TYPEHASH, nav, timestamp, deadline, roundId, vaultAddress]
      )
    );
    
    const signature = await signer.signMessage(ethers.getBytes(structHash));
    return signature;
  }

  async function getSignedNav(nav, roundId, signer, vaultAddress) {
    const block = await ethers.provider.getBlock("latest");
    const timestamp = block.timestamp;
    const deadline = timestamp + 30;
    const signature = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, signer);
    return { nav, timestamp, deadline, roundId, signature };
  }

  beforeEach(async function () {
    [owner, oracle, user, user2, taxCollector, polymarketWallet] = await ethers.getSigners();
    
    const MockUSDC = await ethers.getContractFactory("MockUSDC");
    usdc = await MockUSDC.deploy();
    await usdc.waitForDeployment();
    
    const VaultV5 = await ethers.getContractFactory("PredictFiSniperVaultV5");
    vault = await VaultV5.deploy(
      await usdc.getAddress(),
      oracle.address,
      taxCollector.address,
      polymarketWallet.address,
      INITIAL_NAV,
      MAX_PER_WALLET,
      MAX_TOTAL,
      TARGET_BUFFER
    );
    await vault.waitForDeployment();
    
    await usdc.mint(user.address, ethers.parseUnits("1000", 6));
    await usdc.connect(user).approve(await vault.getAddress(), ethers.parseUnits("1000", 6));
    
    await usdc.mint(user2.address, ethers.parseUnits("1000", 6));
    await usdc.connect(user2).approve(await vault.getAddress(), ethers.parseUnits("1000", 6));
  });

  describe("Permissionless investIdle()", function () {
    it("should keep USDC in vault on deposit (no auto-split)", async function () {
      const vaultAddress = await vault.getAddress();
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      
      const depositAmount = ethers.parseUnits("100", 6);
      const pmBalanceBefore = await usdc.balanceOf(polymarketWallet.address);
      
      await vault.connect(user).deposit(
        depositAmount,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const pmBalanceAfter = await usdc.balanceOf(polymarketWallet.address);
      const vaultBalance = await usdc.balanceOf(vaultAddress);
      
      expect(pmBalanceAfter - pmBalanceBefore).to.equal(0);
      expect(vaultBalance).to.equal(depositAmount);
    });

    it("should allow anyone to call investIdle() to rebalance", async function () {
      const vaultAddress = await vault.getAddress();
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const navData2 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      await vault.connect(user2).deposit(
        ethers.parseUnits("100", 6),
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      const vaultBalanceBefore = await usdc.balanceOf(vaultAddress);
      expect(vaultBalanceBefore).to.equal(ethers.parseUnits("200", 6));
      
      await vault.connect(user2).investIdle();
      
      const vaultBalanceAfter = await usdc.balanceOf(vaultAddress);
      const pmBalance = await usdc.balanceOf(polymarketWallet.address);
      
      expect(vaultBalanceAfter).to.equal(TARGET_BUFFER);
      expect(pmBalance).to.equal(ethers.parseUnits("100", 6));
    });

    it("should do nothing if balance <= targetBuffer", async function () {
      const vaultAddress = await vault.getAddress();
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      
      await vault.connect(user).deposit(
        ethers.parseUnits("50", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const result = await vault.connect(user2).investIdle.staticCall();
      expect(result).to.equal(0);
    });
  });

  describe("Async Withdrawal with Claim-Time NAV", function () {
    beforeEach(async function () {
      const vaultAddress = await vault.getAddress();
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
    });

    it("should create withdrawal request (stores only shares)", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const req = await vault.getWithdrawalRequest(0);
      expect(req.user).to.equal(user.address);
      expect(req.shares).to.equal(shares);
    });

    it("should use claim-time NAV for payout (not request NAV)", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      await usdc.mint(owner.address, ethers.parseUnits("10", 6));
      await usdc.connect(owner).approve(vaultAddress, ethers.parseUnits("10", 6));
      await vault.connect(owner).refillBuffer(ethers.parseUnits("10", 6));
      
      const higherNav = INITIAL_NAV * 104n / 100n;
      const navData2 = await getSignedNav(higherNav, 3, oracle, vaultAddress);
      
      const userBalanceBefore = await usdc.balanceOf(user.address);
      
      await vault.connect(user).claim(
        0,
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      const userBalanceAfter = await usdc.balanceOf(user.address);
      const received = userBalanceAfter - userBalanceBefore;
      
      const expectedGross = (shares * higherNav) / NAV_PRECISION;
      const expectedTax = expectedGross / 100n;
      const expectedNet = expectedGross - expectedTax;
      
      expect(received).to.equal(expectedNet);
    });

    it("should fail claim if buffer insufficient", async function () {
      const vaultAddress = await vault.getAddress();
      
      await vault.connect(owner).setTargetBuffer(ethers.parseUnits("10", 6));
      
      const navData1 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      await vault.connect(user2).deposit(
        ethers.parseUnits("100", 6),
        [navData1.nav, navData1.timestamp, navData1.deadline, navData1.roundId],
        navData1.signature
      );
      
      await vault.investIdle();
      
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 3, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const navData2 = await getSignedNav(INITIAL_NAV, 4, oracle, vaultAddress);
      
      await expect(
        vault.connect(user).claim(
          0,
          [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
          navData2.signature
        )
      ).to.be.revertedWith("Insufficient buffer");
    });

    it("should allow claim after buffer refill", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      await vault.investIdle();
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      await usdc.mint(owner.address, ethers.parseUnits("100", 6));
      await usdc.connect(owner).approve(vaultAddress, ethers.parseUnits("100", 6));
      await vault.connect(owner).refillBuffer(ethers.parseUnits("100", 6));
      
      const navData2 = await getSignedNav(INITIAL_NAV, 3, oracle, vaultAddress);
      
      await expect(
        vault.connect(user).claim(
          0,
          [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
          navData2.signature
        )
      ).to.not.be.reverted;
    });
  });

  describe("Claim Allowed When Paused", function () {
    it("should allow claim even when vault is paused", async function () {
      const vaultAddress = await vault.getAddress();
      
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const shares = await vault.balanceOf(user.address);
      const navData2 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      await vault.connect(owner).setPaused(true);
      
      const navData3 = await getSignedNav(INITIAL_NAV, 3, oracle, vaultAddress);
      
      await expect(
        vault.connect(user).claim(
          0,
          [navData3.nav, navData3.timestamp, navData3.deadline, navData3.roundId],
          navData3.signature
        )
      ).to.not.be.reverted;
    });

    it("should block new deposits when paused", async function () {
      const vaultAddress = await vault.getAddress();
      
      await vault.connect(owner).setPaused(true);
      
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("100", 6),
          [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
          navData.signature
        )
      ).to.be.revertedWith("Vault is paused");
    });

    it("should block new withdrawal requests when paused", async function () {
      const vaultAddress = await vault.getAddress();
      
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      await vault.connect(owner).setPaused(true);
      
      const navData2 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await expect(
        vault.connect(user).requestWithdraw(
          await vault.balanceOf(user.address),
          [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
          navData2.signature
        )
      ).to.be.revertedWith("Vault is paused");
    });

    it("should block investIdle when paused", async function () {
      const vaultAddress = await vault.getAddress();
      
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      await vault.connect(owner).setPaused(true);
      
      await expect(
        vault.investIdle()
      ).to.be.revertedWith("Vault is paused");
    });
  });

  describe("1% Withdrawal Tax", function () {
    it("should collect 1% tax on claim", async function () {
      const vaultAddress = await vault.getAddress();
      
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const shares = await vault.balanceOf(user.address);
      const navData2 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      const taxCollectorBefore = await usdc.balanceOf(taxCollector.address);
      
      const navData3 = await getSignedNav(INITIAL_NAV, 3, oracle, vaultAddress);
      await vault.connect(user).claim(
        0,
        [navData3.nav, navData3.timestamp, navData3.deadline, navData3.roundId],
        navData3.signature
      );
      
      const taxCollectorAfter = await usdc.balanceOf(taxCollector.address);
      expect(taxCollectorAfter - taxCollectorBefore).to.equal(ethers.parseUnits("1", 6));
    });
  });

  describe("Configurable Caps and Target Buffer", function () {
    it("should allow owner to change target buffer", async function () {
      const newBuffer = ethers.parseUnits("500", 6);
      
      await vault.connect(owner).setTargetBuffer(newBuffer);
      
      expect(await vault.targetBuffer()).to.equal(newBuffer);
    });

    it("should allow owner to increase caps", async function () {
      const newPerWallet = ethers.parseUnits("10000", 6);
      const newTotal = ethers.parseUnits("100000", 6);
      
      await vault.connect(owner).setCaps(newPerWallet, newTotal);
      
      expect(await vault.maxDepositPerWallet()).to.equal(newPerWallet);
      expect(await vault.maxTotalDeposits()).to.equal(newTotal);
    });
  });

  describe("Withdrawal Expiry", function () {
    it("should allow cancelling expired withdrawal", async function () {
      const vaultAddress = await vault.getAddress();
      
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const shares = await vault.balanceOf(user.address);
      
      const navData2 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      expect(await vault.balanceOf(user.address)).to.equal(0);
      
      await time.increase(7 * 24 * 3600 + 1);
      
      await vault.connect(user).cancelExpiredWithdrawal(0);
      
      expect(await vault.balanceOf(user.address)).to.equal(shares);
    });
  });
});
