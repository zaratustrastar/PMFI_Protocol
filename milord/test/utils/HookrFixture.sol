// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {SingleChainFixture} from "./SingleChainFixture.sol";
import {HookrLaunchKit} from "../../script/HookrLaunchKit.sol";
import {MockHookrTaxHook} from "../../src/mocks/MockHookrTaxHook.sol";
import {V4SwapAdapter} from "../../src/launch/V4SwapAdapter.sol";
import {MilordLiquidityLock} from "../../src/launch/MilordLiquidityLock.sol";
import {DeployPermit2} from "permit2/test/utils/DeployPermit2.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";

/// @notice Full MILORD deployment plus a local Hookr-style market on the official Uniswap v4 contracts:
///         native ETH / MILORD pool behind MockHookrTaxHook (1.5% buy, 1.5% sell, 20% platform commission,
///         project share to TradingTaxSplitter), one MILORD-only position owned by MilordLiquidityLock,
///         an ETH / USDG pool for the funding swap, and V4SwapAdapter as the buyback route.
///         Launch numbers are ILLUSTRATIVE (10 ETH opening FDV, 50% of supply in the position).
abstract contract HookrFixture is SingleChainFixture, HookrLaunchKit {
    address internal hookrTreasury = makeAddr("hookrTreasury");
    V4 internal v4;
    MockHookrTaxHook internal hook;
    V4SwapAdapter internal swapRoute;
    MilordLiquidityLock internal lock;
    PoolSwapTest internal router;
    PoolKey internal milordKey;
    PoolKey internal usdgKey;
    LaunchParams internal launch;
    uint256 internal positionId;

    uint16 internal constant TAX_BPS = 150;
    uint16 internal constant COMMISSION_BPS = 2000;

    function _initialSwapAdapter() internal pure override returns (address) {
        return address(0);
    }

    /// Share of the 6,767,000,000 supply placed in the MILORD-only position (default 50%).
    function _liquidityShareBps() internal pure virtual returns (uint256) {
        return 5_000;
    }

    function setUp() public virtual override {
        super.setUp();
        new DeployPermit2().deployPermit2();
        v4 = _deployV4Local(address(this));
        router = new PoolSwapTest(v4.poolManager);

        address hookAddr = address(uint160(0x4000000000000000000000000000000000000000) | TAX_HOOK_FLAGS);
        deployCodeTo(
            "MockHookrTaxHook.sol:MockHookrTaxHook",
            abi.encode(v4.poolManager, TAX_BPS, TAX_BPS, COMMISSION_BPS, hookrTreasury, address(d.taxSplit)),
            hookAddr
        );
        hook = MockHookrTaxHook(payable(hookAddr));

        launch = LaunchParams({
            milordForLiquidity: 6_767_000_000 ether * _liquidityShareBps() / 10_000,
            tickUpper: 203_280, // ~10 ETH fully diluted at open
            tickLower: 203_280 - 69_060, // up to ~1,000x the opening price
            lpFee: 3000,
            tickSpacing: 60
        });
        milordKey = _milordKey(address(d.token), hookAddr, launch);
        _openMarket(v4.poolManager, milordKey, launch);

        lock = new MilordLiquidityLock(v4.positionManager, teamSafe);
        vm.prank(treasury);
        d.token.transfer(address(this), launch.milordForLiquidity);
        positionId = _addMilordOnlyLiquidity(v4, milordKey, launch, address(lock));

        // ETH / USDG pool at ~2,500 USDG per ETH (stand-in for the Robinhood WETH/USDG market)
        usdgKey = PoolKey(Currency.wrap(address(0)), Currency.wrap(address(usdg)), 500, 10, IHooks(address(0)));
        v4.poolManager.initialize(usdgKey, TickMath.getSqrtPriceAtTick(-198_080));
        usdg.mint(address(this), 5_000_000e6);
        vm.deal(address(this), 3_000 ether);
        _addEthTokenLiquidity(v4, usdgKey, -198_080 - 6_930, -198_080 + 6_930, 2_000 ether, 5_000_000e6, address(this));

        PoolKey[] memory keys = new PoolKey[](2);
        keys[0] = milordKey;
        keys[1] = usdgKey;
        swapRoute = new V4SwapAdapter(v4.poolManager, keys);
        vm.prank(safe);
        d.burner.setInitialAdapter(address(swapRoute));
    }

    function _buy(address who, uint256 ethIn) internal returns (uint256 milordOut) {
        vm.deal(who, who.balance + ethIn);
        uint256 before = d.token.balanceOf(who);
        vm.prank(who);
        router.swap{value: ethIn}(
            milordKey,
            SwapParams({zeroForOne: true, amountSpecified: -int256(ethIn), sqrtPriceLimitX96: TickMath.MIN_SQRT_PRICE + 1}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        milordOut = d.token.balanceOf(who) - before;
    }

    function _sell(address who, uint256 milordIn) internal returns (uint256 ethOut) {
        uint256 before = who.balance;
        vm.startPrank(who);
        d.token.approve(address(router), milordIn);
        router.swap(
            milordKey,
            SwapParams({zeroForOne: false, amountSpecified: -int256(milordIn), sqrtPriceLimitX96: TickMath.MAX_SQRT_PRICE - 1}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        vm.stopPrank();
        ethOut = who.balance - before;
    }

    /// MILORD price in wei per 1,000,000 MILORD at the current pool tick
    function _weiPerMillion() internal view returns (uint256) {
        (uint160 sqrtP,,,) = _slot0(v4.poolManager, milordKey);
        // price token1/token0 = (sqrtP/2^96)^2 MILORD-wei per ETH-wei -> wei per 1e24 MILORD-wei
        uint256 p = (uint256(sqrtP) * uint256(sqrtP)) >> 96; // MILORD per ETH * 2^96
        return (1e24 << 96) / p;
    }
}
