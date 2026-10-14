// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {SingleChainFixture} from "./SingleChainFixture.sol";
import {HookrLaunchKit} from "../../script/HookrLaunchKit.sol";
import {MockHookrTaxHook} from "../../src/mocks/MockHookrTaxHook.sol";
import {V4SwapAdapter} from "../../src/launch/V4SwapAdapter.sol";
import {MockHookrFamilyLock} from "../../src/mocks/MockHookrFamilyLock.sol";
import {HookrLockFeeCollector, IHookrFamilyLock} from "../../src/launch/HookrLockFeeCollector.sol";
import {UniV3LocalKit} from "../../script/UniV3LocalKit.sol";
import {UniV3SwapVenue, IUniswapV3PoolSwap, IWETH9} from "../../src/automation/UniV3SwapVenue.sol";
import {UniV3TwapReference, IUniswapV3PoolOracle} from "../../src/automation/UniV3TwapReference.sol";
import {IMILORD} from "../../src/interfaces/IMILORD.sol";
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
///         project share to TradingTaxSplitter), the MILORD-only position (100% of supply) held by
///         MockHookrFamilyLock (stand-in for Hookr's launcher + HookrFamilyLock) and locked forever with
///         HookrLockFeeCollector as beneficiary, V4SwapAdapter as the buyback route, and the automation router
///         on an official Uniswap V3 WETH/USDG pool (30-minute TWAP reference).
abstract contract HookrFixture is SingleChainFixture, HookrLaunchKit, UniV3LocalKit {
    address internal hookrTreasury = makeAddr("hookrTreasury");
    V4 internal v4;
    MockHookrTaxHook internal hook;
    V4SwapAdapter internal swapRoute;
    MockHookrFamilyLock internal familyLock;
    HookrLockFeeCollector internal collector;
    bytes32 internal constant FAMILY_ID = keccak256("MILORD launch family (local)");
    PoolSwapTest internal router;
    PoolKey internal milordKey;
    V3Market internal ethUsdg;
    UniV3SwapVenue internal fundingVenue;
    UniV3TwapReference internal twap;
    LaunchParams internal launch;
    uint256 internal launchDust; // MILORD wei the position could not take, burned at launch
    uint256 internal positionId;

    uint16 internal constant TAX_BPS = 150;
    uint16 internal constant COMMISSION_BPS = 2000;

    function _fundMockSwap() internal override {}

    function _initialSwapAdapter() internal pure override returns (address) {
        return address(0);
    }

    /// Share of the 6,767,000,000 supply placed in the MILORD-only position (owner decision 2026-10-14: 100%).
    function _liquidityShareBps() internal pure virtual returns (uint256) {
        return 10_000;
    }

    /// Router bounds used by the fixture (production values are in docs/launch/SAFE_CONFIGURATION.md).
    uint256 internal constant ROUTER_MIN = 0.05 ether;
    uint256 internal constant ROUTER_MAX = 5 ether;
    uint256 internal constant ROUTER_DAILY = 20 ether;
    uint16 internal constant ROUTER_SLIPPAGE = 100; // 1% below the TWAP reference
    uint32 internal constant ROUTER_INTERVAL = 1 hours;
    address internal monitor = makeAddr("monitorPauser");

    function _beforeDeploy() internal virtual override {
        new DeployPermit2().deployPermit2();
        v4 = _deployV4Local(address(this));
        // WETH/USDG V3 market at ~2,500 USDG per ETH, 2,000 ETH + 5,000,000 USDG of liquidity
        usdg.mint(address(this), 5_000_000e6);
        vm.deal(address(this), 3_000 ether);
        ethUsdg = _deployV3EthUsdg(v4.weth, address(usdg), 2_500, 2_000 ether, 5_000_000e6, address(this));
        fundingVenue = new UniV3SwapVenue(IUniswapV3PoolSwap(ethUsdg.pool), IWETH9(v4.weth));
        twap = new UniV3TwapReference(IUniswapV3PoolOracle(ethUsdg.pool), v4.weth, 30 minutes, 100);
        cfg.fundingVenue = address(fundingVenue);
        cfg.fundingReference = address(twap);
        cfg.routerAmounts = [ROUTER_MIN, ROUTER_MAX, ROUTER_DAILY];
        cfg.routerSlippageBps = ROUTER_SLIPPAGE;
        cfg.routerInterval = ROUTER_INTERVAL;
        cfg.routerPauser = monitor;
    }

    function setUp() public virtual override {
        super.setUp();
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

        familyLock = new MockHookrFamilyLock(v4.positionManager, address(this));
        vm.prank(treasury);
        d.token.transfer(address(this), launch.milordForLiquidity);
        positionId = _addMilordOnlyLiquidity(v4, milordKey, launch, address(familyLock));
        launchDust = _burnLaunchRemainder(address(d.token), address(this), 1e6);
        familyLock.addMember(FAMILY_ID, positionId);
        collector = new HookrLockFeeCollector(
            IHookrFamilyLock(address(familyLock)), FAMILY_ID, IMILORD(address(d.token)), payable(address(d.burner)), v4.poolManager
        );
        familyLock.lock(FAMILY_ID, address(collector), familyLock.FOREVER());

        PoolKey[] memory keys = new PoolKey[](1);
        keys[0] = milordKey;
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
