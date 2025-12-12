const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("PredictFiSniperVaultV5 - Async Withdrawals", function () {
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
      MAX_TOTAL
    );
    await vault.waitForDeployment();
    
    await usdc.mint(user.address, ethers.parseUnits("1000", 6));
    await usdc.connect(user).approve(await vault.getAddress(), ethers.parseUnits("1000", 6));
    
    await usdc.mint(user2.address, ethers.parseUnits("1000", 6));
    await usdc.connect(user2).approve(await vault.getAddress(), ethers.parseUnits("1000", 6));
  });

  describe("90/10 Auto-Split Deposit", function () {
    it("should split deposit 90/10 between Polymarket and buffer", async function () {
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
      const vaultBuffer = await usdc.balanceOf(vaultAddress);
      
      expect(pmBalanceAfter - pmBalanceBefore).to.equal(ethers.parseUnits("90", 6));
      expect(vaultBuffer).to.equal(ethers.parseUnits("10", 6));
    });
  });

  describe("Async Withdrawal Flow", function () {
    beforeEach(async function () {
      const vaultAddress = await vault.getAddress();
      const navData = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
    });

    it("should create withdrawal request and lock shares", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      expect(await vault.balanceOf(user.address)).to.equal(0);
      expect(await vault.balanceOf(vaultAddress)).to.equal(shares);
      
      const pending = await vault.getPendingWithdrawalUsdc();
      expect(pending).to.be.gt(0);
    });

    it("should fail claim if buffer insufficient", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      const navData2 = await getSignedNav(INITIAL_NAV, 3, oracle, vaultAddress);
      
      await expect(
        vault.connect(user).claim(
          0,
          [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
          navData2.signature
        )
      ).to.be.revertedWith("Insufficient buffer");
    });

    it("should allow claim after buffer is refilled", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
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
      const userBalanceBefore = await usdc.balanceOf(user.address);
      
      await vault.connect(user).claim(
        0,
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      const userBalanceAfter = await usdc.balanceOf(user.address);
      expect(userBalanceAfter).to.be.gt(userBalanceBefore);
    });

    it("should use lower of request NAV or claim NAV", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      await usdc.mint(owner.address, ethers.parseUnits("100", 6));
      await usdc.connect(owner).approve(vaultAddress, ethers.parseUnits("100", 6));
      await vault.connect(owner).refillBuffer(ethers.parseUnits("100", 6));
      
      const lowerNav = INITIAL_NAV * 97n / 100n;
      const navData2 = await getSignedNav(lowerNav, 3, oracle, vaultAddress);
      
      const userBalanceBefore = await usdc.balanceOf(user.address);
      
      await vault.connect(user).claim(
        0,
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      const userBalanceAfter = await usdc.balanceOf(user.address);
      const received = userBalanceAfter - userBalanceBefore;
      
      const expectedGross = (shares * lowerNav) / NAV_PRECISION;
      const expectedTax = expectedGross / 100n;
      const expectedNet = expectedGross - expectedTax;
      
      expect(received).to.equal(expectedNet);
    });

    it("should allow cancelling expired withdrawal", async function () {
      const vaultAddress = await vault.getAddress();
      const shares = await vault.balanceOf(user.address);
      
      const navData = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      
      await vault.connect(user).requestWithdraw(
        shares,
        [navData.nav, navData.timestamp, navData.deadline, navData.roundId],
        navData.signature
      );
      
      expect(await vault.balanceOf(user.address)).to.equal(0);
      
      await time.increase(7 * 24 * 3600 + 1);
      
      await vault.connect(user).cancelExpiredWithdrawal(0);
      
      expect(await vault.balanceOf(user.address)).to.equal(shares);
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
      
      await usdc.mint(owner.address, ethers.parseUnits("100", 6));
      await usdc.connect(owner).approve(vaultAddress, ethers.parseUnits("100", 6));
      await vault.connect(owner).refillBuffer(ethers.parseUnits("100", 6));
      
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

  describe("FIFO Withdrawal Queue", function () {
    it("should process withdrawals in FIFO order", async function () {
      const vaultAddress = await vault.getAddress();
      
      const navData1 = await getSignedNav(INITIAL_NAV, 1, oracle, vaultAddress);
      await vault.connect(user).deposit(
        ethers.parseUnits("50", 6),
        [navData1.nav, navData1.timestamp, navData1.deadline, navData1.roundId],
        navData1.signature
      );
      
      const navData2 = await getSignedNav(INITIAL_NAV, 2, oracle, vaultAddress);
      await vault.connect(user2).deposit(
        ethers.parseUnits("50", 6),
        [navData2.nav, navData2.timestamp, navData2.deadline, navData2.roundId],
        navData2.signature
      );
      
      const navData3 = await getSignedNav(INITIAL_NAV, 3, oracle, vaultAddress);
      await vault.connect(user).requestWithdraw(
        await vault.balanceOf(user.address),
        [navData3.nav, navData3.timestamp, navData3.deadline, navData3.roundId],
        navData3.signature
      );
      
      const navData4 = await getSignedNav(INITIAL_NAV, 4, oracle, vaultAddress);
      await vault.connect(user2).requestWithdraw(
        await vault.balanceOf(user2.address),
        [navData4.nav, navData4.timestamp, navData4.deadline, navData4.roundId],
        navData4.signature
      );
      
      const req1 = await vault.getWithdrawalRequest(0);
      const req2 = await vault.getWithdrawalRequest(1);
      
      expect(req1[0]).to.equal(user.address);
      expect(req2[0]).to.equal(user2.address);
    });
  });

  describe("Configurable Caps", function () {
    it("should allow owner to increase caps", async function () {
      const newPerWallet = ethers.parseUnits("10000", 6);
      const newTotal = ethers.parseUnits("100000", 6);
      
      await vault.connect(owner).setCaps(newPerWallet, newTotal);
      
      expect(await vault.maxDepositPerWallet()).to.equal(newPerWallet);
      expect(await vault.maxTotalDeposits()).to.equal(newTotal);
    });
  });
});
