// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BaseFixture} from "../utils/BaseFixture.sol";
import {Merkle} from "../utils/Merkle.sol";
import {ParbFundingAdapter} from "../../src/base/ParbFundingAdapter.sol";
import {ShareDistributor} from "../../src/base/ShareDistributor.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MockUSDC} from "../../src/mocks/MockUSDC.sol";

contract DistributionTest is BaseFixture {
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address carol = makeAddr("carol");

    // ───────── funding ─────────

    function test_fund_report_settle_mintsSharesToDistributor() public {
        uint256 b = _fundBatch("week-1-fees", 700e6);
        vm.expectRevert(ParbFundingAdapter.NotClaimable.selector);
        adapter.settle(b);
        _honestReport();
        uint256 shares = adapter.settle(b);
        assertEq(shares, 700e18);
        assertEq(vault.balanceOf(address(dist)), 700e18);
        assertEq(vault.balanceOf(address(adapter)), 0);
        assertEq(adapter.totalSharesMinted(), 700e18);
        assertEq(adapter.batch(b).sharesMinted, 700e18);
    }

    function test_depositedCapitalIsNotProfit() public {
        uint256 b = _fundBatch("w1", 1000e6);
        _honestReport();
        adapter.settle(b);
        uint256 pps = vault.officialPPS();
        uint256 b2 = _fundBatch("w2", 5000e6);
        _honestReport();
        adapter.settle(b2);
        assertEq(vault.officialPPS(), pps, "new fee deposits do not raise PPS");
        assertEq(vault.balanceOf(feeRecipient), 0);
    }

    function test_sourceRefCannotBeFundedTwice() public {
        _fundBatch("same-fees", 100e6);
        usdc.mint(address(adapter), 100e6);
        vm.prank(fundingOperator);
        vm.expectRevert(ParbFundingAdapter.SourceAlreadyFunded.selector);
        adapter.fund("same-fees", 100e6);
    }

    function test_onlyOperatorFunds() public {
        usdc.mint(address(adapter), 100e6);
        vm.expectRevert(ParbFundingAdapter.NotOperator.selector);
        adapter.fund("x", 100e6);
    }

    function test_syncAfterThirdPartyAutoClaim() public {
        uint256 b = _fundBatch("w1", 100e6);
        _honestReport();
        uint256[] memory ids = new uint256[](1);
        ids[0] = adapter.batch(b).requestId;
        vault.autoClaimDeposits(ids); // anyone can; shares still go to distributor
        vm.expectRevert(ParbFundingAdapter.NotClaimable.selector);
        adapter.settle(b);
        assertEq(adapter.syncClaimed(b), 100e18);
        assertEq(vault.balanceOf(address(dist)), 100e18);
    }

    function test_settleIdempotent() public {
        uint256 b = _fundBatch("w1", 100e6);
        _honestReport();
        adapter.settle(b);
        vm.expectRevert(ParbFundingAdapter.AlreadySettled.selector);
        adapter.settle(b);
        vm.expectRevert(ParbFundingAdapter.AlreadySettled.selector);
        adapter.syncClaimed(b);
    }

    // ───────── Merkle claims ─────────

    function _seed(uint256 usdcAmount, bytes32 ref) internal {
        uint256 b = _fundBatch(ref, usdcAmount);
        _honestReport();
        adapter.settle(b);
    }

    function _tree3(uint256 a, uint256 b, uint256 c) internal view returns (bytes32[] memory l) {
        l = new bytes32[](3);
        l[0] = Merkle.leaf(alice, a);
        l[1] = Merkle.leaf(bob, b);
        l[2] = Merkle.leaf(carol, c);
    }

    function _publish(bytes32 root, uint256 total, uint64 epoch) internal {
        vm.prank(publisher);
        dist.proposeRoot(root, total, epoch, keccak256("allocation.json"));
        vm.warp(vm.getBlockTimestamp() + 1 days);
        dist.activateRoot();
    }

    function test_weeklyCumulativeClaims_previousOwnerKeepsEarned() public {
        _seed(1000e6, "w1");
        // week 1: alice held the whole week, bob half a week then sold to carol
        bytes32[] memory l1 = _tree3(600e18, 200e18, 200e18);
        _publish(Merkle.root(l1), 1000e18, 1);

        vm.prank(alice);
        dist.claim(alice, 600e18, Merkle.proof(l1, 0));
        assertEq(vault.balanceOf(alice), 600e18);

        _seed(500e6, "w2");
        // week 2: bob no longer holds; his cumulative stays 200
        bytes32[] memory l2 = _tree3(850e18, 200e18, 450e18);
        _publish(Merkle.root(l2), 1500e18, 2);

        dist.claim(alice, 850e18, Merkle.proof(l2, 0)); // anyone can trigger; pays alice
        assertEq(vault.balanceOf(alice), 850e18);
        dist.claim(bob, 200e18, Merkle.proof(l2, 1)); // bob skipped week 1 claim, loses nothing
        assertEq(vault.balanceOf(bob), 200e18);
        dist.claim(carol, 450e18, Merkle.proof(l2, 2));
        assertEq(vault.balanceOf(carol), 450e18);
        assertEq(dist.totalClaimed(), 1500e18);
        assertEq(vault.balanceOf(address(dist)), 0);

        vm.expectRevert(ShareDistributor.NothingToClaim.selector);
        dist.claim(bob, 200e18, Merkle.proof(l2, 1));
    }

    function test_rootMustBeFunded() public {
        _seed(100e6, "w1");
        bytes32[] memory l = _tree3(60e18, 40e18, 1);
        vm.prank(publisher);
        vm.expectRevert(abi.encodeWithSelector(ShareDistributor.Underfunded.selector, 100e18 + 1, 100e18));
        dist.proposeRoot(Merkle.root(l), 100e18 + 1, 1, 0);
    }

    function test_totalsCannotDecrease() public {
        _seed(100e6, "w1");
        bytes32[] memory l = _tree3(60e18, 40e18, 0);
        _publish(Merkle.root(l), 100e18, 1);
        vm.prank(publisher);
        vm.expectRevert(ShareDistributor.NotIncreasing.selector);
        dist.proposeRoot(Merkle.root(l), 99e18, 2, 0);
        vm.prank(publisher);
        vm.expectRevert(ShareDistributor.NotIncreasing.selector);
        dist.proposeRoot(Merkle.root(l), 100e18, 1, 0);
    }

    function test_activationDelayAndVeto() public {
        _seed(100e6, "w1");
        bytes32[] memory l = _tree3(60e18, 40e18, 0);
        vm.prank(publisher);
        dist.proposeRoot(Merkle.root(l), 100e18, 1, 0);
        vm.expectRevert(ShareDistributor.InvalidProof.selector);
        dist.claim(alice, 60e18, Merkle.proof(l, 0)); // not active yet
        vm.prank(owner);
        dist.vetoPending();
        vm.warp(vm.getBlockTimestamp() + 2 days);
        vm.expectRevert(ShareDistributor.InvalidProof.selector);
        dist.claim(alice, 60e18, Merkle.proof(l, 0));
    }

    function test_onlyPublisherProposes() public {
        vm.expectRevert(ShareDistributor.NotPublisher.selector);
        dist.proposeRoot(bytes32(uint256(1)), 0, 1, 0);
    }

    function test_wrongAmountOrAccountFails() public {
        _seed(100e6, "w1");
        bytes32[] memory l = _tree3(60e18, 40e18, 0);
        _publish(Merkle.root(l), 100e18, 1);
        bytes32[] memory p = Merkle.proof(l, 0);
        vm.expectRevert(ShareDistributor.InvalidProof.selector);
        dist.claim(alice, 61e18, p);
        vm.expectRevert(ShareDistributor.InvalidProof.selector);
        dist.claim(bob, 60e18, p);
    }

    function test_parbCannotBeRescued_unallocatedCarriesForward() public {
        _seed(100e6, "w1");
        bytes32[] memory l = _tree3(60e18, 30e18, 0);
        _publish(Merkle.root(l), 90e18, 1);
        assertEq(dist.unallocated(), 10e18);
        vm.prank(owner);
        vm.expectRevert(bytes("pARB locked"));
        dist.rescue(IERC20(address(vault)), owner, 1);
        MockUSDC other = new MockUSDC();
        other.mint(address(dist), 5);
        vm.prank(owner);
        dist.rescue(IERC20(address(other)), owner, 5);
    }

    function test_holdersCanRedeemClaimedParbThroughVault() public {
        _seed(1000e6, "w1");
        bytes32[] memory l = _tree3(1000e18, 0, 0);
        _publish(Merkle.root(l), 1000e18, 1);
        dist.claim(alice, 1000e18, Merkle.proof(l, 0));
        vm.prank(alice);
        uint256 rr = vault.requestRedeem(1000e18, alice);
        _honestReport();
        vm.prank(alice);
        vault.claimRedeem(rr, alice);
        assertEq(usdc.balanceOf(alice), 1000e6);
    }
}
