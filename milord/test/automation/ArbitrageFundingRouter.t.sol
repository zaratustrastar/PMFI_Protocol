// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HookrFixture} from "../utils/HookrFixture.sol";
import {ArbitrageFundingRouter} from "../../src/automation/ArbitrageFundingRouter.sol";
import {IPriceReference, ISwapVenue} from "../../src/automation/IPriceReference.sol";
import {UniV3TwapReference, IUniswapV3PoolOracle} from "../../src/automation/UniV3TwapReference.sol";
import {UniV3SwapVenue} from "../../src/automation/UniV3SwapVenue.sol";
import {ChainlinkReference, IAggregatorV3} from "../../src/automation/ChainlinkReference.sol";
import {DualReference} from "../../src/automation/DualReference.sol";
import {ParbFundingAdapter} from "../../src/parb/ParbFundingAdapter.sol";
import {IUniswapV3PoolMint} from "../../src/mocks/V3LiquidityHelper.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

contract FixedReference is IPriceReference {
    uint256 public p;

    constructor(uint256 p_) {
        p = p_;
    }

    function assetPerEth() external view returns (uint256) {
        return p;
    }
}

contract MockAggregator is IAggregatorV3 {
    int256 public answer;
    uint256 public updatedAt;
    uint80 public round = 1;
    uint80 public answeredIn = 1;

    function set(int256 a, uint256 t) external {
        answer = a;
        updatedAt = t;
    }

    function setRounds(uint80 r, uint80 ar) external {
        round = r;
        answeredIn = ar;
    }

    function decimals() external pure returns (uint8) {
        return 8;
    }

    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80) {
        return (round, answer, updatedAt, updatedAt, answeredIn);
    }
}

/// @notice Protocol ETH revenue -> USDG -> pArbitrage V3 -> pARB for the daily distributor, with no Safe
///         signature per run, on the official Uniswap V3 WETH/USDG pool and its 30-minute TWAP.
contract ArbitrageFundingRouterTest is HookrFixture {
    ArbitrageFundingRouter internal r;
    address internal stranger = makeAddr("stranger");
    address internal whale = makeAddr("priceManipulator");

    function setUp() public override {
        super.setUp();
        r = d.router;
        vm.warp(_now() + 31 minutes); // the pool's oracle needs 30 minutes of history
        vm.roll(block.number + 150);
    }

    function _fundRouter(uint256 amount) internal {
        vm.deal(address(r), address(r).balance + amount);
    }

    /// Swap on the WETH/USDG pool as a third party (moves spot; the TWAP lags).
    function _pushEthPriceDown(uint256 ethIn) internal {
        vm.deal(whale, ethIn);
        vm.startPrank(whale);
        (bool ok,) = v4.weth.call{value: ethIn}(abi.encodeWithSignature("deposit()"));
        require(ok);
        (ok,) = v4.weth.call(abi.encodeWithSignature("approve(address,uint256)", address(ethUsdg.helper), ethIn));
        require(ok);
        ethUsdg.helper.swap(
            IUniswapV3PoolMint(ethUsdg.pool),
            ethUsdg.wethIsToken0,
            ethIn,
            ethUsdg.wethIsToken0 ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1,
            whale
        );
        vm.stopPrank();
    }

    // ───────── wiring ─────────

    function test_wiring() public view {
        assertEq(r.owner(), arbSafe, "Arbitrage Funding Safe owns the router");
        assertTrue(r.openExecution(), "anyone may trigger");
        assertTrue(r.isPauser(monitor));
        assertTrue(d.adapter.isOperator(address(r)), "router may fund the adapter");
        assertEq(d.taxSplit.payee(2).account, address(r), "tax arbitrage third -> router");
        assertEq(d.entrySplit.payee(0).account, address(r), "entry fee 70% -> router");
        assertEq(d.royaltySplit.payee(1).account, address(r), "royalty 50% -> router");
        assertEq(address(r.priceReference()), address(twap));
        assertEq(twap.window(), 30 minutes);
    }

    // ───────── happy path: revenue -> USDG -> vault -> pARB ─────────

    function test_revenueBecomesPArbAtReportedPps() public {
        vm.deal(address(d.taxSplit), 3 ether);
        d.taxSplit.distribute(address(0));
        uint256 arbEth = address(r).balance;
        assertApproxEqAbs(arbEth, 0.9999 ether, 1, "arbitrage third of the tax");

        uint256 refPrice = twap.assetPerEth();
        assertApproxEqRel(refPrice, 2_500e6, 0.001e18, "TWAP ~2,500 USDG/ETH");
        vm.prank(stranger);
        uint256 batchId = r.execute();

        ParbFundingAdapter.Batch memory b = d.adapter.batch(batchId);
        assertEq(address(r).balance, 0);
        assertGe(b.usdcAmount, (arbEth * refPrice / 1e18) * 9900 / 10_000, "at most 1% below the reference");
        assertEq(r.totalAssetFunded(), b.usdcAmount);
        assertEq(usdg.balanceOf(address(d.adapter)), 0, "USDG went into the vault request");

        // existing asynchronous vault flow: report processes the request, settle mints pARB to the distributor
        _report();
        uint256 shares = d.adapter.settle(batchId);
        assertEq(shares, b.usdcAmount * 1e18 / 1e6, "first deposit priced at the reported $1.00 PPS");
        assertEq(d.vault.balanceOf(address(d.distributor)), shares);
    }

    // ───────── bounds ─────────

    function test_perTxAndDailyCapsAndInterval() public {
        _fundRouter(30 ether);
        r.execute();
        assertEq(r.spentToday(), ROUTER_MAX, "per-transaction max");
        vm.expectRevert(abi.encodeWithSelector(ArbitrageFundingRouter.TooSoon.selector, _now() + ROUTER_INTERVAL));
        r.execute();
        for (uint256 i; i < 3; ++i) {
            vm.warp(_now() + ROUTER_INTERVAL);
            vm.roll(block.number + 300);
            r.execute();
        }
        assertEq(r.spentToday(), ROUTER_DAILY, "daily cap reached");
        vm.warp(_now() + ROUTER_INTERVAL);
        assertEq(r.executable(), 0);
        vm.expectRevert(abi.encodeWithSelector(ArbitrageFundingRouter.BelowThreshold.selector, 10 ether, ROUTER_MIN));
        r.execute();
        vm.warp((_now() / 1 days + 1) * 1 days + 1); // next UTC day
        assertEq(r.executable(), ROUTER_MAX, "daily allowance resets");
        r.execute();
        assertEq(r.spentToday(), ROUTER_MAX);
    }

    function test_threshold() public {
        _fundRouter(ROUTER_MIN - 1);
        vm.expectRevert(abi.encodeWithSelector(ArbitrageFundingRouter.BelowThreshold.selector, ROUTER_MIN - 1, ROUTER_MIN));
        r.execute();
        _fundRouter(1);
        r.execute();
        assertEq(address(r).balance, 0);
    }

    function test_paramBoundsEnforced() public {
        vm.startPrank(arbSafe);
        vm.expectRevert("slippage");
        r.setParams(ROUTER_MIN, ROUTER_MAX, ROUTER_DAILY, 501, ROUTER_INTERVAL);
        vm.expectRevert("amounts");
        r.setParams(ROUTER_MAX + 1, ROUTER_MAX, ROUTER_DAILY, 100, ROUTER_INTERVAL);
        vm.expectRevert("amounts");
        r.setParams(ROUTER_MIN, ROUTER_DAILY + 1, ROUTER_DAILY, 100, ROUTER_INTERVAL);
        vm.expectRevert("interval");
        r.setParams(ROUTER_MIN, ROUTER_MAX, ROUTER_DAILY, 100, 8 days);
        vm.stopPrank();
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, stranger));
        r.setParams(ROUTER_MIN, ROUTER_MAX, ROUTER_DAILY, 100, ROUTER_INTERVAL);
    }

    // ───────── controls ─────────

    function test_pauseRules() public {
        _fundRouter(1 ether);
        vm.prank(stranger);
        vm.expectRevert(ArbitrageFundingRouter.NotPauser.selector);
        r.pause();
        vm.prank(monitor);
        r.pause();
        assertEq(r.executable(), 0);
        vm.expectRevert(ArbitrageFundingRouter.IsPaused.selector);
        r.execute();
        vm.prank(monitor);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, monitor));
        r.unpause();
        vm.prank(arbSafe);
        r.unpause();
        r.execute();
    }

    function test_keeperMode() public {
        _fundRouter(1 ether);
        address keeper = makeAddr("keeper");
        vm.startPrank(arbSafe);
        r.setOpenExecution(false);
        r.setKeeper(keeper, true);
        vm.stopPrank();
        vm.prank(stranger);
        vm.expectRevert(ArbitrageFundingRouter.NotKeeper.selector);
        r.execute();
        vm.prank(keeper);
        r.execute();
    }

    function test_sweepOnlyToOwner() public {
        _fundRouter(1 ether);
        usdg.mint(address(r), 7e6);
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, operator));
        r.sweep(address(0));
        uint256 before = arbSafe.balance;
        vm.startPrank(arbSafe);
        r.sweep(address(0));
        r.sweep(address(usdg));
        vm.stopPrank();
        assertEq(arbSafe.balance - before, 1 ether);
        assertEq(usdg.balanceOf(arbSafe), 7e6);
    }

    function test_keeperCannotReachAdapterOrSafe() public {
        // the router is the only automated adapter operator; a keeper key can only call execute()
        vm.prank(stranger);
        vm.expectRevert(ParbFundingAdapter.NotOperator.selector);
        d.adapter.fund(bytes32("x"), 1);
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, stranger));
        d.adapter.rescue(usdg, stranger, 1);
    }

    // ───────── price protection ─────────

    function test_sameBlockManipulationRefused() public {
        _fundRouter(2 ether);
        _pushEthPriceDown(150 ether); // ~ -14% spot move in this block
        vm.expectRevert(); // SpotDeviatesFromTwap
        r.execute();
        assertEq(address(r).balance, 2 ether, "nothing swapped");
        assertEq(d.adapter.batchCount(), 0);
    }

    function test_smallSpotMoveWithinDeviationStillBoundedBySlippage() public {
        _fundRouter(2 ether);
        _pushEthPriceDown(5 ether); // < 1% spot move: allowed, min output still 1% below the TWAP
        uint256 refPrice = twap.assetPerEth();
        uint256 id = r.execute();
        assertGe(d.adapter.batch(id).usdcAmount, (2 ether * refPrice / 1e18) * 9900 / 10_000);
    }

    function test_noHistoryNoPrice() public {
        UniV3TwapReference longWindow = new UniV3TwapReference(IUniswapV3PoolOracle(ethUsdg.pool), v4.weth, 1 days, 100);
        vm.expectRevert(); // Uniswap V3 'OLD': history shorter than the window
        longWindow.assetPerEth();
        vm.prank(arbSafe);
        r.setRoute(ISwapVenue(address(fundingVenue)), longWindow);
        _fundRouter(1 ether);
        vm.expectRevert();
        r.execute();
    }

    function test_badReferenceCannotForceABadTrade() public {
        // a reference that over-prices ETH makes the venue refuse the trade (minOut not met): no loss possible
        FixedReference high = new FixedReference(3_000e6);
        vm.prank(arbSafe);
        r.setRoute(ISwapVenue(address(fundingVenue)), high);
        _fundRouter(1 ether);
        vm.expectRevert();
        r.execute();
        assertEq(address(r).balance, 1 ether);
    }

    function test_venueRequiresFullFillAndPool() public {
        vm.expectRevert(UniV3SwapVenue.NotPool.selector);
        fundingVenue.uniswapV3SwapCallback(1, 1, "");
        vm.expectRevert(UniV3SwapVenue.UnknownToken.selector);
        fundingVenue.swapExactETHForToken{value: 1}(address(d.token), 0, address(this), _now());
        vm.expectRevert(UniV3SwapVenue.Expired.selector);
        fundingVenue.swapExactETHForToken{value: 1}(address(usdg), 0, address(this), _now() - 1);
    }

    function test_dualReferenceWithChainlink() public {
        MockAggregator feed = new MockAggregator();
        ChainlinkReference cl = new ChainlinkReference(feed, 1 hours, 6);
        feed.set(2_490e8, _now());
        DualReference dual = new DualReference(twap, cl, 100);
        assertEq(dual.assetPerEth(), 2_490e6, "lower of the two");

        feed.set(2_300e8, _now()); // 8% apart
        vm.expectRevert();
        dual.assetPerEth();

        feed.set(2_490e8, _now() - 2 hours);
        vm.expectRevert(abi.encodeWithSelector(ChainlinkReference.StalePrice.selector, _now() - 2 hours));
        cl.assetPerEth();

        feed.set(0, _now());
        vm.expectRevert(abi.encodeWithSelector(ChainlinkReference.BadPrice.selector, int256(0)));
        cl.assetPerEth();

        feed.set(2_490e8, _now());
        feed.setRounds(5, 4);
        vm.expectRevert(abi.encodeWithSelector(ChainlinkReference.StalePrice.selector, _now()));
        cl.assetPerEth();

        // the router can run on the dual reference
        feed.setRounds(1, 1);
        vm.prank(arbSafe);
        r.setRoute(ISwapVenue(address(fundingVenue)), dual);
        _fundRouter(1 ether);
        r.execute();
    }

    // ───────── failure modes ─────────

    function test_routerStoppedRevenueAccumulatesSafely() public {
        vm.prank(monitor);
        r.pause();
        for (uint256 i; i < 5; ++i) {
            vm.deal(address(d.taxSplit), 1 ether);
            d.taxSplit.distribute(address(0));
            vm.warp(_now() + 1 days);
        }
        uint256 held = address(r).balance;
        assertApproxEqAbs(held, 5 * 0.3333 ether, 5);
        vm.prank(arbSafe);
        r.unpause();
        vm.roll(block.number + 10);
        r.execute(); // catches up within the bounds: one max-size swap per interval
        assertEq(address(r).balance, 0, "everything held while paused is funded after unpause");
    }
}
