// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {RevenueSplitter} from "../../src/robinhood/RevenueSplitter.sol";
import {BuybackBurner} from "../../src/robinhood/BuybackBurner.sol";
import {MockUSDC} from "../../src/mocks/MockUSDC.sol";

contract Rejecter {
    receive() external payable {
        revert("no");
    }
}

contract SplitterTest is RobinhoodFixture {
    function test_tradingTaxThirds() public {
        vm.deal(address(taxSplit), 3 ether);
        taxSplit.distribute(address(0));
        assertEq(address(burner).balance, 1.0002 ether);
        assertEq(teamSafe.balance, 0.9999 ether);
        assertEq(arbSafe.balance, 0.9999 ether);
        assertEq(burner.totalEthReceived(), 1.0002 ether);
    }

    function testFuzz_distributeConservesValue(uint96 amount) public {
        vm.deal(address(taxSplit), amount);
        taxSplit.distribute(address(0));
        assertEq(address(burner).balance + teamSafe.balance + arbSafe.balance, amount);
        assertEq(address(taxSplit).balance, 0);
    }

    function test_erc20Split() public {
        MockUSDC u = new MockUSDC();
        u.mint(address(entrySplit), 1000e6);
        entrySplit.distribute(address(u));
        assertEq(u.balanceOf(arbSafe), 700e6);
        assertEq(u.balanceOf(teamSafe), 300e6);
    }

    function test_failedPushCreditedForPull() public {
        Rejecter rj = new Rejecter();
        RevenueSplitter s = new RevenueSplitter(owner, _two(address(rj), 5000, "r", teamSafe, 5000, "t"));
        vm.deal(address(s), 1 ether);
        s.distribute(address(0));
        assertEq(teamSafe.balance, 0.5 ether);
        assertEq(s.owed(address(0), address(rj)), 0.5 ether);
        assertEq(s.undistributed(address(0)), 0, "owed is not re-split");
        s.distribute(address(0));
        assertEq(teamSafe.balance, 0.5 ether);
    }

    function test_sharesOnlyWithinBounds() public {
        RevenueSplitter.Payee[] memory p = new RevenueSplitter.Payee[](2);
        p[0] = RevenueSplitter.Payee(arbSafe, 7000, 6000, 8000, "arb");
        p[1] = RevenueSplitter.Payee(teamSafe, 3000, 2000, 4000, "team");
        RevenueSplitter s = new RevenueSplitter(owner, p);
        uint16[] memory b = new uint16[](2);
        b[0] = 8500;
        b[1] = 1500;
        vm.prank(owner);
        vm.expectRevert(RevenueSplitter.BadConfig.selector);
        s.setShares(b);
        b[0] = 6500;
        b[1] = 3500;
        vm.deal(address(s), 1 ether);
        vm.prank(owner);
        s.setShares(b); // pending ETH split at the OLD shares first
        assertEq(arbSafe.balance, 0.7 ether);
        assertEq(s.payee(0).bps, 6500);
    }

    function test_payeeChangeDelayed() public {
        address n = makeAddr("newTeam");
        vm.startPrank(owner);
        entrySplit.queuePayeeChange(1, n);
        vm.expectRevert(RevenueSplitter.TooEarly.selector);
        entrySplit.executePayeeChange(1);
        vm.warp(block.timestamp + 2 days);
        entrySplit.executePayeeChange(1);
        vm.stopPrank();
        assertEq(entrySplit.payee(1).account, n);
    }

    // ───────── buyback ─────────

    function test_buybackBurnsAndRecords() public {
        vm.deal(address(burner), 1 ether);
        uint256 s0 = token.totalSupply();
        vm.prank(operator);
        uint256 burned = burner.buybackAndBurn(0.1 ether, 99_000_000 ether, block.timestamp);
        assertEq(burned, 100_000_000 ether);
        assertEq(s0 - token.totalSupply(), burned);
        assertEq(burner.totalEthSpent(), 0.1 ether);
        assertEq(burner.totalBurnedFromBuybacks(), burned);
        assertEq(burner.buybackCount(), 1);
        assertEq(burner.budget(), 0.9 ether);
    }

    function test_buybackGuards() public {
        vm.deal(address(burner), 10 ether);
        vm.expectRevert(BuybackBurner.NotOperator.selector);
        burner.buybackAndBurn(0.1 ether, 1, block.timestamp);
        vm.startPrank(operator);
        vm.expectRevert(BuybackBurner.OutOfBounds.selector);
        burner.buybackAndBurn(6 ether, 1, block.timestamp);
        vm.expectRevert(BuybackBurner.OutOfBounds.selector);
        burner.buybackAndBurn(0.001 ether, 1, block.timestamp);
        vm.expectRevert(bytes("slippage"));
        burner.buybackAndBurn(0.1 ether, 200_000_000 ether, block.timestamp);
        vm.stopPrank();
    }

    function test_adapterChangeDelayed() public {
        vm.startPrank(owner);
        burner.queueAdapter(address(0x1234));
        vm.expectRevert(BuybackBurner.TooEarly.selector);
        burner.executeAdapter();
        vm.warp(block.timestamp + 2 days);
        burner.executeAdapter();
        vm.stopPrank();
        assertEq(address(burner.adapter()), address(0x1234));
    }

    function test_burnHeldTokens() public {
        vm.prank(treasury);
        token.transfer(address(burner), 5 ether);
        burner.burnHeldTokens();
        assertEq(burner.totalBurnedDirect(), 5 ether);
    }
}
