// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IMILORD} from "../interfaces/IMILORD.sol";

/// @notice Swap venue adapter (e.g. a thin Uniswap v4 Universal Router wrapper).
///         Must send at least `minOut` MILORD to `recipient` or revert.
interface ISwapAdapter {
    function swapExactETHForToken(address tokenOut, uint256 minOut, address recipient, uint256 deadline)
        external
        payable
        returns (uint256 amountOut);
}

/// @title BuybackBurner
/// @notice Holds the trading-tax "buyback and burn" budget. The budget is ETH until an
///         operator executes a buyback; only then is MILORD bought and burned (totalSupply drops).
///
///  Separate counters keep "budget collected" apart from "actually burned":
///    totalEthReceived     ETH that arrived as burn budget
///    totalEthSpent        ETH actually swapped
///    totalBurnedFromBuybacks / totalBurnedDirect   MILORD actually burned
///  Every buyback is stored as an auditable record.
///
///  Safety: operator-only execution, per-call min/max size, caller-supplied minimum output,
///  deadline, and the adapter address can only change after a delay.
contract BuybackBurner is Ownable2Step, ReentrancyGuard {
    struct Buyback {
        uint256 ethIn;
        uint256 milordBurned;
        uint64 timestamp;
        uint64 blockNumber;
        address operator;
    }

    uint256 public constant ADAPTER_CHANGE_DELAY = 2 days;

    IMILORD public immutable token;
    ISwapAdapter public adapter;
    address public pendingAdapter;
    uint64 public pendingAdapterEta;

    mapping(address => bool) public isOperator;
    uint256 public minBuybackWei = 0.01 ether;
    uint256 public maxBuybackWei = 5 ether;

    uint256 public totalEthReceived;
    uint256 public totalEthSpent;
    uint256 public totalBurnedFromBuybacks;
    uint256 public totalBurnedDirect;

    Buyback[] internal _buybacks;

    event BudgetReceived(address indexed from, uint256 amount);
    event BuybackExecuted(uint256 indexed id, uint256 ethIn, uint256 milordBurned, address operator);
    event DirectBurn(uint256 amount);
    event OperatorSet(address indexed operator, bool allowed);
    event LimitsSet(uint256 minWei, uint256 maxWei);
    event AdapterQueued(address adapter, uint64 eta);
    event AdapterSet(address adapter);

    error NotOperator();
    error OutOfBounds();
    error Slippage(uint256 got, uint256 minOut);
    error TooEarly();

    constructor(address owner_, IMILORD token_, ISwapAdapter adapter_) Ownable(owner_) {
        token = token_;
        adapter = adapter_;
        emit AdapterSet(address(adapter_));
    }

    receive() external payable {
        totalEthReceived += msg.value;
        emit BudgetReceived(msg.sender, msg.value);
    }

    /// @notice ETH budget not yet spent on buybacks.
    function budget() external view returns (uint256) {
        return address(this).balance;
    }

    function buybackAndBurn(uint256 ethIn, uint256 minOut, uint256 deadline)
        external
        nonReentrant
        returns (uint256 burned)
    {
        if (!isOperator[msg.sender]) revert NotOperator();
        if (ethIn < minBuybackWei || ethIn > maxBuybackWei || ethIn > address(this).balance) revert OutOfBounds();
        require(minOut > 0, "minOut");

        uint256 before = token.balanceOf(address(this));
        adapter.swapExactETHForToken{value: ethIn}(address(token), minOut, address(this), deadline);
        burned = token.balanceOf(address(this)) - before;
        if (burned < minOut) revert Slippage(burned, minOut);

        token.burn(burned);
        totalEthSpent += ethIn;
        totalBurnedFromBuybacks += burned;
        _buybacks.push(Buyback(ethIn, burned, uint64(block.timestamp), uint64(block.number), msg.sender));
        emit BuybackExecuted(_buybacks.length - 1, ethIn, burned, msg.sender);
    }

    /// @notice Burn MILORD held here (e.g. a token-denominated tax share). Permissionless.
    function burnHeldTokens() external nonReentrant returns (uint256 amount) {
        amount = token.balanceOf(address(this));
        if (amount == 0) return 0;
        token.burn(amount);
        totalBurnedDirect += amount;
        emit DirectBurn(amount);
    }

    // ───────────────────────────── admin ─────────────────────────────

    function setOperator(address operator, bool allowed) external onlyOwner {
        isOperator[operator] = allowed;
        emit OperatorSet(operator, allowed);
    }

    function setLimits(uint256 minWei, uint256 maxWei) external onlyOwner {
        if (minWei == 0 || minWei > maxWei) revert OutOfBounds();
        minBuybackWei = minWei;
        maxBuybackWei = maxWei;
        emit LimitsSet(minWei, maxWei);
    }

    /// @notice One-time initial route, for deployments where the pool (and so the adapter) only exists after
    ///         this contract: allowed only while no adapter has ever been set. Later changes use the delay.
    function setInitialAdapter(address adapter_) external onlyOwner {
        require(address(adapter) == address(0) && adapter_ != address(0), "adapter already set");
        adapter = ISwapAdapter(adapter_);
        emit AdapterSet(adapter_);
    }

    function queueAdapter(address adapter_) external onlyOwner {
        pendingAdapter = adapter_;
        pendingAdapterEta = uint64(block.timestamp + ADAPTER_CHANGE_DELAY);
        emit AdapterQueued(adapter_, pendingAdapterEta);
    }

    function executeAdapter() external onlyOwner {
        if (pendingAdapter == address(0) || block.timestamp < pendingAdapterEta) revert TooEarly();
        adapter = ISwapAdapter(pendingAdapter);
        pendingAdapter = address(0);
        pendingAdapterEta = 0;
        emit AdapterSet(address(adapter));
    }

    // ───────────────────────────── views ─────────────────────────────

    function buybackCount() external view returns (uint256) {
        return _buybacks.length;
    }

    function buyback(uint256 id) external view returns (Buyback memory) {
        return _buybacks[id];
    }
}
