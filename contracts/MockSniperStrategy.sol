// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "./ISniperStrategy.sol";

/**
 * @title MockSniperStrategy
 * @notice A mock strategy for testing PredictFiSniperVault with keeper NAV updates
 * @dev Implements ISniperStrategy with manual control over totalStrategyValue
 */
contract MockSniperStrategy is ISniperStrategy {
    using SafeERC20 for IERC20;

    /// @notice The underlying asset (USDC)
    address public override asset;

    /// @notice The total value of assets in this strategy
    uint256 public override totalStrategyValue;

    /// @notice The vault that owns this strategy
    address public vault;

    /// @notice The owner of this strategy
    address public owner;

    /// @notice The keeper that can update NAV
    address public keeper;

    /// @notice Emitted when funds are invested
    event Invested(uint256 amount);

    /// @notice Emitted when funds are withdrawn to vault
    event WithdrawnToVault(uint256 amount);

    /// @notice Emitted when totalStrategyValue is updated by keeper
    event StrategyValueUpdated(uint256 oldValue, uint256 newValue);

    /// @notice Emitted when keeper is updated
    event KeeperUpdated(address oldKeeper, address newKeeper);

    /// @notice Emitted when vault is updated
    event VaultUpdated(address oldVault, address newVault);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    modifier onlyKeeper() {
        require(msg.sender == keeper, "not keeper");
        _;
    }

    /**
     * @notice Constructor
     * @param asset_ The address of the underlying asset (TestUSDC)
     * @param vault_ The address of the vault (can be address(0) if set later)
     */
    constructor(address asset_, address vault_) {
        require(asset_ != address(0), "Invalid asset");
        asset = asset_;
        vault = vault_;
        owner = msg.sender;
        totalStrategyValue = 0;
    }

    /**
     * @notice Set the vault address
     * @param vault_ The vault address
     */
    function setVault(address vault_) external onlyOwner {
        require(vault_ != address(0), "Invalid vault");
        emit VaultUpdated(vault, vault_);
        vault = vault_;
    }

    /**
     * @notice Set the keeper address
     * @param _keeper The keeper address
     */
    function setKeeper(address _keeper) external onlyOwner {
        emit KeeperUpdated(keeper, _keeper);
        keeper = _keeper;
    }

    /**
     * @notice Invest funds from the vault into this strategy
     * @param amount The amount to invest
     * @dev Only callable by the vault
     */
    function invest(uint256 amount) external override onlyVault {
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
    function withdrawToVault(uint256 amount) external override onlyVault {
        require(amount > 0, "Amount must be > 0");
        require(amount <= totalStrategyValue, "Insufficient strategy value");
        
        totalStrategyValue -= amount;
        IERC20(asset).safeTransfer(vault, amount);
        
        emit WithdrawnToVault(amount);
    }

    /**
     * @notice Update the strategy NAV (called by keeper bot)
     * @param newValue The new total strategy value
     * @dev Only callable by keeper
     */
    function updateStrategyValue(uint256 newValue) external onlyKeeper {
        emit StrategyValueUpdated(totalStrategyValue, newValue);
        totalStrategyValue = newValue;
    }

    /**
     * @notice Legacy function for backward compatibility with tests
     * @param value The new total strategy value
     * @dev Only callable by owner (for testing purposes)
     */
    function setTotalStrategyValue(uint256 value) external onlyOwner {
        emit StrategyValueUpdated(totalStrategyValue, value);
        totalStrategyValue = value;
    }
}
