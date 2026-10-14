// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {ISwapVenue} from "./IPriceReference.sol";

interface IWETH9 {
    function deposit() external payable;
    function transfer(address to, uint256 value) external returns (bool);
}

interface IUniswapV3PoolSwap {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1);
}

/// @notice ETH -> token through one Uniswap V3 pool (on Robinhood Chain: the WETH/USDG pool). Wraps ETH, swaps
///         exact-in straight against the pool, pays in the callback, and requires the whole input to fill and at
///         least `minOut` to arrive. No owner, no balances kept between calls.
contract UniV3SwapVenue is ISwapVenue {
    using SafeERC20 for IERC20;

    IUniswapV3PoolSwap public immutable pool;
    IWETH9 public immutable weth;
    address public immutable token; // the pool's other token
    bool internal immutable wethIsToken0;

    error UnknownToken();
    error Expired();
    error PartialFill(uint256 paid, uint256 expected);
    error Slippage(uint256 got, uint256 minOut);
    error NotPool();

    constructor(IUniswapV3PoolSwap pool_, IWETH9 weth_) {
        bool zero = pool_.token0() == address(weth_);
        require(zero || pool_.token1() == address(weth_), "pool has no WETH");
        pool = pool_;
        weth = weth_;
        wethIsToken0 = zero;
        token = zero ? pool_.token1() : pool_.token0();
    }

    function swapExactETHForToken(address tokenOut, uint256 minOut, address recipient, uint256 deadline)
        external
        payable
        returns (uint256 amountOut)
    {
        if (tokenOut != token) revert UnknownToken();
        if (block.timestamp > deadline) revert Expired();
        require(msg.value > 0 && msg.value <= uint256(type(int256).max), "amount");
        weth.deposit{value: msg.value}();
        uint256 before = IERC20(token).balanceOf(recipient);
        (int256 a0, int256 a1) = pool.swap(
            recipient,
            wethIsToken0,
            int256(msg.value),
            wethIsToken0 ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1,
            ""
        );
        uint256 paid = uint256(wethIsToken0 ? a0 : a1);
        if (paid != msg.value) revert PartialFill(paid, msg.value);
        amountOut = IERC20(token).balanceOf(recipient) - before;
        if (amountOut < minOut) revert Slippage(amountOut, minOut);
    }

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        if (msg.sender != address(pool)) revert NotPool();
        uint256 owed = uint256(wethIsToken0 ? amount0Delta : amount1Delta);
        require(weth.transfer(msg.sender, owed), "pay");
    }
}
