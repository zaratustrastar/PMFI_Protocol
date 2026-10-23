// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {IPriceReference} from "./IPriceReference.sol";

interface IUniswapV3PoolOracle {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function slot0()
        external
        view
        returns (uint160 sqrtPriceX96, int24 tick, uint16, uint16, uint16, uint8, bool);
    function observe(uint32[] calldata secondsAgos)
        external
        view
        returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidityCumulativeX128s);
}

/// @notice ETH price from a Uniswap V3 pool's built-in oracle (time-weighted mean tick over `window` seconds).
///         Uniswap V3 records an observation on every block's first swap; Uniswap v4 has no such oracle, which is
///         why the ETH -> USDG reference reads the Robinhood WETH/USDG V3 pool, not a v4 pool.
///         Manipulation guards:
///          - the mean is taken over `window` (e.g. 30 min): moving it costs holding a skewed price for that long;
///          - `observe` reverts if the pool's history is shorter than `window` (no fallback price);
///          - the current tick may differ from the mean by at most `maxTickDeviation` (~1 bp per tick), so a
///            swap that pushed spot away inside the block is refused rather than traded against.
contract UniV3TwapReference is IPriceReference {
    IUniswapV3PoolOracle public immutable pool;
    address public immutable weth;
    uint32 public immutable window;
    int24 public immutable maxTickDeviation;
    bool internal immutable wethIsToken0;

    error SpotDeviatesFromTwap(int24 spot, int24 mean);

    constructor(IUniswapV3PoolOracle pool_, address weth_, uint32 window_, int24 maxTickDeviation_) {
        require(window_ >= 10 minutes && window_ <= 1 days, "window");
        require(maxTickDeviation_ > 0 && maxTickDeviation_ <= 2000, "deviation");
        bool zero = pool_.token0() == weth_;
        require(zero || pool_.token1() == weth_, "pool has no WETH");
        pool = pool_;
        weth = weth_;
        window = window_;
        maxTickDeviation = maxTickDeviation_;
        wethIsToken0 = zero;
    }

    function meanTick() public view returns (int24 tick) {
        uint32[] memory ago = new uint32[](2);
        ago[0] = window;
        int56[] memory cum;
        (cum,) = pool.observe(ago);
        int56 delta = cum[1] - cum[0];
        tick = int24(delta / int56(uint56(window)));
        if (delta < 0 && (delta % int56(uint56(window)) != 0)) tick--; // round toward negative infinity
    }

    function assetPerEth() external view returns (uint256) {
        int24 mean = meanTick();
        (, int24 spot,,,,,) = pool.slot0();
        int24 diff = spot > mean ? spot - mean : mean - spot;
        if (diff > maxTickDeviation) revert SpotDeviatesFromTwap(spot, mean);
        return quoteAtTick(mean, 1e18, wethIsToken0);
    }

    /// @dev Uniswap V3 OracleLibrary.getQuoteAtTick, for base = WETH.
    function quoteAtTick(int24 tick, uint128 baseAmount, bool baseIsToken0) public pure returns (uint256) {
        uint160 sqrtRatioX96 = TickMath.getSqrtPriceAtTick(tick);
        if (sqrtRatioX96 <= type(uint128).max) {
            uint256 ratioX192 = uint256(sqrtRatioX96) * sqrtRatioX96;
            return baseIsToken0
                ? FullMath.mulDiv(ratioX192, baseAmount, 1 << 192)
                : FullMath.mulDiv(1 << 192, baseAmount, ratioX192);
        }
        uint256 ratioX128 = FullMath.mulDiv(sqrtRatioX96, sqrtRatioX96, 1 << 64);
        return baseIsToken0
            ? FullMath.mulDiv(ratioX128, baseAmount, 1 << 128)
            : FullMath.mulDiv(1 << 128, baseAmount, ratioX128);
    }
}
