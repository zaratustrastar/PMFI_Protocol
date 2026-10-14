// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HookrFixture} from "../utils/HookrFixture.sol";
import {MockHookrFamilyLock} from "../../src/mocks/MockHookrFamilyLock.sol";
import {HookrLockFeeCollector} from "../../src/launch/HookrLockFeeCollector.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";

/// @notice Permanent lock of the founding position with LP fees collected into buyback-and-burn.
///         The position is held by MockHookrFamilyLock (same surface as Hookr-1's HookrFamilyLock ABI) over real
///         Uniswap v4 fee accounting; HookrLockFeeCollector is the beneficiary.
///         Brief rules checked here: (1) non-withdrawable, (2) no role can take principal, (3) ETH fees -> buyback,
///         (4) MILORD fees burned, (5) trading and NFT conversion unaffected, (6) fees collectable all along,
///         (7) no principal collected or burned as a fee.
contract HookrLockFeesTest is HookrFixture {
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function _trade() internal returns (uint256 ethIn, uint256 milordSold) {
        ethIn = 3 ether;
        uint256 got = _buy(alice, ethIn);
        milordSold = got / 3;
        _sell(alice, milordSold);
        _buy(bob, 0.5 ether);
        ethIn += 0.5 ether;
    }

    function test_ethFeesToBurner_milordFeesBurned_exactly() public {
        (uint256 ethIn, uint256 milordSold) = _trade();
        uint256 burnerEth = address(d.burner).balance;
        uint256 supply = d.token.totalSupply();
        uint256 liq = v4.positionManager.getPositionLiquidity(positionId);

        vm.prank(makeAddr("anyone")); // permissionless trigger
        (uint256 a0, uint256 a1) = collector.collect(0);

        assertGt(a0, 0);
        assertGt(a1, 0);
        assertEq(address(d.burner).balance - burnerEth, a0, "every ETH fee wei reaches BuybackBurner");
        assertEq(supply - d.token.totalSupply(), a1, "supply falls by exactly the MILORD fees");
        assertEq(address(collector).balance, 0);
        assertEq(d.token.balanceOf(address(collector)), 0);
        assertEq(d.token.balanceOf(address(d.burner)), 0);
        assertEq(collector.totalEthForwarded(), a0);
        assertEq(collector.totalMilordBurned(), a1);
        assertEq(v4.positionManager.getPositionLiquidity(positionId), liq, "principal untouched");
        // fees are the 0.3% LP fee on what reached the pool (after the 1.5% tax), never more
        assertLe(a0, (ethIn * 9850 / 10_000) * 30 / 10_000 + 1);
        assertLe(a1, (milordSold * 30) / 10_000 + 1);
        assertApproxEqRel(a0, (ethIn * 9850 / 10_000) * 30 / 10_000, 0.001e18);
    }

    function test_collectWithNoTradesTakesNothing() public {
        uint256 supply = d.token.totalSupply();
        uint256 pmMilord = d.token.balanceOf(address(v4.poolManager));
        (uint256 a0, uint256 a1) = collector.collect(0);
        assertEq(a0, 0);
        assertEq(a1, 0);
        assertEq(d.token.totalSupply(), supply, "no principal burned");
        assertEq(d.token.balanceOf(address(v4.poolManager)), pmMilord, "no principal left the pool");
    }

    function test_secondCollectIsEmpty() public {
        _trade();
        collector.collect(0);
        uint256 supply = d.token.totalSupply();
        (uint256 a0, uint256 a1) = collector.collect(0);
        assertEq(a0 + a1, 0);
        assertEq(d.token.totalSupply(), supply);
    }

    function test_principalStaysInPoolAfterFeeCollection() public {
        // Collecting removes exactly the fees from the pool; the rest is the locked principal, which buyers can
        // still take out in full by buying (and nobody else can).
        _trade();
        uint256 pmBefore = d.token.balanceOf(address(v4.poolManager));
        (, uint256 a1) = collector.collect(0);
        uint256 inPool = d.token.balanceOf(address(v4.poolManager));
        assertEq(pmBefore - inPool, a1, "only the MILORD fee left the pool");
        uint256 out = _buy(bob, 400 ether);
        assertApproxEqRel(out, inPool, 0.0001e18, "the locked principal is what buyers receive");
    }

    function test_onlyBeneficiaryCollects_noRoleWithdraws() public {
        _trade();
        vm.expectRevert(abi.encodeWithSelector(MockHookrFamilyLock.NotBeneficiary.selector, FAMILY_ID, address(this)));
        familyLock.collect(FAMILY_ID, 0, 0);
        vm.prank(safe);
        vm.expectRevert(abi.encodeWithSelector(MockHookrFamilyLock.NotBeneficiary.selector, FAMILY_ID, safe));
        familyLock.collect(FAMILY_ID, 0, 0);
        // release never works for a FOREVER lock, even for the beneficiary
        vm.prank(address(collector));
        vm.expectRevert(abi.encodeWithSelector(MockHookrFamilyLock.StillLocked.selector, FAMILY_ID, type(uint64).max));
        familyLock.release(FAMILY_ID, address(this));
        // the lock cannot be re-pointed and no member can be added
        vm.expectRevert(abi.encodeWithSelector(MockHookrFamilyLock.NotLockable.selector, FAMILY_ID, address(this)));
        familyLock.lock(FAMILY_ID, address(this), type(uint64).max);
        vm.expectRevert(abi.encodeWithSelector(MockHookrFamilyLock.NotLockable.selector, FAMILY_ID, address(this)));
        familyLock.addMember(FAMILY_ID, positionId);
        // the collector has no owner and no withdraw function
        (bool ok,) = address(collector).call(abi.encodeWithSignature("owner()"));
        assertFalse(ok);
        (ok,) = address(collector).call(abi.encodeWithSignature("withdraw(address,uint256)", address(this), 1));
        assertFalse(ok);
        assertEq(IERC721(address(v4.positionManager)).ownerOf(positionId), address(familyLock));
    }

    function test_redeemClaimsBurnsMilordClaims() public {
        _buy(alice, 1 ether);
        // simulate Hookr paying fees as PoolManager claims: give the collector ERC-6909 MILORD claims
        vm.deal(address(collector), 0);
        vm.deal(address(this), 1 ether);
        address payer = makeAddr("claimsPayer");
        vm.deal(payer, 0.2 ether);
        vm.prank(payer);
        router.swap{value: 0.2 ether}(
            milordKey,
            SwapParams({zeroForOne: true, amountSpecified: -int256(0.2 ether), sqrtPriceLimitX96: TickMath.MIN_SQRT_PRICE + 1}),
            PoolSwapTest.TestSettings({takeClaims: true, settleUsingBurn: false}),
            ""
        );
        uint256 claims = v4.poolManager.balanceOf(payer, Currency.wrap(address(d.token)).toId());
        assertGt(claims, 0);
        vm.prank(payer);
        v4.poolManager.transfer(address(collector), Currency.wrap(address(d.token)).toId(), claims);

        uint256 supply = d.token.totalSupply();
        collector.redeemClaims(Currency.wrap(address(d.token)));
        assertEq(supply - d.token.totalSupply(), claims, "claims redeemed and burned");
        assertEq(v4.poolManager.balanceOf(address(collector), Currency.wrap(address(d.token)).toId()), 0);
    }

    function test_unlockCallbackOnlyPoolManager() public {
        vm.expectRevert(HookrLockFeeCollector.NotPoolManager.selector);
        collector.unlockCallback(abi.encode(Currency.wrap(address(0)), uint256(1)));
    }

    function test_collectionDoesNotInterfereWithTradingOrConversion() public {
        _buy(alice, 2 ether);
        collector.collect(0);
        assertGt(_buy(bob, 1 ether), 0, "buys still work");
        collector.collect(0);
        assertGt(_sell(alice, 10_000_000 ether), 0, "sells still work");
        collector.collect(0);

        // NFT conversion from MILORD bought on the market
        uint256 price = _weiPerMillion();
        MilordConverter.EntryQuote memory q =
            MilordConverter.EntryQuote(alice, price, uint64(_now()), uint64(_now() + 300), keccak256("q1"));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(quotePk, d.converter.quoteDigest(q));
        uint256 fee = d.converter.entryFeeFor(price);
        vm.deal(alice, alice.balance + fee);
        vm.startPrank(alice);
        d.token.approve(address(d.converter), M);
        uint256 id = d.converter.convert{value: fee}(q, abi.encodePacked(r, s, v), fee);
        vm.stopPrank();
        assertEq(d.nft.ownerOf(id), alice);
        collector.collect(0);
        vm.prank(alice);
        d.converter.redeem(id);
        assertEq(d.token.balanceOf(address(d.converter)), 0, "escrow released, untouched by fee collection");
    }

    function testFuzz_collectNeverTakesPrincipal(uint96 buyWei, uint16 sellFrac) public {
        uint256 b = bound(buyWei, 0.01 ether, 50 ether);
        uint256 got = _buy(alice, b);
        uint256 sold = got * bound(sellFrac, 0, 10_000) / 10_000;
        if (sold > 0) _sell(alice, sold);
        uint256 supply = d.token.totalSupply();
        uint256 liq = v4.positionManager.getPositionLiquidity(positionId);
        (uint256 a0, uint256 a1) = collector.collect(0);
        assertLe(a0, (b * 9850 / 10_000) * 30 / 10_000 + 10, "ETH fee <= 0.3% of ETH in");
        assertLe(a1, sold * 30 / 10_000 + 10, "MILORD fee <= 0.3% of MILORD sold");
        assertEq(supply - d.token.totalSupply(), a1);
        assertEq(v4.positionManager.getPositionLiquidity(positionId), liq);
    }
}
