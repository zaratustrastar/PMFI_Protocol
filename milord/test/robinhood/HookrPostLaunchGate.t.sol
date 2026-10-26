// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {HookrPostLaunch} from "../../script/HookrPostLaunch.s.sol";

/// Refusals of the post-launch deployment script (buyback route + LP fee collector).
contract HookrPostLaunchGateTest is Test {
    function test_refusals() public {
        vm.setEnv("MILORD_TOKEN", vm.toString(makeAddr("milord")));
        vm.setEnv("MILORD_POOL_HOOK", vm.toString(makeAddr("root")));
        vm.setEnv("MILORD_POOL_FEE", "8388608");
        vm.setEnv("MILORD_POOL_TICK_SPACING", "60");
        vm.setEnv("V4_POOL_MANAGER", vm.toString(makeAddr("pm")));
        vm.setEnv("MILORD_FAMILY_ID", vm.toString(bytes32(uint256(7))));
        vm.setEnv("MILORD_BUYBACK_BURNER", vm.toString(makeAddr("burner")));
        vm.setEnv("HOOKR_FAMILY_LOCK", vm.toString(makeAddr("lock")));
        HookrPostLaunch s = new HookrPostLaunch();

        vm.chainId(8453);
        vm.expectRevert(bytes("Robinhood Chain, its testnet or local only"));
        s.run();

        vm.chainId(4663);
        vm.setEnv("MILORD_POOL_ID", vm.toString(bytes32(0)));
        vm.expectRevert(bytes("4663: MILORD_POOL_ID (from offchain/record-launch.mjs) is required"));
        s.run();

        vm.setEnv("MILORD_POOL_ID", vm.toString(bytes32(uint256(1))));
        vm.expectRevert(bytes("pool key does not hash to MILORD_POOL_ID"));
        s.run();
        vm.setEnv("MILORD_POOL_ID", vm.toString(bytes32(0)));
    }
}
