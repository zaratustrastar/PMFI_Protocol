// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

interface IUniswapV3PoolMint {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function mint(address recipient, int24 tickLower, int24 tickUpper, uint128 amount, bytes calldata data)
        external
        returns (uint256 amount0, uint256 amount1);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data)
        external
        returns (int256 amount0, int256 amount1);
}

/// @notice LOCAL/TEST ONLY. Adds liquidity to, and swaps against, a Uniswap V3 pool from tokens the caller
///         sends in the same transaction (pays in the mint/swap callbacks from the caller's allowance).
contract V3LiquidityHelper {
    address private _payer;

    function mint(IUniswapV3PoolMint pool, int24 tickLower, int24 tickUpper, uint128 liquidity, address recipient)
        external
        returns (uint256 amount0, uint256 amount1)
    {
        _payer = msg.sender;
        (amount0, amount1) = pool.mint(recipient, tickLower, tickUpper, liquidity, abi.encode(pool));
        _payer = address(0);
    }

    /// Exact-in swap paid by the caller (used to move or manipulate the price in tests and the simulation).
    function swap(IUniswapV3PoolMint pool, bool zeroForOne, uint256 amountIn, uint160 limit, address recipient)
        external
        returns (int256 amount0, int256 amount1)
    {
        _payer = msg.sender;
        (amount0, amount1) = pool.swap(recipient, zeroForOne, int256(amountIn), limit, abi.encode(pool));
        _payer = address(0);
    }

    function uniswapV3MintCallback(uint256 owed0, uint256 owed1, bytes calldata data) external {
        _pay(data, owed0, owed1);
    }

    function uniswapV3SwapCallback(int256 d0, int256 d1, bytes calldata data) external {
        _pay(data, d0 > 0 ? uint256(d0) : 0, d1 > 0 ? uint256(d1) : 0);
    }

    function _pay(bytes calldata data, uint256 owed0, uint256 owed1) private {
        address pool = abi.decode(data, (address));
        require(msg.sender == pool && _payer != address(0), "callback");
        if (owed0 > 0) require(IERC20(IUniswapV3PoolMint(pool).token0()).transferFrom(_payer, pool, owed0), "pay0");
        if (owed1 > 0) require(IERC20(IUniswapV3PoolMint(pool).token1()).transferFrom(_payer, pool, owed1), "pay1");
    }
}
