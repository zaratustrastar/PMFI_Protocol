// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";
import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title ShareDistributor
/// @notice Holds pARB minted for Milords holders and pays it out against a cumulative
///         Merkle root (one leaf per holder address: cumulative pARB ever allocated).
///
///  - Daily epochs: epoch k is the UTC day [genesis + (k-1) days, genesis + k days), genesis at 00:00 UTC.
///    A root covers every day up to and including `epoch`, so a late or missed publication is caught up
///    by the next root and nothing is lost.
///  - Cumulative leaves: claiming late or skipping days loses nothing and nothing expires; a claim pays
///    `cumulative - claimed[account]`. Allocations earned by a former holder stay with them.
///  - Solvency: a root can only be proposed if pARB held + pARB already claimed covers its
///    total cumulative allocation, and totals never decrease. The same pARB cannot be
///    allocated twice.
///  - Roots activate after `activationDelay`; the owner (team Safe) can veto a pending root
///    during that window. A new root cannot be proposed while a pending root is still in its
///    delay: it would restart the review clock (with daily proposals and a 1-day delay, a root
///    could then never activate). Proposing first activates a pending root whose delay has passed.
///    Nobody can withdraw pARB except through claims.
///  - Leaf format = OpenZeppelin StandardMerkleTree ["address","uint256"].
contract ShareDistributor is Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct RootInfo {
        bytes32 root;
        uint256 totalCumulative;
        uint64 epoch; // last UTC day included (daily epoch index, 1 = the genesis day)
        uint64 activatesAt;
        bytes32 dataHash; // hash/CID of the published allocation file
    }

    uint256 public constant MAX_ACTIVATION_DELAY = 7 days;

    IERC20 public immutable parb;
    address public publisher;
    uint256 public activationDelay = 1 days;

    RootInfo public active;
    RootInfo public pending;
    mapping(address => uint256) public claimed;
    uint256 public totalClaimed;

    event PublisherSet(address indexed publisher);
    event ActivationDelaySet(uint256 delay);
    event RootProposed(
        bytes32 indexed root, uint256 totalCumulative, uint64 epoch, uint64 activatesAt, bytes32 dataHash
    );
    event RootActivated(bytes32 indexed root, uint64 epoch);
    event RootVetoed(bytes32 indexed root);
    event Claimed(address indexed account, uint256 amount, uint256 cumulative);

    error NotPublisher();
    error Underfunded(uint256 needed, uint256 available);
    error NotIncreasing();
    error InvalidProof();
    error NothingToClaim();
    error PendingRootNotActivated(uint64 epoch, uint64 activatesAt);

    constructor(address owner_, IERC20 parb_, address publisher_) Ownable(owner_) {
        parb = parb_;
        publisher = publisher_;
        emit PublisherSet(publisher_);
    }

    // ───────────────────────────── roots ─────────────────────────────

    function proposeRoot(bytes32 root, uint256 totalCumulative, uint64 epoch, bytes32 dataHash) external {
        if (msg.sender != publisher) revert NotPublisher();
        _activateIfReady();
        if (pending.root != bytes32(0)) revert PendingRootNotActivated(pending.epoch, pending.activatesAt);
        if (root == bytes32(0)) revert InvalidProof();
        if (totalCumulative < active.totalCumulative || epoch <= active.epoch) revert NotIncreasing();
        uint256 available = parb.balanceOf(address(this)) + totalClaimed;
        if (totalCumulative > available) revert Underfunded(totalCumulative, available);

        uint64 eta = uint64(block.timestamp + activationDelay);
        pending = RootInfo(root, totalCumulative, epoch, eta, dataHash); // the slot is empty (checked above)
        emit RootProposed(root, totalCumulative, epoch, eta, dataHash);
    }

    function vetoPending() external onlyOwner {
        emit RootVetoed(pending.root);
        delete pending;
    }

    function activateRoot() external {
        _activateIfReady();
    }

    function _activateIfReady() internal {
        if (pending.root != bytes32(0) && block.timestamp >= pending.activatesAt) {
            active = pending;
            delete pending;
            emit RootActivated(active.root, active.epoch);
        }
    }

    // ───────────────────────────── claims ─────────────────────────────

    /// @notice Pay `account` its unclaimed pARB. Anyone may call; funds only go to `account`.
    function claim(address account, uint256 cumulative, bytes32[] calldata proof)
        external
        nonReentrant
        returns (uint256 amount)
    {
        _activateIfReady();
        bytes32 leaf = keccak256(bytes.concat(keccak256(abi.encode(account, cumulative))));
        if (!MerkleProof.verifyCalldata(proof, active.root, leaf)) revert InvalidProof();
        uint256 already = claimed[account];
        if (cumulative <= already) revert NothingToClaim();
        amount = cumulative - already;
        claimed[account] = cumulative;
        totalClaimed += amount;
        parb.safeTransfer(account, amount);
        emit Claimed(account, amount, cumulative);
    }

    /// @notice pARB held but not yet allocated in the active root (carried forward).
    function unallocated() external view returns (uint256) {
        uint256 held = parb.balanceOf(address(this));
        uint256 owed = active.totalCumulative - totalClaimed;
        return held > owed ? held - owed : 0;
    }

    // ───────────────────────────── admin ─────────────────────────────

    function setPublisher(address p) external onlyOwner {
        publisher = p;
        emit PublisherSet(p);
    }

    function setActivationDelay(uint256 d) external onlyOwner {
        require(d >= 1 hours && d <= MAX_ACTIVATION_DELAY, "range");
        activationDelay = d;
        emit ActivationDelaySet(d);
    }

    /// @notice Rescue tokens sent by mistake. pARB can never be withdrawn this way.
    function rescue(IERC20 token, address to, uint256 amount) external onlyOwner {
        require(address(token) != address(parb), "pARB locked");
        token.safeTransfer(to, amount);
    }
}
