// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HookrFixture} from "../utils/HookrFixture.sol";
import {BuybackBurner} from "../../src/robinhood/BuybackBurner.sol";
import {V4SwapAdapter} from "../../src/launch/V4SwapAdapter.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

/// @notice Hookr-style launch on real Uniswap v4 contracts: MILORD-only liquidity, 1.5% ETH taxes,
///         platform commission, equal three-way project split, real buyback-and-burn, LP fees kept apart.
contract HookrLaunchTest is HookrFixture {
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function test_marketOpensWithMilordOnly() public view {
        (, int24 tick,,) = _slot0(v4.poolManager, milordKey);
        assertEq(tick, launch.tickUpper, "opens at the top of the range");
        assertEq(address(v4.poolManager).balance, 0, "no ETH in the MILORD pool");
        assertApproxEqAbs(d.token.balanceOf(address(v4.poolManager)), launch.milordForLiquidity, 1e18, "MILORD in");
        assertEq(IERC721(address(v4.positionManager)).ownerOf(positionId), address(familyLock), "position held by the (mock) Hookr family lock");
        (address beneficiary, uint64 unlockBlock) = familyLock.lockOf(FAMILY_ID);
        assertEq(beneficiary, address(collector));
        assertEq(unlockBlock, type(uint64).max, "locked forever");
        assertEq(d.token.totalSupply() + launchDust, 6_767_000_000 ether);
    }

    function test_firstTradeMustBeABuy() public {
        // 100% of supply is in the pool, so nobody can sell before someone buys
        assertEq(d.token.balanceOf(treasury), 0);
        assertEq(d.token.balanceOf(bob), 0);
        assertEq(address(v4.poolManager).balance, 0, "no ETH for sellers yet");
        assertGt(_buy(alice, 1 ether), 0, "the first buy works");
        assertGt(_sell(alice, 1_000_000 ether), 0, "then sells are paid from the buyer's ETH");
    }

    function test_buyTaxCommissionAndEqualProjectSplit() public {
        uint256 ethIn = 2 ether;
        uint256 out = _buy(alice, ethIn);
        assertGt(out, 0);
        uint256 tax = (ethIn * TAX_BPS) / 10_000; // 0.03 ETH
        assertEq(hook.totalBuyTax(), tax);
        assertEq(hook.platformAccrued(), (tax * COMMISSION_BPS) / 10_000, "Hookr 0.30% of the trade");

        hook.collect();
        assertEq(hookrTreasury.balance, 0.006 ether);
        assertEq(address(d.taxSplit).balance, 0.024 ether);
        d.taxSplit.distribute(address(0));
        // 0.40% of the trade each (burner gets the 1-bp rounding remainder of 33.34/33.33/33.33)
        assertEq(teamSafe.balance, (0.024 ether * 3333) / 10_000);
        assertEq(address(d.router).balance, (0.024 ether * 3333) / 10_000, "arbitrage third -> funding router");
        assertEq(address(d.burner).balance, 0.024 ether - 2 * ((0.024 ether * 3333) / 10_000));
        assertApproxEqRel(teamSafe.balance, (ethIn * 40) / 10_000, 1e15);
    }

    function test_sellTaxIsTakenInEth() public {
        uint256 got = _buy(alice, 5 ether);
        uint256 before = hook.totalSellTax();
        uint256 ethOut = _sell(alice, got / 2);
        uint256 sellTax = hook.totalSellTax() - before;
        assertGt(ethOut, 0);
        // tax = 1.5% of gross ETH output; seller receives the other 98.5%
        assertApproxEqAbs(sellTax, ((ethOut + sellTax) * TAX_BPS) / 10_000, 1);
    }

    function test_sellersCannotTakeMoreEthThanBuyersBrought() public {
        uint256 a = _buy(alice, 3 ether);
        uint256 b = _buy(bob, 1 ether);
        uint256 outA = _sell(alice, a);
        uint256 outB = _sell(bob, b);
        assertLt(outA + outB, 4 ether, "round trip loses taxes and LP fees");
        assertGt(outA + outB, 3.8 ether, "but only those");
        // all buyer ETH not paid back is held as tax (hook) or LP fees (pool, owed to the locked position)
        assertApproxEqAbs(address(v4.poolManager).balance + address(hook).balance, 4 ether - outA - outB, 1e6);
    }

    function test_priceImpactOfExampleBuys() public {
        uint256 p0 = _weiPerMillion();
        _buy(alice, 0.1 ether);
        uint256 p1 = _weiPerMillion();
        _buy(alice, 0.9 ether);
        uint256 p2 = _weiPerMillion();
        _buy(alice, 9 ether);
        uint256 p3 = _weiPerMillion();
        emit log_named_uint("open wei per 1M MILORD", p0);
        emit log_named_uint("after 0.1 ETH", p1);
        emit log_named_uint("after 1 ETH total", p2);
        emit log_named_uint("after 10 ETH total", p3);
        assertGt(p1, p0);
        assertGt(p3, p2);
        assertApproxEqRel(p0, 1.4777e15, 0.01e18, "~0.00148 ETH per 1M at 10 ETH FDV");
    }

    function test_buybackPurchasesAndBurnsMilord() public {
        _buy(alice, 20 ether);
        hook.collect();
        d.taxSplit.distribute(address(0));
        vm.deal(address(d.burner), 1 ether); // plus accumulated budget for a threshold-sized buyback
        uint256 supplyBefore = d.token.totalSupply();
        uint256 quotePerEth = _weiPerMillion();
        uint256 expected = (0.5 ether * 1e24) / quotePerEth;
        vm.prank(operator);
        uint256 burned = d.burner.buybackAndBurn(0.5 ether, (expected * 90) / 100, block.timestamp + 60);
        assertGt(burned, 0);
        assertEq(d.token.totalSupply(), supplyBefore - burned, "true supply reduction");
        assertEq(d.burner.totalBurnedFromBuybacks(), burned);
        BuybackBurner.Buyback memory r = d.burner.buyback(0);
        assertEq(r.milordBurned, burned);
        assertEq(r.ethIn, 0.5 ether);
        assertEq(d.token.balanceOf(address(0xdead)), 0, "no dead-address transfer");
    }

    function test_buybackRespectsPriceLimit() public {
        _buy(alice, 1 ether);
        vm.deal(address(d.burner), 1 ether);
        vm.prank(operator);
        vm.expectRevert(); // minOut above what the pool gives
        d.burner.buybackAndBurn(0.5 ether, 10_000_000_000 ether, block.timestamp + 60);
    }

    function test_burnerBelowThresholdWaits() public {
        vm.deal(address(d.burner), 0.005 ether);
        vm.prank(operator);
        vm.expectRevert(BuybackBurner.OutOfBounds.selector);
        d.burner.buybackAndBurn(0.005 ether, 1, block.timestamp + 60);
    }

    function test_lpFeesStaySeparateFromTax() public {
        uint256 got = _buy(alice, 4 ether);
        _sell(alice, got / 2);
        uint256 taxBefore = hook.totalBuyTax() + hook.totalSellTax();
        uint256 burnerEth = address(d.burner).balance;
        uint256 supply = d.token.totalSupply();
        collector.collect(0);
        // 0.3% LP fee on the 3.94 ETH that reached the pool goes to buyback-and-burn; the MILORD fee is burned
        assertApproxEqRel(address(d.burner).balance - burnerEth, (3.94 ether * 30) / 10_000, 0.01e18);
        assertLt(d.token.totalSupply(), supply, "MILORD LP fees burned");
        assertEq(hook.totalBuyTax() + hook.totalSellTax(), taxBefore, "LP fee collection never touches tax");
        assertEq(IERC721(address(v4.positionManager)).ownerOf(positionId), address(familyLock), "still locked");
        assertGt(v4.positionManager.getPositionLiquidity(positionId), 0);
    }

    function test_adapterRejectsUnknownTokenAndExpired() public {
        vm.expectRevert(abi.encodeWithSelector(V4SwapAdapter.UnknownToken.selector, address(usdg)));
        swapRoute.swapExactETHForToken{value: 0}(address(usdg), 0, address(this), block.timestamp);
        vm.expectRevert(V4SwapAdapter.Expired.selector);
        swapRoute.swapExactETHForToken{value: 0}(address(d.token), 0, address(this), block.timestamp - 1);
    }

    function test_initialAdapterOnlyOnce() public {
        vm.prank(safe);
        vm.expectRevert("adapter already set");
        d.burner.setInitialAdapter(address(1));
    }

    function testFuzz_commissionRecalculatesProjectShares(uint16 commission, uint96 ethIn) public pure {
        commission = uint16(bound(commission, 2_000, 5_000));
        uint256 vol = bound(ethIn, 0.01 ether, 1_000 ether);
        uint256 tax = (vol * 150) / 10_000;
        uint256 cut = (tax * commission) / 10_000;
        uint256 project = tax - cut;
        uint256 each = (project * 3333) / 10_000;
        uint256 burnShare = project - 2 * each;
        assertLe(burnShare - each, project / 10_000 + 3, "equal thirds within 1 bp of the project share");
        assertEq(cut + each * 2 + burnShare, tax, "nothing lost");
    }

    receive() external payable {}
}
