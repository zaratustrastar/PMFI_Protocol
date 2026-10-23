// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {V3LiquidityHelper, IUniswapV3PoolMint} from "../src/mocks/V3LiquidityHelper.sol";

interface IUniswapV3FactoryLite {
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
}

interface IUniswapV3PoolInit {
    function initialize(uint160 sqrtPriceX96) external;
    function increaseObservationCardinalityNext(uint16 observationCardinalityNext) external;
    function token0() external view returns (address);
}

interface IWETHDeposit {
    function deposit() external payable;
}

/// @notice LOCAL/TEST stand-in for the Robinhood Chain WETH/USDG Uniswap V3 market: the official Uniswap V3
///         factory build (the v3-core npm artifact), a 0.05% pool at ~2,500 USDG per ETH with a ±50% range,
///         and an oracle buffer large enough for a 30-minute TWAP.
abstract contract UniV3LocalKit {
    Vm private constant _vm3 = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    struct V3Market {
        address factory;
        address pool;
        V3LiquidityHelper helper;
        bool wethIsToken0;
    }

    /// `usdgPerEth` in whole USDG; `lp` owns the position. Caller must hold `ethAmount` ETH and `usdgAmount` USDG.
    function _deployV3EthUsdg(address weth, address usdg, uint256 usdgPerEth, uint256 ethAmount, uint256 usdgAmount, address lp)
        internal
        returns (V3Market memory m)
    {
        m.factory = _vm3.deployCode("node_modules/@uniswap/v3-core/artifacts/contracts/UniswapV3Factory.sol/UniswapV3Factory.json");
        m.pool = IUniswapV3FactoryLite(m.factory).createPool(weth, usdg, 500);
        m.wethIsToken0 = IUniswapV3PoolInit(m.pool).token0() == weth;
        // raw price token1/token0: USDG(6) per WETH(18) = usdgPerEth * 1e6 / 1e18
        int24 tick = _tickFor(usdgPerEth, m.wethIsToken0);
        IUniswapV3PoolInit(m.pool).initialize(TickMath.getSqrtPriceAtTick(tick));
        IUniswapV3PoolInit(m.pool).increaseObservationCardinalityNext(200);
        m.helper = new V3LiquidityHelper();

        int24 lo = ((tick - 6_930) / 10) * 10;
        int24 hi = ((tick + 6_930) / 10) * 10;
        (uint256 a0, uint256 a1) = m.wethIsToken0 ? (ethAmount, usdgAmount) : (usdgAmount, ethAmount);
        uint128 liquidity = LiquidityAmounts.getLiquidityForAmounts(
            TickMath.getSqrtPriceAtTick(tick), TickMath.getSqrtPriceAtTick(lo), TickMath.getSqrtPriceAtTick(hi), a0, a1
        );
        IWETHDeposit(weth).deposit{value: ethAmount}();
        IERC20(weth).approve(address(m.helper), ethAmount);
        IERC20(usdg).approve(address(m.helper), usdgAmount);
        m.helper.mint(IUniswapV3PoolMint(m.pool), lo, hi, liquidity, lp);
    }

    /// Tick nearest to `usdgPerEth` (whole USDG per ETH) for the pool's token order.
    function _tickFor(uint256 usdgPerEth, bool wethIsToken0) internal pure returns (int24) {
        // 1.0001^t = usdgPerEth * 1e-12 (WETH token0) or its inverse; search via TickMath (monotone).
        // price X192 = usdgPerEth * 2^192 / 1e12
        uint256 target = wethIsToken0 ? (usdgPerEth << 96) / 1e12 : (uint256(1e12) << 96) / usdgPerEth; // priceX96
        int24 lo = TickMath.MIN_TICK;
        int24 hi = TickMath.MAX_TICK;
        while (hi - lo > 1) {
            int24 mid = int24((int256(lo) + int256(hi)) / 2);
            uint256 s = TickMath.getSqrtPriceAtTick(mid);
            uint256 p = FullMath.mulDiv(s, s, 1 << 96);
            if (p <= target) lo = mid;
            else hi = mid;
        }
        return (lo / 10) * 10;
    }
}
