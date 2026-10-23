// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

/**
 * @title PMFIArbVaultV3
 * @notice Fork of PMFIArbVaultV2 (contracts/PMFIArbVaultV2.sol) for a NEW, separate
 *         pArbitrage instance funded by MILORD protocol fees. Same request/report/claim/tend
 *         design, same signed ReportDataV2 struct shape (new domain salt), same
 *         bootstrap-share and FIFO queue mechanics. Legacy V1/V2 deployments are untouched.
 *
 *  Changes from V2 (each covered by tests in test/base/):
 *   V3-1  Performance fee is a constructor parameter (immutable, <= 20%). MILORD instance: 0.
 *   V3-2  PPS denominator counts every share that still has a claim on backing:
 *         V2 subtracted pending-redeem shares (their assets stay in backing) -> PPS inflated.
 *   V3-3  Deposits processed but not yet claimed are counted as outstanding shares.
 *         V2 counted their assets in backing but not their shares -> PPS inflated next report.
 *   V3-4  No silent reset to $1. If backing is zero while shares are outstanding, report()
 *         reverts (shutdown path instead) rather than pricing new deposits at $1.
 *   V3-5  Reconciliation guards in report(): reportedAssets must cover the USDC provably held
 *         by the vault, and PPS may not move more than maxPpsMoveBps in one report unless the
 *         owner pre-approved that report nonce.
 *   V3-6  Optional depositor allowlist (MILORD instance: only the funding adapter deposits).
 *         Share transfers and redemptions stay open to every holder.
 *   V3-7  emergencyWithdraw cannot take USDC reserved for pending deposits or claimable
 *         redemptions.
 *   V3-8  Shutdown is an orderly exit: report() still runs (redeems only), pending deposits
 *         can be cancelled immediately.
 *   V3-9  Unused MAX_REPORT_INTERVAL constant removed.
 *   V3-10 Chain-neutral: the asset is any 6-decimal USD stablecoin (USDC on Base, USDG on
 *         Robinhood Chain). The constructor rejects other decimals because INITIAL_PPS,
 *         MIN_DEPOSIT_USDC and minimumIdleAmount are expressed in 6-decimal units.
 *         Variable names keep "usdc" so the reporter, bot and frontend ABIs are unchanged.
 */
contract PMFIArbVaultV3 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    // ═══════════════════════════════════════════ Constants

    bytes32 public constant REPORT_TYPEHASH = keccak256(
        "ReportDataV2(uint256 reportedAssets,uint256 timestamp,uint256 deadline,uint256 nonce,address vault,uint256 chainId,bytes32 domainSalt)"
    );
    bytes32 public constant DOMAIN_SALT = keccak256("PMFIArbVaultV3.milord.v1");

    uint256 public constant NAV_PRECISION = 1e18;
    uint256 public constant INITIAL_PPS = 1e6; // $1.00 (USDC units per 1e18 shares), first deposit only
    uint256 public constant MIN_DEPOSIT_USDC = 10 * 1e6;
    uint256 public constant MAX_PERF_FEE_BPS = 2000;
    uint256 public constant MAX_REQUEST_AGE = 30 days;
    uint256 public constant MAX_REPORT_STALENESS = 1 hours;
    uint256 public constant MAX_REQUESTS_PER_REPORT = 100;
    uint256 public constant BOOTSTRAP_SHARES = 1e18;
    address public constant DEAD_ADDRESS = 0x000000000000000000000000000000000000dEaD;

    // ═══════════════════════════════════════════ Immutables

    IERC20 public immutable usdc;
    address public immutable reportSigner;
    uint256 public immutable perfFeeBps; // V3-1

    // ═══════════════════════════════════════════ Configuration

    address public feeRecipient;
    address public arbServicerWallet;
    uint256 public maxTotalDeposits;
    uint256 public targetIdleBps;
    uint256 public minimumIdleAmount;
    uint256 public tendCooldown;
    uint256 public reportCooldown;
    uint256 public maxPpsMoveBps; // V3-5
    bool public paused;
    bool public shutdown;
    bool public depositorRestricted; // V3-6
    mapping(address => bool) public isAllowedDepositor;
    mapping(uint256 nonce => bool) public largeMoveApproved; // V3-5

    // ═══════════════════════════════════════════ Accounting

    uint256 public officialPPS;
    uint256 public lastReportedBackingAssets;
    uint256 public highWaterMarkAssets;
    uint256 public lastReportNonce;
    uint256 public lastReportTimestamp;
    uint256 public lastTendTimestamp;

    uint256 public totalPendingDepositAssets;
    uint256 public totalClaimableRedeemAssets;
    uint256 public totalPendingRedeemShares;
    uint256 public totalUnclaimedDepositShares; // V3-3

    enum RequestStatus {
        PENDING,
        CLAIMABLE,
        CLAIMED,
        CANCELLED
    }

    struct DepositRequest {
        address owner;
        address receiver;
        uint256 assets;
        uint256 submittedAt;
        RequestStatus status;
        uint256 processedPPS;
    }

    struct RedeemRequest {
        address owner;
        address receiver;
        uint256 shares;
        uint256 submittedAt;
        RequestStatus status;
        uint256 claimableAssets;
    }

    DepositRequest[] public depositRequests;
    RedeemRequest[] public redeemRequests;
    uint256 public nextDepositToProcess;
    uint256 public nextRedeemToProcess;
    mapping(address => uint256[]) public userDepositRequests;
    mapping(address => uint256[]) public userRedeemRequests;

    struct ReportData {
        uint256 reportedAssets;
        uint256 timestamp;
        uint256 deadline;
        uint256 nonce;
    }

    struct ReportLocals {
        uint256 reportedAssets;
        uint256 backingAssetsNow;
        int256 profitAboveHWM;
        uint256 feeAssets;
        uint256 feeShares;
        uint256 outstandingShares;
        uint256 netBacking;
        uint256 newPPS;
        uint256 depositsProcessed;
        uint256 depositAssetsProcessed;
        uint256 redeemsProcessed;
        uint256 redeemAssetsProcessed;
    }

    // ═══════════════════════════════════════════ Events

    event DepositRequested(uint256 indexed requestId, address indexed owner, address indexed receiver, uint256 assets);
    event DepositClaimed(uint256 indexed requestId, address indexed receiver, uint256 shares, uint256 pps);
    event RedeemRequested(uint256 indexed requestId, address indexed owner, address indexed receiver, uint256 shares);
    event RedeemClaimed(uint256 indexed requestId, address indexed receiver, uint256 assets);
    event RequestCancelled(uint256 indexed requestId, bool isDeposit, address indexed owner);
    event Reported(
        uint256 reportedAssets,
        uint256 backingAssetsNow,
        int256 profitAboveHWM,
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
    event DepositorAllowed(address indexed depositor, bool allowed);
    event LargeMoveApproved(uint256 indexed nonce);
    event Paused(bool isPaused);
    event Shutdown(bool isShutdown);
    event EmergencyWithdraw(address indexed to, uint256 amount);

    error ZeroNav();
    error PpsMoveTooLarge(uint256 oldPps, uint256 newPps);
    error ReportBelowOnchainCash(uint256 reported, uint256 onchain);

    constructor(
        string memory name_,
        string memory symbol_,
        address _usdc,
        address _reportSigner,
        address _feeRecipient,
        address _arbServicerWallet,
        uint256 _maxTotal,
        uint256 _perfFeeBps
    ) ERC20(name_, symbol_) Ownable(msg.sender) {
        require(_usdc != address(0), "Invalid USDC");
        require(_reportSigner != address(0), "Invalid signer");
        require(_feeRecipient != address(0), "Invalid fee recipient");
        require(_arbServicerWallet != address(0), "Invalid servicer");
        require(_perfFeeBps <= MAX_PERF_FEE_BPS, "Perf fee too high");
        require(IERC20Metadata(_usdc).decimals() == 6, "Asset must have 6 decimals"); // V3-10

        usdc = IERC20(_usdc);
        reportSigner = _reportSigner;
        feeRecipient = _feeRecipient;
        arbServicerWallet = _arbServicerWallet;
        maxTotalDeposits = _maxTotal;
        perfFeeBps = _perfFeeBps;

        officialPPS = INITIAL_PPS;
        targetIdleBps = 1000;
        minimumIdleAmount = 100e6;
        tendCooldown = 60;
        reportCooldown = 3600;
        maxPpsMoveBps = 1000; // 10% per report

        _mint(DEAD_ADDRESS, BOOTSTRAP_SHARES);
    }

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

    // ═══════════════════════════════════════════ Deposit

    function requestDeposit(uint256 assets, address receiver)
        external
        nonReentrant
        whenNotPaused
        whenNotShutdown
        returns (uint256 requestId)
    {
        if (depositorRestricted) require(isAllowedDepositor[msg.sender], "Depositor not allowed");
        require(assets >= MIN_DEPOSIT_USDC, "Below $10 minimum");
        require(receiver != address(0), "Invalid receiver");
        require(totalPendingDepositAssets + lastReportedBackingAssets + assets <= maxTotalDeposits, "Exceeds vault cap");

        usdc.safeTransferFrom(msg.sender, address(this), assets);
        totalPendingDepositAssets += assets;

        requestId = depositRequests.length;
        depositRequests.push(DepositRequest(msg.sender, receiver, assets, block.timestamp, RequestStatus.PENDING, 0));
        userDepositRequests[msg.sender].push(requestId);
        emit DepositRequested(requestId, msg.sender, receiver, assets);
    }

    function claimDeposit(uint256 requestId, address receiver) external nonReentrant returns (uint256 shares) {
        require(requestId < depositRequests.length, "Invalid request");
        DepositRequest storage req = depositRequests[requestId];
        require(req.owner == msg.sender, "Not your request");
        require(req.status == RequestStatus.CLAIMABLE, "Not claimable yet");
        address to = receiver != address(0) ? receiver : req.receiver;
        shares = _claimDeposit(requestId, req, to);
    }

    function autoClaimDeposits(uint256[] calldata requestIds) external nonReentrant {
        for (uint256 i = 0; i < requestIds.length; i++) {
            uint256 rid = requestIds[i];
            if (rid >= depositRequests.length) continue;
            DepositRequest storage req = depositRequests[rid];
            if (req.status != RequestStatus.CLAIMABLE) continue;
            _claimDeposit(rid, req, req.receiver);
        }
    }

    function _claimDeposit(uint256 rid, DepositRequest storage req, address to) internal returns (uint256 shares) {
        shares = _depositShares(req);
        require(shares > 0, "Zero shares");
        req.status = RequestStatus.CLAIMED;
        totalUnclaimedDepositShares -= shares; // V3-3
        _mint(to, shares);
        emit DepositClaimed(rid, to, shares, req.processedPPS);
    }

    function _depositShares(DepositRequest storage req) internal view returns (uint256) {
        return (req.assets * NAV_PRECISION) / req.processedPPS;
    }

    // ═══════════════════════════════════════════ Redeem

    function requestRedeem(uint256 shares, address receiver) external nonReentrant returns (uint256 requestId) {
        require(shares > 0, "Zero shares");
        require(balanceOf(msg.sender) >= shares, "Insufficient shares");
        require(receiver != address(0), "Invalid receiver");

        _transfer(msg.sender, address(this), shares);
        totalPendingRedeemShares += shares;

        requestId = redeemRequests.length;
        redeemRequests.push(RedeemRequest(msg.sender, receiver, shares, block.timestamp, RequestStatus.PENDING, 0));
        userRedeemRequests[msg.sender].push(requestId);
        emit RedeemRequested(requestId, msg.sender, receiver, shares);
    }

    function claimRedeem(uint256 requestId, address receiver) external nonReentrant returns (uint256 assets) {
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

    // ═══════════════════════════════════════════ Cancel

    function cancelRequest(uint256 requestId, bool isDeposit) external nonReentrant {
        if (isDeposit) {
            require(requestId < depositRequests.length, "Invalid request");
            DepositRequest storage req = depositRequests[requestId];
            require(req.owner == msg.sender, "Not your request");
            require(req.status == RequestStatus.PENDING, "Not pending");
            require(shutdown || block.timestamp > req.submittedAt + MAX_REQUEST_AGE, "Not yet cancelable"); // V3-8
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

    // ═══════════════════════════════════════════ tend()

    function tend() external nonReentrant whenNotPaused {
        require(block.timestamp >= lastTendTimestamp + tendCooldown, "Tend cooldown");
        lastTendTimestamp = block.timestamp;

        uint256 idle = _idleBalance();
        uint256 redemptionReserve = (totalPendingRedeemShares * officialPPS) / NAV_PRECISION;
        uint256 totalReserve = redemptionReserve + _targetIdleAmount();
        uint256 deployable = idle > totalReserve ? idle - totalReserve : 0;
        if (deployable > 0) usdc.safeTransfer(arbServicerWallet, deployable);
        emit TendExecuted(deployable, arbServicerWallet);
    }

    // ═══════════════════════════════════════════ report()

    function report(ReportData calldata data, bytes calldata signature, uint256 maxDeposits, uint256 maxRedeems)
        external
        nonReentrant
    {
        require(!paused || shutdown, "Vault is paused"); // V3-8
        require(block.timestamp >= lastReportTimestamp + reportCooldown, "Report cooldown");
        _verifyReportSignature(data, signature);
        bool firstReport = lastReportNonce == 0;
        lastReportNonce = data.nonce;
        lastReportTimestamp = block.timestamp;

        ReportLocals memory L;
        L.reportedAssets = data.reportedAssets;

        // V3-5: the vault's own USDC is part of reportedAssets, so a report below it is wrong.
        uint256 onchain = usdc.balanceOf(address(this));
        if (L.reportedAssets < onchain) revert ReportBelowOnchainCash(L.reportedAssets, onchain);

        uint256 deductions = totalPendingDepositAssets + totalClaimableRedeemAssets;
        L.backingAssetsNow = L.reportedAssets > deductions ? L.reportedAssets - deductions : 0;

        L.profitAboveHWM = L.backingAssetsNow >= highWaterMarkAssets
            ? int256(L.backingAssetsNow - highWaterMarkAssets)
            : -int256(highWaterMarkAssets - L.backingAssetsNow);
        if (L.profitAboveHWM > 0 && perfFeeBps > 0) {
            L.feeAssets = (uint256(L.profitAboveHWM) * perfFeeBps) / 10000;
        }

        // V3-2 / V3-3: every share with a claim on backing — circulating, locked for redeem,
        // and processed-but-unclaimed deposit shares. Bootstrap shares excluded as in V2.
        L.outstandingShares = totalSupply() - balanceOf(DEAD_ADDRESS) + totalUnclaimedDepositShares;

        if (L.feeAssets > 0 && L.outstandingShares > 0 && L.backingAssetsNow > L.feeAssets) {
            L.feeShares = (L.feeAssets * L.outstandingShares) / (L.backingAssetsNow - L.feeAssets);
            if (L.feeShares > 0) {
                _mint(feeRecipient, L.feeShares);
                emit PerfFeeMinted(feeRecipient, L.feeShares, L.feeAssets);
            }
        }

        L.netBacking = L.backingAssetsNow > L.feeAssets ? L.backingAssetsNow - L.feeAssets : L.backingAssetsNow;
        if (L.outstandingShares == 0) {
            L.newPPS = INITIAL_PPS; // empty vault: first depositors at $1
        } else {
            L.newPPS = (L.netBacking * NAV_PRECISION) / L.outstandingShares;
            if (L.newPPS == 0) revert ZeroNav(); // V3-4
            if (!firstReport) _checkPpsMove(officialPPS, L.newPPS, data.nonce);
        }
        officialPPS = L.newPPS;

        if (!shutdown) {
            (L.depositsProcessed, L.depositAssetsProcessed) = _processDepositRequests(L.newPPS, maxDeposits);
        }
        (L.redeemsProcessed, L.redeemAssetsProcessed) = _processRedeemRequests(maxRedeems);

        _updateHWM(L.netBacking, L.depositAssetsProcessed, L.redeemAssetsProcessed);

        uint256 newDeductions = totalPendingDepositAssets + totalClaimableRedeemAssets;
        lastReportedBackingAssets = L.reportedAssets > newDeductions ? L.reportedAssets - newDeductions : 0;

        _emitReported(L, data.nonce);
    }

    /// @dev Emits Reported. A separate function only to keep report()'s stack shallow enough for
    ///      non-IR development builds (profile.lowmem); the via-IR production bytecode is unchanged.
    function _emitReported(ReportLocals memory L, uint256 nonce) private {
        emit Reported(
            L.reportedAssets,
            L.backingAssetsNow,
            L.profitAboveHWM,
            L.feeShares,
            L.feeAssets,
            L.newPPS,
            L.depositsProcessed,
            L.redeemsProcessed,
            nonce
        );
    }

    function _checkPpsMove(uint256 oldPps, uint256 newPps, uint256 nonce) internal view {
        if (largeMoveApproved[nonce]) return;
        uint256 diff = newPps > oldPps ? newPps - oldPps : oldPps - newPps;
        if (diff * 10000 > oldPps * maxPpsMoveBps) revert PpsMoveTooLarge(oldPps, newPps);
    }

    function _updateHWM(uint256 netBacking, uint256 depositAssets, uint256 redeemAssets) internal {
        uint256 newHWM = netBacking + depositAssets;
        newHWM = newHWM > redeemAssets ? newHWM - redeemAssets : 0;
        if (newHWM < highWaterMarkAssets && redeemAssets == 0) newHWM = highWaterMarkAssets;
        highWaterMarkAssets = newHWM;
    }

    // ═══════════════════════════════════════════ Internal

    function _idleBalance() internal view returns (uint256) {
        uint256 vaultBalance = usdc.balanceOf(address(this));
        uint256 reserved = totalPendingDepositAssets + totalClaimableRedeemAssets;
        return vaultBalance > reserved ? vaultBalance - reserved : 0;
    }

    function _targetIdleAmount() internal view returns (uint256) {
        uint256 target = (lastReportedBackingAssets * targetIdleBps) / 10000;
        return target > minimumIdleAmount ? target : minimumIdleAmount;
    }

    function _processDepositRequests(uint256 pps, uint256 maxCount)
        internal
        returns (uint256 processed, uint256 assetsProcessed)
    {
        uint256 len = depositRequests.length;
        uint256 limit = maxCount == 0 || maxCount > MAX_REQUESTS_PER_REPORT ? MAX_REQUESTS_PER_REPORT : maxCount;
        for (uint256 i = nextDepositToProcess; i < len && processed < limit; i++) {
            DepositRequest storage req = depositRequests[i];
            if (req.status == RequestStatus.PENDING) {
                req.status = RequestStatus.CLAIMABLE;
                req.processedPPS = pps;
                totalPendingDepositAssets -= req.assets;
                totalUnclaimedDepositShares += _depositShares(req); // V3-3
                assetsProcessed += req.assets;
                processed++;
            }
        }
        if (nextDepositToProcess + processed <= len) {
            nextDepositToProcess += processed;
            while (
                nextDepositToProcess < len && depositRequests[nextDepositToProcess].status != RequestStatus.PENDING
            ) {
                nextDepositToProcess++;
            }
        }
    }

    function _processRedeemRequests(uint256 maxCount) internal returns (uint256 processed, uint256 assetsProcessed) {
        uint256 availableLiquidity = _idleBalance();
        uint256 len = redeemRequests.length;
        uint256 limit = maxCount == 0 || maxCount > MAX_REQUESTS_PER_REPORT ? MAX_REQUESTS_PER_REPORT : maxCount;
        for (uint256 i = nextRedeemToProcess; i < len && processed < limit; i++) {
            RedeemRequest storage req = redeemRequests[i];
            if (req.status != RequestStatus.PENDING) continue;
            uint256 needed = (req.shares * officialPPS) / NAV_PRECISION;
            if (availableLiquidity < needed) break;
            req.status = RequestStatus.CLAIMABLE;
            req.claimableAssets = needed;
            _burn(address(this), req.shares);
            totalPendingRedeemShares -= req.shares;
            totalClaimableRedeemAssets += needed;
            availableLiquidity -= needed;
            assetsProcessed += needed;
            nextRedeemToProcess = i + 1;
            processed++;
        }
    }

    function _verifyReportSignature(ReportData calldata data, bytes calldata signature) internal view {
        require(block.timestamp <= data.deadline, "Report expired");
        require(block.timestamp - data.timestamp <= MAX_REPORT_STALENESS, "Report too old");
        require(data.nonce > lastReportNonce, "Nonce must increase");
        bytes32 structHash = reportStructHash(data);
        address signer = structHash.toEthSignedMessageHash().recover(signature);
        require(signer == reportSigner, "Invalid signer");
    }

    function reportStructHash(ReportData calldata data) public view returns (bytes32) {
        return keccak256(
            abi.encode(
                REPORT_TYPEHASH,
                data.reportedAssets,
                data.timestamp,
                data.deadline,
                data.nonce,
                address(this),
                block.chainid,
                DOMAIN_SALT
            )
        );
    }

    // ═══════════════════════════════════════════ Views

    function idleBalance() external view returns (uint256) {
        return _idleBalance();
    }

    function depositRequestCount() external view returns (uint256) {
        return depositRequests.length;
    }

    function redeemRequestCount() external view returns (uint256) {
        return redeemRequests.length;
    }

    function previewClaimDeposit(uint256 requestId) external view returns (uint256) {
        DepositRequest storage req = depositRequests[requestId];
        require(req.status == RequestStatus.CLAIMABLE, "Not claimable");
        return _depositShares(req);
    }

    function previewDeposit(uint256 assets) external view returns (uint256) {
        return (assets * NAV_PRECISION) / officialPPS;
    }

    function previewRedeem(uint256 shares) external view returns (uint256) {
        return (shares * officialPPS) / NAV_PRECISION;
    }

    function circulatingSupply() public view returns (uint256) {
        return totalSupply() - balanceOf(DEAD_ADDRESS);
    }

    function getUserDepositRequests(address user) external view returns (uint256[] memory) {
        return userDepositRequests[user];
    }

    function getUserRedeemRequests(address user) external view returns (uint256[] memory) {
        return userRedeemRequests[user];
    }

    // ═══════════════════════════════════════════ Admin

    function refillBuffer(uint256 amount) external nonReentrant {
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        emit BufferRefilled(msg.sender, amount);
    }

    function setFeeRecipient(address a) external onlyOwner {
        require(a != address(0), "Invalid");
        feeRecipient = a;
        emit AddressUpdated("feeRecipient", a);
    }

    function setArbServicerWallet(address a) external onlyOwner {
        require(a != address(0), "Invalid");
        arbServicerWallet = a;
        emit AddressUpdated("arbServicerWallet", a);
    }

    function setCap(uint256 v) external onlyOwner {
        maxTotalDeposits = v;
        emit ConfigUpdated("maxTotalDeposits", v);
    }

    function setTargetIdleBps(uint256 v) external onlyOwner {
        require(v <= 5000, "Max 50% idle target");
        targetIdleBps = v;
        emit ConfigUpdated("targetIdleBps", v);
    }

    function setMinimumIdleAmount(uint256 v) external onlyOwner {
        minimumIdleAmount = v;
        emit ConfigUpdated("minimumIdleAmount", v);
    }

    function setTendCooldown(uint256 v) external onlyOwner {
        require(v <= 1 hours, "Max 1 hour cooldown");
        tendCooldown = v;
        emit ConfigUpdated("tendCooldown", v);
    }

    function setReportCooldown(uint256 v) external onlyOwner {
        require(v <= 24 hours, "Max 24 hour cooldown");
        reportCooldown = v;
        emit ConfigUpdated("reportCooldown", v);
    }

    function setMaxPpsMoveBps(uint256 v) external onlyOwner {
        require(v > 0 && v <= 5000, "Out of range");
        maxPpsMoveBps = v;
        emit ConfigUpdated("maxPpsMoveBps", v);
    }

    /// @notice Pre-approve one report nonce whose PPS move exceeds maxPpsMoveBps
    ///         (after off-chain reconciliation has explained the move).
    function approveLargeMove(uint256 nonce) external onlyOwner {
        largeMoveApproved[nonce] = true;
        emit LargeMoveApproved(nonce);
    }

    function setDepositorRestricted(bool v) external onlyOwner {
        depositorRestricted = v;
        emit ConfigUpdated("depositorRestricted", v ? 1 : 0);
    }

    function setAllowedDepositor(address depositor, bool allowed) external onlyOwner {
        isAllowedDepositor[depositor] = allowed;
        emit DepositorAllowed(depositor, allowed);
    }

    function setPaused(bool v) external onlyOwner {
        paused = v;
        emit Paused(v);
    }

    function setShutdown(bool v) external onlyOwner {
        shutdown = v;
        if (v) paused = true;
        emit Shutdown(v);
    }

    /// @notice V3-7: only USDC not reserved for pending deposits or claimable redemptions.
    function emergencyWithdraw(address to, uint256 amount) external onlyOwner whenShutdown {
        require(to != address(0), "Invalid address");
        require(amount <= _idleBalance(), "Reserved USDC");
        usdc.safeTransfer(to, amount);
        emit EmergencyWithdraw(to, amount);
    }
}
