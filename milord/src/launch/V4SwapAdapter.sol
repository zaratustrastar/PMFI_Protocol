// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice Exact-input ETH -> token swaps against fixed Uniswap v4 pools. Implements the `ISwapAdapter`
///         interface used by BuybackBurner (ETH -> MILORD on the Hookr pool) and usable by the Arbitrage Funding
///         Safe (ETH -> USDG). No owner, no stored funds: the pool for each output token is fixed at deployment,
///         every swap enforces `minOut` (the caller's price limit) and a deadline, and output goes straight to
///         `recipient`. Replacing the route means deploying a new adapter, which BuybackBurner only accepts after
///         its 2-day adapter delay.
contract V4SwapAdapter is IUnlockCallback {
    IPoolManager public immutable poolManager;
    mapping(address token => PoolKey) internal _keys;
    address[] public tokens;

    error UnknownToken(address token);
    error Expired();
    error Slippage(uint256 out, uint256 minOut);

    event Swapped(address indexed tokenOut, address indexed recipient, uint256 ethIn, uint256 amountOut);

    constructor(IPoolManager manager, PoolKey[] memory keys) {
        poolManager = manager;
        for (uint256 i; i < keys.length; ++i) {
            require(Currency.unwrap(keys[i].currency0) == address(0), "pool must be native ETH / token");
            address t = Currency.unwrap(keys[i].currency1);
            require(Currency.unwrap(_keys[t].currency1) == address(0), "duplicate token");
            _keys[t] = keys[i];
            tokens.push(t);
        }
    }

    function poolKey(address token) external view returns (PoolKey memory) {
        return _keys[token];
    }

    function swapExactETHForToken(address tokenOut, uint256 minOut, address recipient, uint256 deadline)
        external
        payable
        returns (uint256 out)
    {
        if (block.timestamp > deadline) revert Expired();
        PoolKey memory key = _keys[tokenOut];
        if (Currency.unwrap(key.currency1) != tokenOut || tokenOut == address(0)) revert UnknownToken(tokenOut);
        out = abi.decode(poolManager.unlock(abi.encode(key, msg.value, recipient)), (uint256));
        if (out < minOut) revert Slippage(out, minOut);
        emit Swapped(tokenOut, recipient, msg.value, out);
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(poolManager), "not pool manager");
        (PoolKey memory key, uint256 ethIn, address recipient) = abi.decode(data, (PoolKey, uint256, address));
        BalanceDelta d = poolManager.swap(
            key,
            SwapParams({zeroForOne: true, amountSpecified: -int256(ethIn), sqrtPriceLimitX96: TickMath.MIN_SQRT_PRICE + 1}),
            ""
        );
        uint256 paid = uint256(uint128(-d.amount0()));
        require(paid == ethIn, "partial fill"); // pool ran out of liquidity: revert rather than refund
        uint256 out = uint256(uint128(d.amount1()));
        poolManager.settle{value: paid}();
        poolManager.take(key.currency1, recipient, out);
        return abi.encode(out);
    }
}
