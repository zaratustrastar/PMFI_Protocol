// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HookrFixture} from "../utils/HookrFixture.sol";

/// @notice Owner-approved ~10 ETH opening FDV, MILORD-only liquidity, 0 ETH seeded. For each candidate allocation:
///         the first buy works immediately, its size/price impact matches offchain/launch-10eth.mjs, and once buyers
///         have put ETH in, sellers receive ETH (never more than buyers brought).
abstract contract HookrAllocationBase is HookrFixture {
    address internal buyer = makeAddr("firstBuyer");
    address internal holder = makeAddr("reserveHolder");

    /// expected MILORD (whole tokens) for a 1 ETH first buy and the spot change in bp (from launch-10eth.mjs, 1,000x range)
    function _expected() internal pure virtual returns (uint256 milordOut1Eth, uint256 priceChangeBp1Eth);

    function test_openingPriceIsTenEthFdv() public view {
        uint256 fdvWei = _weiPerMillion() * 6_767;
        assertApproxEqRel(fdvWei, 10.058 ether, 0.001e18, "opening FDV ~10 ETH (tick rounded to spacing 60)");
        assertApproxEqAbs(address(v4.poolManager).balance, 2_000 ether, 1e6, "only the USDG pool's ETH: no ETH seeded in the MILORD pool");
    }

    function test_firstBuyWorksImmediatelyAndMatchesModel() public {
        uint256 p0 = _weiPerMillion();
        uint256 out = _buy(buyer, 1 ether);
        (uint256 expOut, uint256 expBp) = _expected();
        assertApproxEqRel(out, expOut * 1e18, 0.002e18, "MILORD out matches the model");
        uint256 changeBp = (_weiPerMillion() - p0) * 10_000 / p0;
        assertApproxEqAbs(changeBp, expBp, 5, "price impact matches the model");
    }

    function test_buyerEthThenEnablesSells() public {
        vm.prank(treasury);
        d.token.transfer(holder, 100_000_000 ether);
        assertEq(_sell(holder, 1_000_000 ether), 0, "before any buy: 0 ETH");

        _buy(buyer, 1 ether);
        uint256 got = _sell(holder, 1_000_000 ether);
        assertGt(got, 0, "after a buy: seller receives ETH");

        // even a large sell can only take out what buyers brought in (net of tax and LP fee)
        uint256 gotLarge = _sell(holder, 99_000_000 ether);
        assertLt(got + gotLarge, 1 ether * 985 / 1000 * 997 / 1000, "bounded by buyers' net ETH");
    }

    receive() external payable {}
}

contract HookrAllocation25Test is HookrAllocationBase {
    function _liquidityShareBps() internal pure override returns (uint256) { return 2_500; }
    function _expected() internal pure override returns (uint256, uint256) { return (479_400_000, 8_994); }
}

contract HookrAllocation50Test is HookrAllocationBase {
    function _liquidityShareBps() internal pure override returns (uint256) { return 5_000; }
    function _expected() internal pure override returns (uint256, uint256) { return (555_700_000, 4_140); }
}

contract HookrAllocation75Test is HookrAllocationBase {
    function _liquidityShareBps() internal pure override returns (uint256) { return 7_500; }
    function _expected() internal pure override returns (uint256, uint256) { return (586_800_000, 2_680); }
}
