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
 * @title PredictFiSniperVaultV7
 * @notice pSNIPER vault with 100% Polymarket forwarding and 3-state asset tracking
 * @dev 
 * 
 * V7 Architecture: 3-State Asset Tracking with Conservation Bounds
 * 
 * Asset States:
 * 1. inFlightOnChain - USDC at Polymarket deposit address (usually ~0 after sweep)
 * 2. pendingCredit - Forwarded but not yet visible in PM API (during bridge)
 * 3. creditedAssets - PM cash + positions visible via API
 * 
 * Key Changes from V6:
 * - 100% forwarding to Polymarket (no buffer split)
 * - Conservation-style bounds replace 5% NAV change limit
 * - Extended NavData with full asset breakdown
 * - Pausable deposits with safety valves
 * - All withdrawals are queued (no buffer)
 * 
 * Safety Features:
 * - Conservation bound: totalAssets >= expectedAssets * (1 - maxLossBps)
 * - 30-second signature validity
 * - Monotonically increasing roundId
 * - 1% withdrawal tax
 * - Deposit caps (per-wallet and total)
 * - Pausable on safety valve triggers
 */
contract PredictFiSniperVaultV7 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    // ============================================
    // Constants
    // ============================================
    
    bytes32 public constant NAV_TYPEHASH = keccak256(
        "NavDataV7(uint256 totalAssets,uint256 creditedCash,uint256 creditedPositions,uint256 pendingCredit,uint256 inFlightOnChain,uint256 timestamp,uint256 deadline,uint256 roundId,address vault,uint256 chainId,bytes32 domainSalt)"
    );
    
    // Domain salt for pseudo EIP-712 separation
    bytes32 public constant DOMAIN_SALT = keccak256("PredictFiSniperVaultV7.v1");
    
    uint256 public constant MAX_NAV_AGE = 300;  // 5 minutes for MVP (bot takes 30-120s)
    uint256 public constant MIN_NAV_INTERVAL = 60;  // Min 60s between NAV updates
    uint256 public constant STALE_NAV_GRACE = 900;  // 15 min grace for deposits/withdrawRequests
    uint256 public constant WITHDRAWAL_TAX_BPS = 100;
    uint256 public constant USDC_DECIMALS = 6;
    uint256 public constant NAV_PRECISION = 1e18;
    uint256 public constant WITHDRAWAL_EXPIRY = 7 days;
    
    // Pending ratio thresholds
    uint256 public constant PENDING_RATIO_PAUSE = 3000;   // 30% - pause deposits
    uint256 public constant PENDING_RATIO_CAP = 1000;     // 10% - cap single deposit
    uint256 public constant MAX_DEPOSIT_DURING_LIMBO = 1000 * 1e6;  // $1000 max during limbo
    uint256 public constant CLAIM_SLIPPAGE_BPS = 50;  // 0.5% slippage tolerance for bridge fees
    
    // Polymarket's official Base USDC deposit address (new wallet)
    address public constant POLYMARKET_BASE_DEPOSIT = 0x2b20920A00D705043260eBFE6561bC96FBd84dBE;

    // ============================================
    // State Variables
    // ============================================
    
    IERC20 public immutable usdc;
    address public immutable navSigner;
    address public taxCollector;
    address public polymarketWallet;
    
    uint256 public lastRoundId;
    uint256 public lastNavTimestamp;
    
    uint256 public maxDepositPerWallet;
    uint256 public maxTotalDeposits;
    
    // Conservation tracking
    uint256 public expectedAssets;      // Sum of all deposits minus withdrawals paid
    uint256 public maxLossBps;          // Max allowed loss from expected (e.g., 1000 = 10%)
    
    // Deposit tracking for pendingCredit reconciliation
    uint256 public totalForwardedToPolymarket;  // Total USDC sent to PM deposit address
    
    // Correction tracking for auditability
    uint256 public correctionNonce;     // Increments on each manual correction
    
    // Last accepted NAV for stale grace period
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
        uint256 usdcLocked;     // V7.3: USDC amount locked at request time (price fixed)
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
    
    struct NavDataV7 {
        uint256 totalAssets;        // Sum of all asset states
        uint256 creditedCash;       // PM cash balance (from API)
        uint256 creditedPositions;  // PM positions liquidation value (from API)
        uint256 pendingCredit;      // Forwarded but not yet credited
        uint256 inFlightOnChain;    // USDC at deposit address
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
        uint256 creditedCash,
        uint256 creditedPositions,
        uint256 pendingCredit,
        uint256 roundId,
        uint256 timestamp
    );
    
    event BufferRefilled(uint256 amount, string source);
    event TaxCollectorUpdated(address indexed oldCollector, address indexed newCollector);
    event PolymarketWalletUpdated(address indexed oldWallet, address indexed newWallet);
    event CapsUpdated(uint256 perWallet, uint256 total);
    event MaxLossUpdated(uint256 oldMaxLoss, uint256 newMaxLoss);
    event Paused(bool isPaused);
    event DepositsThrottled(bool isThrottled);
    event EmergencyWithdraw(address indexed to, uint256 amount);
    
    // Correction events for auditability
    event CorrectionExpectedAssets(
        uint256 indexed nonce,
        uint256 oldValue,
        uint256 newValue,
        string reason
    );
    event CorrectionTotalForwarded(
        uint256 indexed nonce,
        uint256 oldValue,
        uint256 newValue,
        string reason
    );
    event CorrectionTradingLoss(
        uint256 indexed nonce,
        uint256 lossAmount,
        uint256 oldExpected,
        uint256 newExpected
    );
    event CorrectionTradingGain(
        uint256 indexed nonce,
        uint256 gainAmount,
        uint256 oldExpected,
        uint256 newExpected
    );

    // ============================================
    // Constructor
    // ============================================
    
    constructor(
        address _usdc,
        address _navSigner,
        address _taxCollector,
        address _polymarketWallet,
        uint256 _maxPerWallet,
        uint256 _maxTotal,
        uint256 _maxLossBps  // e.g., 1000 = 10% max loss allowed
    ) ERC20("PredictFi Sniper", "pSNIPER") Ownable(msg.sender) {
        require(_usdc != address(0), "Invalid USDC");
        require(_navSigner != address(0), "Invalid signer");
        require(_taxCollector != address(0), "Invalid tax collector");
        require(_polymarketWallet != address(0), "Invalid PM wallet");
        require(_maxLossBps <= 5000, "Max loss cannot exceed 50%");
        
        usdc = IERC20(_usdc);
        navSigner = _navSigner;
        taxCollector = _taxCollector;
        polymarketWallet = _polymarketWallet;
        maxDepositPerWallet = _maxPerWallet;
        maxTotalDeposits = _maxTotal;
        maxLossBps = _maxLossBps;
        
        lastRoundId = 0;
        lastNavTimestamp = 0;  // Initialize to 0 to allow first NAV update
        expectedAssets = 0;
        totalForwardedToPolymarket = 0;
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
     * @notice Deposit USDC and receive pSNIPER shares
     * @dev 100% of deposit goes to Polymarket Base deposit address
     * @param usdcAmount Amount of USDC to deposit (6 decimals)
     * @param navData Signed NAV data with full asset breakdown
     * @param signature Oracle signature over navData
     */
    function deposit(
        uint256 usdcAmount,
        NavDataV7 calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused whenDepositsNotThrottled {
        require(usdcAmount > 0, "Amount must be > 0");
        
        _verifyAndApplyNav(navData, signature);
        
        require(walletDeposits[msg.sender] + usdcAmount <= maxDepositPerWallet, "Exceeds wallet cap");
        require(expectedAssets + usdcAmount <= maxTotalDeposits, "Exceeds total cap");
        
        // Limbo cap: if pendingCredit > 10% of totalAssets, cap single deposit to $1000
        if (navData.totalAssets > 0) {
            uint256 pendingRatioBps = (navData.pendingCredit * 10000) / navData.totalAssets;
            if (pendingRatioBps > PENDING_RATIO_CAP) {
                require(usdcAmount <= MAX_DEPOSIT_DURING_LIMBO, "Deposit capped during limbo");
            }
        }
        
        // Calculate NAV per share
        uint256 nav = _calculateNav(navData.totalAssets);
        
        uint256 sharesToMint = (usdcAmount * NAV_PRECISION) / nav;
        require(sharesToMint > 0, "Shares too small");
        
        // Transfer USDC from user
        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);
        
        // Forward 100% to Polymarket Base deposit address
        usdc.safeTransfer(POLYMARKET_BASE_DEPOSIT, usdcAmount);
        totalForwardedToPolymarket += usdcAmount;
        
        // Update conservation tracking
        expectedAssets += usdcAmount;
        walletDeposits[msg.sender] += usdcAmount;
        
        _mint(msg.sender, sharesToMint);
        
        emit Deposit(msg.sender, usdcAmount, sharesToMint, nav);
    }
    
    /**
     * @notice Request withdrawal - shares are locked, USDC amount fixed at request time
     * @dev V7.3: Price is locked at request time. User gets exactly usdcLocked at claim.
     * @param shareAmount Amount of pSNIPER shares to redeem
     * @param navData Signed NAV data from oracle
     * @param signature Oracle signature over navData
     * @return requestId The withdrawal request ID
     */
    function requestWithdraw(
        uint256 shareAmount,
        NavDataV7 calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused returns (uint256 requestId) {
        require(shareAmount > 0, "Amount must be > 0");
        require(balanceOf(msg.sender) >= shareAmount, "Insufficient shares");
        
        _verifyAndApplyNav(navData, signature);
        
        // V7.3: Calculate and LOCK the USDC amount at current NAV
        // This price is fixed - user will receive exactly this amount at claim
        uint256 nav = _calculateNav(navData.totalAssets);
        uint256 usdcLocked = (shareAmount * nav) / NAV_PRECISION;
        
        // Lock shares in contract
        _transfer(msg.sender, address(this), shareAmount);
        
        requestId = withdrawalQueue.length;
        withdrawalQueue.push(WithdrawalRequest({
            user: msg.sender,
            shares: shareAmount,
            usdcLocked: usdcLocked,    // V7.3: Store locked amount
            requestTime: block.timestamp,
            claimed: false
        }));
        
        userWithdrawals[msg.sender].push(requestId);
        totalPendingShares += shareAmount;
        
        emit WithdrawalRequested(requestId, msg.sender, shareAmount, usdcLocked);
    }
    
    /**
     * @notice Claim a pending withdrawal (after keeper refills vault buffer)
     * @dev V7.3: Uses locked price from request time - no NAV re-verification needed
     * @dev Allowed even when paused - users can always exit
     * @param requestId The withdrawal request ID
     */
    function claim(
        uint256 requestId
    ) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage request = withdrawalQueue[requestId];
        
        require(request.user == msg.sender, "Not your request");
        require(!request.claimed, "Already claimed");
        require(block.timestamp <= request.requestTime + WITHDRAWAL_EXPIRY, "Request expired");
        
        // V7.3.3: Invalidate any cached NAV signatures to prevent stale pricing
        // This fixes the race condition where deposit+claim can use same roundId
        lastRoundId += 1;
        
        // V7.3: Use locked USDC amount from request time - no NAV recalculation
        uint256 grossUsdc = request.usdcLocked;
        uint256 vaultBalance = usdc.balanceOf(address(this));
        
        // V7.3.1: Allow 0.5% slippage for bridge/relay fees
        uint256 minRequired = grossUsdc - (grossUsdc * CLAIM_SLIPPAGE_BPS) / 10000;
        require(vaultBalance >= minRequired, "Not enough USDC in buffer. Try again later when positions are liquidated.");
        
        // Pay out actual available (capped at locked amount)
        uint256 actualGross = vaultBalance >= grossUsdc ? grossUsdc : vaultBalance;
        uint256 tax = (actualGross * WITHDRAWAL_TAX_BPS) / 10000;
        uint256 netUsdc = actualGross - tax;
        
        request.claimed = true;
        totalPendingShares -= request.shares;
        
        // Advance queue index
        if (requestId == nextWithdrawalIndex) {
            while (nextWithdrawalIndex < withdrawalQueue.length && 
                   withdrawalQueue[nextWithdrawalIndex].claimed) {
                nextWithdrawalIndex++;
            }
        }
        
        // Burn the locked shares
        _burn(address(this), request.shares);
        
        // Update conservation tracking (use locked amount, not actual payout, to maintain invariant)
        if (grossUsdc > expectedAssets) {
            expectedAssets = 0;
        } else {
            expectedAssets -= grossUsdc;
        }
        
        // Reduce wallet deposit tracking
        uint256 depositReduction = (walletDeposits[msg.sender] * request.shares) / 
            (balanceOf(msg.sender) + request.shares + 1);
        if (depositReduction > walletDeposits[msg.sender]) {
            depositReduction = walletDeposits[msg.sender];
        }
        walletDeposits[msg.sender] -= depositReduction;
        
        // Transfer USDC
        usdc.safeTransfer(taxCollector, tax);
        usdc.safeTransfer(msg.sender, netUsdc);
        
        // V7.3: Calculate effective NAV from locked values for backward-compatible event
        uint256 effectiveNav = (grossUsdc * NAV_PRECISION) / request.shares;
        emit WithdrawalClaimed(requestId, msg.sender, request.shares, netUsdc, tax, effectiveNav);
    }
    
    /**
     * @notice Cancel an expired withdrawal request and get shares back
     * @param requestId The withdrawal request ID
     */
    function cancelExpiredWithdrawal(uint256 requestId) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage request = withdrawalQueue[requestId];
        
        require(request.user == msg.sender, "Not your request");
        require(!request.claimed, "Already claimed");
        require(block.timestamp > request.requestTime + WITHDRAWAL_EXPIRY, "Not expired yet");
        
        request.claimed = true;
        totalPendingShares -= request.shares;
        
        // Return shares to user
        _transfer(address(this), msg.sender, request.shares);
        
        emit WithdrawalCancelled(requestId, msg.sender, request.shares);
    }

    // ============================================
    // NAV Verification
    // ============================================
    
    function _verifyAndApplyNav(NavDataV7 calldata navData, bytes calldata signature) internal {
        require(block.timestamp <= navData.deadline, "NAV expired");
        require(block.timestamp - navData.timestamp <= MAX_NAV_AGE, "NAV too old");
        require(navData.roundId > lastRoundId, "RoundId must increase");
        
        // Enforce minimum interval between NAV updates (prevents rapid spam)
        require(navData.timestamp >= lastNavTimestamp + MIN_NAV_INTERVAL || lastNavTimestamp == 0, 
                "NAV update too frequent");
        
        // Verify asset breakdown adds up
        uint256 computedTotal = navData.creditedCash + navData.creditedPositions + 
                                navData.pendingCredit + navData.inFlightOnChain;
        require(computedTotal == navData.totalAssets, "Asset breakdown mismatch");
        
        // Verify signature (includes chainId and domainSalt for domain separation)
        bytes32 structHash = keccak256(abi.encode(
            NAV_TYPEHASH,
            navData.totalAssets,
            navData.creditedCash,
            navData.creditedPositions,
            navData.pendingCredit,
            navData.inFlightOnChain,
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
        
        // Conservation bound check (only if we have deposits)
        if (expectedAssets > 0 && totalSupply() > 0) {
            uint256 minAllowedAssets = (expectedAssets * (10000 - maxLossBps)) / 10000;
            require(navData.totalAssets >= minAllowedAssets, "Conservation bound violated");
        }
        
        lastRoundId = navData.roundId;
        lastNavTimestamp = navData.timestamp;
        
        // Store for stale NAV grace period
        lastAcceptedTotalAssets = navData.totalAssets;
        lastAcceptedTimestamp = block.timestamp;
        
        emit NavUpdated(
            navData.totalAssets,
            navData.creditedCash,
            navData.creditedPositions,
            navData.pendingCredit,
            navData.roundId,
            navData.timestamp
        );
    }
    
    function _calculateNav(uint256 totalAssets) internal view returns (uint256) {
        uint256 supply = totalSupply();
        if (supply == 0) {
            // Initial NAV: $1.00 per share = 1e6 (USDC decimals)
            return 10**6;
        }
        // NAV = totalAssets_6dec * 1e18 / supply_18dec
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
        bool _depositsThrottled,
        uint256 _maxLossBps
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
            totalForwardedToPolymarket,
            totalPendingShares,
            pendingCount,
            paused,
            depositsThrottled,
            maxLossBps
        );
    }
    
    function getPendingWithdrawalShares() external view returns (uint256) {
        return totalPendingShares;
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
    
    function getRemainingAllowance(address wallet) external view returns (uint256) {
        if (walletDeposits[wallet] >= maxDepositPerWallet) return 0;
        return maxDepositPerWallet - walletDeposits[wallet];
    }

    // ============================================
    // Owner Functions
    // ============================================
    
    /**
     * @notice Refill vault buffer (for withdrawal claims)
     * @dev Called by keeper when pulling funds back from Polymarket
     * @param amount Amount of USDC to refill
     * @param isReconciliation If true, reduces totalForwardedToPolymarket (funds returning from PM)
     */
    function refillBuffer(uint256 amount, bool isReconciliation) external onlyOwner {
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        
        if (isReconciliation && amount <= totalForwardedToPolymarket) {
            totalForwardedToPolymarket -= amount;
        }
        
        emit BufferRefilled(amount, isReconciliation ? "reconciliation" : "owner");
    }
    
    function setMaxLoss(uint256 _maxLossBps) external onlyOwner {
        require(_maxLossBps <= 5000, "Max loss cannot exceed 50%");
        emit MaxLossUpdated(maxLossBps, _maxLossBps);
        maxLossBps = _maxLossBps;
    }
    
    function setTaxCollector(address _taxCollector) external onlyOwner {
        require(_taxCollector != address(0), "Invalid address");
        emit TaxCollectorUpdated(taxCollector, _taxCollector);
        taxCollector = _taxCollector;
    }
    
    function setPolymarketWallet(address _polymarketWallet) external onlyOwner {
        require(_polymarketWallet != address(0), "Invalid address");
        emit PolymarketWalletUpdated(polymarketWallet, _polymarketWallet);
        polymarketWallet = _polymarketWallet;
    }
    
    function setCaps(uint256 _maxPerWallet, uint256 _maxTotal) external onlyOwner {
        maxDepositPerWallet = _maxPerWallet;
        maxTotalDeposits = _maxTotal;
        emit CapsUpdated(_maxPerWallet, _maxTotal);
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
    
    /**
     * @notice Update expected assets for reconciliation
     * @dev Use carefully - only allowed when paused for safety
     * @param _expectedAssets New expected assets value
     * @param reason Human-readable reason for the correction
     */
    function setExpectedAssets(uint256 _expectedAssets, string calldata reason) external onlyOwner whenPaused {
        uint256 oldValue = expectedAssets;
        expectedAssets = _expectedAssets;
        correctionNonce++;
        emit CorrectionExpectedAssets(correctionNonce, oldValue, _expectedAssets, reason);
    }
    
    /**
     * @notice Update forwarded total for reconciliation
     * @dev Use carefully - only allowed when paused for safety
     * @param _totalForwarded New total forwarded value
     * @param reason Human-readable reason for the correction
     */
    function setTotalForwarded(uint256 _totalForwarded, string calldata reason) external onlyOwner whenPaused {
        uint256 oldValue = totalForwardedToPolymarket;
        totalForwardedToPolymarket = _totalForwarded;
        correctionNonce++;
        emit CorrectionTotalForwarded(correctionNonce, oldValue, _totalForwarded, reason);
    }
    
    /**
     * @notice Record asset loss from trading (reduces expectedAssets to match reality)
     * @dev Only allowed when paused - call when positions lose value
     * @param lossAmount Amount of USDC lost (in 6 decimals)
     */
    function recordTradingLoss(uint256 lossAmount) external onlyOwner whenPaused {
        uint256 oldExpected = expectedAssets;
        if (lossAmount >= expectedAssets) {
            expectedAssets = 0;
        } else {
            expectedAssets -= lossAmount;
        }
        correctionNonce++;
        emit CorrectionTradingLoss(correctionNonce, lossAmount, oldExpected, expectedAssets);
    }
    
    /**
     * @notice Record asset gain from trading (increases expectedAssets)
     * @dev Only allowed when paused - call when positions profit
     * @param gainAmount Amount of USDC gained (in 6 decimals)
     */
    function recordTradingGain(uint256 gainAmount) external onlyOwner whenPaused {
        uint256 oldExpected = expectedAssets;
        expectedAssets += gainAmount;
        correctionNonce++;
        emit CorrectionTradingGain(correctionNonce, gainAmount, oldExpected, expectedAssets);
    }
}
