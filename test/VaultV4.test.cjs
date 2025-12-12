const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("PredictFiSniperVaultV4 - Signature Verification", function () {
  let vault;
  let usdc;
  let owner;
  let oracle;
  let user;
  let taxCollector;
  
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
    
    const messageHash = ethers.hashMessage(ethers.getBytes(structHash));
    const signature = await signer.signMessage(ethers.getBytes(structHash));
    
    return signature;
  }

  beforeEach(async function () {
    [owner, oracle, user, taxCollector] = await ethers.getSigners();
    
    const MockUSDC = await ethers.getContractFactory("MockUSDC");
    usdc = await MockUSDC.deploy();
    await usdc.waitForDeployment();
    
    const VaultV4 = await ethers.getContractFactory("PredictFiSniperVaultV4");
    vault = await VaultV4.deploy(
      await usdc.getAddress(),
      oracle.address,
      taxCollector.address,
      INITIAL_NAV,
      MAX_PER_WALLET,
      MAX_TOTAL
    );
    await vault.waitForDeployment();
    
    await usdc.mint(user.address, ethers.parseUnits("1000", 6));
    await usdc.connect(user).approve(await vault.getAddress(), ethers.parseUnits("1000", 6));
  });

  describe("Valid Signature Tests", function () {
    it("should accept a valid signature and allow deposit", async function () {
      const nav = INITIAL_NAV;
      const timestamp = Math.floor(Date.now() / 1000);
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      
      const depositAmount = ethers.parseUnits("10", 6);
      
      await expect(
        vault.connect(user).deposit(
          depositAmount,
          { nav, timestamp, deadline, roundId },
          signature
        )
      ).to.emit(vault, "Deposit");
      
      expect(await vault.balanceOf(user.address)).to.be.gt(0);
    });
  });

  describe("Invalid Signature Tests", function () {
    it("should reject expired deadline", async function () {
      const nav = INITIAL_NAV;
      const timestamp = Math.floor(Date.now() / 1000) - 60;
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      
      const depositAmount = ethers.parseUnits("10", 6);
      
      await expect(
        vault.connect(user).deposit(
          depositAmount,
          { nav, timestamp, deadline, roundId },
          signature
        )
      ).to.be.revertedWith("NAV expired");
    });

    it("should reject older roundId", async function () {
      const nav = INITIAL_NAV;
      const timestamp = Math.floor(Date.now() / 1000);
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature1 = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      
      await vault.connect(user).deposit(
        ethers.parseUnits("10", 6),
        { nav, timestamp, deadline, roundId },
        signature1
      );
      
      const timestamp2 = timestamp + 1;
      const deadline2 = timestamp2 + 30;
      const sameRoundId = 1;
      
      const signature2 = await signNavData(nav, timestamp2, deadline2, sameRoundId, vaultAddress, oracle);
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("10", 6),
          { nav, timestamp: timestamp2, deadline: deadline2, roundId: sameRoundId },
          signature2
        )
      ).to.be.revertedWith("RoundId must increase");
    });

    it("should reject modified NAV with original signature", async function () {
      const nav = INITIAL_NAV;
      const timestamp = Math.floor(Date.now() / 1000);
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      
      const modifiedNav = nav * 2n;
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("10", 6),
          { nav: modifiedNav, timestamp, deadline, roundId },
          signature
        )
      ).to.be.revertedWith("Invalid NAV signer");
    });

    it("should reject signature from wrong signer", async function () {
      const nav = INITIAL_NAV;
      const timestamp = Math.floor(Date.now() / 1000);
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, user);
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("10", 6),
          { nav, timestamp, deadline, roundId },
          signature
        )
      ).to.be.revertedWith("Invalid NAV signer");
    });

    it("should reject NAV too old", async function () {
      const nav = INITIAL_NAV;
      const timestamp = Math.floor(Date.now() / 1000) - 60;
      const deadline = Math.floor(Date.now() / 1000) + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("10", 6),
          { nav, timestamp, deadline, roundId },
          signature
        )
      ).to.be.revertedWith("NAV too old");
    });
  });

  describe("NAV Change Limit Tests", function () {
    it("should reject NAV change greater than 5%", async function () {
      const nav = INITIAL_NAV;
      const block = await ethers.provider.getBlock("latest");
      const timestamp = block.timestamp;
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature1 = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      await vault.connect(user).deposit(
        ethers.parseUnits("10", 6),
        { nav, timestamp, deadline, roundId },
        signature1
      );
      
      const bigJumpNav = nav * 110n / 100n;
      const block2 = await ethers.provider.getBlock("latest");
      const timestamp2 = block2.timestamp;
      const deadline2 = timestamp2 + 30;
      const roundId2 = 2;
      
      const signature2 = await signNavData(bigJumpNav, timestamp2, deadline2, roundId2, vaultAddress, oracle);
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("10", 6),
          { nav: bigJumpNav, timestamp: timestamp2, deadline: deadline2, roundId: roundId2 },
          signature2
        )
      ).to.be.revertedWith("NAV change too large");
    });

    it("should accept NAV change within 5%", async function () {
      const nav = INITIAL_NAV;
      const block = await ethers.provider.getBlock("latest");
      const timestamp = block.timestamp;
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature1 = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      await vault.connect(user).deposit(
        ethers.parseUnits("10", 6),
        { nav, timestamp, deadline, roundId },
        signature1
      );
      
      const smallChangeNav = nav * 103n / 100n;
      const block2 = await ethers.provider.getBlock("latest");
      const timestamp2 = block2.timestamp;
      const deadline2 = timestamp2 + 30;
      const roundId2 = 2;
      
      const signature2 = await signNavData(smallChangeNav, timestamp2, deadline2, roundId2, vaultAddress, oracle);
      
      await expect(
        vault.connect(user).deposit(
          ethers.parseUnits("10", 6),
          { nav: smallChangeNav, timestamp: timestamp2, deadline: deadline2, roundId: roundId2 },
          signature2
        )
      ).to.emit(vault, "Deposit");
    });
  });

  describe("Withdrawal Tax Tests", function () {
    it("should collect 1% tax on withdrawal", async function () {
      const nav = INITIAL_NAV;
      const block = await ethers.provider.getBlock("latest");
      const timestamp = block.timestamp;
      const deadline = timestamp + 30;
      const roundId = 1;
      const vaultAddress = await vault.getAddress();
      
      const signature1 = await signNavData(nav, timestamp, deadline, roundId, vaultAddress, oracle);
      await vault.connect(user).deposit(
        ethers.parseUnits("100", 6),
        { nav, timestamp, deadline, roundId },
        signature1
      );
      
      const shares = await vault.balanceOf(user.address);
      const taxCollectorBalanceBefore = await usdc.balanceOf(taxCollector.address);
      
      const block2 = await ethers.provider.getBlock("latest");
      const timestamp2 = block2.timestamp;
      const deadline2 = timestamp2 + 30;
      const roundId2 = 2;
      const signature2 = await signNavData(nav, timestamp2, deadline2, roundId2, vaultAddress, oracle);
      
      await vault.connect(user).redeem(
        shares,
        { nav, timestamp: timestamp2, deadline: deadline2, roundId: roundId2 },
        signature2
      );
      
      const taxCollectorBalanceAfter = await usdc.balanceOf(taxCollector.address);
      const taxCollected = taxCollectorBalanceAfter - taxCollectorBalanceBefore;
      
      expect(taxCollected).to.equal(ethers.parseUnits("1", 6));
    });
  });
});
