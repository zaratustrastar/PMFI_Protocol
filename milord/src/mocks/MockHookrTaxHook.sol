// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta, toBeforeSwapDelta, BeforeSwapDeltaLibrary} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {Currency, CurrencyLibrary} from "@uniswap/v4-core/src/types/Currency.sol";

/// @notice TEST-ONLY stand-in for Hookr's "Tax + Conversion" Hook Block on a native-ETH / token pool.
///         It is NOT Hookr's code and must never be deployed: on Robinhood Chain the pool is opened through
///         Hookr's own launcher and hook. It reproduces the economics we rely on so the local simulation can
///         run real Uniswap v4 swaps:
///           - buy tax and sell tax in bps of the ETH side, both paid in ETH (currency0),
///           - a platform commission (Hookr) taken as a share of the tax,
///           - the rest accrued to one project recipient (our TradingTaxSplitter),
///           - every parameter fixed at construction (Hookr blocks are frozen once a market opens).
///         Supported: exact-input buys and sells, exact-output buys. Exact-output sells revert (not needed by
///         the simulation; Hookr's own handling must be checked against its source).
contract MockHookrTaxHook {
    using CurrencyLibrary for Currency;

    IPoolManager public immutable poolManager;
    uint16 public immutable buyTaxBps;
    uint16 public immutable sellTaxBps;
    uint16 public immutable commissionBps; // share of the tax kept by the platform
    address public immutable platform; // Hookr treasury
    address public immutable projectRecipient; // TradingTaxSplitter

    uint256 public platformAccrued;
    uint256 public projectAccrued;
    // reconciliation counters (ETH)
    uint256 public buyVolume;
    uint256 public sellVolume;
    uint256 public totalBuyTax;
    uint256 public totalSellTax;
    uint256 public totalPlatformPaid;
    uint256 public totalProjectPaid;

    event Taxed(bool isBuy, uint256 ethVolume, uint256 tax, uint256 platformCut, uint256 projectCut);
    event Collected(uint256 platformAmount, uint256 projectAmount);

    constructor(
        IPoolManager manager,
        uint16 buyBps,
        uint16 sellBps,
        uint16 commission,
        address platform_,
        address projectRecipient_
    ) {
        require(buyBps <= 1_000 && sellBps <= 1_000 && commission <= 10_000, "bounds");
        poolManager = manager;
        buyTaxBps = buyBps;
        sellTaxBps = sellBps;
        commissionBps = commission;
        platform = platform_;
        projectRecipient = projectRecipient_;
    }

    modifier onlyPoolManager() {
        require(msg.sender == address(poolManager), "not pool manager");
        _;
    }

    function beforeSwap(address, PoolKey calldata key, SwapParams calldata params, bytes calldata)
        external
        onlyPoolManager
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        require(Currency.unwrap(key.currency0) == address(0), "ETH must be currency0");
        // Exact-input buy: ETH is the specified side. Tax it before the swap so the pool only sees the rest.
        if (params.zeroForOne && params.amountSpecified < 0) {
            uint256 ethIn = uint256(-params.amountSpecified);
            uint256 tax = (ethIn * buyTaxBps) / 10_000;
            if (tax > 0) {
                poolManager.mint(address(this), key.currency0.toId(), tax); // ERC-6909 claim: needs no PoolManager ETH mid-swap
                _accrue(true, ethIn, tax);
            }
            return (IHooks.beforeSwap.selector, toBeforeSwapDelta(int128(int256(tax)), 0), 0);
        }
        require(params.zeroForOne || params.amountSpecified < 0, "exact-output sells unsupported");
        return (IHooks.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, 0);
    }

    function afterSwap(address, PoolKey calldata key, SwapParams calldata params, BalanceDelta delta, bytes calldata)
        external
        onlyPoolManager
        returns (bytes4, int128)
    {
        if (params.zeroForOne && params.amountSpecified < 0) return (IHooks.afterSwap.selector, 0); // taxed before
        // Exact-output buy (ETH is unspecified input) or exact-input sell (ETH is unspecified output).
        int128 eth = delta.amount0();
        uint256 volume = uint256(uint128(eth < 0 ? -eth : eth));
        uint256 tax = (volume * (params.zeroForOne ? buyTaxBps : sellTaxBps)) / 10_000;
        if (tax > 0) {
            poolManager.mint(address(this), key.currency0.toId(), tax); // ERC-6909 claim: needs no PoolManager ETH mid-swap
            _accrue(params.zeroForOne, volume, tax);
        }
        return (IHooks.afterSwap.selector, int128(int256(tax)));
    }

    function _accrue(bool isBuy, uint256 volume, uint256 tax) internal {
        uint256 cut = (tax * commissionBps) / 10_000;
        platformAccrued += cut;
        projectAccrued += tax - cut;
        if (isBuy) {
            buyVolume += volume;
            totalBuyTax += tax;
        } else {
            sellVolume += volume;
            totalSellTax += tax;
        }
        emit Taxed(isBuy, volume, tax, cut, tax - cut);
    }

    /// @notice Permissionless settlement: pays accrued ETH to the fixed platform and project recipients.
    function collect() external returns (uint256 p, uint256 q) {
        (p, q) = (platformAccrued, projectAccrued);
        platformAccrued = 0;
        projectAccrued = 0;
        totalPlatformPaid += p;
        totalProjectPaid += q;
        uint256 claims = poolManager.balanceOf(address(this), 0); // native ETH claim id
        if (claims > 0) poolManager.unlock(abi.encode(claims));
        if (p > 0) _send(platform, p);
        if (q > 0) _send(projectRecipient, q);
        emit Collected(p, q);
    }

    function unlockCallback(bytes calldata data) external onlyPoolManager returns (bytes memory) {
        uint256 amount = abi.decode(data, (uint256));
        poolManager.burn(address(this), 0, amount);
        poolManager.take(Currency.wrap(address(0)), address(this), amount);
        return "";
    }

    function _send(address to, uint256 amount) internal {
        (bool ok,) = to.call{value: amount}("");
        require(ok, "send failed");
    }

    receive() external payable {
        require(msg.sender == address(poolManager), "only pool manager");
    }
}
