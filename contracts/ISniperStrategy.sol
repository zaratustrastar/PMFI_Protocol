// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ISniperStrategy
 * @notice Interface for sniper trading strategies used by PredictFiSniperVault
 */
interface ISniperStrategy {
    /**
     * @notice Returns the total value of assets managed by this strategy
     * @return The total value in the underlying asset denomination
     */
    function totalStrategyValue() external view returns (uint256);

    /**
     * @notice Invests the specified amount of assets into the strategy
     * @param amount The amount of assets to invest
     */
    function invest(uint256 amount) external;

    /**
     * @notice Withdraws assets from the strategy back to the vault
     * @param amount The amount of assets to withdraw
     */
    function withdrawToVault(uint256 amount) external;

    /**
     * @notice Returns the address of the underlying asset
     * @return The address of the asset token
     */
    function asset() external view returns (address);
}
