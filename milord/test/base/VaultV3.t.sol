// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BaseFixture} from "../utils/BaseFixture.sol";
import {PMFIArbVaultV3} from "../../src/base/PMFIArbVaultV3.sol";

/// V3 behaviour with an open (unrestricted) vault so individual depositors can be modelled.
contract VaultV3Test is BaseFixture {
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    function setUp() public override {
        super.setUp();
        vm.prank(owner);
        vault.setDepositorRestricted(false);
    }

    function _deposit(address who, uint256 amt) internal returns (uint256 rid) {
        usdc.mint(who, amt);
        vm.startPrank(who);
        usdc.approve(address(vault), amt);
        rid = vault.requestDeposit(amt, who);
        vm.stopPrank();
    }

    function _claim(address who, uint256 rid) internal {
        vm.prank(who);
        vault.claimDeposit(rid, who);
    }

    function test_firstDepositAtOneDollar() public {
        uint256 rid = _deposit(alice, 1000e6);
        _honestReport();
        assertEq(vault.officialPPS(), 1e6);
        _claim(alice, rid);
        assertEq(vault.balanceOf(alice), 1000e18);
    }

    function test_zeroPerformanceFee_profitGoesToHolders() public {
        uint256 rid = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, rid);
        _tendAll();
        _pnl(50e6); // +5%
        _honestReport();
        assertEq(vault.balanceOf(feeRecipient), 0, "no fee shares");
        assertEq(vault.officialPPS(), 1.05e6);
        assertEq(vault.previewRedeem(1000e18), 1050e6);
    }

    function test_negativePeriodLowersPps_notForcedToOne() public {
        uint256 rid = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, rid);
        _tendAll();
        _pnl(-80e6); // -8%
        _honestReport();
        assertEq(vault.officialPPS(), 0.92e6);
        // next depositor buys at the lower price, receiving more shares
        uint256 rid2 = _deposit(bob, 920e6);
        _honestReport();
        _claim(bob, rid2);
        assertEq(vault.balanceOf(bob), 1000e18);
        assertEq(vault.officialPPS(), 0.92e6);
    }

    function test_unclaimedDepositSharesCounted() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        _deposit(bob, 1000e6);
        _honestReport(); // bob processed, NOT claimed
        assertEq(vault.totalUnclaimedDepositShares(), 1000e18);
        _honestReport(); // no P&L
        assertEq(vault.officialPPS(), 1e6, "V2 would report 2e6 here");
    }

    function test_pendingRedeemSharesCounted() public {
        vm.prank(owner);
        vault.setMinimumIdleAmount(0);
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        _tendAll(); // idle left: 10% target = 100e6
        vm.prank(alice);
        vault.requestRedeem(500e18, alice); // needs 500e6, only 100e6 idle -> stays pending
        _honestReport();
        assertEq(vault.officialPPS(), 1e6, "V2 would report 2e6 here");
        assertEq(vault.totalPendingRedeemShares(), 500e18);
    }

    function test_redeemIsRequestBased_notInstant() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        vm.prank(alice);
        uint256 rr = vault.requestRedeem(400e18, alice);
        vm.prank(alice);
        vm.expectRevert(bytes("Not claimable yet"));
        vault.claimRedeem(rr, alice);
        _honestReport();
        vm.prank(alice);
        vault.claimRedeem(rr, alice);
        assertEq(usdc.balanceOf(alice), 400e6);
    }

    function test_zeroNavReverts() public {
        vm.startPrank(owner);
        vault.setMinimumIdleAmount(0);
        vault.setTargetIdleBps(0);
        vault.approveLargeMove(reportNonce + 3);
        vm.stopPrank();
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        _tendAll();
        _pnl(-1000e6);
        PMFIArbVaultV3.ReportData memory d = _reportData(0);
        bytes memory sig = _sigFor(address(vault), vault.DOMAIN_SALT(), d);
        vm.expectRevert(PMFIArbVaultV3.ZeroNav.selector);
        vault.report(d, sig, 0, 0);
    }

    function test_ppsMoveGuard() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        _tendAll();
        _pnl(150e6); // +15% in one report
        PMFIArbVaultV3.ReportData memory d = _reportData(usdc.balanceOf(address(vault)) + offchain);
        bytes memory sig = _sigFor(address(vault), vault.DOMAIN_SALT(), d);
        vm.expectRevert(abi.encodeWithSelector(PMFIArbVaultV3.PpsMoveTooLarge.selector, 1e6, 1.15e6));
        vault.report(d, sig, 0, 0);
        vm.prank(owner);
        vault.approveLargeMove(d.nonce);
        vault.report(d, sig, 0, 0);
        assertEq(vault.officialPPS(), 1.15e6);
    }

    function test_reportBelowOnchainCashReverts() public {
        _deposit(alice, 1000e6);
        PMFIArbVaultV3.ReportData memory d = _reportData(999e6);
        bytes memory sig = _sigFor(address(vault), vault.DOMAIN_SALT(), d);
        vm.expectRevert(abi.encodeWithSelector(PMFIArbVaultV3.ReportBelowOnchainCash.selector, 999e6, 1000e6));
        vault.report(d, sig, 0, 0);
    }

    function test_reportSignatureChecks() public {
        PMFIArbVaultV3.ReportData memory d = _reportData(0);
        bytes memory sig = _sigFor(address(vault), keccak256("wrong salt"), d);
        vm.expectRevert(bytes("Invalid signer"));
        vault.report(d, sig, 0, 0);
        sig = _sigFor(address(vault), vault.DOMAIN_SALT(), d);
        vault.report(d, sig, 0, 0);
        vm.expectRevert(bytes("Nonce must increase"));
        vault.report(d, sig, 0, 0);
    }

    function test_allowlistRestrictsDepositsOnly() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        vm.prank(owner);
        vault.setDepositorRestricted(true);
        usdc.mint(bob, 100e6);
        vm.startPrank(bob);
        usdc.approve(address(vault), 100e6);
        vm.expectRevert(bytes("Depositor not allowed"));
        vault.requestDeposit(100e6, bob);
        vm.stopPrank();
        vm.prank(alice);
        vault.transfer(bob, 10e18); // pARB stays transferable
        vm.prank(bob);
        vault.requestRedeem(10e18, bob); // and redeemable by any holder
    }

    function test_emergencyWithdrawCannotTakeReserved() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        _deposit(bob, 500e6); // pending
        vm.prank(alice);
        vault.requestRedeem(200e18, alice);
        _honestReport(); // bob processed, alice claimable 200e6
        _deposit(bob, 300e6); // new pending deposit
        vm.prank(owner);
        vault.setShutdown(true);
        uint256 reserved = 300e6 + 200e6;
        uint256 idle = usdc.balanceOf(address(vault)) - reserved;
        vm.startPrank(owner);
        vm.expectRevert(bytes("Reserved USDC"));
        vault.emergencyWithdraw(owner, idle + 1);
        vault.emergencyWithdraw(owner, idle);
        vm.stopPrank();
        vm.prank(alice);
        vault.claimRedeem(0, alice);
        assertEq(usdc.balanceOf(alice), 200e6);
    }

    function test_shutdownStillProcessesRedeemsAndAllowsDepositCancel() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _honestReport();
        _claim(alice, r1);
        uint256 pend = _deposit(bob, 100e6);
        vm.prank(owner);
        vault.setShutdown(true);
        vm.prank(bob);
        vault.cancelRequest(pend, true);
        assertEq(usdc.balanceOf(bob), 100e6);
        vm.prank(alice);
        uint256 rr = vault.requestRedeem(1000e18, alice);
        _honestReport();
        vm.prank(alice);
        vault.claimRedeem(rr, alice);
        assertEq(usdc.balanceOf(alice), 1000e6);
    }

    function test_perfFeeConstructorBounded() public {
        vm.expectRevert(bytes("Perf fee too high"));
        new PMFIArbVaultV3("x", "x", address(usdc), reporter, feeRecipient, servicer, 1, 2001);
    }

    function testFuzz_depositSharesMatchPps(uint256 amount, uint256 pnlBps) public {
        amount = bound(amount, 10e6, 1_000_000e6);
        pnlBps = bound(pnlBps, 9000, 11000);
        uint256 r1 = _deposit(alice, 100_000e6);
        _honestReport();
        _claim(alice, r1);
        _tendAll();
        uint256 total = usdc.balanceOf(address(vault)) + offchain;
        uint256 target = (total * pnlBps) / 10000;
        if (target > total) _pnl(int256(target - total));
        else _pnl(-int256(total - target));
        _honestReport();
        uint256 pps = vault.officialPPS();
        uint256 r2 = _deposit(bob, amount);
        _honestReport();
        assertEq(vault.officialPPS(), pps, "deposits do not move PPS");
        _claim(bob, r2);
        assertEq(vault.balanceOf(bob), (amount * 1e18) / pps);
        // value conservation: bob's shares are worth at most what he paid
        assertLe(vault.previewRedeem(vault.balanceOf(bob)), amount);
    }

    function test_missedReports_depositorCanExitAfter30Days() public {
        uint256 rid = _deposit(alice, 500e6);
        vm.prank(alice);
        vm.expectRevert(bytes("Not yet cancelable"));
        vault.cancelRequest(rid, true);
        vm.warp(vm.getBlockTimestamp() + 30 days + 1);
        vm.prank(alice);
        vault.cancelRequest(rid, true);
        assertEq(usdc.balanceOf(alice), 500e6);
        assertEq(vault.totalPendingDepositAssets(), 0);
    }
}
