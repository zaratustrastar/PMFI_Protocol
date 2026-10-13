// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {StrategyRegistry} from "../../src/robinhood/StrategyRegistry.sol";

contract RegistryTest is RobinhoodFixture {
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    function test_eightInitialCharacteristics_onlyArbitrageEnabled() public view {
        assertEq(registry.characteristicCount(), 8);
        assertEq(registry.strategyCount(), 1);
        assertEq(registry.defaultStrategyId(), 1);
        // every one of the 28 pairs resolves to Arbitrage
        for (uint256 a = 1; a <= 8; ++a) {
            for (uint256 b = a + 1; b <= 8; ++b) {
                assertEq(registry.resolve(a, b), 1);
            }
        }
    }

    function test_selectTwo_freeAndRecorded() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(alice);
        uint256 sid = registry.select(id, 5, 2);
        assertEq(sid, 1);
        (bool personal, uint256 a, uint256 b, uint256 s) = registry.currentPreference(id);
        assertTrue(personal);
        assertEq(a, 2);
        assertEq(b, 5);
        assertEq(s, 1);
        assertEq(registry.origin(id).holder, alice);
    }

    function test_selectGuards() public {
        uint256 id = _convert(alice, 1 ether);
        vm.startPrank(alice);
        vm.expectRevert(StrategyRegistry.Invalid.selector);
        registry.select(id, 3, 3);
        vm.expectRevert(StrategyRegistry.Invalid.selector);
        registry.select(id, 3, 9);
        vm.stopPrank();
        vm.prank(bob);
        vm.expectRevert(StrategyRegistry.NotHolder.selector);
        registry.select(id, 1, 2);
    }

    function test_newHolderDoesNotInherit_historyKept() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(alice);
        registry.select(id, 1, 2);
        vm.prank(alice);
        nft.transferFrom(alice, bob, id);
        (bool personal,,, uint256 s) = registry.currentPreference(id);
        assertFalse(personal);
        assertEq(s, 1, "default strategy");
        vm.prank(bob);
        registry.select(id, 3, 4);
        assertEq(registry.history(id).length, 2);
        assertEq(registry.origin(id).charA, 1, "origin preserved");
    }

    function test_reissuedNftDoesNotInherit() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(alice);
        registry.select(id, 1, 2);
        vm.prank(alice);
        conv.redeem(id);
        (bool p1,,,) = registry.currentPreference(id);
        assertFalse(p1, "inactive");
        vm.prank(alice);
        vm.expectRevert(StrategyRegistry.NotHolder.selector);
        registry.select(id, 1, 2);

        // same holder converts again and gets the same ID back: new activation, no preference
        uint256 id2 = _convert(alice, 1 ether);
        assertEq(id2, id);
        (bool p2,,,) = registry.currentPreference(id);
        assertFalse(p2);
    }

    function test_addStrategyAndCharacteristicLater_withoutReplacingCollection() public {
        uint256 id = _convert(alice, 1 ether);
        vm.startPrank(owner);
        uint256 c9 = registry.addCharacteristic("Characteristic 9");
        uint256 s2 = registry.addStrategy("Future strategy", 8453, address(0xCAFE), address(0xCAFE), false);
        registry.setPairStrategy(1, c9, s2);
        vm.stopPrank();
        assertEq(registry.resolve(1, c9), 1, "disabled -> default");

        vm.prank(owner);
        registry.setStrategyEnabled(s2, true);
        assertEq(registry.resolve(c9, 1), s2);
        vm.prank(alice);
        assertEq(registry.select(id, c9, 1), s2);

        // disabling it again falls back without the holder doing anything
        vm.prank(owner);
        registry.setStrategyEnabled(s2, false);
        (,,, uint256 s) = registry.currentPreference(id);
        assertEq(s, 1);
    }

    function test_cannotDisableDefault() public {
        vm.prank(owner);
        vm.expectRevert(StrategyRegistry.Invalid.selector);
        registry.setStrategyEnabled(1, false);
    }
}
