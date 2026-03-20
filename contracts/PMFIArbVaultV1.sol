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
 * @title PMFIArbVaultV1
 * @notice pARB vault — PMFI cross-venue arbitrage vault (Polymarket × Kalshi × Opinion Labs)
 * @dev
 *
 * Architecture:
 * - Users deposit USDC → receive pARB shares
 * - Servicer wallet receives forwarded deposits and places arb trades on both venues
 * - NAV oracle signs over (poly_cash + kalshi_cash + liquid_position_value) using bids
 * - Withdrawals are queued; claims require vault buffer to be refilled by servicer
 *
 * Key differences from pSNIPER:
 * - Domain salt: PMFIArbVaultV1.v1
 * - Token: pARB
 * - Deposits forwarded to dedicated pArb servicer wallet (not Polymarket deposit address)
 * - NAV includes poly_cash, kalshi_cash, open_positions_liquid_value, settled_pnl
 * - MIN_DEPOSIT_USDC: $10 (vs pSNIPER's $5)
 * - ArbNavData struct includes both poly and kalshi cash breakdown
 *
 * Security:
 * - 30-second NAV validity window
 * - Monotonically increasing roundId (domain-isolated from pSNIPER)
 * - Withdrawal queue with 7-day expiry
 * - Pausable deposits
 * - Total vault deposit cap
 * - Withdrawal tax BPS (configurable, default 0)
 * - Reentrancy guard
 */
contract PMFIArbVaultV1 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    // ============================================
    // Constants
    // ============================================

    bytes32 public constant NAV_TYPEHASH = keccak256(
        "ArbNavDataV1(uint256 totalAssets,uint256 polyCash,uint256 kalshiCash,uint256 openPositionsValue,uint256 settledPnl,uint256 timestamp,uint256 deadline,uint256 roundId,address vault,uint256 chainId,bytes32 domainSalt)"
    );

    /// @dev Domain salt isolates this vault from pSNIPER signatures
    bytes32 public constant DOMAIN_SALT = keccak256("PMFIArbVaultV1.v1");

    uint256 public constant MAX_NAV_AGE = 30;            // 30-second NAV validity window
    uint256 public constant MIN_NAV_INTERVAL = 25;       // 25s minimum between NAV updates
    uint256 public constant WITHDRAWAL_TAX_BPS = 0;      // No withdrawal tax
    uint256 public constant USDC_DECIMALS = 6;
    uint256 public constant NAV_PRECISION = 1e18;
    uint256 public constant WITHDRAWAL_EXPIRY = 7 days;
    uint256 public constant MIN_DEPOSIT_USDC = 10 * 1e6; // $10 minimum deposit
    uint256 public constant CLAIM_SLIPPAGE_BPS = 100;    // 1% slippage tolerance

    // ============================================
    // State Variables
    // ============================================

    IERC20 public immutable usdc;
    address public immutable navSigner;
    address public taxCollector;
    address public arbServicerWallet;  // Dedicated pArb servicer (not Polymarket deposit address)

    uint256 public lastRoundId;
    uint256 public lastNavTimestamp;

    uint256 public maxTotalDeposits;
    uint256 public expectedAssets;
    uint256 public totalForwardedToServicer;
    uint256 public correctionNonce;

    uint256 public lastAcceptedTotalAssets;
    uint256 public lastAcceptedTimestamp;

    mapping(address => uint256) public walletDeposits;

    bool public paused;
    bool public depositsThrottled;

    // ============================================
    // Withdrawal Queue
    // ============================================

    struct WithdrawalRequest {
        address user;
        uint256 shares;
        uint256 usdcLocked;
        uint256 requestTime;
        bool claimed;
    }

    WithdrawalRequest[] public withdrawalQueue;
    uint256 public nextWithdrawalIndex;
    uint256 public totalPendingShares;

    mapping(address => uint256[]) public userWithdrawals;

    // ============================================
    // Structs
    // ============================================

    struct ArbNavDataV1 {
        uint256 totalAssets;         // poly_cash + kalshi_cash + open_positions + settled_pnl
        uint256 polyCash;            // Servicer's Polymarket cash balance (USDC, 6 dec)
        uint256 kalshiCash;          // Servicer's Kalshi cash balance (USDC, 6 dec)
        uint256 openPositionsValue;  // Liquid value of open arb positions (bids, 6 dec)
        uint256 settledPnl;          // Cumulative settled PnL from closed positions (6 dec)
        uint256 timestamp;
        uint256 deadline;
        uint256 roundId;
    }

    // ============================================
    // Events
    // ============================================

    event Deposit(
        address indexed user,
        uint256 usdcAmount,
        uint256 sharesReceived,
        uint256 navUsed
    );

    event WithdrawalRequested(
        uint256 indexed requestId,
        address indexed user,
        uint256 shares,
        uint256 expectedUsdc
    );

    event WithdrawalClaimed(
        uint256 indexed requestId,
        address indexed user,
        uint256 shares,
        uint256 usdcReceived,
        uint256 taxPaid,
        uint256 navUsed
    );

    event WithdrawalCancelled(
        uint256 indexed requestId,
        address indexed user,
        uint256 sharesReturned
    );

    event NavUpdated(
        uint256 totalAssets,
        uint256 polyCash,
        uint256 kalshiCash,
        uint256 openPositionsValue,
        uint256 settledPnl,
        uint256 roundId,
        uint256 timestamp
    );

    event BufferRefilled(uint256 amount, string source);
    event TaxCollectorUpdated(address indexed oldCollector, address indexed newCollector);
    event ArbServicerWalletUpdated(address indexed oldWallet, address indexed newWallet);
    event CapsUpdated(uint256 total);
    event Paused(bool isPaused);
    event DepositsThrottled(bool isThrottled);
    event EmergencyWithdraw(address indexed to, uint256 amount);

    event CorrectionExpectedAssets(
        uint256 indexed nonce,
        uint256 oldValue,
        uint256 newValue,
        string reason
    );

    // ============================================
    // Constructor
    // ============================================

    constructor(
        address _usdc,
        address _navSigner,
        address _taxCollector,
        address _arbServicerWallet,
        uint256 _maxTotal
    ) ERC20("PMFI Arb", "pARB") Ownable(msg.sender) {
        require(_usdc != address(0), "Invalid USDC");
        require(_navSigner != address(0), "Invalid signer");
        require(_taxCollector != address(0), "Invalid tax collector");
        require(_arbServicerWallet != address(0), "Invalid servicer wallet");

        usdc = IERC20(_usdc);
        navSigner = _navSigner;
        taxCollector = _taxCollector;
        arbServicerWallet = _arbServicerWallet;
        maxTotalDeposits = _maxTotal;

        lastRoundId = 0;
        lastNavTimestamp = 0;
        expectedAssets = 0;
        totalForwardedToServicer = 0;
    }

    // ============================================
    // Modifiers
    // ============================================

    modifier whenNotPaused() {
        require(!paused, "Vault is paused");
        _;
    }

    modifier whenPaused() {
        require(paused, "Vault must be paused");
        _;
    }

    modifier whenDepositsNotThrottled() {
        require(!depositsThrottled, "Deposits throttled");
        _;
    }

    // ============================================
    // Core Functions
    // ============================================

    /**
     * @notice Deposit USDC and receive pARB shares
     * @dev 100% of deposit goes to the dedicated pArb servicer wallet
     * @param usdcAmount Amount of USDC to deposit (6 decimals, minimum $10)
     * @param navData Signed NAV data with full asset breakdown
     * @param signature Oracle signature over navData
     */
    function deposit(
        uint256 usdcAmount,
        ArbNavDataV1 calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused whenDepositsNotThrottled {
        require(usdcAmount >= MIN_DEPOSIT_USDC, "Below $10 minimum deposit");

        _verifyAndApplyNav(navData, signature);

        require(expectedAssets + usdcAmount <= maxTotalDeposits, "Exceeds total cap");

        uint256 nav = _calculateNav(navData.totalAssets);
        uint256 sharesToMint = (usdcAmount * NAV_PRECISION) / nav;
        require(sharesToMint > 0, "Shares too small");

        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);

        // Forward 100% to dedicated pArb servicer wallet
        usdc.safeTransfer(arbServicerWallet, usdcAmount);
        totalForwardedToServicer += usdcAmount;

        expectedAssets += usdcAmount;
        walletDeposits[msg.sender] += usdcAmount;

        _mint(msg.sender, sharesToMint);

        emit Deposit(msg.sender, usdcAmount, sharesToMint, nav);
    }

    /**
     * @notice Request withdrawal — shares are locked, USDC amount fixed at request time
     * @param shareAmount Amount of pARB shares to redeem
     * @param navData Signed NAV data from oracle
     * @param signature Oracle signature over navData
     * @return requestId The withdrawal request ID
     */
    function requestWithdraw(
        uint256 shareAmount,
        ArbNavDataV1 calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused returns (uint256 requestId) {
        require(shareAmount > 0, "Amount must be > 0");
        require(balanceOf(msg.sender) >= shareAmount, "Insufficient shares");

        _verifyAndApplyNav(navData, signature);

        uint256 nav = _calculateNav(navData.totalAssets);
        uint256 usdcLocked = (shareAmount * nav) / NAV_PRECISION;

        _transfer(msg.sender, address(this), shareAmount);

        requestId = withdrawalQueue.length;
        withdrawalQueue.push(WithdrawalRequest({
            user: msg.sender,
            shares: shareAmount,
            usdcLocked: usdcLocked,
            requestTime: block.timestamp,
            claimed: false
        }));

        userWithdrawals[msg.sender].push(requestId);
        totalPendingShares += shareAmount;

        emit WithdrawalRequested(requestId, msg.sender, shareAmount, usdcLocked);
    }

    /**
     * @notice Claim a pending withdrawal (after keeper refills vault buffer)
     * @dev Allowed even when paused — users can always exit
     * @param requestId The withdrawal request ID
     */
    function claim(uint256 requestId) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage req = withdrawalQueue[requestId];

        require(req.user == msg.sender, "Not your request");
        require(!req.claimed, "Already claimed");
        require(block.timestamp <= req.requestTime + WITHDRAWAL_EXPIRY, "Request expired");

        // Invalidate cached NAV to prevent stale pricing race condition
        lastRoundId += 1;

        uint256 grossUsdc = req.usdcLocked;
        uint256 vaultBalance = usdc.balanceOf(address(this));

        uint256 minRequired = grossUsdc - (grossUsdc * CLAIM_SLIPPAGE_BPS) / 10000;
        require(
            vaultBalance >= minRequired,
            "Insufficient vault buffer. Try again when positions are liquidated."
        );

        uint256 actualGross = vaultBalance >= grossUsdc ? grossUsdc : vaultBalance;
        uint256 tax = (actualGross * WITHDRAWAL_TAX_BPS) / 10000;
        uint256 netUsdc = actualGross - tax;

        req.claimed = true;
        totalPendingShares -= req.shares;

        if (requestId == nextWithdrawalIndex) {
            while (nextWithdrawalIndex < withdrawalQueue.length &&
                   withdrawalQueue[nextWithdrawalIndex].claimed) {
                nextWithdrawalIndex++;
            }
        }

        _burn(address(this), req.shares);

        if (grossUsdc > expectedAssets) {
            expectedAssets = 0;
        } else {
            expectedAssets -= grossUsdc;
        }

        uint256 depositReduction = (walletDeposits[msg.sender] * req.shares) /
            (balanceOf(msg.sender) + req.shares + 1);
        if (depositReduction > walletDeposits[msg.sender]) {
            depositReduction = walletDeposits[msg.sender];
        }
        walletDeposits[msg.sender] -= depositReduction;

        usdc.safeTransfer(taxCollector, tax);
        usdc.safeTransfer(msg.sender, netUsdc);

        uint256 effectiveNav = (grossUsdc * NAV_PRECISION) / req.shares;
        emit WithdrawalClaimed(requestId, msg.sender, req.shares, netUsdc, tax, effectiveNav);
    }

    /**
     * @notice Cancel an expired withdrawal request and get shares back
     */
    function cancelExpiredWithdrawal(uint256 requestId) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage req = withdrawalQueue[requestId];

        require(req.user == msg.sender, "Not your request");
        require(!req.claimed, "Already claimed");
        require(block.timestamp > req.requestTime + WITHDRAWAL_EXPIRY, "Not expired yet");

        req.claimed = true;
        totalPendingShares -= req.shares;

        _transfer(address(this), msg.sender, req.shares);

        emit WithdrawalCancelled(requestId, msg.sender, req.shares);
    }

    // ============================================
    // NAV Verification
    // ============================================

    function _verifyAndApplyNav(ArbNavDataV1 calldata navData, bytes calldata signature) internal {
        require(block.timestamp <= navData.deadline, "NAV expired");
        require(block.timestamp - navData.timestamp <= MAX_NAV_AGE, "NAV too old");
        require(navData.roundId > lastRoundId, "RoundId must increase");
        require(
            navData.timestamp >= lastNavTimestamp + MIN_NAV_INTERVAL || lastNavTimestamp == 0,
            "NAV update too frequent"
        );

        // Verify asset breakdown adds up
        uint256 computedTotal = navData.polyCash + navData.kalshiCash +
                                navData.openPositionsValue + navData.settledPnl;
        require(computedTotal == navData.totalAssets, "Asset breakdown mismatch");

        // Verify ECDSA signature (includes chainId and domain salt for isolation)
        bytes32 structHash = keccak256(abi.encode(
            NAV_TYPEHASH,
            navData.totalAssets,
            navData.polyCash,
            navData.kalshiCash,
            navData.openPositionsValue,
            navData.settledPnl,
            navData.timestamp,
            navData.deadline,
            navData.roundId,
            address(this),
            block.chainid,
            DOMAIN_SALT
        ));
        bytes32 digest = structHash.toEthSignedMessageHash();
        address signer = digest.recover(signature);

        require(signer == navSigner, "Invalid NAV signer");

        lastRoundId = navData.roundId;
        lastNavTimestamp = navData.timestamp;
        lastAcceptedTotalAssets = navData.totalAssets;
        lastAcceptedTimestamp = block.timestamp;

        emit NavUpdated(
            navData.totalAssets,
            navData.polyCash,
            navData.kalshiCash,
            navData.openPositionsValue,
            navData.settledPnl,
            navData.roundId,
            navData.timestamp
        );
    }

    function _calculateNav(uint256 totalAssets) internal view returns (uint256) {
        uint256 supply = totalSupply();
        if (supply == 0) {
            return 10**6; // Initial NAV: $1.00 per share
        }
        return (totalAssets * NAV_PRECISION) / supply;
    }

    // ============================================
    // View Functions
    // ============================================

    function previewDeposit(uint256 usdcAmount, uint256 totalAssets) external view returns (uint256 shares) {
        uint256 nav = _calculateNav(totalAssets);
        shares = (usdcAmount * NAV_PRECISION) / nav;
    }

    function previewRedeem(uint256 shareAmount, uint256 totalAssets) external view returns (uint256 netUsdc, uint256 tax) {
        uint256 nav = _calculateNav(totalAssets);
        uint256 grossUsdc = (shareAmount * nav) / NAV_PRECISION;
        tax = (grossUsdc * WITHDRAWAL_TAX_BPS) / 10000;
        netUsdc = grossUsdc - tax;
    }

    function getVaultState() external view returns (
        uint256 _lastRoundId,
        uint256 _lastNavTimestamp,
        uint256 _totalSupply,
        uint256 _vaultBuffer,
        uint256 _expectedAssets,
        uint256 _totalForwarded,
        uint256 _totalPendingShares,
        uint256 _pendingWithdrawalsCount,
        bool _paused,
        bool _depositsThrottled
    ) {
        uint256 pendingCount = 0;
        for (uint256 i = nextWithdrawalIndex; i < withdrawalQueue.length; i++) {
            if (!withdrawalQueue[i].claimed) pendingCount++;
        }

        return (
            lastRoundId,
            lastNavTimestamp,
            totalSupply(),
            usdc.balanceOf(address(this)),
            expectedAssets,
            totalForwardedToServicer,
            totalPendingShares,
            pendingCount,
            paused,
            depositsThrottled
        );
    }

    function getUserWithdrawals(address user) external view returns (uint256[] memory) {
        return userWithdrawals[user];
    }

    function getWithdrawalRequest(uint256 requestId) external view returns (
        address user,
        uint256 shares,
        uint256 usdcLocked,
        uint256 requestTime,
        bool claimed,
        bool expired
    ) {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage r = withdrawalQueue[requestId];
        return (
            r.user,
            r.shares,
            r.usdcLocked,
            r.requestTime,
            r.claimed,
            block.timestamp > r.requestTime + WITHDRAWAL_EXPIRY
        );
    }

    // ============================================
    // Owner Functions
    // ============================================

    /**
     * @notice Refill vault buffer for withdrawal claims
     * @dev Called by keeper when pulling funds back from servicer
     */
    function refillBuffer(uint256 amount, bool isReconciliation) external onlyOwner {
        usdc.safeTransferFrom(msg.sender, address(this), amount);

        if (isReconciliation && amount <= totalForwardedToServicer) {
            totalForwardedToServicer -= amount;
        }

        emit BufferRefilled(amount, isReconciliation ? "reconciliation" : "owner");
    }

    function setTaxCollector(address _taxCollector) external onlyOwner {
        require(_taxCollector != address(0), "Invalid address");
        emit TaxCollectorUpdated(taxCollector, _taxCollector);
        taxCollector = _taxCollector;
    }

    function setArbServicerWallet(address _arbServicerWallet) external onlyOwner {
        require(_arbServicerWallet != address(0), "Invalid address");
        emit ArbServicerWalletUpdated(arbServicerWallet, _arbServicerWallet);
        arbServicerWallet = _arbServicerWallet;
    }

    function setCap(uint256 _maxTotal) external onlyOwner {
        maxTotalDeposits = _maxTotal;
        emit CapsUpdated(_maxTotal);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit Paused(_paused);
    }

    function setDepositsThrottled(bool _throttled) external onlyOwner {
        depositsThrottled = _throttled;
        emit DepositsThrottled(_throttled);
    }

    function emergencyWithdraw(address to, uint256 amount) external onlyOwner {
        require(to != address(0), "Invalid address");
        usdc.safeTransfer(to, amount);
        emit EmergencyWithdraw(to, amount);
    }

    function setExpectedAssets(uint256 _expectedAssets, string calldata reason) external onlyOwner whenPaused {
        uint256 oldValue = expectedAssets;
        expectedAssets = _expectedAssets;
        correctionNonce++;
        emit CorrectionExpectedAssets(correctionNonce, oldValue, _expectedAssets, reason);
    }
}
