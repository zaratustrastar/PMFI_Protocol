// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IParbVault {
    function requestDeposit(uint256 assets, address receiver) external returns (uint256 requestId);
    function autoClaimDeposits(uint256[] calldata requestIds) external;
    function previewClaimDeposit(uint256 requestId) external view returns (uint256);
    function depositRequests(uint256 id)
        external
        view
        returns (
            address owner,
            address receiver,
            uint256 assets,
            uint256 submittedAt,
            uint8 status,
            uint256 processedPPS
        );
}

/// @title ParbFundingAdapter
/// @notice Turns MILORD protocol-fee stablecoin (USDG on Robinhood Chain) into pARB shares through the vault's
///         normal deposit flow. It adds no accounting of its own: shares come from
///         requestDeposit -> report -> autoClaimDeposits exactly like any depositor.
///
///  - Every funding batch carries a `sourceRef` (e.g. hash of the Robinhood Chain fee
///    transfers and bridge tx it came from). A sourceRef can be funded once, so the same
///    fees cannot become shares twice.
///  - Shares are minted straight to the ShareDistributor (deposit receiver), never here.
///  - The adapter only spends USDC it holds; it cannot pull from the vault or distributor.
contract ParbFundingAdapter is Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct Batch {
        bytes32 sourceRef;
        uint256 usdcAmount;
        uint256 requestId;
        uint256 sharesMinted; // 0 until settled
        uint64 fundedAt;
        uint64 settledAt;
    }

    IERC20 public immutable usdc;
    IParbVault public immutable vault;
    address public immutable distributor;

    mapping(address => bool) public isOperator;
    mapping(bytes32 => bool) public sourceUsed;
    Batch[] internal _batches;

    uint256 public totalUsdcDeposited;
    uint256 public totalSharesMinted;

    event OperatorSet(address indexed operator, bool allowed);
    event BatchFunded(uint256 indexed batchId, bytes32 indexed sourceRef, uint256 usdcAmount, uint256 requestId);
    event BatchSettled(uint256 indexed batchId, uint256 sharesMinted);

    error NotOperator();
    error SourceAlreadyFunded();
    error NotClaimable();
    error AlreadySettled();

    constructor(address owner_, IERC20 usdc_, IParbVault vault_, address distributor_) Ownable(owner_) {
        require(address(usdc_) != address(0) && address(vault_) != address(0) && distributor_ != address(0), "zero");
        usdc = usdc_;
        vault = vault_;
        distributor = distributor_;
    }

    function setOperator(address op, bool allowed) external onlyOwner {
        isOperator[op] = allowed;
        emit OperatorSet(op, allowed);
    }

    /// @notice Deposit `amount` held USDC into the vault on behalf of the distributor.
    function fund(bytes32 sourceRef, uint256 amount) external nonReentrant returns (uint256 batchId) {
        if (!isOperator[msg.sender]) revert NotOperator();
        if (sourceRef == bytes32(0) || sourceUsed[sourceRef]) revert SourceAlreadyFunded();
        sourceUsed[sourceRef] = true;

        usdc.forceApprove(address(vault), amount);
        uint256 rid = vault.requestDeposit(amount, distributor);
        usdc.forceApprove(address(vault), 0);

        totalUsdcDeposited += amount;
        batchId = _batches.length;
        _batches.push(Batch(sourceRef, amount, rid, 0, uint64(block.timestamp), 0));
        emit BatchFunded(batchId, sourceRef, amount, rid);
    }

    /// @notice After a vault report processed the request, mint the shares to the
    ///         distributor and record how many. Permissionless.
    function settle(uint256 batchId) external nonReentrant returns (uint256 shares) {
        Batch storage b = _batches[batchId];
        if (b.settledAt != 0) revert AlreadySettled();
        (,,,, uint8 status,) = vault.depositRequests(b.requestId);
        if (status != 1) revert NotClaimable(); // 1 = CLAIMABLE

        IERC20 parb = IERC20(address(vault));
        uint256 before = parb.balanceOf(distributor);
        uint256[] memory ids = new uint256[](1);
        ids[0] = b.requestId;
        vault.autoClaimDeposits(ids);
        shares = parb.balanceOf(distributor) - before;
        _record(b, batchId, shares);
    }

    /// @notice Record a batch whose request someone else already auto-claimed
    ///         (autoClaimDeposits is permissionless on the vault).
    function syncClaimed(uint256 batchId) external returns (uint256 shares) {
        Batch storage b = _batches[batchId];
        if (b.settledAt != 0) revert AlreadySettled();
        (,,,, uint8 status, uint256 pps) = vault.depositRequests(b.requestId);
        if (status != 2) revert NotClaimable(); // 2 = CLAIMED
        shares = (b.usdcAmount * 1e18) / pps;
        _record(b, batchId, shares);
    }

    function _record(Batch storage b, uint256 batchId, uint256 shares) internal {
        b.sharesMinted = shares;
        b.settledAt = uint64(block.timestamp);
        totalSharesMinted += shares;
        emit BatchSettled(batchId, shares);
    }

    function batchCount() external view returns (uint256) {
        return _batches.length;
    }

    function batch(uint256 id) external view returns (Batch memory) {
        return _batches[id];
    }

    /// @notice Return USDC that was sent here by mistake (never touches deposited funds,
    ///         which live in the vault).
    function rescue(IERC20 token, address to, uint256 amount) external onlyOwner {
        token.safeTransfer(to, amount);
    }
}
