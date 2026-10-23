// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @notice Conservative ETH price in the deposit asset. Reverts instead of returning a doubtful price.
interface IPriceReference {
    /// @return price amount of the asset (in its smallest unit) per 1e18 wei of ETH
    function assetPerEth() external view returns (uint256 price);
}

/// @notice Swaps native ETH for `tokenOut` and must deliver at least `minOut` to `recipient` or revert.
interface ISwapVenue {
    function swapExactETHForToken(address tokenOut, uint256 minOut, address recipient, uint256 deadline)
        external
        payable
        returns (uint256 amountOut);
}
