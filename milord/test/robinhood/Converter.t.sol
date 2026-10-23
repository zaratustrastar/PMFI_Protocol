// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MockUSDC} from "../../src/mocks/MockUSDC.sol";
import {stdStorage, StdStorage} from "forge-std/Test.sol";

contract ConverterTest is RobinhoodFixture {
    using stdStorage for StdStorage;

    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    // ───────── entry ─────────

    function test_convert_escrowsExactlyOneMillionAndChargesTenPercent() public {
        uint256 price = 0.5 ether; // 1M MILORD worth 0.5 ETH -> fee 0.05 ETH
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, price);
        uint256 before = alice.balance;
        vm.prank(alice);
        uint256 id = conv.convert{value: 0.06 ether}(q, sig, 0.05 ether);

        assertEq(id, 1);
        assertEq(nft.ownerOf(1), alice);
        assertEq(token.balanceOf(alice), 0);
        assertEq(token.balanceOf(address(conv)), M);
        assertEq(before - alice.balance, 0.05 ether, "excess refunded");
        assertEq(address(entrySplit).balance, 0.05 ether);
        assertEq(conv.backedCount(), 1);
    }

    function test_convert_minimumFeeAppliesForLowPrice() public {
        assertEq(conv.entryFeeFor(0.001 ether), 0.002 ether);
        assertEq(conv.entryFeeFor(0.02 ether), 0.002 ether);
        assertEq(conv.entryFeeFor(0.03 ether), 0.003 ether);
    }

    function testFuzz_entryFeeFormula(uint256 price) public view {
        price = bound(price, 1, 1e30);
        uint256 pct = price / 10;
        assertEq(conv.entryFeeFor(price), pct > 0.002 ether ? pct : 0.002 ether);
    }

    function test_entryFeeSplit_70_30() public {
        _convert(alice, 1 ether); // fee 0.1 ETH
        entrySplit.distribute(address(0));
        assertEq(arbSafe.balance, 0.07 ether);
        assertEq(teamSafe.balance, 0.03 ether);
    }

    function test_revert_expiredQuote() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.warp(block.timestamp + 6 minutes);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.QuoteExpired.selector);
        conv.convert{value: 1 ether}(q, sig, 1 ether);
    }

    function test_revert_quoteLifetimeTooLong() public {
        _fund(alice, M, 1 ether);
        MilordConverter.EntryQuote memory q = MilordConverter.EntryQuote(
            alice, 1 ether, uint64(block.timestamp), uint64(block.timestamp + 1 hours), bytes32("n")
        );
        bytes memory sig = _sign(q, signerPk);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.QuoteExpired.selector);
        conv.convert{value: 1 ether}(q, sig, 1 ether);
    }

    function test_revert_replayedQuote() public {
        _fund(alice, 2 * M, 2 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.startPrank(alice);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
        vm.expectRevert(MilordConverter.QuoteReused.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
        vm.stopPrank();
    }

    function test_revert_quoteForAnotherAccount() public {
        _fund(bob, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.prank(bob);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
    }

    function test_revert_unapprovedSigner() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q,) = _quote(alice, 1 ether);
        bytes memory sig = _sign(q, 0xBAD);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
    }

    function test_revert_tamperedPrice() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        q.weiPerMillion = 0.01 ether; // try to pay the minimum
        vm.prank(alice);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
    }

    function test_revoked_signer_cannotQuote() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.prank(owner);
        conv.setQuoteSigner(quoteSigner, false);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
    }

    function test_revert_feeAboveUserMax() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(MilordConverter.FeeAboveMax.selector, 0.1 ether, 0.05 ether));
        conv.convert{value: 0.1 ether}(q, sig, 0.05 ether);
    }

    function test_revert_insufficientFee() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(MilordConverter.InsufficientFee.selector, 0.1 ether, 0.09 ether));
        conv.convert{value: 0.09 ether}(q, sig, 1 ether);
    }

    function test_revert_insufficientMilord() public {
        vm.prank(treasury);
        token.transfer(alice, M - 1);
        vm.deal(alice, 1 ether);
        vm.prank(alice);
        token.approve(address(conv), type(uint256).max);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.prank(alice);
        vm.expectRevert();
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
    }

    function test_pauseBlocksEntryOnly() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(owner);
        conv.setEntryPaused(true);

        _fund(bob, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(bob, 1 ether);
        vm.prank(bob);
        vm.expectRevert(MilordConverter.EntryPaused.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);

        vm.prank(alice);
        conv.redeem(id); // redemption still works
        assertEq(token.balanceOf(alice), 900_000 ether);
    }

    function test_supplyExhausted() public {
        stdstore.target(address(conv)).sig("nextMintId()").checked_write(uint256(6768));
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, 1 ether);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.SupplyExhausted.selector);
        conv.convert{value: 0.1 ether}(q, sig, 0.1 ether);
    }

    // ───────── exit ─────────

    function test_redeem_releases900kBurns100k_returnsToInventory() public {
        uint256 id = _convert(alice, 1 ether);
        uint256 supplyBefore = token.totalSupply();
        vm.prank(alice);
        uint256 released = conv.redeem(id);
        assertEq(released, 900_000 ether);
        assertEq(token.balanceOf(alice), 900_000 ether);
        assertEq(supplyBefore - token.totalSupply(), 100_000 ether);
        assertEq(nft.ownerOf(id), address(conv));
        assertFalse(nft.isActive(id));
        assertEq(conv.inventorySize(), 1);
        assertEq(conv.backedCount(), 0);
        assertEq(token.balanceOf(address(conv)), 0);
    }

    function test_redeem_onlyHolder() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(bob);
        vm.expectRevert(MilordConverter.NotHolder.selector);
        conv.redeem(id);
    }

    function test_reissueKeepsIdentity_LIFO() public {
        uint256 a = _convert(alice, 1 ether);
        uint256 b = _convert(bob, 1 ether);
        vm.prank(alice);
        conv.redeem(a);
        vm.prank(bob);
        conv.redeem(b);
        // last returned (b) is issued first; no ID choice possible
        uint256 c = _convert(makeAddr("carol"), 1 ether);
        assertEq(c, b);
        assertEq(nft.activationCount(b), 2);
        assertEq(nft.totalMinted(), 2);
    }

    function test_transferredNftCarriesRedemptionRight() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(alice);
        nft.transferFrom(alice, bob, id);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.NotHolder.selector);
        conv.redeem(id);
        vm.prank(bob);
        conv.redeem(id);
        assertEq(token.balanceOf(bob), 900_000 ether);
    }

    // ───────── admin bounds ─────────

    function test_reduceFeesImmediately() public {
        vm.prank(owner);
        conv.reduceFeeParams(MilordConverter.FeeParams(500, 0, 0.001 ether));
        (uint16 e, uint16 x, uint256 m) = conv.feeParams();
        assertEq(e, 500);
        assertEq(x, 0);
        assertEq(m, 0.001 ether);
        uint256 id = _convert(alice, 1 ether);
        vm.prank(alice);
        assertEq(conv.redeem(id), M);
    }

    function test_reduceCannotIncrease() public {
        vm.prank(owner);
        vm.expectRevert(MilordConverter.BadParams.selector);
        conv.reduceFeeParams(MilordConverter.FeeParams(1500, 1000, 0.002 ether));
    }

    function test_increaseNeedsDelayAndCeiling() public {
        vm.startPrank(owner);
        vm.expectRevert(MilordConverter.BadParams.selector);
        conv.queueFeeParams(MilordConverter.FeeParams(2001, 1000, 0.002 ether));
        vm.expectRevert(MilordConverter.BadParams.selector);
        conv.queueFeeParams(MilordConverter.FeeParams(1000, 1001, 0.002 ether));
        conv.queueFeeParams(MilordConverter.FeeParams(1500, 1000, 0.003 ether));
        vm.expectRevert(MilordConverter.TooEarly.selector);
        conv.executeFeeParams();
        vm.warp(block.timestamp + 2 days);
        conv.executeFeeParams();
        vm.stopPrank();
        assertEq(conv.entryFeeFor(1 ether), 0.15 ether);
    }

    function test_onlyOwnerAdmin() public {
        vm.expectRevert();
        conv.setEntryPaused(true);
        vm.expectRevert();
        conv.setQuoteSigner(address(1), true);
        vm.expectRevert();
        conv.reduceFeeParams(MilordConverter.FeeParams(0, 0, 0));
    }

    function test_ownerCannotRescueEscrow() public {
        _convert(alice, 1 ether);
        vm.prank(owner);
        vm.expectRevert(bytes("principal"));
        conv.rescueERC20(IERC20(address(token)), owner, M);
        // other tokens are rescuable
        MockUSDC u = new MockUSDC();
        u.mint(address(conv), 5);
        vm.prank(owner);
        conv.rescueERC20(IERC20(address(u)), owner, 5);
        assertEq(u.balanceOf(owner), 5);
    }

    function test_feeRecipientChangeDelayed() public {
        address n = makeAddr("newSplit");
        vm.startPrank(owner);
        conv.queueEntryFeeRecipient(n);
        vm.expectRevert(MilordConverter.TooEarly.selector);
        conv.executeEntryFeeRecipient();
        vm.warp(block.timestamp + 2 days);
        conv.executeEntryFeeRecipient();
        vm.stopPrank();
        assertEq(conv.entryFeeRecipient(), n);
    }

    function test_maxSimultaneouslyBackedFallsWithBurns() public {
        assertEq(conv.maxSimultaneouslyBacked(), 6767);
        vm.prank(treasury);
        token.burn(M);
        assertEq(conv.maxSimultaneouslyBacked(), 6766);
    }

    function testFuzz_cycle_conservesTokens(uint8 n) public {
        n = uint8(bound(n, 1, 20));
        uint256 supply0 = token.totalSupply();
        for (uint256 i; i < n; ++i) {
            address u = address(uint160(0x1000 + i));
            uint256 id = _convert(u, 1 ether);
            if (i % 2 == 0) {
                vm.prank(u);
                conv.redeem(id);
            }
        }
        uint256 redeemed = (uint256(n) + 1) / 2;
        assertEq(supply0 - token.totalSupply(), redeemed * 100_000 ether);
        assertEq(token.balanceOf(address(conv)), conv.backedCount() * M);
        assertEq(conv.backedCount(), nft.activeSupply());
    }
}
