// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IPriceReference, ISwapVenue} from "./IPriceReference.sol";

interface IParbFundingAdapter {
    function fund(bytes32 sourceRef, uint256 amount) external returns (uint256 batchId);
}

/// @notice Arbitrage share of protocol ETH revenue -> USDG -> pArbitrage V3 deposit, without Safe signatures.
///
///  - The three splitters pay their arbitrage share here (tax 1/3, entry fees 70%, royalties 50%).
///  - `execute()` swaps a bounded amount of ETH for USDG through `venue`, with the minimum output computed HERE
///    from `reference` minus `maxSlippageBps`; the caller cannot choose the price. The USDG goes straight to the
///    ParbFundingAdapter, which requests a vault deposit for the ShareDistributor through the vault's existing
///    asynchronous flow. pARB is minted later by the vault, against the accepted deposit, at the reported PPS.
///  - Bounds: minimum swap (threshold), maximum per transaction, maximum per UTC day, minimum interval.
///  - Execution is open to anyone or limited to keepers (`openExecution`). Keepers have no other power.
///  - The owner (Arbitrage Funding Safe) sets bounds, pauses and unpauses, and can sweep funds ONLY to itself.
///    Pausers (monitoring keys) can pause but not unpause.
contract ArbitrageFundingRouter is Ownable2Step, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint16 public constant MAX_SLIPPAGE_CAP_BPS = 500; // 5% hard ceiling on the configurable slippage
    uint32 public constant MAX_INTERVAL = 7 days;

    IERC20 public immutable asset; // USDG
    IParbFundingAdapter public immutable adapter;

    ISwapVenue public venue;
    IPriceReference public priceReference;
    uint256 public minSwapWei;
    uint256 public maxSwapWei;
    uint256 public dailyCapWei;
    uint16 public maxSlippageBps;
    uint32 public minInterval;
    bool public openExecution;
    bool public paused;

    mapping(address => bool) public isKeeper;
    mapping(address => bool) public isPauser;

    uint256 public currentDay;
    uint256 public spentToday;
    uint64 public lastExecution;
    uint256 public nonce;
    uint256 public totalEthSwapped;
    uint256 public totalAssetFunded;

    event Executed(uint256 indexed batchId, bytes32 sourceRef, uint256 ethIn, uint256 assetOut, uint256 minOut, uint256 price);
    event ParamsSet(uint256 minSwapWei, uint256 maxSwapWei, uint256 dailyCapWei, uint16 maxSlippageBps, uint32 minInterval);
    event RouteSet(address venue, address priceReference);
    event KeeperSet(address indexed keeper, bool allowed);
    event PauserSet(address indexed pauser, bool allowed);
    event OpenExecutionSet(bool open);
    event Paused(address indexed by);
    event Unpaused();
    event Swept(address indexed token, uint256 amount);

    error IsPaused();
    error NotKeeper();
    error NotPauser();
    error BelowThreshold(uint256 available, uint256 minSwapWei);
    error TooSoon(uint256 nextAllowed);
    error ShortOutput(uint256 got, uint256 minOut);

    constructor(
        address owner_,
        IERC20 asset_,
        IParbFundingAdapter adapter_,
        ISwapVenue venue_,
        IPriceReference reference_,
        uint256 minSwapWei_,
        uint256 maxSwapWei_,
        uint256 dailyCapWei_,
        uint16 maxSlippageBps_,
        uint32 minInterval_
    ) Ownable(owner_) {
        require(address(asset_) != address(0) && address(adapter_) != address(0), "zero");
        asset = asset_;
        adapter = adapter_;
        _setRoute(venue_, reference_);
        _setParams(minSwapWei_, maxSwapWei_, dailyCapWei_, maxSlippageBps_, minInterval_);
    }

    receive() external payable {}

    // ───────────────────────────── execution ─────────────────────────────

    /// @notice ETH this call would swap now (0 if below the threshold, paused, or capped for today).
    function executable() public view returns (uint256 amount) {
        if (paused) return 0;
        uint256 left = dailyCapWei - (_today() == currentDay ? spentToday : 0);
        amount = address(this).balance;
        if (amount > maxSwapWei) amount = maxSwapWei;
        if (amount > left) amount = left;
        if (amount < minSwapWei) amount = 0;
    }

    function execute() external nonReentrant returns (uint256 batchId) {
        if (paused) revert IsPaused();
        if (!openExecution && !isKeeper[msg.sender]) revert NotKeeper();
        if (lastExecution != 0 && block.timestamp < lastExecution + minInterval) revert TooSoon(lastExecution + minInterval);
        uint256 day = _today();
        if (day != currentDay) {
            currentDay = day;
            spentToday = 0;
        }
        uint256 ethIn = executable();
        if (ethIn == 0) revert BelowThreshold(address(this).balance, minSwapWei);

        uint256 price = priceReference.assetPerEth(); // reverts on doubtful prices: nothing is swapped
        uint256 minOut = (ethIn * price / 1e18) * (10_000 - maxSlippageBps) / 10_000;
        uint256 before = asset.balanceOf(address(adapter));
        venue.swapExactETHForToken{value: ethIn}(address(asset), minOut, address(adapter), block.timestamp);
        uint256 got = asset.balanceOf(address(adapter)) - before;
        if (got < minOut) revert ShortOutput(got, minOut);

        spentToday += ethIn;
        lastExecution = uint64(block.timestamp);
        totalEthSwapped += ethIn;
        totalAssetFunded += got;
        bytes32 sourceRef = keccak256(abi.encode(block.chainid, address(this), ++nonce));
        batchId = adapter.fund(sourceRef, got);
        emit Executed(batchId, sourceRef, ethIn, got, minOut, price);
    }

    function _today() internal view returns (uint256) {
        return block.timestamp / 1 days;
    }

    // ───────────────────────────── controls ─────────────────────────────

    function pause() external {
        if (msg.sender != owner() && !isPauser[msg.sender]) revert NotPauser();
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused();
    }

    function setParams(uint256 minSwapWei_, uint256 maxSwapWei_, uint256 dailyCapWei_, uint16 maxSlippageBps_, uint32 minInterval_)
        external
        onlyOwner
    {
        _setParams(minSwapWei_, maxSwapWei_, dailyCapWei_, maxSlippageBps_, minInterval_);
    }

    function setRoute(ISwapVenue venue_, IPriceReference reference_) external onlyOwner {
        _setRoute(venue_, reference_);
    }

    function setKeeper(address keeper, bool allowed) external onlyOwner {
        isKeeper[keeper] = allowed;
        emit KeeperSet(keeper, allowed);
    }

    function setPauser(address pauser, bool allowed) external onlyOwner {
        isPauser[pauser] = allowed;
        emit PauserSet(pauser, allowed);
    }

    function setOpenExecution(bool open) external onlyOwner {
        openExecution = open;
        emit OpenExecutionSet(open);
    }

    /// @notice Emergency exit: funds can only go back to the owner (the Arbitrage Funding Safe).
    function sweep(address token) external onlyOwner nonReentrant {
        uint256 amount;
        if (token == address(0)) {
            amount = address(this).balance;
            (bool ok,) = owner().call{value: amount}("");
            require(ok, "eth");
        } else {
            amount = IERC20(token).balanceOf(address(this));
            IERC20(token).safeTransfer(owner(), amount);
        }
        emit Swept(token, amount);
    }

    function _setParams(uint256 minSwapWei_, uint256 maxSwapWei_, uint256 dailyCapWei_, uint16 maxSlippageBps_, uint32 minInterval_)
        internal
    {
        require(minSwapWei_ > 0 && minSwapWei_ <= maxSwapWei_ && maxSwapWei_ <= dailyCapWei_, "amounts");
        require(maxSlippageBps_ > 0 && maxSlippageBps_ <= MAX_SLIPPAGE_CAP_BPS, "slippage");
        require(minInterval_ <= MAX_INTERVAL, "interval");
        minSwapWei = minSwapWei_;
        maxSwapWei = maxSwapWei_;
        dailyCapWei = dailyCapWei_;
        maxSlippageBps = maxSlippageBps_;
        minInterval = minInterval_;
        emit ParamsSet(minSwapWei_, maxSwapWei_, dailyCapWei_, maxSlippageBps_, minInterval_);
    }

    function _setRoute(ISwapVenue venue_, IPriceReference reference_) internal {
        require(address(venue_) != address(0) && address(reference_) != address(0), "zero");
        venue = venue_;
        priceReference = reference_;
        emit RouteSet(address(venue_), address(reference_));
    }
}
