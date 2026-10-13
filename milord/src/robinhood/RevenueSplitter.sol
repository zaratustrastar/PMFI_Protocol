// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title RevenueSplitter
/// @notice Splits protocol revenue (native ETH and ERC-20s such as WETH) between a fixed
///         number of payees. One instance per revenue stream:
///           - NFT entry fees:   70% Arbitrage Funding Safe / 30% Team Safe
///           - NFT royalties:    50% Team Safe / 50% Arbitrage Funding Safe
///           - Trading tax:      1/3 BuybackBurner / 1/3 Team Safe / 1/3 Arbitrage Funding Safe
///
///  - receive() does no work, so marketplaces paying royalties with a 2,300-gas stipend succeed.
///  - distribute() is permissionless. It splits the not-yet-distributed balance at the current
///    shares and pushes to payees; a failed push is credited for a later pull (withdraw).
///  - Shares can only move inside per-payee [min,max] bounds fixed at construction. A share
///    change takes effect immediately, after ETH already received is split at the old shares.
///  - Changing a payee ADDRESS is time-delayed (`PAYEE_CHANGE_DELAY`) and cancellable.
contract RevenueSplitter is Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint16 public constant BPS = 10_000;
    uint256 public constant PAYEE_CHANGE_DELAY = 2 days;
    address public constant NATIVE = address(0);

    struct Payee {
        address account;
        uint16 bps;
        uint16 minBps;
        uint16 maxBps;
        string label;
    }

    struct PendingPayee {
        address account;
        uint64 eta;
    }

    Payee[] internal _payees;
    mapping(uint256 index => PendingPayee) public pendingPayee;

    /// token => payee => credited but not yet withdrawn (only after a failed push)
    mapping(address => mapping(address => uint256)) public owed;
    /// token => total credited but not yet withdrawn
    mapping(address => uint256) public totalOwed;
    /// token => lifetime amount split
    mapping(address => uint256) public totalDistributed;
    /// token => payee => lifetime amount allocated
    mapping(address => mapping(address => uint256)) public totalAllocated;

    event Received(address indexed from, uint256 amount);
    event Distributed(address indexed token, uint256 amount);
    event Allocated(address indexed token, address indexed payee, uint256 amount, bool pushed);
    event Withdrawn(address indexed token, address indexed payee, uint256 amount);
    event SharesUpdated(uint16[] bps);
    event PayeeChangeQueued(uint256 indexed index, address account, uint64 eta);
    event PayeeChangeCancelled(uint256 indexed index);
    event PayeeChanged(uint256 indexed index, address oldAccount, address newAccount);

    error BadConfig();
    error TooEarly();

    constructor(address owner_, Payee[] memory payees_) Ownable(owner_) {
        uint256 sum;
        if (payees_.length == 0 || payees_.length > 8) revert BadConfig();
        for (uint256 i; i < payees_.length; ++i) {
            Payee memory p = payees_[i];
            if (p.account == address(0) || p.minBps > p.bps || p.bps > p.maxBps || p.maxBps > BPS) {
                revert BadConfig();
            }
            sum += p.bps;
            _payees.push(p);
        }
        if (sum != BPS) revert BadConfig();
    }

    receive() external payable {
        emit Received(msg.sender, msg.value);
    }

    // ───────────────────────────── splitting ─────────────────────────────

    /// @notice Amount of `token` (NATIVE for ETH) received but not yet split.
    function undistributed(address token) public view returns (uint256) {
        uint256 bal = token == NATIVE ? address(this).balance : IERC20(token).balanceOf(address(this));
        return bal - totalOwed[token];
    }

    function distribute(address token) public nonReentrant returns (uint256 amount) {
        amount = undistributed(token);
        if (amount == 0) return 0;
        totalDistributed[token] += amount;
        uint256 remaining = amount;
        uint256 n = _payees.length;
        for (uint256 i; i < n; ++i) {
            Payee storage p = _payees[i];
            uint256 part = i == n - 1 ? remaining : (amount * p.bps) / BPS;
            remaining -= part;
            if (part == 0) continue;
            totalAllocated[token][p.account] += part;
            bool pushed = _push(token, p.account, part);
            if (!pushed) {
                owed[token][p.account] += part;
                totalOwed[token] += part;
            }
            emit Allocated(token, p.account, part, pushed);
        }
        emit Distributed(token, amount);
    }

    /// @notice Pull anything credited after a failed push.
    function withdraw(address token) external nonReentrant {
        uint256 amount = owed[token][msg.sender];
        require(amount > 0, "nothing owed");
        owed[token][msg.sender] = 0;
        totalOwed[token] -= amount;
        if (token == NATIVE) {
            (bool ok,) = msg.sender.call{value: amount}("");
            require(ok, "send failed");
        } else {
            IERC20(token).safeTransfer(msg.sender, amount);
        }
        emit Withdrawn(token, msg.sender, amount);
    }

    function _push(address token, address to, uint256 amount) internal returns (bool) {
        if (token == NATIVE) {
            (bool ok,) = to.call{value: amount, gas: 100_000}("");
            return ok;
        }
        (bool success, bytes memory data) = token.call(abi.encodeCall(IERC20.transfer, (to, amount)));
        return success && (data.length == 0 || abi.decode(data, (bool)));
    }

    // ───────────────────────────── admin ─────────────────────────────

    /// @notice Change shares within the per-payee bounds fixed at construction. ETH received
    ///         so far is split at the old shares first.
    function setShares(uint16[] calldata bps) external onlyOwner {
        if (bps.length != _payees.length) revert BadConfig();
        if (undistributed(NATIVE) > 0) distribute(NATIVE);
        uint256 sum;
        for (uint256 i; i < bps.length; ++i) {
            Payee storage p = _payees[i];
            if (bps[i] < p.minBps || bps[i] > p.maxBps) revert BadConfig();
            sum += bps[i];
        }
        if (sum != BPS) revert BadConfig();
        for (uint256 i; i < bps.length; ++i) {
            _payees[i].bps = bps[i];
        }
        emit SharesUpdated(bps);
    }

    function queuePayeeChange(uint256 index, address account) external onlyOwner {
        if (index >= _payees.length || account == address(0)) revert BadConfig();
        uint64 eta = uint64(block.timestamp + PAYEE_CHANGE_DELAY);
        pendingPayee[index] = PendingPayee(account, eta);
        emit PayeeChangeQueued(index, account, eta);
    }

    function cancelPayeeChange(uint256 index) external onlyOwner {
        delete pendingPayee[index];
        emit PayeeChangeCancelled(index);
    }

    function executePayeeChange(uint256 index) external onlyOwner {
        PendingPayee memory pp = pendingPayee[index];
        if (pp.account == address(0)) revert BadConfig();
        if (block.timestamp < pp.eta) revert TooEarly();
        if (undistributed(NATIVE) > 0) distribute(NATIVE);
        address old = _payees[index].account;
        _payees[index].account = pp.account;
        delete pendingPayee[index];
        emit PayeeChanged(index, old, pp.account);
    }

    // ───────────────────────────── views ─────────────────────────────

    function payeeCount() external view returns (uint256) {
        return _payees.length;
    }

    function payee(uint256 index) external view returns (Payee memory) {
        return _payees[index];
    }

    function payees() external view returns (Payee[] memory) {
        return _payees;
    }
}
