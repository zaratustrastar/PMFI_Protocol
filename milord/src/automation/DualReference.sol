// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPriceReference} from "./IPriceReference.sol";

/// @notice Two independent references must agree within `maxDeviationBps`; the lower price is used, so the swap's
///         minimum output is set by the more conservative source. Either one reverting stops the swap.
contract DualReference is IPriceReference {
    IPriceReference public immutable a;
    IPriceReference public immutable b;
    uint16 public immutable maxDeviationBps;

    error ReferencesDisagree(uint256 a, uint256 b);

    constructor(IPriceReference a_, IPriceReference b_, uint16 maxDeviationBps_) {
        require(maxDeviationBps_ > 0 && maxDeviationBps_ <= 1000, "deviation");
        a = a_;
        b = b_;
        maxDeviationBps = maxDeviationBps_;
    }

    function assetPerEth() external view returns (uint256) {
        uint256 pa = a.assetPerEth();
        uint256 pb = b.assetPerEth();
        (uint256 lo, uint256 hi) = pa < pb ? (pa, pb) : (pb, pa);
        if ((hi - lo) * 10_000 > lo * maxDeviationBps) revert ReferencesDisagree(pa, pb);
        return lo;
    }
}
