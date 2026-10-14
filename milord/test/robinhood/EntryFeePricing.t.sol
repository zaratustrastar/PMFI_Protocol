// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";
import {IMILORD} from "../../src/interfaces/IMILORD.sol";

/// Entry fee = max(0.002 ETH, 10% of the ETH value of 1,000,000 MILORD), where the quote service supplies
/// weiPerMillion from the Hookr MILORD/ETH pool (offchain/lib/entry-price.mjs). At a fully diluted value of
/// F ETH, 1,000,000 MILORD is worth F / 6,767 ETH (6,767,000,000 MILORD total supply).
contract EntryFeePricingTest is RobinhoodFixture {
    address alice = makeAddr("alice");

    function _wpm(uint256 fdvWei) internal pure returns (uint256) {
        return fdvWei / 6767;
    }

    function test_fee_at10EthFdv_isMinimum() public view {
        assertApproxEqAbs(_wpm(10 ether), 0.001478 ether, 0.000001 ether);
        assertEq(conv.entryFeeFor(_wpm(10 ether)), 0.002 ether);
    }

    function test_fee_at100EthFdv_isMinimum() public view {
        assertEq(conv.entryFeeFor(_wpm(100 ether)), 0.002 ether);
    }

    function test_fee_at200EthFdv() public view {
        assertApproxEqAbs(conv.entryFeeFor(_wpm(200 ether)), 0.002955 ether, 0.000001 ether);
    }

    function test_fee_at500EthFdv() public view {
        assertApproxEqAbs(conv.entryFeeFor(_wpm(500 ether)), 0.007388 ether, 0.000001 ether);
    }

    function test_minimumCrossover_at135_34EthFdv() public view {
        assertEq(conv.entryFeeFor(_wpm(135.3 ether)), 0.002 ether);
        assertGt(conv.entryFeeFor(_wpm(135.4 ether)), 0.002 ether);
    }

    /// Redemption never reads a quote or a price: it works with every quote signer revoked and entry paused.
    function test_redeem_independentOfPricing() public {
        uint256 id = _convert(alice, _wpm(500 ether));
        vm.startPrank(owner);
        conv.setQuoteSigner(quoteSigner, false);
        conv.setEntryPaused(true);
        vm.stopPrank();

        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, _wpm(500 ether));
        vm.prank(alice);
        vm.expectRevert();
        conv.convert{value: 0.01 ether}(q, sig, 0.01 ether);

        uint256 before = token.balanceOf(alice);
        vm.prank(alice);
        uint256 released = conv.redeem(id);
        assertEq(token.balanceOf(alice) - before, released);
        assertGt(released, 0);
    }

    function test_gas_convertAndRedeem() public {
        _fund(alice, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, _wpm(500 ether));
        uint256 fee = conv.entryFeeFor(q.weiPerMillion);
        vm.prank(alice);
        uint256 g = gasleft();
        uint256 id = conv.convert{value: fee}(q, sig, fee);
        emit log_named_uint("convert gas (first NFT, cold)", g - gasleft());
        vm.prank(alice);
        g = gasleft();
        conv.redeem(id);
        emit log_named_uint("redeem gas", g - gasleft());
    }
}

/// A compromised or faulty quote signer can sign any price. These tests pin down what it still cannot do.
contract CompromisedSignerTest is RobinhoodFixture {
    address alice = makeAddr("alice");
    uint256 constant FAIR = uint256(500 ether) / 6767; // 500 ETH FDV
    uint256 constant INFLATED = FAIR * 100; // signer quotes 100x the market price

    function setUp() public override {
        super.setUp();
        _fund(alice, 3 * M, 10 ether);
    }

    function _signed(uint256 wpm, uint64 issued, uint64 expires, bytes32 nonce)
        internal
        view
        returns (MilordConverter.EntryQuote memory q, bytes memory sig)
    {
        q = MilordConverter.EntryQuote(alice, wpm, issued, expires, nonce);
        sig = _sign(q, signerPk);
    }

    /// Overpricing: the user's own maxFee (computed by the website from the pool, not from the quote) caps payment.
    function test_overpricedQuote_rejectedByUserMaxFee() public {
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, INFLATED);
        uint256 userCap = conv.entryFeeFor((FAIR * 110) / 100);
        uint256 inflatedFee = conv.entryFeeFor(INFLATED);
        uint256 balBefore = alice.balance;
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(MilordConverter.FeeAboveMax.selector, inflatedFee, userCap));
        conv.convert{value: 5 ether}(q, sig, userCap);
        assertEq(alice.balance, balBefore, "nothing taken");
        assertEq(token.balanceOf(alice), 3 * M, "no MILORD taken");
    }

    /// Never more than msg.value, and anything above the fee is refunded.
    function test_paymentNeverExceedsMsgValue_excessRefunded() public {
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, INFLATED);
        uint256 fee = conv.entryFeeFor(INFLATED);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(MilordConverter.InsufficientFee.selector, fee, fee - 1));
        conv.convert{value: fee - 1}(q, sig, type(uint256).max);

        (q, sig) = _quote(alice, FAIR);
        uint256 fair = conv.entryFeeFor(FAIR);
        uint256 balBefore = alice.balance;
        vm.prank(alice);
        conv.convert{value: 1 ether}(q, sig, fair);
        assertEq(balBefore - alice.balance, fair, "paid exactly the fee, rest refunded");
    }

    /// EIP-712 domain binds chain id: a quote signed for another chain is rejected.
    function test_chainBinding() public {
        vm.chainId(1);
        (MilordConverter.EntryQuote memory q, bytes memory sig) =
            _signed(FAIR, uint64(block.timestamp), uint64(block.timestamp + 5 minutes), "chain");
        bytes32 onOtherChain = conv.quoteDigest(q);
        vm.chainId(4663); // literal: the optimizer may fold repeated block.chainid reads in a test
        assertTrue(conv.quoteDigest(q) != onOtherChain, "domain includes chain id");
        uint256 fee = conv.entryFeeFor(FAIR);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: fee}(q, sig, fee);
    }

    /// EIP-712 domain binds the converter address: a quote for another deployment is rejected.
    function test_contractBinding() public {
        MilordConverter other = new MilordConverter(owner, IMILORD(address(token)), nft, address(entrySplit), quoteSigner);
        MilordConverter.EntryQuote memory q = MilordConverter.EntryQuote(
            alice, FAIR, uint64(block.timestamp), uint64(block.timestamp + 5 minutes), "contract"
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerPk, other.quoteDigest(q));
        uint256 fee = conv.entryFeeFor(FAIR);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: fee}(q, abi.encodePacked(r, s, v), fee);
    }

    /// Quotes are bound to the caller.
    function test_accountBinding() public {
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, FAIR);
        address bob = makeAddr("bob");
        _fund(bob, M, 1 ether);
        vm.prank(bob);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: 0.01 ether}(q, sig, 0.01 ether);
    }

    function test_expiry_notYetValid_andTooLong() public {
        uint64 t = uint64(block.timestamp);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _signed(FAIR, t, t + 5 minutes, "exp");
        vm.warp(t + 5 minutes + 1);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.QuoteExpired.selector);
        conv.convert{value: 0.01 ether}(q, sig, 0.01 ether);

        (q, sig) = _signed(FAIR, uint64(block.timestamp + 60), uint64(block.timestamp + 120), "future");
        vm.prank(alice);
        vm.expectRevert(MilordConverter.QuoteExpired.selector);
        conv.convert{value: 0.01 ether}(q, sig, 0.01 ether);

        (q, sig) = _signed(FAIR, uint64(block.timestamp), uint64(block.timestamp + 1 days), "long");
        vm.prank(alice);
        vm.expectRevert(MilordConverter.QuoteExpired.selector);
        conv.convert{value: 0.01 ether}(q, sig, 0.01 ether);
    }

    function test_replayRejected() public {
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, FAIR);
        uint256 fee = conv.entryFeeFor(FAIR);
        vm.startPrank(alice);
        conv.convert{value: fee}(q, sig, fee);
        vm.expectRevert(MilordConverter.QuoteReused.selector);
        conv.convert{value: fee}(q, sig, fee);
        vm.stopPrank();
    }

    /// Revoking the signer kills its outstanding, still-unexpired quotes immediately.
    function test_revocationKillsOutstandingQuotes() public {
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(alice, INFLATED);
        vm.prank(owner);
        conv.setQuoteSigner(quoteSigner, false);
        vm.prank(alice);
        vm.expectRevert(MilordConverter.BadQuote.selector);
        conv.convert{value: 5 ether}(q, sig, type(uint256).max);
    }

    /// Only the owner (Team Safe) can add or revoke signers.
    function test_onlyOwnerManagesSigners() public {
        vm.prank(alice);
        vm.expectRevert();
        conv.setQuoteSigner(alice, true);
    }
}
