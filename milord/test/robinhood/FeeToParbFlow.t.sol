// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {SingleChainFixture} from "../utils/SingleChainFixture.sol";
import {Merkle} from "../utils/Merkle.sol";
import {ParbFundingAdapter} from "../../src/parb/ParbFundingAdapter.sol";
import {ShareDistributor} from "../../src/parb/ShareDistributor.sol";

/// The complete Robinhood Chain flow, one chain end to end:
/// conversions, royalty, trading tax -> splitters -> Arbitrage Safe ETH -> USDG ->
/// adapter.fund -> vault report -> settle (pARB to distributor) -> time-weighted weekly
/// cumulative root -> holder claims -> pARB redemption for USDG. No bridge anywhere.
contract FeeToParbFlowTest is SingleChainFixture {
    uint256 constant DAY = 1 days;
    uint256 constant PRICE = 0.5 ether; // SCENARIO: ETH value of 1M MILORD -> fee 0.05 ETH
    uint256 constant USDG_PER_ETH = 2500e6; // SCENARIO: mock ETH->USDG swap rate

    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address carol = makeAddr("carol");
    address dave = makeAddr("dave");
    address erin = makeAddr("erin");
    address frank = makeAddr("frank");

    uint256 T0;
    uint256 bobId;
    uint256 carolId;
    uint64 epoch;

    address[] accts;
    uint256[] cumul;
    bytes32[] leaves;

    function setUp() public override {
        super.setUp();
        T0 = _now();
    }

    // ───────── helpers ─────────

    function _at(uint256 t) internal {
        vm.warp(t);
    }

    /// Arbitrage Safe: swap all ETH it received since last time into USDG (mock DEX),
    /// send to the adapter, operator funds a batch tagged with a unique sourceRef.
    function _fundWeek(bytes32 ref) internal returns (uint256 batchId, uint256 usdgIn) {
        d.entrySplit.distribute(address(0));
        d.royaltySplit.distribute(address(0));
        d.taxSplit.distribute(address(0));
        uint256 eth = arbSafe.balance; // the Arbitrage Safe only holds fee ETH
        usdgIn = (eth * USDG_PER_ETH) / 1 ether;
        vm.prank(arbSafe);
        payable(dex).transfer(eth);
        usdg.mint(arbSafe, usdgIn); // DEX pays out USDG
        vm.prank(arbSafe);
        usdg.transfer(address(d.adapter), usdgIn);
        vm.prank(operator);
        batchId = d.adapter.fund(ref, usdgIn);
    }

    function _tend() internal {
        vm.warp(_now() + 61);
        d.vault.tend();
    }

    function _pnl(int256 bps) internal {
        uint256 bal = usdg.balanceOf(servicer);
        uint256 delta = (bal * uint256(bps < 0 ? -bps : bps)) / 10000;
        if (bps >= 0) usdg.mint(servicer, delta);
        else {
            vm.prank(servicer);
            usdg.transfer(address(0xdead), delta);
        }
    }

    function _alloc(address who, uint256 secs, uint256 totalSecs, uint256 budget) internal {
        uint256 a = (budget * secs) / totalSecs;
        for (uint256 i; i < accts.length; ++i) {
            if (accts[i] == who) {
                cumul[i] += a;
                return;
            }
        }
        accts.push(who);
        cumul.push(a);
    }

    function _publish() internal returns (uint256 total) {
        delete leaves;
        for (uint256 i; i < accts.length; ++i) {
            leaves.push(Merkle.leaf(accts[i], cumul[i]));
            total += cumul[i];
        }
        vm.prank(operator);
        d.distributor.proposeRoot(Merkle.root(leaves), total, ++epoch, keccak256("epoch file"));
        vm.warp(_now() + 1 days);
        d.distributor.activateRoot();
    }

    function _claim(address who) internal returns (uint256 paid) {
        for (uint256 i; i < accts.length; ++i) {
            if (accts[i] == who) return d.distributor.claim(who, cumul[i], Merkle.proof(leaves, i));
        }
        revert("no leaf");
    }

    function _cumulative(address who) internal view returns (uint256) {
        for (uint256 i; i < accts.length; ++i) if (accts[i] == who) return cumul[i];
        return 0;
    }

    function _week1() internal returns (uint256 shares) {
        // day 0: alice x2, bob, carol; day 2: dave
        _convert(alice, PRICE);
        _convert(alice, PRICE);
        bobId = _convert(bob, PRICE);
        carolId = _convert(carol, PRICE);
        _at(T0 + 2 * DAY);
        _convert(dave, PRICE);
        // day 3.5: bob sells to erin through a royalty-paying marketplace (sale price 1 ETH)
        _at(T0 + 84 hours);
        vm.prank(bob);
        d.nft.transferFrom(bob, erin, bobId);
        (address recv, uint256 royalty) = d.nft.royaltyInfo(bobId, 1 ether);
        vm.deal(address(this), 1 ether);
        payable(recv).transfer(royalty);
        // day 5: carol redeems
        _at(T0 + 5 * DAY);
        vm.prank(carol);
        d.converter.redeem(carolId);
        // illustrative trading-tax inflow (mechanism pending decision)
        vm.deal(address(d.taxSplit), 0.3 ether);

        _at(T0 + 7 * DAY);
        (uint256 b,) = _fundWeek("week-1 fees");
        _report();
        shares = d.adapter.settle(b);
        _tend();
    }

    // ───────── tests ─────────

    function test_week1_feesBecomePArbForTimeWeightedHolders() public {
        uint256 shares = _week1();

        // 5 entries x 0.05 ETH x 70% + royalty 0.067 x 50% + tax 0.3 x 33.33%
        uint256 arbEth = 0.175 ether + 0.0335 ether + 0.09999 ether;
        assertEq(arbSafe.balance, 0, "all arbitrage ETH swapped");
        assertEq(dex.balance, arbEth);
        assertEq(d.adapter.totalUsdcDeposited(), (arbEth * USDG_PER_ETH) / 1 ether);
        assertEq(shares, (d.adapter.totalUsdcDeposited() * 1e18) / 1e6, "first deposit at $1.00");
        assertEq(d.vault.balanceOf(address(d.distributor)), shares);
        assertEq(usdg.balanceOf(servicer) + usdg.balanceOf(address(d.vault)), d.adapter.totalUsdcDeposited());

        // time-weighted NFT-seconds: alice 2x7d, bob 3.5d, erin 3.5d, carol 5d, dave 5d = 31 days
        uint256 tot = 31 * DAY;
        _alloc(alice, 14 * DAY, tot, shares);
        _alloc(bob, 84 hours, tot, shares);
        _alloc(erin, 84 hours, tot, shares);
        _alloc(carol, 5 * DAY, tot, shares);
        _alloc(dave, 5 * DAY, tot, shares);
        uint256 total = _publish();
        assertLe(total, shares);
        assertLt(shares - total, 5, "only rounding dust carried");

        assertGt(_claim(alice), 0);
        assertEq(d.vault.balanceOf(alice), _cumulative(alice));
        assertEq(_claim(carol), _cumulative(carol), "redeemer keeps what she earned");
        assertEq(_claim(erin), _cumulative(erin));
        assertEq(_cumulative(bob), _cumulative(erin), "seller and buyer split the week by time");
    }

    function test_twoWeeks_lossThenCumulativeClaims_andRedeemForUsdg() public {
        uint256 s1 = _week1();
        uint256 tot1 = 31 * DAY;
        _alloc(alice, 14 * DAY, tot1, s1);
        _alloc(bob, 84 hours, tot1, s1);
        _alloc(erin, 84 hours, tot1, s1);
        _alloc(carol, 5 * DAY, tot1, s1);
        _alloc(dave, 5 * DAY, tot1, s1);
        _publish();
        _claim(alice);

        // week 2 starts at T0+8d (after the root delay); -5% on deployed capital
        uint256 W2 = _now();
        _pnl(-500);
        _convert(frank, PRICE); // reissues carol's returned ID
        assertEq(d.nft.ownerOf(carolId), frank);
        assertEq(d.nft.activationCount(carolId), 2);
        _at(W2 + 7 * DAY);
        (uint256 b,) = _fundWeek("week-2 fees");
        _report();
        uint256 pps = d.vault.officialPPS();
        assertLt(pps, 1e6, "loss shows in PPS, not reset to $1");
        uint256 s2 = d.adapter.settle(b);
        assertGt(s2, (0.035 ether * USDG_PER_ETH / 1 ether) * 1e12, "fees buy more shares below $1");
        _tend();

        // week 2 weights: alice 14d, erin 7d, dave 7d, frank 7d; bob and carol hold nothing
        uint256 tot2 = 35 * DAY;
        uint256 bobBefore = _cumulative(bob);
        _alloc(alice, 14 * DAY, tot2, s2);
        _alloc(erin, 7 * DAY, tot2, s2);
        _alloc(dave, 7 * DAY, tot2, s2);
        _alloc(frank, 7 * DAY, tot2, s2);
        _publish();
        assertEq(_cumulative(bob), bobBefore, "former holder's allocation unchanged");

        // bob never claimed week 1; one cumulative claim pays it
        assertEq(_claim(bob), bobBefore);
        // alice already claimed week 1; this pays only week 2
        uint256 paid = _claim(alice);
        assertEq(d.vault.balanceOf(alice), _cumulative(alice));
        assertGt(paid, 0);
        vm.expectRevert(ShareDistributor.NothingToClaim.selector);
        _claim(alice);

        // solvency
        (, uint256 totalCum,,,) = d.distributor.active();
        assertGe(d.vault.balanceOf(address(d.distributor)) + d.distributor.totalClaimed(), totalCum);

        // bob redeems his pARB for USDG through the normal queue (not instant)
        uint256 bobShares = d.vault.balanceOf(bob);
        vm.prank(bob);
        uint256 rid = d.vault.requestRedeem(bobShares, bob);
        vm.warp(_now() + 1 hours);
        _report();
        vm.prank(bob);
        uint256 got = d.vault.claimRedeem(rid, bob);
        assertEq(got, (bobShares * d.vault.officialPPS()) / 1e18);
        assertEq(usdg.balanceOf(bob), got);
    }

    function test_sameFeesCannotBeFundedTwice() public {
        _week1();
        usdg.mint(address(d.adapter), 100e6);
        vm.prank(operator);
        vm.expectRevert(ParbFundingAdapter.SourceAlreadyFunded.selector);
        d.adapter.fund("week-1 fees", 100e6);
    }

    function test_noPerformanceFeeOnProfit() public {
        _week1();
        _pnl(300);
        vm.warp(_now() + 1 hours);
        _report();
        assertGt(d.vault.officialPPS(), 1e6);
        assertEq(d.vault.balanceOf(teamSafe), 0);
    }

    // ───────── USDG issuer controls (compatibility risks) ─────────

    function test_usdgPause_blocksCashMovementButNotShareClaims() public {
        uint256 s = _week1();
        _alloc(alice, 1, 1, s);
        _publish();
        usdg.setPaused(true);
        // pARB claims move pARB, not USDG: still work
        _claim(alice);
        // redemption request works; USDG payout waits for unpause
        vm.prank(alice);
        uint256 rid = d.vault.requestRedeem(1e18, alice);
        vm.warp(_now() + 1 hours);
        _report();
        vm.prank(alice);
        vm.expectRevert(bytes("USDG: paused"));
        d.vault.claimRedeem(rid, alice);
        // new fee deposits also wait
        vm.prank(operator);
        vm.expectRevert(bytes("USDG: paused"));
        d.adapter.fund("blocked", 100e6);
        usdg.setPaused(false);
        vm.prank(alice);
        d.vault.claimRedeem(rid, alice);
    }

    function test_usdgFreezeOfVault_locksRedemptions() public {
        uint256 s = _week1();
        _alloc(alice, 1, 1, s);
        _publish();
        _claim(alice);
        usdg.setFrozen(address(d.vault), true);
        vm.prank(alice);
        uint256 rid = d.vault.requestRedeem(1e18, alice);
        vm.warp(_now() + 1 hours);
        _report(); // accounting still runs: it only reads balances
        vm.prank(alice);
        vm.expectRevert(bytes("USDG: frozen"));
        d.vault.claimRedeem(rid, alice);
        // pARB itself is not USDG, so holders can still move it
        vm.prank(alice);
        d.vault.transfer(bob, 1e18);
    }
}
