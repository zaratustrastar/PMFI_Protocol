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
 * @title PredictFiSniperVaultV5
 * @notice pSNIPER vault with async withdrawals and permissionless rebalancing
 * @dev 
 * 
 * Key V5 Features (Refined):
 * - Permissionless investIdle(): Anyone can rebalance idle funds to Polymarket
 * - Async Withdrawals: requestWithdraw → watchdog refills → claim
 * - Claim-time NAV: Users get current pro-rata value (simpler, no stored NAV)
 * - Signed NAV: Bot signs NAV off-chain, users include signature in tx
 * - 1% withdrawal tax to deployer
 * - Configurable per-wallet and total deposit caps
 * - Claim allowed even when paused (users can always exit)
 */
contract PredictFiSniperVaultV5 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    // ============================================
    // Constants
    // ============================================
    
    bytes32 public constant NAV_TYPEHASH = keccak256(
        "NavData(uint256 nav,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)"
    );
    
    uint256 public constant MAX_NAV_AGE = 30;
    uint256 public constant MAX_NAV_CHANGE_BPS = 500;
    uint256 public constant WITHDRAWAL_TAX_BPS = 100;
    uint256 public constant USDC_DECIMALS = 6;
    uint256 public constant NAV_PRECISION = 1e18;
    
    uint256 public constant WITHDRAWAL_EXPIRY = 7 days;

    // ============================================
    // State Variables
    // ============================================
    
    IERC20 public immutable usdc;
    address public immutable navSigner;
    address public taxCollector;
    address public polymarketWallet;
    
    uint256 public lastNav;
    uint256 public lastRoundId;
    uint256 public lastNavTimestamp;
    
    uint256 public maxDepositPerWallet;
    uint256 public maxTotalDeposits;
    uint256 public totalDeposited;
    
    uint256 public targetBuffer;
    
    mapping(address => uint256) public walletDeposits;
    
    bool public paused;
    bool public depositsThrottled;

    // ============================================
    // Withdrawal Queue
    // ============================================
    
    struct WithdrawalRequest {
        address user;
        uint256 shares;
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
    
    struct NavData {
        uint256 nav;
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
        uint256 shares
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
    
    event BufferRefilled(uint256 amount, string source);
    event IdleFundsInvested(uint256 amount, address indexed caller);
    event NavUpdated(uint256 newNav, uint256 roundId, uint256 timestamp);
    event TaxCollectorUpdated(address indexed oldCollector, address indexed newCollector);
    event PolymarketWalletUpdated(address indexed oldWallet, address indexed newWallet);
    event CapsUpdated(uint256 perWallet, uint256 total);
    event TargetBufferUpdated(uint256 oldBuffer, uint256 newBuffer);
    event Paused(bool isPaused);
    event DepositsThrottled(bool isThrottled);
    event EmergencyWithdraw(address indexed to, uint256 amount);

    // ============================================
    // Constructor
    // ============================================
    
    constructor(
        address _usdc,
        address _navSigner,
        address _taxCollector,
        address _polymarketWallet,
        uint256 _initialNav,
        uint256 _maxPerWallet,
        uint256 _maxTotal,
        uint256 _targetBuffer
    ) ERC20("PredictFi Sniper", "pSNIPER") Ownable(msg.sender) {
        require(_usdc != address(0), "Invalid USDC");
        require(_navSigner != address(0), "Invalid signer");
        require(_taxCollector != address(0), "Invalid tax collector");
        require(_polymarketWallet != address(0), "Invalid PM wallet");
        require(_initialNav > 0, "Invalid initial NAV");
        
        usdc = IERC20(_usdc);
        navSigner = _navSigner;
        taxCollector = _taxCollector;
        polymarketWallet = _polymarketWallet;
        lastNav = _initialNav;
        lastRoundId = 0;
        lastNavTimestamp = block.timestamp;
        maxDepositPerWallet = _maxPerWallet;
        maxTotalDeposits = _maxTotal;
        targetBuffer = _targetBuffer;
    }

    // ============================================
    // Modifiers
    // ============================================
    
    modifier whenNotPaused() {
        require(!paused, "Vault is paused");
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
     * @notice Deposit USDC and receive pSNIPE shares
     * @dev All USDC stays in vault; call investIdle() to rebalance
     * @param usdcAmount Amount of USDC to deposit (6 decimals)
     * @param navData Signed NAV data from oracle
     * @param signature Oracle signature over navData
     */
    function deposit(
        uint256 usdcAmount,
        NavData calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused whenDepositsNotThrottled {
        require(usdcAmount > 0, "Amount must be > 0");
        
        _verifyAndApplyNav(navData, signature);
        
        require(walletDeposits[msg.sender] + usdcAmount <= maxDepositPerWallet, "Exceeds wallet cap");
        require(totalDeposited + usdcAmount <= maxTotalDeposits, "Exceeds total cap");
        
        uint256 sharesToMint = (usdcAmount * NAV_PRECISION) / navData.nav;
        require(sharesToMint > 0, "Shares too small");
        
        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);
        
        walletDeposits[msg.sender] += usdcAmount;
        totalDeposited += usdcAmount;
        
        _mint(msg.sender, sharesToMint);
        
        emit Deposit(msg.sender, usdcAmount, sharesToMint, navData.nav);
    }
    
    /**
     * @notice Permissionless: Send idle funds above targetBuffer to Polymarket
     * @dev Anyone can call this to rebalance the vault
     * @return amountSent Amount of USDC sent to Polymarket wallet
     */
    function investIdle() external nonReentrant returns (uint256 amountSent) {
        uint256 balance = usdc.balanceOf(address(this));
        if (balance <= targetBuffer) return 0;
        
        amountSent = balance - targetBuffer;
        usdc.safeTransfer(polymarketWallet, amountSent);
        
        emit IdleFundsInvested(amountSent, msg.sender);
    }
    
    /**
     * @notice Request withdrawal - shares are locked, USDC paid later
     * @param shareAmount Amount of pSNIPE shares to redeem
     * @param navData Signed NAV data from oracle
     * @param signature Oracle signature over navData
     * @return requestId The withdrawal request ID
     */
    function requestWithdraw(
        uint256 shareAmount,
        NavData calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused returns (uint256 requestId) {
        require(shareAmount > 0, "Amount must be > 0");
        require(balanceOf(msg.sender) >= shareAmount, "Insufficient shares");
        
        _verifyAndApplyNav(navData, signature);
        
        _transfer(msg.sender, address(this), shareAmount);
        
        requestId = withdrawalQueue.length;
        withdrawalQueue.push(WithdrawalRequest({
            user: msg.sender,
            shares: shareAmount,
            requestTime: block.timestamp,
            claimed: false
        }));
        
        userWithdrawals[msg.sender].push(requestId);
        totalPendingShares += shareAmount;
        
        emit WithdrawalRequested(requestId, msg.sender, shareAmount);
    }
    
    /**
     * @notice Claim a pending withdrawal (after buffer is refilled)
     * @dev Uses claim-time NAV only (simpler, user gets current pro-rata share)
     * @dev Allowed even when paused - users can always exit
     * @param requestId The withdrawal request ID
     * @param navData Fresh signed NAV data
     * @param signature Oracle signature over navData
     */
    function claim(
        uint256 requestId,
        NavData calldata navData,
        bytes calldata signature
    ) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage request = withdrawalQueue[requestId];
        
        require(request.user == msg.sender, "Not your request");
        require(!request.claimed, "Already claimed");
        require(block.timestamp <= request.requestTime + WITHDRAWAL_EXPIRY, "Request expired");
        
        _verifyAndApplyNav(navData, signature);
        
        uint256 grossUsdc = (request.shares * navData.nav) / NAV_PRECISION;
        uint256 tax = (grossUsdc * WITHDRAWAL_TAX_BPS) / 10000;
        uint256 netUsdc = grossUsdc - tax;
        
        require(usdc.balanceOf(address(this)) >= grossUsdc, "Insufficient buffer");
        
        request.claimed = true;
        totalPendingShares -= request.shares;
        
        if (requestId == nextWithdrawalIndex) {
            while (nextWithdrawalIndex < withdrawalQueue.length && 
                   withdrawalQueue[nextWithdrawalIndex].claimed) {
                nextWithdrawalIndex++;
            }
        }
        
        _burn(address(this), request.shares);
        
        uint256 depositReduction = (walletDeposits[msg.sender] * request.shares) / 
            (balanceOf(msg.sender) + request.shares + 1);
        if (depositReduction > walletDeposits[msg.sender]) {
            depositReduction = walletDeposits[msg.sender];
        }
        walletDeposits[msg.sender] -= depositReduction;
        if (depositReduction > totalDeposited) {
            totalDeposited = 0;
        } else {
            totalDeposited -= depositReduction;
        }
        
        usdc.safeTransfer(taxCollector, tax);
        usdc.safeTransfer(msg.sender, netUsdc);
        
        emit WithdrawalClaimed(requestId, msg.sender, request.shares, netUsdc, tax, navData.nav);
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
        
        _transfer(address(this), msg.sender, request.shares);
        
        emit WithdrawalCancelled(requestId, msg.sender, request.shares);
    }

    // ============================================
    // NAV Verification
    // ============================================
    
    function _verifyAndApplyNav(NavData calldata navData, bytes calldata signature) internal {
        require(block.timestamp <= navData.deadline, "NAV expired");
        require(block.timestamp - navData.timestamp <= MAX_NAV_AGE, "NAV too old");
        require(navData.roundId > lastRoundId, "RoundId must increase");
        
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
        
        if (lastNav > 0 && navData.nav > 0) {
            uint256 diff = navData.nav > lastNav 
                ? navData.nav - lastNav 
                : lastNav - navData.nav;
            uint256 changePercent = (diff * 10000) / lastNav;
            require(changePercent <= MAX_NAV_CHANGE_BPS, "NAV change too large");
        }
        
        lastNav = navData.nav;
        lastRoundId = navData.roundId;
        lastNavTimestamp = navData.timestamp;
        
        emit NavUpdated(navData.nav, navData.roundId, navData.timestamp);
    }

    // ============================================
    // View Functions
    // ============================================
    
    function previewDeposit(uint256 usdcAmount, uint256 nav) external pure returns (uint256) {
        return (usdcAmount * NAV_PRECISION) / nav;
    }
    
    function previewRedeem(uint256 shareAmount, uint256 nav) external pure returns (uint256 netUsdc, uint256 tax) {
        uint256 grossUsdc = (shareAmount * nav) / NAV_PRECISION;
        tax = (grossUsdc * WITHDRAWAL_TAX_BPS) / 10000;
        netUsdc = grossUsdc - tax;
    }
    
    function getIdleBalance() external view returns (uint256 idle, uint256 buffer) {
        buffer = usdc.balanceOf(address(this));
        idle = buffer > targetBuffer ? buffer - targetBuffer : 0;
    }
    
    function getVaultState() external view returns (
        uint256 _lastNav,
        uint256 _lastRoundId,
        uint256 _lastNavTimestamp,
        uint256 _totalSupply,
        uint256 _vaultBalance,
        uint256 _totalDeposited,
        uint256 _totalPendingShares,
        uint256 _pendingWithdrawalsCount,
        bool _paused,
        bool _depositsThrottled,
        uint256 _targetBuffer
    ) {
        uint256 pendingCount = 0;
        for (uint256 i = nextWithdrawalIndex; i < withdrawalQueue.length; i++) {
            if (!withdrawalQueue[i].claimed) pendingCount++;
        }
        
        return (
            lastNav,
            lastRoundId,
            lastNavTimestamp,
            totalSupply(),
            usdc.balanceOf(address(this)),
            totalDeposited,
            totalPendingShares,
            pendingCount,
            paused,
            depositsThrottled,
            targetBuffer
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
        uint256 requestTime,
        bool claimed,
        bool expired
    ) {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage r = withdrawalQueue[requestId];
        return (
            r.user,
            r.shares,
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
    
    function refillBuffer(uint256 amount) external onlyOwner {
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        emit BufferRefilled(amount, "owner");
    }
    
    function setTargetBuffer(uint256 _targetBuffer) external onlyOwner {
        emit TargetBufferUpdated(targetBuffer, _targetBuffer);
        targetBuffer = _targetBuffer;
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
}
