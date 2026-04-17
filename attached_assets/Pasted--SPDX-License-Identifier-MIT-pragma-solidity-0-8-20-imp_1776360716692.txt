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
 * @title PMFIArbVaultV2
 * @notice pARB Vault V2 — async, report-based cross-venue arb vault
 *         (Polymarket × Kalshi × Opinion Labs)
 *
 * ═══════════════════════════════════════════════════════════════
 * ARCHITECTURE OVERVIEW
 * ═══════════════════════════════════════════════════════════════
 *
 * Key changes from V1:
 *  - No live NAV required for user deposit or redeem actions
 *  - requestDeposit / claimDeposit replaces instant deposit()
 *  - requestRedeem / claimRedeem replaces requestWithdraw / claim()
 *  - Idle USDC buffer (~10%) is kept in vault; not forwarded wholesale
 *  - 20% performance fee only on realized net profits (high-water-mark style)
 *  - report() is the ONLY official accounting checkpoint
 *  - tend() is permissionless maintenance; MUST NOT change PPS
 *  - Off-chain signed data consumed ONLY in report(), never in user paths
 *
 * ═══════════════════════════════════════════════════════════════
 * STATE MACHINE PER REQUEST
 * ═══════════════════════════════════════════════════════════════
 *
 *  DepositRequest:  PENDING → CLAIMABLE → CLAIMED
 *                              ↓ (if > MAX_REQUEST_AGE with no report)
 *                           CANCELLED  (user can cancel & get USDC back)
 *
 *  RedeemRequest:   PENDING → CLAIMABLE → CLAIMED
 *                              ↓ (if > MAX_REQUEST_AGE with no report)
 *                           CANCELLED  (user can cancel & get shares back)
 *
 * ═══════════════════════════════════════════════════════════════
 * REPORT() ORDER
 * ═══════════════════════════════════════════════════════════════
 *
 *  A. snapshot conservative reportedAssets (from signed input)
 *  B. compute backingAssetsNow = reportedAssets − pendingDeposits − claimableRedeems
 *  C. periodPnl = backingAssetsNow − lastReportedBackingAssets
 *  D. performance fee: 20% of profit above lossCarryforward
 *  E. mint fee shares to feeRecipient (dilution mechanism)
 *  F. update officialPPS
 *  G. process pending deposit requests → CLAIMABLE at newPPS
 *  H. process pending redeem requests → CLAIMABLE if idle liquidity allows (FIFO)
 *  I. update lastReportedBackingAssets
 *  J. emit Reported event
 *
 * ═══════════════════════════════════════════════════════════════
 * INVARIANTS
 * ═══════════════════════════════════════════════════════════════
 *
 *  1. totalPendingDepositAssets == sum(PENDING depositRequests .assets)
 *  2. totalClaimableRedeemAssets <= usdc.balanceOf(address(this))
 *  3. totalPendingRedeemShares == balanceOf(address(this))  [shares held in vault]
 *  4. tend() never changes officialPPS or processes requests
 *  5. report() is the only function that updates officialPPS
 *  6. lossCarryforward is monotonically decreasing on profit, never negative
 *  7. officialPPS is monotonically non-decreasing except during loss periods
 *  8. processed request conversion rates are immutable after CLAIMABLE
 *  9. idleBalance = usdc.balanceOf(this) − totalPendingDepositAssets − totalClaimableRedeemAssets
 *
 * ═══════════════════════════════════════════════════════════════
 * MIGRATION FROM V1
 * ═══════════════════════════════════════════════════════════════
 *
 *  - V1 shares (pARB V1) cannot be migrated on-chain automatically
 *  - Operators must:
 *    1. Pause V1 deposits, process all pending V1 claims
 *    2. Deploy V2, seed with initial report (reportedAssets = 0 for clean start)
 *    3. V1 shareholders redeem from V1; re-deposit into V2 if desired
 *  - V2 uses domain salt "PMFIArbVaultV2.v1" (isolated from V1 sigs)
 */
contract PMFIArbVaultV2 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    // ═══════════════════════════════════════════
    // Constants
    // ═══════════════════════════════════════════

    bytes32 public constant REPORT_TYPEHASH = keccak256(
        "ReportDataV2(uint256 reportedAssets,uint256 timestamp,uint256 deadline,uint256 nonce,address vault,uint256 chainId,bytes32 domainSalt)"
    );

    /// @dev Domain salt isolates V2 signatures from V1 and pSNIPER
    bytes32 public constant DOMAIN_SALT = keccak256("PMFIArbVaultV2.v1");

    uint256 public constant NAV_PRECISION    = 1e18;
    uint256 public constant INITIAL_PPS      = 1e6;         // $1.00 (USDC units per 1e18 shares)
    uint256 public constant MIN_DEPOSIT_USDC = 10 * 1e6;    // $10 minimum deposit
    uint256 public constant PERF_FEE_BPS     = 2000;        // 20% performance fee
    uint256 public constant MAX_REQUEST_AGE  = 30 days;     // cancel window for stale requests
    uint256 public constant MAX_REPORT_STALENESS = 1 hours; // max age of report signature
    uint256 public constant MAX_REPORT_INTERVAL  = 7 days;  // circuit breaker: pause if exceeded
    uint256 public constant MAX_REQUESTS_PER_REPORT = 100;  // gas cap per report()

    /// @dev Bootstrap shares permanently locked at dead address on deployment.
    ///      Ensures totalSupply() > 0 at all times, preventing share-price inflation attacks
    ///      where a first depositor manipulates PPS by donating USDC before any shares exist.
    ///      Value: 1000 shares (1000e18 raw). Cost to attacker to grief with dead-share dilution
    ///      equals the dead-share fraction of reported assets — negligible in practice.
    uint256 public constant BOOTSTRAP_SHARES = 1000e18;
    address public constant DEAD_ADDRESS = 0x000000000000000000000000000000000000dEaD;

    // ═══════════════════════════════════════════
    // Immutables
    // ═══════════════════════════════════════════

    IERC20  public immutable usdc;
    address public immutable reportSigner; // signs ReportDataV2

    // ═══════════════════════════════════════════
    // Configuration (owner-settable)
    // ═══════════════════════════════════════════

    address public feeRecipient;
    address public arbServicerWallet;
    uint256 public maxTotalDeposits;
    uint256 public targetIdleBps;       // default 1000 = 10%
    uint256 public minimumIdleAmount;   // absolute floor (USDC, 6 dec)
    uint256 public tendCooldown;        // min seconds between tend() calls
    uint256 public reportCooldown;      // min seconds between report() calls
    bool    public paused;
    bool    public shutdown;

    // ═══════════════════════════════════════════
    // Official Accounting
    // ═══════════════════════════════════════════

    uint256 public officialPPS;                  // USDC-per-1e18-shares (6 dec precision)
    uint256 public lastReportedBackingAssets;    // backing for outstanding shares, last report
    uint256 public lossCarryforward;             // unrecovered losses before fee can apply (6 dec)
    uint256 public lastReportNonce;
    uint256 public lastReportTimestamp;
    uint256 public lastTendTimestamp;

    // ═══════════════════════════════════════════
    // Pending / Claimable Accounting
    // ═══════════════════════════════════════════

    uint256 public totalPendingDepositAssets;   // USDC received but not yet shares (6 dec)
    uint256 public totalClaimableRedeemAssets;  // USDC reserved for processed redeems (6 dec)
    uint256 public totalPendingRedeemShares;    // shares locked in vault awaiting processing

    // ═══════════════════════════════════════════
    // Request Queues
    // ═══════════════════════════════════════════

    enum RequestStatus { PENDING, CLAIMABLE, CLAIMED, CANCELLED }

    struct DepositRequest {
        address owner;
        address receiver;
        uint256 assets;        // USDC deposited (6 dec)
        uint256 submittedAt;
        RequestStatus status;
        uint256 processedPPS;  // 0 until set by report(); shares = assets * NAV_PRECISION / processedPPS
    }

    struct RedeemRequest {
        address owner;
        address receiver;
        uint256 shares;          // shares locked (18 dec)
        uint256 submittedAt;
        RequestStatus status;
        uint256 claimableAssets; // 0 until set by report()
    }

    DepositRequest[] public depositRequests;
    RedeemRequest[]  public redeemRequests;

    uint256 public nextDepositToProcess; // FIFO pointer — report() processes from here
    uint256 public nextRedeemToProcess;  // FIFO pointer — report() processes from here

    mapping(address => uint256[]) public userDepositRequests;
    mapping(address => uint256[]) public userRedeemRequests;

    // ═══════════════════════════════════════════
    // Report Struct
    // ═══════════════════════════════════════════

    struct ReportData {
        uint256 reportedAssets; // conservative total assets (6 dec USDC)
        uint256 timestamp;
        uint256 deadline;
        uint256 nonce;          // must be strictly > lastReportNonce
    }

    // ═══════════════════════════════════════════
    // Events
    // ═══════════════════════════════════════════

    event DepositRequested(uint256 indexed requestId, address indexed owner, address indexed receiver, uint256 assets);
    event DepositClaimed(uint256 indexed requestId, address indexed receiver, uint256 shares, uint256 pps);
    event RedeemRequested(uint256 indexed requestId, address indexed owner, address indexed receiver, uint256 shares);
    event RedeemClaimed(uint256 indexed requestId, address indexed receiver, uint256 assets);
    event RequestCancelled(uint256 indexed requestId, bool isDeposit, address indexed owner);
    event Reported(
        uint256 reportedAssets,
        uint256 backingAssetsNow,
        int256  periodPnl,
        uint256 feeShares,
        uint256 feeAssets,
        uint256 newPPS,
        uint256 depositsProcessed,
        uint256 redeemsProcessed,
        uint256 nonce
    );
    event TendExecuted(uint256 amountDeployed, address indexed servicerWallet);
    event PerfFeeMinted(address indexed recipient, uint256 feeShares, uint256 feeAssets);
    event BufferRefilled(address indexed from, uint256 amount);
    event ConfigUpdated(bytes32 indexed key, uint256 value);
    event AddressUpdated(bytes32 indexed key, address value);
    event Paused(bool isPaused);
    event Shutdown(bool isShutdown);
    event EmergencyWithdraw(address indexed to, uint256 amount);

    // ═══════════════════════════════════════════
    // Constructor
    // ═══════════════════════════════════════════

    constructor(
        address _usdc,
        address _reportSigner,
        address _feeRecipient,
        address _arbServicerWallet,
        uint256 _maxTotal
    ) ERC20("PMFI Arb", "pARB") Ownable(msg.sender) {
        require(_usdc             != address(0), "Invalid USDC");
        require(_reportSigner     != address(0), "Invalid signer");
        require(_feeRecipient     != address(0), "Invalid fee recipient");
        require(_arbServicerWallet != address(0), "Invalid servicer");

        usdc               = IERC20(_usdc);
        reportSigner       = _reportSigner;
        feeRecipient       = _feeRecipient;
        arbServicerWallet  = _arbServicerWallet;
        maxTotalDeposits   = _maxTotal;

        officialPPS         = INITIAL_PPS;
        targetIdleBps       = 1000;    // 10%
        minimumIdleAmount   = 100e6;   // $100
        tendCooldown        = 60;      // 1 minute
        reportCooldown      = 3600;    // 1 hour

        // Bootstrap: mint permanent dead shares so totalSupply() is never 0.
        // Prevents first-depositor share-price inflation attacks.
        // These shares are unclaimable and will never dilute practical holders meaningfully.
        _mint(DEAD_ADDRESS, BOOTSTRAP_SHARES);
    }

    // ═══════════════════════════════════════════
    // Modifiers
    // ═══════════════════════════════════════════

    modifier whenNotPaused() {
        require(!paused, "Vault is paused");
        _;
    }

    modifier whenNotShutdown() {
        require(!shutdown, "Vault is shut down");
        _;
    }

    modifier whenShutdown() {
        require(shutdown, "Not in shutdown");
        _;
    }

    // ═══════════════════════════════════════════
    // User Flow — Deposit
    // ═══════════════════════════════════════════

    /**
     * @notice Step 1 of deposit: transfer USDC into vault and record a pending request.
     *         Shares are NOT minted until the next report() processes this request.
     * @param assets   USDC amount (6 dec, minimum $10)
     * @param receiver Address that will receive the minted shares on claimDeposit()
     * @return requestId  Use this ID to call claimDeposit() after report() processes it
     */
    function requestDeposit(uint256 assets, address receiver)
        external
        nonReentrant
        whenNotPaused
        whenNotShutdown
        returns (uint256 requestId)
    {
        require(assets >= MIN_DEPOSIT_USDC, "Below $10 minimum");
        require(receiver != address(0), "Invalid receiver");
        require(
            totalPendingDepositAssets + lastReportedBackingAssets + assets <= maxTotalDeposits,
            "Exceeds vault cap"
        );

        usdc.safeTransferFrom(msg.sender, address(this), assets);
        totalPendingDepositAssets += assets;

        requestId = depositRequests.length;
        depositRequests.push(DepositRequest({
            owner:        msg.sender,
            receiver:     receiver,
            assets:       assets,
            submittedAt:  block.timestamp,
            status:       RequestStatus.PENDING,
            processedPPS: 0
        }));
        userDepositRequests[msg.sender].push(requestId);

        emit DepositRequested(requestId, msg.sender, receiver, assets);
    }

    /**
     * @notice Step 2 of deposit: after report() has processed the request, mint shares.
     * @param requestId  The ID returned by requestDeposit()
     * @param receiver   Override receiver (must be original request receiver or owner)
     * @return shares    pARB shares minted
     */
    function claimDeposit(uint256 requestId, address receiver)
        external
        nonReentrant
        returns (uint256 shares)
    {
        require(requestId < depositRequests.length, "Invalid request");
        DepositRequest storage req = depositRequests[requestId];
        require(req.owner == msg.sender, "Not your request");
        require(req.status == RequestStatus.CLAIMABLE, "Not claimable yet");

        req.status = RequestStatus.CLAIMED;
        shares = (req.assets * NAV_PRECISION) / req.processedPPS;
        require(shares > 0, "Zero shares");

        address to = receiver != address(0) ? receiver : req.receiver;
        _mint(to, shares);

        emit DepositClaimed(requestId, to, shares, req.processedPPS);
    }

    // ═══════════════════════════════════════════
    // User Flow — Redeem
    // ═══════════════════════════════════════════

    /**
     * @notice Step 1 of redeem: lock shares in vault and record a pending request.
     *         USDC amount is NOT fixed until the next report() that has sufficient liquidity.
     * @param shares   pARB shares to redeem (18 dec)
     * @param receiver Address that will receive USDC on claimRedeem()
     * @return requestId  Use this ID to call claimRedeem() after report() processes it
     */
    function requestRedeem(uint256 shares, address receiver)
        external
        nonReentrant
        returns (uint256 requestId)
    {
        require(shares > 0, "Zero shares");
        require(balanceOf(msg.sender) >= shares, "Insufficient shares");
        require(receiver != address(0), "Invalid receiver");

        _transfer(msg.sender, address(this), shares);
        totalPendingRedeemShares += shares;

        requestId = redeemRequests.length;
        redeemRequests.push(RedeemRequest({
            owner:           msg.sender,
            receiver:        receiver,
            shares:          shares,
            submittedAt:     block.timestamp,
            status:          RequestStatus.PENDING,
            claimableAssets: 0
        }));
        userRedeemRequests[msg.sender].push(requestId);

        emit RedeemRequested(requestId, msg.sender, receiver, shares);
    }

    /**
     * @notice Step 2 of redeem: after report() has allocated liquidity, receive USDC.
     * @param requestId  The ID returned by requestRedeem()
     * @param receiver   Override receiver (must be called by original owner)
     * @return assets    USDC transferred to receiver
     */
    function claimRedeem(uint256 requestId, address receiver)
        external
        nonReentrant
        returns (uint256 assets)
    {
        require(requestId < redeemRequests.length, "Invalid request");
        RedeemRequest storage req = redeemRequests[requestId];
        require(req.owner == msg.sender, "Not your request");
        require(req.status == RequestStatus.CLAIMABLE, "Not claimable yet");

        req.status = RequestStatus.CLAIMED;
        assets = req.claimableAssets;
        totalClaimableRedeemAssets -= assets;

        address to = receiver != address(0) ? receiver : req.receiver;
        usdc.safeTransfer(to, assets);

        emit RedeemClaimed(requestId, to, assets);
    }

    // ═══════════════════════════════════════════
    // Keeper Auto-Claim Flow
    // ═══════════════════════════════════════════

    /**
     * @notice Keeper-triggered auto-claim for deposit requests.
     *         Permissionless — shares always go to the receiver stored at requestDeposit time.
     *         Silently skips requests that are not CLAIMABLE or have invalid IDs.
     *         Enables the off-chain bot to push shares to users immediately after report().
     */
    function autoClaimDeposits(uint256[] calldata requestIds) external nonReentrant {
        for (uint256 i = 0; i < requestIds.length; i++) {
            uint256 rid = requestIds[i];
            if (rid >= depositRequests.length) continue;
            DepositRequest storage req = depositRequests[rid];
            if (req.status != RequestStatus.CLAIMABLE) continue;

            uint256 shares = (req.assets * NAV_PRECISION) / req.processedPPS;
            if (shares == 0) continue;

            req.status = RequestStatus.CLAIMED;
            _mint(req.receiver, shares);
            emit DepositClaimed(rid, req.receiver, shares, req.processedPPS);
        }
    }

    /**
     * @notice Keeper-triggered auto-claim for redeem requests.
     *         Permissionless — USDC always goes to the receiver stored at requestRedeem time.
     *         Silently skips requests that are not CLAIMABLE or have invalid IDs.
     *         Enables the off-chain bot to push USDC to users immediately after report().
     */
    function autoClaimRedeems(uint256[] calldata requestIds) external nonReentrant {
        for (uint256 i = 0; i < requestIds.length; i++) {
            uint256 rid = requestIds[i];
            if (rid >= redeemRequests.length) continue;
            RedeemRequest storage req = redeemRequests[rid];
            if (req.status != RequestStatus.CLAIMABLE) continue;

            uint256 assets = req.claimableAssets;
            if (assets == 0) continue;

            req.status = RequestStatus.CLAIMED;
            totalClaimableRedeemAssets -= assets;
            usdc.safeTransfer(req.receiver, assets);
            emit RedeemClaimed(rid, req.receiver, assets);
        }
    }

    // ═══════════════════════════════════════════
    // Cancel Flow
    // ═══════════════════════════════════════════

    /**
     * @notice Cancel a stale PENDING request and recover assets/shares.
     *         Only callable by request owner after MAX_REQUEST_AGE without processing.
     *         Protects users if the bot stops running.
     */
    function cancelRequest(uint256 requestId, bool isDeposit)
        external
        nonReentrant
    {
        if (isDeposit) {
            require(requestId < depositRequests.length, "Invalid request");
            DepositRequest storage req = depositRequests[requestId];
            require(req.owner == msg.sender, "Not your request");
            require(req.status == RequestStatus.PENDING, "Not pending");
            require(block.timestamp > req.submittedAt + MAX_REQUEST_AGE, "Not yet cancelable");

            req.status = RequestStatus.CANCELLED;
            totalPendingDepositAssets -= req.assets;
            usdc.safeTransfer(msg.sender, req.assets);
        } else {
            require(requestId < redeemRequests.length, "Invalid request");
            RedeemRequest storage req = redeemRequests[requestId];
            require(req.owner == msg.sender, "Not your request");
            require(req.status == RequestStatus.PENDING, "Not pending");
            require(block.timestamp > req.submittedAt + MAX_REQUEST_AGE, "Not yet cancelable");

            req.status = RequestStatus.CANCELLED;
            totalPendingRedeemShares -= req.shares;
            _transfer(address(this), msg.sender, req.shares);
        }

        emit RequestCancelled(requestId, isDeposit, msg.sender);
    }

    // ═══════════════════════════════════════════
    // Permissionless Keeper — tend()
    // ═══════════════════════════════════════════

    /**
     * @notice Maintenance tick: transfer deployable idle capital to servicer wallet.
     *         Anyone can call after tendCooldown seconds.
     *
     *  INVARIANTS (never violated):
     *    1. tend() NEVER changes officialPPS. report() is the only accounting checkpoint.
     *    2. tend() NEVER processes deposit or redeem requests. Only report() does.
     *    3. tend() NEVER moves USDC reserved for pending redemptions.
     *
     *  Idle reservation order (strict priority):
     *    a. Pending redeem USDC  — (totalPendingRedeemShares × officialPPS) — first priority
     *    b. Target idle buffer   — max(totalAssets × targetIdleBps, minimumIdleAmount)
     *    c. Deployable excess    — everything above (a + b) flows to servicer wallet
     *
     *  Off-chain withdrawal waterfall (bot runs after tend()):
     *    1. vault idle USDC  (here)
     *    2. servicer free USDC swept back via refillBuffer()
     *    3. settled venue proceeds
     *    4. controlled position unwind (off-chain bot decision)
     */
    function tend() external nonReentrant whenNotPaused {
        require(block.timestamp >= lastTendTimestamp + tendCooldown, "Tend cooldown");
        lastTendTimestamp = block.timestamp;

        uint256 idle = _idleBalance();
        uint256 targetIdle = _targetIdleAmount();

        // PRIORITY 1: reserve USDC for all outstanding pending redemptions (at officialPPS)
        uint256 redemptionReserve = (totalPendingRedeemShares * officialPPS) / NAV_PRECISION;

        // PRIORITY 2: reserve target idle buffer on top of redemption reserve
        uint256 totalReserve = redemptionReserve + targetIdle;

        // Only transfer the true excess above both reserves
        uint256 deployable = idle > totalReserve ? idle - totalReserve : 0;

        if (deployable > 0 && arbServicerWallet != address(0)) {
            usdc.safeTransfer(arbServicerWallet, deployable);
        }

        emit TendExecuted(deployable, arbServicerWallet);
    }

    // ═══════════════════════════════════════════
    // Permissionless Keeper — report()
    // ═══════════════════════════════════════════

    /**
     * @notice Official accounting checkpoint. Updates PPS, processes request batches,
     *         and applies performance fees. Requires a valid signed ReportData.
     *
     *  reportedAssets must be conservative (cash + settled proceeds; NOT open position marks).
     *  Caller may be anyone after reportCooldown. Bot should call every ~1–6 hours.
     *
     * @param data      Conservative asset snapshot + nonce
     * @param signature ECDSA signature by reportSigner over ABI-encoded data + domain
     * @param maxDeposits  Max deposit requests to process in this call (gas limit)
     * @param maxRedeems   Max redeem requests to process in this call (gas limit)
     */
    function report(
        ReportData calldata data,
        bytes calldata signature,
        uint256 maxDeposits,
        uint256 maxRedeems
    )
        external
        nonReentrant
        whenNotPaused
    {
        require(block.timestamp >= lastReportTimestamp + reportCooldown, "Report cooldown");
        _verifyReportSignature(data, signature);

        lastReportNonce     = data.nonce;
        lastReportTimestamp = block.timestamp;

        // ── A. Snapshot reportedAssets ────────────────────────────────────
        uint256 reportedAssets = data.reportedAssets;

        // ── B. Compute backingAssetsNow ───────────────────────────────────
        // Excludes pending deposits (not yet shares) and claimable redeems (already reserved)
        uint256 deductions = totalPendingDepositAssets + totalClaimableRedeemAssets;
        uint256 backingAssetsNow = reportedAssets > deductions
            ? reportedAssets - deductions
            : 0;

        // ── C. Compute periodPnl ──────────────────────────────────────────
        int256 periodPnl = int256(backingAssetsNow) - int256(lastReportedBackingAssets);

        // ── D. Performance fee (high-water-mark style, realized only) ─────
        uint256 feeAssets = 0;
        if (periodPnl < 0) {
            // Loss period: accumulate to carryforward, no fee
            lossCarryforward += uint256(-periodPnl);
        } else if (periodPnl > 0) {
            uint256 profit = uint256(periodPnl);
            // Recover prior losses first
            uint256 profitAfterLoss = profit > lossCarryforward
                ? profit - lossCarryforward
                : 0;
            feeAssets = (profitAfterLoss * PERF_FEE_BPS) / 10000;
            lossCarryforward = lossCarryforward > profit
                ? lossCarryforward - profit
                : 0;
        }

        // ── E. Mint fee shares via dilution ───────────────────────────────
        // Fee is realized as new shares minted to feeRecipient.
        // Existing shareholders bear cost proportional to their profit.
        // feeShares formula gives feeRecipient exactly feeAssets worth of value:
        //   feeShares = feeAssets × outstandingShares / (backingAssetsNow − feeAssets)
        uint256 outstandingShares = totalSupply() - totalPendingRedeemShares;
        uint256 feeShares = 0;
        if (feeAssets > 0 && outstandingShares > 0 && backingAssetsNow > feeAssets) {
            feeShares = (feeAssets * outstandingShares) / (backingAssetsNow - feeAssets);
            if (feeShares > 0) {
                _mint(feeRecipient, feeShares);
                emit PerfFeeMinted(feeRecipient, feeShares, feeAssets);
            }
        }

        // ── F. Update officialPPS ──────────────────────────────────────────
        // newPPS = (backingAssetsNow − feeAssets) / outstandingShares (before fee dilution)
        // After fee share minting, per-share backing is correctly newPPS for all holders.
        uint256 netBacking = backingAssetsNow > feeAssets
            ? backingAssetsNow - feeAssets
            : backingAssetsNow;
        uint256 newPPS = outstandingShares > 0
            ? (netBacking * NAV_PRECISION) / outstandingShares
            : INITIAL_PPS;
        if (newPPS == 0) newPPS = INITIAL_PPS;
        officialPPS = newPPS;

        // ── G. Process pending deposit requests ───────────────────────────
        uint256 depositsProcessed = _processDepositRequests(newPPS, maxDeposits);

        // ── H. Process pending redeem requests ────────────────────────────
        uint256 redeemsProcessed = _processRedeemRequests(maxRedeems);

        // ── I. Update lastReportedBackingAssets ───────────────────────────
        // Recompute after request processing side-effects on totalPendingDepositAssets
        // and totalClaimableRedeemAssets.
        uint256 newDeductions = totalPendingDepositAssets + totalClaimableRedeemAssets;
        lastReportedBackingAssets = reportedAssets > newDeductions
            ? reportedAssets - newDeductions
            : 0;

        // ── J. Emit ───────────────────────────────────────────────────────
        emit Reported(
            reportedAssets,
            backingAssetsNow,
            periodPnl,
            feeShares,
            feeAssets,
            newPPS,
            depositsProcessed,
            redeemsProcessed,
            data.nonce
        );
    }

    // ═══════════════════════════════════════════
    // Internal Accounting
    // ═══════════════════════════════════════════

    function _idleBalance() internal view returns (uint256) {
        uint256 vaultBalance = usdc.balanceOf(address(this));
        uint256 reserved     = totalPendingDepositAssets + totalClaimableRedeemAssets;
        return vaultBalance > reserved ? vaultBalance - reserved : 0;
    }

    function _targetIdleAmount() internal view returns (uint256) {
        uint256 target = (lastReportedBackingAssets * targetIdleBps) / 10000;
        return target > minimumIdleAmount ? target : minimumIdleAmount;
    }

    function _processDepositRequests(uint256 pps, uint256 maxCount)
        internal
        returns (uint256 processed)
    {
        uint256 len   = depositRequests.length;
        uint256 limit = maxCount == 0 || maxCount > MAX_REQUESTS_PER_REPORT
            ? MAX_REQUESTS_PER_REPORT
            : maxCount;

        for (uint256 i = nextDepositToProcess; i < len && processed < limit; i++) {
            DepositRequest storage req = depositRequests[i];
            if (req.status == RequestStatus.PENDING) {
                req.status       = RequestStatus.CLAIMABLE;
                req.processedPPS = pps;
                totalPendingDepositAssets -= req.assets;
                processed++;
            }
        }

        if (nextDepositToProcess + processed <= len) {
            nextDepositToProcess += processed;
            // Advance past already-cancelled requests too
            while (nextDepositToProcess < len &&
                   depositRequests[nextDepositToProcess].status != RequestStatus.PENDING) {
                nextDepositToProcess++;
            }
        }
    }

    function _processRedeemRequests(uint256 maxCount)
        internal
        returns (uint256 processed)
    {
        uint256 availableLiquidity = _idleBalance();
        uint256 len   = redeemRequests.length;
        uint256 limit = maxCount == 0 || maxCount > MAX_REQUESTS_PER_REPORT
            ? MAX_REQUESTS_PER_REPORT
            : maxCount;

        for (uint256 i = nextRedeemToProcess; i < len && processed < limit; i++) {
            RedeemRequest storage req = redeemRequests[i];

            if (req.status != RequestStatus.PENDING) {
                // Skip non-pending (cancelled, etc.) without counting against limit
                continue;
            }

            uint256 needed = (req.shares * officialPPS) / NAV_PRECISION;

            // FIFO: if we can't fill this request, stop — do not skip ahead
            if (availableLiquidity < needed) break;

            req.status          = RequestStatus.CLAIMABLE;
            req.claimableAssets = needed;
            _burn(address(this), req.shares);
            totalPendingRedeemShares   -= req.shares;
            totalClaimableRedeemAssets += needed;
            availableLiquidity         -= needed;
            nextRedeemToProcess         = i + 1;
            processed++;
        }
    }

    function _verifyReportSignature(ReportData calldata data, bytes calldata signature)
        internal
        view
    {
        require(block.timestamp <= data.deadline, "Report expired");
        require(block.timestamp - data.timestamp <= MAX_REPORT_STALENESS, "Report too old");
        require(data.nonce > lastReportNonce, "Nonce must increase");

        bytes32 structHash = keccak256(abi.encode(
            REPORT_TYPEHASH,
            data.reportedAssets,
            data.timestamp,
            data.deadline,
            data.nonce,
            address(this),
            block.chainid,
            DOMAIN_SALT
        ));
        bytes32 digest = structHash.toEthSignedMessageHash();
        address signer = digest.recover(signature);
        require(signer == reportSigner, "Invalid signer");
    }

    // ═══════════════════════════════════════════
    // View Functions
    // ═══════════════════════════════════════════

    /// @notice Vault idle USDC (excludes pending deposits and reserved redeems)
    function idleBalance() external view returns (uint256) {
        return _idleBalance();
    }

    /// @notice Total number of deposit requests ever submitted (all statuses)
    function depositRequestCount() external view returns (uint256) {
        return depositRequests.length;
    }

    /// @notice Total number of redeem requests ever submitted (all statuses)
    function redeemRequestCount() external view returns (uint256) {
        return redeemRequests.length;
    }

    /// @notice Estimated USDC a depositor will receive on claimDeposit()
    function previewClaimDeposit(uint256 requestId) external view returns (uint256 shares) {
        require(requestId < depositRequests.length, "Invalid");
        DepositRequest storage req = depositRequests[requestId];
        require(req.status == RequestStatus.CLAIMABLE, "Not claimable");
        shares = (req.assets * NAV_PRECISION) / req.processedPPS;
    }

    /// @notice USDC a redeemer will receive on claimRedeem()
    function previewClaimRedeem(uint256 requestId) external view returns (uint256 assets) {
        require(requestId < redeemRequests.length, "Invalid");
        RedeemRequest storage req = redeemRequests[requestId];
        require(req.status == RequestStatus.CLAIMABLE, "Not claimable");
        assets = req.claimableAssets;
    }

    /// @notice Estimated shares for depositing `assets` USDC at current officialPPS
    function previewDeposit(uint256 assets) external view returns (uint256 shares) {
        shares = (assets * NAV_PRECISION) / officialPPS;
    }

    /// @notice Estimated USDC for redeeming `shares` at current officialPPS
    function previewRedeem(uint256 shares) external view returns (uint256 assets) {
        assets = (shares * officialPPS) / NAV_PRECISION;
    }

    function getDepositRequest(uint256 requestId) external view returns (
        address owner, address receiver, uint256 assets, uint256 submittedAt,
        RequestStatus status, uint256 processedPPS, uint256 estimatedShares
    ) {
        require(requestId < depositRequests.length, "Invalid");
        DepositRequest storage req = depositRequests[requestId];
        uint256 estShares = req.processedPPS > 0
            ? (req.assets * NAV_PRECISION) / req.processedPPS
            : (req.assets * NAV_PRECISION) / officialPPS;
        return (req.owner, req.receiver, req.assets, req.submittedAt,
                req.status, req.processedPPS, estShares);
    }

    function getRedeemRequest(uint256 requestId) external view returns (
        address owner, address receiver, uint256 shares, uint256 submittedAt,
        RequestStatus status, uint256 claimableAssets, uint256 estimatedAssets
    ) {
        require(requestId < redeemRequests.length, "Invalid");
        RedeemRequest storage req = redeemRequests[requestId];
        uint256 estAssets = req.claimableAssets > 0
            ? req.claimableAssets
            : (req.shares * officialPPS) / NAV_PRECISION;
        return (req.owner, req.receiver, req.shares, req.submittedAt,
                req.status, req.claimableAssets, estAssets);
    }

    function getUserDepositRequests(address user) external view returns (uint256[] memory) {
        return userDepositRequests[user];
    }

    function getUserRedeemRequests(address user) external view returns (uint256[] memory) {
        return userRedeemRequests[user];
    }

    function getVaultState() external view returns (
        uint256 _officialPPS,
        uint256 _totalSupply,
        uint256 _idleBal,
        uint256 _lastReportedBacking,
        uint256 _lossCarryforward,
        uint256 _pendingDepositAssets,
        uint256 _claimableRedeemAssets,
        uint256 _pendingRedeemShares,
        uint256 _lastReportTimestamp,
        uint256 _lastReportNonce,
        bool    _paused,
        bool    _shutdown
    ) {
        return (
            officialPPS,
            totalSupply(),
            _idleBalance(),
            lastReportedBackingAssets,
            lossCarryforward,
            totalPendingDepositAssets,
            totalClaimableRedeemAssets,
            totalPendingRedeemShares,
            lastReportTimestamp,
            lastReportNonce,
            paused,
            shutdown
        );
    }

    // ═══════════════════════════════════════════
    // Admin / Keeper Support
    // ═══════════════════════════════════════════

    /**
     * @notice Refill vault buffer (permissionless — only adds USDC, never removes).
     *         Called by off-chain bot to sweep servicer cash back when redemptions pending.
     */
    function refillBuffer(uint256 amount) external nonReentrant {
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        emit BufferRefilled(msg.sender, amount);
    }

    function setFeeRecipient(address _feeRecipient) external onlyOwner {
        require(_feeRecipient != address(0), "Invalid");
        feeRecipient = _feeRecipient;
        emit AddressUpdated("feeRecipient", _feeRecipient);
    }

    function setArbServicerWallet(address _arbServicerWallet) external onlyOwner {
        require(_arbServicerWallet != address(0), "Invalid");
        arbServicerWallet = _arbServicerWallet;
        emit AddressUpdated("arbServicerWallet", _arbServicerWallet);
    }

    function setCap(uint256 _maxTotal) external onlyOwner {
        maxTotalDeposits = _maxTotal;
        emit ConfigUpdated("maxTotalDeposits", _maxTotal);
    }

    function setTargetIdleBps(uint256 _bps) external onlyOwner {
        require(_bps <= 5000, "Max 50% idle target");
        targetIdleBps = _bps;
        emit ConfigUpdated("targetIdleBps", _bps);
    }

    function setMinimumIdleAmount(uint256 _amount) external onlyOwner {
        minimumIdleAmount = _amount;
        emit ConfigUpdated("minimumIdleAmount", _amount);
    }

    function setTendCooldown(uint256 _seconds) external onlyOwner {
        require(_seconds <= 1 hours, "Max 1 hour cooldown");
        tendCooldown = _seconds;
        emit ConfigUpdated("tendCooldown", _seconds);
    }

    function setReportCooldown(uint256 _seconds) external onlyOwner {
        require(_seconds <= 24 hours, "Max 24 hour cooldown");
        reportCooldown = _seconds;
        emit ConfigUpdated("reportCooldown", _seconds);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit Paused(_paused);
    }

    function setShutdown(bool _shutdown) external onlyOwner {
        shutdown = _shutdown;
        // Pause deposits when shutting down
        if (_shutdown) paused = true;
        emit Shutdown(_shutdown);
    }

    /**
     * @notice Emergency USDC recovery — only callable when shutdown.
     *         Normal claims and refillBuffer() still work in shutdown mode.
     */
    function emergencyWithdraw(address to, uint256 amount) external onlyOwner whenShutdown {
        require(to != address(0), "Invalid address");
        usdc.safeTransfer(to, amount);
        emit EmergencyWithdraw(to, amount);
    }
}
