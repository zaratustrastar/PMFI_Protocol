// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

/**
 * @title PredictFiSniperVaultV4
 * @notice pSNIPER vault with signed NAV oracle pattern
 * @dev Users pay gas for deposit/withdraw, oracle signs NAV off-chain
 * 
 * Key features:
 * - Signed NAV: Bot signs NAV off-chain, users include signature in tx
 * - 1% withdrawal tax to deployer
 * - Per-wallet and total deposit caps
 * - No on-chain NAV updates needed (zero oracle gas cost)
 */
contract PredictFiSniperVaultV4 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    // ============================================
    // Constants
    // ============================================
    
    /// @notice NAV type hash for EIP-712 style signing
    bytes32 public constant NAV_TYPEHASH = keccak256(
        "NavData(uint256 nav,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)"
    );
    
    /// @notice Maximum age of a signed NAV (30 seconds)
    uint256 public constant MAX_NAV_AGE = 30;
    
    /// @notice Maximum NAV change per update (5% = 500 bps)
    uint256 public constant MAX_NAV_CHANGE_BPS = 500;
    
    /// @notice Withdrawal tax in basis points (1% = 100 bps)
    uint256 public constant WITHDRAWAL_TAX_BPS = 100;
    
    /// @notice USDC decimals
    uint256 public constant USDC_DECIMALS = 6;
    
    /// @notice NAV precision (1e18 for 18 decimal precision)
    uint256 public constant NAV_PRECISION = 1e18;

    // ============================================
    // State Variables
    // ============================================
    
    /// @notice USDC token address
    IERC20 public immutable usdc;
    
    /// @notice Oracle signer address (can sign NAV updates)
    address public immutable navSigner;
    
    /// @notice Tax collector address (receives 1% withdrawal tax)
    address public taxCollector;
    
    /// @notice Last accepted NAV (stored for reference/UI)
    uint256 public lastNav;
    
    /// @notice Last accepted roundId (monotonically increasing)
    uint256 public lastRoundId;
    
    /// @notice Last NAV update timestamp
    uint256 public lastNavTimestamp;
    
    /// @notice Maximum deposit per wallet (in USDC, 6 decimals)
    uint256 public maxDepositPerWallet;
    
    /// @notice Maximum total deposits (in USDC, 6 decimals)
    uint256 public maxTotalDeposits;
    
    /// @notice Total USDC deposited (for cap tracking)
    uint256 public totalDeposited;
    
    /// @notice Deposits per wallet
    mapping(address => uint256) public walletDeposits;
    
    /// @notice Pause state
    bool public paused;

    // ============================================
    // Structs
    // ============================================
    
    /// @notice Signed NAV data structure
    struct NavData {
        uint256 nav;        // NAV per pSNIPE in USDC terms, scaled by 1e18
        uint256 timestamp;  // When this NAV was computed
        uint256 deadline;   // Last block timestamp where this is valid
        uint256 roundId;    // Monotonically increasing index
    }

    // ============================================
    // Events
    // ============================================
    
    event Deposit(address indexed user, uint256 usdcAmount, uint256 sharesReceived, uint256 navUsed);
    event Withdraw(address indexed user, uint256 sharesRedeemed, uint256 usdcReceived, uint256 taxPaid, uint256 navUsed);
    event NavUpdated(uint256 newNav, uint256 roundId, uint256 timestamp);
    event TaxCollectorUpdated(address indexed oldCollector, address indexed newCollector);
    event CapsUpdated(uint256 perWallet, uint256 total);
    event Paused(bool isPaused);
    event EmergencyWithdraw(address indexed to, uint256 amount);

    // ============================================
    // Constructor
    // ============================================
    
    /**
     * @param _usdc USDC token address
     * @param _navSigner Address that can sign NAV updates (oracle key)
     * @param _taxCollector Address to receive withdrawal taxes
     * @param _initialNav Initial NAV (1e18 = 1 USDC per pSNIPE)
     * @param _maxPerWallet Max deposit per wallet in USDC (6 decimals)
     * @param _maxTotal Max total deposits in USDC (6 decimals)
     */
    constructor(
        address _usdc,
        address _navSigner,
        address _taxCollector,
        uint256 _initialNav,
        uint256 _maxPerWallet,
        uint256 _maxTotal
    ) ERC20("PredictFi Sniper", "pSNIPER") Ownable(msg.sender) {
        require(_usdc != address(0), "Invalid USDC");
        require(_navSigner != address(0), "Invalid signer");
        require(_taxCollector != address(0), "Invalid tax collector");
        require(_initialNav > 0, "Invalid initial NAV");
        
        usdc = IERC20(_usdc);
        navSigner = _navSigner;
        taxCollector = _taxCollector;
        lastNav = _initialNav;
        lastRoundId = 0;
        lastNavTimestamp = block.timestamp;
        maxDepositPerWallet = _maxPerWallet;
        maxTotalDeposits = _maxTotal;
    }

    // ============================================
    // Modifiers
    // ============================================
    
    modifier whenNotPaused() {
        require(!paused, "Vault is paused");
        _;
    }

    // ============================================
    // Core Functions
    // ============================================
    
    /**
     * @notice Deposit USDC and receive pSNIPE shares
     * @param usdcAmount Amount of USDC to deposit (6 decimals)
     * @param navData Signed NAV data from oracle
     * @param signature Oracle signature over navData
     */
    function deposit(
        uint256 usdcAmount,
        NavData calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused {
        require(usdcAmount > 0, "Amount must be > 0");
        
        // Verify and apply signed NAV
        _verifyAndApplyNav(navData, signature);
        
        // Check caps
        require(walletDeposits[msg.sender] + usdcAmount <= maxDepositPerWallet, "Exceeds wallet cap");
        require(totalDeposited + usdcAmount <= maxTotalDeposits, "Exceeds total cap");
        
        // Calculate shares to mint
        // shares = usdcAmount * NAV_PRECISION / nav
        // Since nav is in 1e18 and usdcAmount is in 1e6, we need to scale
        uint256 sharesToMint = (usdcAmount * NAV_PRECISION) / navData.nav;
        require(sharesToMint > 0, "Shares too small");
        
        // Transfer USDC from user
        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);
        
        // Update accounting
        walletDeposits[msg.sender] += usdcAmount;
        totalDeposited += usdcAmount;
        
        // Mint shares
        _mint(msg.sender, sharesToMint);
        
        emit Deposit(msg.sender, usdcAmount, sharesToMint, navData.nav);
    }
    
    /**
     * @notice Redeem pSNIPE shares for USDC (1% tax applies)
     * @param shareAmount Amount of pSNIPE shares to redeem
     * @param navData Signed NAV data from oracle
     * @param signature Oracle signature over navData
     */
    function redeem(
        uint256 shareAmount,
        NavData calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused {
        require(shareAmount > 0, "Amount must be > 0");
        require(balanceOf(msg.sender) >= shareAmount, "Insufficient shares");
        
        // Verify and apply signed NAV
        _verifyAndApplyNav(navData, signature);
        
        // Calculate USDC value
        // usdcValue = shares * nav / NAV_PRECISION
        uint256 grossUsdc = (shareAmount * navData.nav) / NAV_PRECISION;
        
        // Calculate 1% tax
        uint256 tax = (grossUsdc * WITHDRAWAL_TAX_BPS) / 10000;
        uint256 netUsdc = grossUsdc - tax;
        
        // Check vault has enough USDC
        require(usdc.balanceOf(address(this)) >= grossUsdc, "Insufficient liquidity");
        
        // Burn shares
        _burn(msg.sender, shareAmount);
        
        // Update accounting (reduce wallet deposits proportionally)
        uint256 depositReduction = (walletDeposits[msg.sender] * shareAmount) / (balanceOf(msg.sender) + shareAmount);
        if (depositReduction > walletDeposits[msg.sender]) {
            depositReduction = walletDeposits[msg.sender];
        }
        walletDeposits[msg.sender] -= depositReduction;
        if (depositReduction > totalDeposited) {
            totalDeposited = 0;
        } else {
            totalDeposited -= depositReduction;
        }
        
        // Transfer tax to collector
        usdc.safeTransfer(taxCollector, tax);
        
        // Transfer net USDC to user
        usdc.safeTransfer(msg.sender, netUsdc);
        
        emit Withdraw(msg.sender, shareAmount, netUsdc, tax, navData.nav);
    }

    // ============================================
    // NAV Verification
    // ============================================
    
    /**
     * @notice Verify NAV signature and apply if valid
     * @param navData NAV data to verify
     * @param signature Signature from oracle
     */
    function _verifyAndApplyNav(NavData calldata navData, bytes calldata signature) internal {
        // Check deadline
        require(block.timestamp <= navData.deadline, "NAV expired");
        
        // Check age
        require(block.timestamp - navData.timestamp <= MAX_NAV_AGE, "NAV too old");
        
        // Check roundId is increasing (prevent replay)
        require(navData.roundId > lastRoundId, "RoundId must increase");
        
        // Verify signature
        bytes32 structHash = keccak256(abi.encode(
            NAV_TYPEHASH,
            navData.nav,
            navData.timestamp,
            navData.deadline,
            navData.roundId,
            address(this)
        ));
        bytes32 digest = structHash.toEthSignedMessageHash();
        address signer = digest.recover(signature);
        
        require(signer == navSigner, "Invalid NAV signer");
        
        // Check NAV change is reasonable (unless first update or small values)
        if (lastNav > 0 && navData.nav > 0) {
            uint256 diff = navData.nav > lastNav 
                ? navData.nav - lastNav 
                : lastNav - navData.nav;
            uint256 changePercent = (diff * 10000) / lastNav;
            require(changePercent <= MAX_NAV_CHANGE_BPS, "NAV change too large");
        }
        
        // Apply NAV
        lastNav = navData.nav;
        lastRoundId = navData.roundId;
        lastNavTimestamp = navData.timestamp;
        
        emit NavUpdated(navData.nav, navData.roundId, navData.timestamp);
    }
    
    /**
     * @notice Preview how many shares you'd get for a deposit
     * @param usdcAmount USDC amount (6 decimals)
     * @param nav NAV to use (1e18 scale)
     */
    function previewDeposit(uint256 usdcAmount, uint256 nav) external pure returns (uint256) {
        return (usdcAmount * NAV_PRECISION) / nav;
    }
    
    /**
     * @notice Preview how much USDC you'd get for redeeming (after 1% tax)
     * @param shareAmount Shares to redeem
     * @param nav NAV to use (1e18 scale)
     */
    function previewRedeem(uint256 shareAmount, uint256 nav) external pure returns (uint256 netUsdc, uint256 tax) {
        uint256 grossUsdc = (shareAmount * nav) / NAV_PRECISION;
        tax = (grossUsdc * WITHDRAWAL_TAX_BPS) / 10000;
        netUsdc = grossUsdc - tax;
    }

    // ============================================
    // View Functions
    // ============================================
    
    /**
     * @notice Get current vault state
     */
    function getVaultState() external view returns (
        uint256 _lastNav,
        uint256 _lastRoundId,
        uint256 _lastNavTimestamp,
        uint256 _totalSupply,
        uint256 _vaultBalance,
        uint256 _totalDeposited,
        bool _paused
    ) {
        return (
            lastNav,
            lastRoundId,
            lastNavTimestamp,
            totalSupply(),
            usdc.balanceOf(address(this)),
            totalDeposited,
            paused
        );
    }
    
    /**
     * @notice Get user's position
     */
    function getUserPosition(address user) external view returns (
        uint256 shares,
        uint256 deposits,
        uint256 remainingCap
    ) {
        shares = balanceOf(user);
        deposits = walletDeposits[user];
        remainingCap = maxDepositPerWallet > deposits ? maxDepositPerWallet - deposits : 0;
    }

    // ============================================
    // Owner Functions
    // ============================================
    
    /**
     * @notice Update tax collector address
     */
    function setTaxCollector(address _taxCollector) external onlyOwner {
        require(_taxCollector != address(0), "Invalid address");
        emit TaxCollectorUpdated(taxCollector, _taxCollector);
        taxCollector = _taxCollector;
    }
    
    /**
     * @notice Update deposit caps
     */
    function setCaps(uint256 _maxPerWallet, uint256 _maxTotal) external onlyOwner {
        maxDepositPerWallet = _maxPerWallet;
        maxTotalDeposits = _maxTotal;
        emit CapsUpdated(_maxPerWallet, _maxTotal);
    }
    
    /**
     * @notice Pause/unpause vault
     */
    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit Paused(_paused);
    }
    
    /**
     * @notice Emergency withdraw USDC to owner
     */
    function emergencyWithdraw(uint256 amount) external onlyOwner {
        require(amount <= usdc.balanceOf(address(this)), "Insufficient balance");
        usdc.safeTransfer(owner(), amount);
        emit EmergencyWithdraw(owner(), amount);
    }
    
    /**
     * @notice Force set lastNav (only for initialization/recovery)
     * @dev Use with caution - bypasses signature verification
     */
    function forceSetNav(uint256 _nav, uint256 _roundId) external onlyOwner {
        require(_nav > 0, "Invalid NAV");
        lastNav = _nav;
        lastRoundId = _roundId;
        lastNavTimestamp = block.timestamp;
        emit NavUpdated(_nav, _roundId, block.timestamp);
    }

    // ============================================
    // ERC20 Overrides
    // ============================================
    
    function decimals() public pure override returns (uint8) {
        return 18;
    }
}
