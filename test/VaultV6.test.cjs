const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("PredictFiSniperVaultV6", function () {
  let vault, usdc, owner, user1, user2;
  let navSigner, taxCollector, polymarketWallet;
  
  const INITIAL_NAV = ethers.parseUnits("1", 18); // $1.00
  const MAX_PER_WALLET = ethers.parseUnits("1000", 6); // 1000 USDC
  const MAX_TOTAL = ethers.parseUnits("10000", 6); // 10k USDC
  const BUFFER_PERCENT_BPS = 1000; // 10%
  const MAX_REBALANCE = ethers.parseUnits("1000", 6);
  
  const POLYMARKET_BASE_DEPOSIT = "0xa76a91208FC7CB88420070AF978D12F440cab2F0";

  beforeEach(async function () {
    [owner, user1, user2] = await ethers.getSigners();
    navSigner = owner;
    taxCollector = owner;
    polymarketWallet = owner;

    // Deploy mock USDC
    const MockERC20 = await ethers.getContractFactory("MockERC20");
    usdc = await MockERC20.deploy("USD Coin", "USDC", 6);
    await usdc.waitForDeployment();

    // Deploy V6 vault
    const VaultV6 = await ethers.getContractFactory("PredictFiSniperVaultV6");
    vault = await VaultV6.deploy(
      await usdc.getAddress(),
      navSigner.address,
      taxCollector.address,
      polymarketWallet.address,
      INITIAL_NAV,
      MAX_PER_WALLET,
      MAX_TOTAL,
      BUFFER_PERCENT_BPS,
      MAX_REBALANCE
    );
    await vault.waitForDeployment();

    // Mint USDC to users
    await usdc.mint(user1.address, ethers.parseUnits("10000", 6));
    await usdc.mint(user2.address, ethers.parseUnits("10000", 6));
    
    // Approve vault
    await usdc.connect(user1).approve(await vault.getAddress(), ethers.MaxUint256);
    await usdc.connect(user2).approve(await vault.getAddress(), ethers.MaxUint256);
  });

  async function signNav(nav, roundId) {
    const timestamp = Math.floor(Date.now() / 1000);
    const deadline = timestamp + 30;
    
    const navData = {
      nav: nav,
      timestamp: timestamp,
      deadline: deadline,
      roundId: roundId
    };
    
    const NAV_TYPEHASH = ethers.keccak256(
      ethers.toUtf8Bytes("NavData(uint256 nav,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)")
    );
    
    const structHash = ethers.keccak256(
      ethers.AbiCoder.defaultAbiCoder().encode(
        ["bytes32", "uint256", "uint256", "uint256", "uint256", "address"],
        [NAV_TYPEHASH, nav, timestamp, deadline, roundId, await vault.getAddress()]
      )
    );
    
    const messageHash = ethers.hashMessage(ethers.getBytes(structHash));
    const signature = await navSigner.signMessage(ethers.getBytes(structHash));
    
    return { navData, signature };
  }

  describe("Auto-Split Deposits", function () {
    it("should split deposit 10/90 between buffer and Polymarket", async function () {
      const depositAmount = ethers.parseUnits("100", 6); // $100
      const expectedToBuffer = ethers.parseUnits("10", 6); // 10%
      const expectedToPolymarket = ethers.parseUnits("90", 6); // 90%
      
      const { navData, signature } = await signNav(INITIAL_NAV, 1);
      
      const vaultBalanceBefore = await usdc.balanceOf(await vault.getAddress());
      const pmBalanceBefore = await usdc.balanceOf(POLYMARKET_BASE_DEPOSIT);
      
      await vault.connect(user1).deposit(depositAmount, navData, signature);
      
      const vaultBalanceAfter = await usdc.balanceOf(await vault.getAddress());
      const pmBalanceAfter = await usdc.balanceOf(POLYMARKET_BASE_DEPOSIT);
      
      // Vault should have 10% (the buffer)
      expect(vaultBalanceAfter - vaultBalanceBefore).to.equal(expectedToBuffer);
      
      // Polymarket should have received 90%
      expect(pmBalanceAfter - pmBalanceBefore).to.equal(expectedToPolymarket);
      
      // User should have received shares for full $100
      const userShares = await vault.balanceOf(user1.address);
      expect(userShares).to.equal(depositAmount * BigInt(1e12)); // NAV = 1e18, so shares = amount * 1e12
      
      // Total sent to Polymarket should be tracked
      const totalSent = await vault.totalSentToPolymarket();
      expect(totalSent).to.equal(expectedToPolymarket);
    });

    it("should emit correct event with split amounts", async function () {
      const depositAmount = ethers.parseUnits("100", 6);
      const { navData, signature } = await signNav(INITIAL_NAV, 1);
      
      await expect(vault.connect(user1).deposit(depositAmount, navData, signature))
        .to.emit(vault, "Deposit")
        .withArgs(
          user1.address,
          depositAmount,
          depositAmount * BigInt(1e12), // shares
          INITIAL_NAV,
          ethers.parseUnits("10", 6), // toBuffer
          ethers.parseUnits("90", 6)  // toPolymarket
        );
    });

    it("should allow owner to change buffer percentage", async function () {
      // Change to 20% buffer
      await vault.setBufferPercent(2000);
      
      const { navData, signature } = await signNav(INITIAL_NAV, 1);
      const depositAmount = ethers.parseUnits("100", 6);
      
      const vaultBalanceBefore = await usdc.balanceOf(await vault.getAddress());
      await vault.connect(user1).deposit(depositAmount, navData, signature);
      const vaultBalanceAfter = await usdc.balanceOf(await vault.getAddress());
      
      // Now 20% should stay in vault
      expect(vaultBalanceAfter - vaultBalanceBefore).to.equal(ethers.parseUnits("20", 6));
    });

    it("should reject buffer percent > 50%", async function () {
      await expect(vault.setBufferPercent(5001)).to.be.revertedWith("Buffer must be 0-50%");
    });

    it("should preview deposit correctly", async function () {
      const depositAmount = ethers.parseUnits("100", 6);
      const [shares, toBuffer, toPolymarket] = await vault.previewDeposit(depositAmount, INITIAL_NAV);
      
      expect(shares).to.equal(depositAmount * BigInt(1e12));
      expect(toBuffer).to.equal(ethers.parseUnits("10", 6));
      expect(toPolymarket).to.equal(ethers.parseUnits("90", 6));
    });
  });

  describe("Polymarket Base Deposit Address", function () {
    it("should have correct hardcoded Polymarket deposit address", async function () {
      const pmDeposit = await vault.POLYMARKET_BASE_DEPOSIT();
      expect(pmDeposit).to.equal(POLYMARKET_BASE_DEPOSIT);
    });
  });

  describe("getVaultState", function () {
    it("should return correct state including totalSentToPolymarket", async function () {
      const { navData, signature } = await signNav(INITIAL_NAV, 1);
      await vault.connect(user1).deposit(ethers.parseUnits("100", 6), navData, signature);
      
      const state = await vault.getVaultState();
      expect(state._totalSentToPolymarket).to.equal(ethers.parseUnits("90", 6));
      expect(state._vaultBalance).to.equal(ethers.parseUnits("10", 6));
      expect(state._bufferPercentBps).to.equal(1000);
    });
  });
});
