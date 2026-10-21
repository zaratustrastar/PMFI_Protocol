// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console2} from "forge-std/Test.sol";
import {IHookrLauncher} from "./hookr/interfaces/IHookrLauncher.sol";
import {IHookrFamilyLock} from "./hookr/interfaces/IHookrFamilyLock.sol";
import {MilordForkBase} from "./MilordForkBase.sol";

/// Priority 3: permanent HookrFamilyLock, LP fees through HookrLockFeeCollector into BuybackBurner, buybacks through the
/// real Hookr pool, and the Option D 90/10 split of post-commission Anti-Snipe revenue.
contract MilordLockBuybackFork is MilordForkBase {
    uint64 internal constant FOREVER = type(uint64).max;

    function setUp() public {
        _forkSetUp();
    }

    function _lockForever() internal returns (uint256 ethPaidToSafe, uint256 milordPaidToSafe) {
        uint256 e0 = safe.balance;
        uint256 m0 = milord.balanceOf(safe);
        vm.startPrank(safe);
        LAUNCHER.transferFamily(familyId, address(LOCK), 0);
        LOCK.lock(familyId, address(collector), FOREVER);
        vm.stopPrank();
        ethPaidToSafe = safe.balance - e0;
        milordPaidToSafe = milord.balanceOf(safe) - m0;
    }

    /// The Safe hands the family to HookrFamilyLock forever with HookrLockFeeCollector as beneficiary; afterwards no
    /// one can withdraw principal, release, or redirect fees.
    function test_permanentLock() public {
        _approvedLaunch();
        IHookrLauncher.Position memory pos = LAUNCHER.position(familyId, 0);
        _lockForever();

        assertEq(LAUNCHER.familyOwner(familyId), address(LOCK), "lock owns the family");
        (address ben, uint64 unlockBlock) = LOCK.lockOf(familyId);
        assertEq(ben, address(collector));
        assertEq(unlockBlock, FOREVER, "type(uint64).max = locked forever");

        vm.prank(safe);
        vm.expectRevert();
        LAUNCHER.withdraw(familyId, 0, pos.liquidity, 0, 0, safe, block.timestamp);

        vm.roll(block.number + 10_000_000);
        vm.prank(address(collector));
        vm.expectRevert(abi.encodeWithSelector(IHookrFamilyLock.StillLocked.selector, familyId, uint256(FOREVER)));
        LOCK.release(familyId, safe);

        vm.prank(makeAddr("stranger"));
        vm.expectRevert(abi.encodeWithSelector(IHookrFamilyLock.NotBeneficiary.selector, familyId, makeAddr("stranger")));
        LOCK.collect(familyId, 0, 0);

        vm.prank(safe);
        vm.expectRevert();
        LAUNCHER.transferFamily(familyId, safe, 0);

        assertEq(LAUNCHER.position(familyId, 0).liquidity, pos.liquidity, "principal untouched");
    }

    /// Trading fees after the lock: anyone triggers HookrLockFeeCollector; ETH fees reach BuybackBurner, MILORD fees are
    /// burned; principal stays.
    function test_feeCollectionToBuybackBurner() public {
        _approvedLaunch();
        _lockForever();
        uint128 liq = LAUNCHER.position(familyId, 0).liquidity;
        _swap(true, 2 ether);
        vm.roll(block.number + 1);
        _swap(false, milord.balanceOf(address(this)) / 2);
        vm.roll(block.number + 1);
        _swap(true, 1 ether);

        uint256 eth0 = burner.totalEthReceived();
        uint256 supply0 = milord.totalSupply();
        vm.prank(makeAddr("anyone"));
        (uint256 a0, uint256 a1) = collector.collect(0);
        assertApproxEqAbs(a0, lpFeeEthTotal, 4, "ETH LP fees = sum of Swap-event fees");
        assertApproxEqAbs(a1, lpFeeMilordTotal, 4, "MILORD LP fees = sum of Swap-event fees");
        assertEq(burner.totalEthReceived() - eth0, a0, "all ETH fees reached BuybackBurner");
        assertEq(supply0 - milord.totalSupply(), a1, "all MILORD fees burned");
        assertEq(address(collector).balance, 0);
        assertEq(milord.balanceOf(address(collector)), 0);
        assertEq(LAUNCHER.position(familyId, 0).liquidity, liq, "principal untouched");
        console2.log("collected ETH / MILORD fees", a0, a1);
    }

    /// BuybackBurner buys MILORD through V4SwapAdapter on the real Hookr pool (unauthenticated exact-input swap on the
    /// PoolManager), pays the 1.5% tax like any buyer, and burns what it bought.
    function test_buybackAndBurnThroughHookr() public {
        _approvedLaunch();
        _lockForever();
        (bool ok,) = payable(address(burner)).call{value: 2 ether}("");
        assertTrue(ok);
        uint256 supply0 = milord.totalSupply();
        uint256 q0 = buyQueue.pending();
        vm.prank(operator);
        uint256 burned = burner.buybackAndBurn(1 ether, 1, block.timestamp + 60);
        assertGt(burned, 0);
        assertEq(milord.totalSupply(), supply0 - burned, "supply fell by the bought amount");
        assertEq(burner.totalEthSpent(), 1 ether);
        assertApproxEqAbs(buyQueue.pending() - q0, 0.015 ether, 10, "buyback paid the 1.5% buy tax");
        console2.log("1 ETH buyback burned MILORD", burned);
    }

    /// Option D (exploration): with Anti-Snipe on, the Safe owns the family through the window, then hands it to
    /// HookrFamilyLock. Acceptance pays the Safe every LP fee earned so far; the Safe splits the ETH from PoolManager
    /// Swap events: base LP fee + 90% of the post-commission Snipe to BuybackBurner, 10% to the Team Safe, MILORD
    /// fees burned. Later fees are base-only and go through the collector.
    function test_optionD_delayedHandover_90_10() public {
        _antiSnipeLaunch();
        uint256 p0 = _protocolClaim();
        for (uint256 i; i < 6; i++) {
            vm.warp(launchTs + i * 600);
            vm.roll(block.number + 10);
            _swap(true, 0.5 ether);
        }
        _swap(false, milord.balanceOf(address(this)) / 4);
        uint256 hookrSnipe = _protocolClaim() - p0;

        // Principal stays locked through the window even though the Safe owns the family.
        IHookrLauncher.Position memory pos = LAUNCHER.position(familyId, 0);
        vm.prank(safe);
        vm.expectRevert();
        LAUNCHER.withdraw(familyId, 0, pos.liquidity, 0, 0, safe, block.timestamp);

        vm.roll(guardEnd);
        vm.warp(launchTs + snipeSeconds);
        (uint256 windowEth, uint256 windowMilord) = _lockForever();
        assertApproxEqAbs(windowEth, lpFeeEthTotal, 12, "handover pays the window's ETH LP fees to the Safe");
        assertApproxEqAbs(windowMilord, lpFeeMilordTotal, 12, "and its MILORD LP fees");

        // The Safe's split, computed only from Swap events.
        uint256 surcharge = lpSurchargeEthTotal;
        uint256 toTeam = surcharge / 10;
        uint256 toBurner = windowEth - toTeam; // base LP fee + 90% of surcharge + rounding
        uint256 burnerEth0 = burner.totalEthReceived();
        uint256 supply0 = milord.totalSupply();
        vm.startPrank(safe);
        (bool a,) = payable(address(burner)).call{value: toBurner}("");
        (bool b,) = payable(team).call{value: toTeam}("");
        milord.transfer(address(burner), windowMilord);
        vm.stopPrank();
        assertTrue(a && b);
        burner.burnHeldTokens();
        assertEq(burner.totalEthReceived() - burnerEth0, toBurner);
        assertEq(team.balance, toTeam);
        assertEq(supply0 - milord.totalSupply(), windowMilord, "window MILORD fees burned");
        assertApproxEqAbs(toBurner - (windowEth - surcharge), surcharge - toTeam, 12, "burner got base + 90% of surcharge");

        console2.log("window ETH LP fees", windowEth);
        console2.log("  of which post-commission Snipe", surcharge);
        console2.log("  90% to buyback (with base fee)", toBurner);
        console2.log("  10% to Team Safe", toTeam);
        console2.log("Hookr 20% of Snipe (not ours)", hookrSnipe);

        // After the handover: base-only fees, through the collector.
        uint256 before = lpFeeEthTotal;
        uint256 surchargeBefore = lpSurchargeEthTotal;
        _swap(true, 1 ether);
        assertEq(lpSurchargeEthTotal, surchargeBefore, "no Snipe after the guard");
        uint256 eth1 = burner.totalEthReceived();
        (uint256 c0,) = collector.collect(0);
        assertApproxEqAbs(c0, lpFeeEthTotal - before, 10);
        assertEq(burner.totalEthReceived() - eth1, c0);
    }
}
