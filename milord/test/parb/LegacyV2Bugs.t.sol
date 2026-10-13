// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {MockUSDC} from "../../src/mocks/MockUSDC.sol";
import {PMFIArbVaultV2} from "../../src/legacy/PMFIArbVaultV2.sol";

/// Reproduces accounting issues in the committed PMFIArbVaultV2 source (compiled locally,
/// logic unchanged). These motivate the V3 fork; the deployed V2 is not touched.
contract LegacyV2BugsTest is Test {
    uint256 pk = 0xB0B;
    MockUSDC usdc;
    PMFIArbVaultV2 v2;
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");
    address servicer = makeAddr("servicer");
    uint256 nonce;

    function setUp() public {
        vm.warp(1_700_000_000);
        usdc = new MockUSDC();
        v2 = new PMFIArbVaultV2(address(usdc), vm.addr(pk), makeAddr("fee"), servicer, 10_000_000e6);
        v2.setReportCooldown(0);
    }

    function _report(uint256 assets) internal {
        uint256 t = vm.getBlockTimestamp();
        PMFIArbVaultV2.ReportData memory d = PMFIArbVaultV2.ReportData(assets, t, t + 600, ++nonce);
        bytes32 h = keccak256(
            abi.encode(
                v2.REPORT_TYPEHASH(),
                d.reportedAssets,
                d.timestamp,
                d.deadline,
                d.nonce,
                address(v2),
                block.chainid,
                v2.DOMAIN_SALT()
            )
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, MessageHashUtils.toEthSignedMessageHash(h));
        v2.report(d, abi.encodePacked(r, s, v), 0, 0);
    }

    function _deposit(address who, uint256 amt) internal returns (uint256 rid) {
        usdc.mint(who, amt);
        vm.startPrank(who);
        usdc.approve(address(v2), amt);
        rid = v2.requestDeposit(amt, who);
        vm.stopPrank();
    }

    function test_V2_unclaimedDepositInflatesPps() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _report(1000e6);
        vm.prank(alice);
        v2.claimDeposit(r1, alice);
        _deposit(bob, 1000e6);
        _report(2000e6); // bob processed, not claimed
        _report(2000e6); // nothing happened in the market
        assertEq(v2.officialPPS(), 2e6, "PPS doubled with zero profit");
        assertEq(v2.previewRedeem(1000e18), 2000e6, "alice's shares now claim bob's money");
    }

    function test_V2_pendingRedeemInflatesPps() public {
        v2.setMinimumIdleAmount(0);
        uint256 r1 = _deposit(alice, 1000e6);
        _report(1000e6);
        vm.prank(alice);
        v2.claimDeposit(r1, alice);
        vm.warp(vm.getBlockTimestamp() + 61);
        v2.tend(); // 900e6 to servicer, 100e6 idle
        vm.prank(alice);
        v2.requestRedeem(500e18, alice);
        _report(1000e6);
        assertEq(v2.officialPPS(), 2e6, "PPS doubled with zero profit");
    }

    function test_V2_zeroNavResetsToOneDollar() public {
        uint256 r1 = _deposit(alice, 1000e6);
        _report(1000e6);
        vm.prank(alice);
        v2.claimDeposit(r1, alice);
        _report(0); // V2 accepts a report below its own on-chain USDC
        assertEq(v2.officialPPS(), 1e6, "total loss reported as $1.00");
    }

    function test_V2_fixedTwentyPercentFee() public view {
        assertEq(v2.PERF_FEE_BPS(), 2000);
    }
}
