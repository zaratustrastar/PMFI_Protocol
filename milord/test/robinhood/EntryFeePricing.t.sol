// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";

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
