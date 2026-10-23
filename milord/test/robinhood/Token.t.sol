// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";

contract TokenTest is RobinhoodFixture {
    function test_supplyAndDecimals() public view {
        assertEq(token.totalSupply(), 6_767_000_000 ether);
        assertEq(token.decimals(), 18);
        assertEq(token.balanceOf(treasury) + token.balanceOf(address(swap)), 6_767_000_000 ether);
    }

    function test_noMintFunction() public {
        (bool ok,) = address(token).call(abi.encodeWithSignature("mint(address,uint256)", address(this), 1));
        assertFalse(ok);
    }

    function testFuzz_transferHasNoTax(uint256 amount) public {
        amount = bound(amount, 0, token.balanceOf(treasury));
        address to = makeAddr("to");
        vm.prank(treasury);
        token.transfer(to, amount);
        assertEq(token.balanceOf(to), amount);
    }

    function test_burnReducesSupply() public {
        vm.prank(treasury);
        token.burn(5 ether);
        assertEq(token.totalSupply(), 6_767_000_000 ether - 5 ether);
    }
}
