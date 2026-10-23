// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {HookrFixture} from "../utils/HookrFixture.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

/// @notice Final launch configuration (owner decision 2026-10-14): all 6,767,000,000 MILORD in one MILORD-only
///         position, 0 ETH, ~10 ETH opening FDV. Checks that nothing stays outside the pool, that the first buy
///         works and matches offchain/launch-final.mjs, and that sells pay ETH only from what buyers brought in.
contract HookrFullSupplyTest is HookrFixture {
    address internal buyer = makeAddr("firstBuyer");
    address internal second = makeAddr("secondBuyer");

    function test_allSupplyEntersTheFoundingPosition() public {
        uint256 supply = 6_767_000_000 ether;
        assertEq(launch.milordForLiquidity, supply, "the whole supply is offered to the position");
        assertLe(launchDust, 1e6, "rounding remainder is dust");
        assertEq(d.token.balanceOf(treasury), 0, "allocation holder keeps nothing");
        assertEq(d.token.balanceOf(address(this)), 0, "deployer keeps nothing");
        assertEq(d.token.balanceOf(teamSafe), 0, "team keeps nothing");
        assertEq(d.token.balanceOf(address(v4.poolManager)), supply - launchDust, "everything else is in the pool");
        assertEq(d.token.totalSupply(), supply - launchDust, "the remainder was burned, not kept");
        assertEq(IERC721(address(v4.positionManager)).ownerOf(positionId), address(familyLock));
        emit log_named_uint("MILORD wei in the position", supply - launchDust);
        emit log_named_uint("MILORD wei burned as rounding remainder", launchDust);
    }

    function test_opensAtTenEthFdvWithZeroEth() public view {
        (, int24 tick,,) = _slot0(v4.poolManager, milordKey);
        assertEq(tick, launch.tickUpper);
        assertApproxEqRel(_weiPerMillion() * 6_767, 10.058 ether, 0.001e18, "opening FDV ~10 ETH");
        assertEq(address(v4.poolManager).balance, 0, "0 ETH in the MILORD pool");
    }

    function test_firstBuyWorksAndMatchesModel() public {
        uint256 p0 = _weiPerMillion();
        uint256 small = _buy(buyer, 0.1 ether);
        assertApproxEqRel(small, 65_454_764 ether, 0.002e18, "0.1 ETH first buy");
        uint256 p1 = _weiPerMillion();
        assertApproxEqAbs((p1 - p0) * 10_000 / p0, 190, 3, "+1.9%");
        uint256 rest = _buy(buyer, 0.9 ether);
        assertApproxEqRel(small + rest, 603_660_290 ether, 0.003e18, "1 ETH in total");
        assertApproxEqAbs((_weiPerMillion() - p0) * 10_000 / p0, 1_980, 10, "+19.8%");
    }

    function test_buysThenSellsExecute() public {
        uint256 a = _buy(buyer, 1 ether);
        uint256 b = _buy(second, 0.5 ether);
        uint256 outB = _sell(second, b);
        assertGt(outB, 0, "a seller is paid from buyers' ETH");
        assertLt(outB, 0.5 ether, "round trip pays tax and LP fee");
        uint256 outA = _sell(buyer, a);
        assertGt(outA, 0);
        assertLt(outA + outB, 1.5 ether * 985 / 1000 * 997 / 1000 + 1e6, "never more than buyers' net ETH");
        // after everyone sold, the pool is back near the opening price and still holds all unsold MILORD
        assertApproxEqRel(_weiPerMillion() * 6_767, 10.058 ether, 0.01e18);
    }

    function test_wholePositionIsPurchasableWithoutLimits() public {
        uint256 got = _buy(buyer, 400 ether); // more than the ~323.5 ETH needed: the swap stops at the range end
        assertApproxEqRel(got, 6_767_000_000 ether - launchDust, 0.0001e18, "one swap can buy the whole position");
    }

    receive() external payable {}
}
