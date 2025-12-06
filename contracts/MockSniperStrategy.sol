// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "./ISniperStrategy.sol";

/**
 * @title MockSniperStrategy
 * @notice A mock strategy for testing PredictFiSniperVault
 * @dev Implements ISniperStrategy with manual control over totalStrategyValue
 */
contract MockSniperStrategy is ISniperStrategy, Ownable {
    using SafeERC20 for IERC20;

    /// @notice The underlying asset (USDC)
    address public override asset;

    /// @notice The total value of assets in this strategy
    uint256 public override totalStrategyValue;

    /// @notice The vault that owns this strategy
    address public vault;

    /// @notice Emitted when funds are invested
    event Invested(uint256 amount);

    /// @notice Emitted when funds are withdrawn to vault
    event WithdrawnToVault(uint256 amount);

    /// @notice Emitted when totalStrategyValue is manually set
    event TotalStrategyValueSet(uint256 oldValue, uint256 newValue);

    /**
     * @notice Constructor
     * @param asset_ The address of the underlying asset (TestUSDC)
     */
    constructor(address asset_) Ownable(msg.sender) {
        require(asset_ != address(0), "Invalid asset");
        asset = asset_;
        totalStrategyValue = 0;
    }

    /**
     * @notice Set the vault address
     * @param vault_ The vault address
     */
    function setVault(address vault_) external onlyOwner {
        require(vault_ != address(0), "Invalid vault");
        vault = vault_;
    }

    /**
     * @notice Invest funds from the vault into this strategy
     * @param amount The amount to invest
     * @dev Only callable by the vault
     */
    function invest(uint256 amount) external override {
        require(msg.sender == vault, "Only vault can invest");
        require(amount > 0, "Amount must be > 0");
        
        IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
        totalStrategyValue += amount;
        
        emit Invested(amount);
    }

    /**
     * @notice Withdraw funds from strategy back to the vault
     * @param amount The amount to withdraw
     * @dev Only callable by the vault
     */
    function withdrawToVault(uint256 amount) external override {
        require(msg.sender == vault, "Only vault can withdraw");
        require(amount > 0, "Amount must be > 0");
        require(amount <= totalStrategyValue, "Insufficient strategy value");
        
        totalStrategyValue -= amount;
        IERC20(asset).safeTransfer(vault, amount);
        
        emit WithdrawnToVault(amount);
    }

    /**
     * @notice Manually set totalStrategyValue for testing
     * @param value The new total strategy value
     */
    function setTotalStrategyValue(uint256 value) external onlyOwner {
        emit TotalStrategyValueSet(totalStrategyValue, value);
        totalStrategyValue = value;
    }
}
