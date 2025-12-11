// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/utils/math/Math.sol";
import "./ISniperStrategy.sol";

/**
 * @title PredictFiSniperVaultV3
 * @notice ERC4626 vault with 1% withdrawal tax, per-wallet caps, and configurable limits
 * @dev V3 adds withdrawal tax on top of V2 functionality
 */
contract PredictFiSniperVaultV3 is ERC4626, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @notice The trading strategy contract
    ISniperStrategy public strategy;

    /// @notice Address that receives performance fees
    address public feeCollector;

    /// @notice Address that receives withdrawal tax
    address public taxCollector;

    /// @notice Performance fee in basis points (e.g., 500 = 5%)
    uint256 public performanceFeeBps;

    /// @notice Withdrawal tax in basis points (e.g., 100 = 1%)
    uint256 public withdrawalTaxBps;

    /// @notice Buffer percentage in basis points to keep as idle USDC
    uint256 public bufferBps;

    /// @notice Last recorded total assets for performance fee calculation
    uint256 public lastTotalAssets;

    /// @notice Maximum total deposits allowed in the vault
    uint256 public maxTotalDeposits;

    /// @notice Maximum assets (USDC) each wallet can deposit
    uint256 public walletDepositCap;

    /// @notice Tracks total deposited per address
    mapping(address => uint256) public walletDeposited;

    /// @notice Emitted when there's not enough liquidity for a withdrawal
    event LiquidityShortfall(uint256 assetsNeeded, uint256 availableAssets);

    /// @notice Emitted when strategy is updated
    event StrategyUpdated(address indexed oldStrategy, address indexed newStrategy);

    /// @notice Emitted when fee collector is updated
    event FeeCollectorUpdated(address indexed oldCollector, address indexed newCollector);

    /// @notice Emitted when tax collector is updated
    event TaxCollectorUpdated(address indexed oldCollector, address indexed newCollector);

    /// @notice Emitted when performance fee is updated
    event PerformanceFeeBpsUpdated(uint256 oldFee, uint256 newFee);

    /// @notice Emitted when withdrawal tax is updated
    event WithdrawalTaxBpsUpdated(uint256 oldTax, uint256 newTax);

    /// @notice Emitted when buffer is updated
    event BufferBpsUpdated(uint256 oldBuffer, uint256 newBuffer);

    /// @notice Emitted when max deposits is updated
    event MaxTotalDepositsUpdated(uint256 oldMax, uint256 newMax);

    /// @notice Emitted when wallet deposit cap is updated
    event WalletDepositCapUpdated(uint256 oldCap, uint256 newCap);

    /// @notice Emitted when idle funds are invested
    event IdleInvested(uint256 amount);

    /// @notice Emitted when performance fees are harvested
    event FeesHarvested(uint256 profit, uint256 feeShares);

    /// @notice Emitted when withdrawal tax is collected
    event WithdrawalTaxCollected(address indexed user, uint256 grossAssets, uint256 taxAmount);

    /**
     * @notice Constructor
     * @param _asset The underlying asset (USDC)
     * @param _strategy The initial trading strategy
     * @param _feeCollector Address to receive performance fees
     * @param _taxCollector Address to receive withdrawal tax
     * @param _performanceFeeBps Performance fee in basis points
     * @param _withdrawalTaxBps Withdrawal tax in basis points
     * @param _bufferBps Buffer percentage in basis points
     * @param _maxTotalDeposits Maximum total deposits allowed
     * @param _walletDepositCap Maximum deposit per wallet
     */
    constructor(
        IERC20 _asset,
        ISniperStrategy _strategy,
        address _feeCollector,
        address _taxCollector,
        uint256 _performanceFeeBps,
        uint256 _withdrawalTaxBps,
        uint256 _bufferBps,
        uint256 _maxTotalDeposits,
        uint256 _walletDepositCap
    )
        ERC4626(_asset)
        ERC20("PredictFi Sniper Vault V3", "pSNIPERv3")
        Ownable(msg.sender)
    {
        require(address(_strategy) != address(0), "Invalid strategy");
        require(_feeCollector != address(0), "Invalid fee collector");
        require(_taxCollector != address(0), "Invalid tax collector");
        require(_performanceFeeBps <= 5000, "Fee too high"); // Max 50%
        require(_withdrawalTaxBps <= 1000, "Tax too high"); // Max 10%
        require(_bufferBps <= 10000, "Buffer too high");

        strategy = _strategy;
        feeCollector = _feeCollector;
        taxCollector = _taxCollector;
        performanceFeeBps = _performanceFeeBps;
        withdrawalTaxBps = _withdrawalTaxBps;
        bufferBps = _bufferBps;
        maxTotalDeposits = _maxTotalDeposits;
        walletDepositCap = _walletDepositCap;
        lastTotalAssets = 0;
    }

    // ============================================
    // View Functions
    // ============================================

    /**
     * @notice Returns total assets under management
     * @return Total idle USDC plus strategy value
     */
    function totalAssets() public view override returns (uint256) {
        uint256 idle = IERC20(asset()).balanceOf(address(this));
        uint256 strategyValue = address(strategy) != address(0)
            ? strategy.totalStrategyValue()
            : 0;
        return idle + strategyValue;
    }

    /**
     * @notice Calculate gross assets needed to deliver net assets after tax
     * @param netAssets The net amount user wants to receive
     * @return Gross assets before tax (what user needs to burn shares for)
     */
    function grossAssetsForNet(uint256 netAssets) public view returns (uint256) {
        // net = gross * (1 - tax/10000)
        // gross = net / (1 - tax/10000) = net * 10000 / (10000 - tax)
        return (netAssets * 10000) / (10000 - withdrawalTaxBps);
    }

    /**
     * @notice Preview how many shares needed to withdraw assets (ERC4626 compliant)
     * @dev Returns shares needed to receive exactly 'assets' after tax
     * @param assets The net amount user will receive
     * @return Shares to burn
     */
    function previewWithdraw(uint256 assets) public view override returns (uint256) {
        // Calculate gross amount needed (before tax) to deliver 'assets' net
        uint256 grossAssets = grossAssetsForNet(assets);
        // Convert gross to shares
        return _convertToShares(grossAssets, Math.Rounding.Ceil);
    }

    /**
     * @notice Preview assets received after redeeming shares (ERC4626 compliant)
     * @dev Returns net assets after 1% tax deduction
     * @param shares The shares to redeem
     * @return Net assets user will receive
     */
    function previewRedeem(uint256 shares) public view override returns (uint256) {
        uint256 grossAssets = _convertToAssets(shares, Math.Rounding.Floor);
        uint256 tax = (grossAssets * withdrawalTaxBps) / 10000;
        return grossAssets - tax;
    }

    // ============================================
    // Owner Functions
    // ============================================

    /**
     * @notice Update the trading strategy
     * @param _strategy New strategy address
     */
    function setStrategy(ISniperStrategy _strategy) external onlyOwner {
        require(address(_strategy) != address(0), "Invalid strategy");
        emit StrategyUpdated(address(strategy), address(_strategy));
        strategy = _strategy;
    }

    /**
     * @notice Update the fee collector address
     * @param _feeCollector New fee collector address
     */
    function setFeeCollector(address _feeCollector) external onlyOwner {
        require(_feeCollector != address(0), "Invalid fee collector");
        emit FeeCollectorUpdated(feeCollector, _feeCollector);
        feeCollector = _feeCollector;
    }

    /**
     * @notice Update the tax collector address
     * @param _taxCollector New tax collector address
     */
    function setTaxCollector(address _taxCollector) external onlyOwner {
        require(_taxCollector != address(0), "Invalid tax collector");
        emit TaxCollectorUpdated(taxCollector, _taxCollector);
        taxCollector = _taxCollector;
    }

    /**
     * @notice Update the performance fee
     * @param _performanceFeeBps New fee in basis points
     */
    function setPerformanceFeeBps(uint256 _performanceFeeBps) external onlyOwner {
        require(_performanceFeeBps <= 5000, "Fee too high");
        emit PerformanceFeeBpsUpdated(performanceFeeBps, _performanceFeeBps);
        performanceFeeBps = _performanceFeeBps;
    }

    /**
     * @notice Update the withdrawal tax
     * @param _withdrawalTaxBps New tax in basis points
     */
    function setWithdrawalTaxBps(uint256 _withdrawalTaxBps) external onlyOwner {
        require(_withdrawalTaxBps <= 1000, "Tax too high");
        emit WithdrawalTaxBpsUpdated(withdrawalTaxBps, _withdrawalTaxBps);
        withdrawalTaxBps = _withdrawalTaxBps;
    }

    /**
     * @notice Update the buffer percentage
     * @param _bufferBps New buffer in basis points
     */
    function setBufferBps(uint256 _bufferBps) external onlyOwner {
        require(_bufferBps <= 10000, "Buffer too high");
        emit BufferBpsUpdated(bufferBps, _bufferBps);
        bufferBps = _bufferBps;
    }

    /**
     * @notice Update the maximum total deposits
     * @param _maxTotalDeposits New maximum deposits
     */
    function setMaxTotalDeposits(uint256 _maxTotalDeposits) external onlyOwner {
        emit MaxTotalDepositsUpdated(maxTotalDeposits, _maxTotalDeposits);
        maxTotalDeposits = _maxTotalDeposits;
    }

    /**
     * @notice Update the per-wallet deposit cap
     * @param _cap New wallet deposit cap
     */
    function setWalletDepositCap(uint256 _cap) external onlyOwner {
        emit WalletDepositCapUpdated(walletDepositCap, _cap);
        walletDepositCap = _cap;
    }

    // ============================================
    // Core Functions
    // ============================================

    /**
     * @notice Invest idle USDC above the buffer target into the strategy
     */
    function investIdle() external nonReentrant {
        uint256 total = totalAssets();
        uint256 targetIdle = (total * bufferBps) / 10000;
        uint256 currentIdle = IERC20(asset()).balanceOf(address(this));

        if (currentIdle > targetIdle) {
            uint256 toInvest = currentIdle - targetIdle;
            IERC20(asset()).safeIncreaseAllowance(address(strategy), toInvest);
            strategy.invest(toInvest);
            emit IdleInvested(toInvest);
        }
    }

    /**
     * @notice Harvest performance fees on profits
     * @dev Mints vault shares to feeCollector based on profit since last harvest
     */
    function harvest() external nonReentrant {
        uint256 currentTotal = totalAssets();

        if (currentTotal > lastTotalAssets) {
            uint256 profit = currentTotal - lastTotalAssets;
            uint256 feeAssets = (profit * performanceFeeBps) / 10000;

            if (feeAssets > 0) {
                uint256 feeShares = convertToShares(feeAssets);
                if (feeShares > 0) {
                    _mint(feeCollector, feeShares);
                    emit FeesHarvested(profit, feeShares);
                }
            }
        }

        lastTotalAssets = totalAssets();
    }

    // ============================================
    // Override Deposit/Mint with Per-Wallet Caps
    // ============================================

    /**
     * @notice Deposit assets with global and per-wallet cap enforcement
     */
    function deposit(
        uint256 assets,
        address receiver
    ) public override nonReentrant returns (uint256) {
        require(totalAssets() + assets <= maxTotalDeposits, "cap reached");
        require(walletDeposited[receiver] + assets <= walletDepositCap, "wallet cap reached");
        
        walletDeposited[receiver] += assets;
        uint256 shares = super.deposit(assets, receiver);
        lastTotalAssets = totalAssets();
        return shares;
    }

    /**
     * @notice Mint shares with global and per-wallet cap enforcement
     */
    function mint(
        uint256 shares,
        address receiver
    ) public override nonReentrant returns (uint256) {
        uint256 assets = previewMint(shares);
        require(totalAssets() + assets <= maxTotalDeposits, "cap reached");
        require(walletDeposited[receiver] + assets <= walletDepositCap, "wallet cap reached");
        
        walletDeposited[receiver] += assets;
        uint256 result = super.mint(shares, receiver);
        lastTotalAssets = totalAssets();
        return result;
    }

    // ============================================
    // Override Withdraw/Redeem with Tax and Liquidity
    // ============================================

    /**
     * @notice Withdraw assets with 1% tax (ERC4626 compliant)
     * @dev User specifies net assets they want to receive. Tax is calculated on gross.
     * @param assets The NET amount user wants to receive (after tax)
     * @param receiver Address to receive the net assets
     * @param owner Address whose shares will be burned
     * @return shares Amount of shares burned
     */
    function withdraw(
        uint256 assets,
        address receiver,
        address owner
    ) public override nonReentrant returns (uint256) {
        // 'assets' is what user wants to receive (net)
        // Calculate gross amount needed to deliver 'assets' after tax
        uint256 grossAssets = grossAssetsForNet(assets);
        uint256 tax = grossAssets - assets;
        
        // Prepare liquidity for gross amount
        _prepareLiquidity(grossAssets);
        
        // Calculate shares to burn (based on gross assets)
        uint256 shares = _convertToShares(grossAssets, Math.Rounding.Ceil);
        
        if (msg.sender != owner) {
            _spendAllowance(owner, msg.sender, shares);
        }
        
        _burn(owner, shares);
        
        // Transfer tax to collector, net to receiver
        IERC20(asset()).safeTransfer(taxCollector, tax);
        IERC20(asset()).safeTransfer(receiver, assets);
        
        emit Withdraw(msg.sender, receiver, owner, assets, shares);
        emit WithdrawalTaxCollected(owner, grossAssets, tax);
        
        lastTotalAssets = totalAssets();
        return shares;
    }

    /**
     * @notice Redeem shares with 1% tax (ERC4626 compliant)
     * @dev User specifies shares to burn. Receives net assets after tax.
     * @param shares Amount of shares to redeem
     * @param receiver Address to receive the net assets
     * @param owner Address whose shares will be burned
     * @return Net assets sent to receiver
     */
    function redeem(
        uint256 shares,
        address receiver,
        address owner
    ) public override nonReentrant returns (uint256) {
        // Calculate gross assets for the shares
        uint256 grossAssets = _convertToAssets(shares, Math.Rounding.Floor);
        uint256 tax = (grossAssets * withdrawalTaxBps) / 10000;
        uint256 netAssets = grossAssets - tax;
        
        // Prepare liquidity for gross amount
        _prepareLiquidity(grossAssets);
        
        if (msg.sender != owner) {
            _spendAllowance(owner, msg.sender, shares);
        }
        
        _burn(owner, shares);
        
        // Transfer tax to collector, net to receiver
        IERC20(asset()).safeTransfer(taxCollector, tax);
        IERC20(asset()).safeTransfer(receiver, netAssets);
        
        emit Withdraw(msg.sender, receiver, owner, netAssets, shares);
        emit WithdrawalTaxCollected(owner, grossAssets, tax);
        
        lastTotalAssets = totalAssets();
        return netAssets;
    }

    // ============================================
    // Internal Functions
    // ============================================

    /**
     * @notice Prepare liquidity for a withdrawal
     * @param assetsNeeded Amount of assets needed for the withdrawal
     * @dev Pulls from strategy if idle balance is insufficient
     */
    function _prepareLiquidity(uint256 assetsNeeded) internal {
        uint256 idle = IERC20(asset()).balanceOf(address(this));

        if (idle < assetsNeeded) {
            uint256 shortfall = assetsNeeded - idle;
            strategy.withdrawToVault(shortfall);

            uint256 newIdle = IERC20(asset()).balanceOf(address(this));
            if (newIdle < assetsNeeded) {
                emit LiquidityShortfall(assetsNeeded, newIdle);
                revert("Insufficient liquidity");
            }
        }
    }
}
