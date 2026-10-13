// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {MilordsNFT} from "../../src/robinhood/MilordsNFT.sol";
import {MilordGovernor} from "../../src/robinhood/MilordGovernor.sol";
import {IERC2981} from "@openzeppelin/contracts/interfaces/IERC2981.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

contract NFTTest is RobinhoodFixture {
    address alice = makeAddr("alice");
    address bob = makeAddr("bob");

    function test_onlyConverterIssuesAndRetires() public {
        vm.expectRevert(MilordsNFT.NotConverter.selector);
        nft.issue(alice, 1);
        uint256 id = _convert(alice, 1 ether);
        vm.expectRevert(MilordsNFT.NotConverter.selector);
        nft.retire(id, alice);
    }

    function test_converterIsSetOnce() public {
        vm.prank(owner);
        vm.expectRevert(MilordsNFT.ConverterAlreadySet.selector);
        nft.setConverter(address(1));
    }

    function test_oneNftOneVote_inventoryExcluded() public {
        uint256 a = _convert(alice, 1 ether);
        _convert(bob, 1 ether);
        assertEq(nft.getVotes(alice), 1, "auto self-delegated");
        assertEq(nft.getVotes(bob), 1);
        uint256 t0 = vm.getBlockTimestamp();
        vm.warp(t0 + 1);
        assertEq(nft.getPastTotalSupply(t0), 2);

        vm.prank(alice);
        conv.redeem(a);
        assertEq(nft.getVotes(alice), 0);
        assertEq(nft.getVotes(address(conv)), 0, "inventory has no votes");
        vm.warp(t0 + 2);
        assertEq(nft.getPastTotalSupply(t0 + 1), 1);
        assertEq(nft.activeSupply(), 1);
    }

    function test_votesFollowTransfer() public {
        uint256 id = _convert(alice, 1 ether);
        vm.prank(alice);
        nft.transferFrom(alice, bob, id);
        assertEq(nft.getVotes(alice), 0);
        assertEq(nft.getVotes(bob), 1);
    }

    function test_explicitDelegationRespected() public {
        _convert(alice, 1 ether);
        vm.prank(alice);
        nft.delegate(bob);
        assertEq(nft.getVotes(bob), 1);
        _convert(alice, 1 ether); // second NFT follows alice's existing delegate
        assertEq(nft.getVotes(bob), 2);
        assertEq(nft.getVotes(alice), 0);
    }

    function test_timestampClock() public view {
        assertEq(nft.clock(), block.timestamp);
        assertEq(nft.CLOCK_MODE(), "mode=timestamp");
    }

    function test_royalty_6_7_percent_toSplitter_capped() public {
        (address r, uint256 amt) = nft.royaltyInfo(1, 1 ether);
        assertEq(r, address(royaltySplit));
        assertEq(amt, 0.067 ether);
        vm.prank(owner);
        vm.expectRevert(MilordsNFT.RoyaltyTooHigh.selector);
        nft.setRoyalty(address(royaltySplit), 1001);
        assertTrue(nft.supportsInterface(type(IERC2981).interfaceId));
        assertTrue(nft.supportsInterface(type(IERC721).interfaceId));
    }

    function test_royaltyPaymentWithStipendSplits50_50() public {
        vm.deal(address(this), 1 ether);
        payable(address(royaltySplit)).transfer(0.067 ether); // 2,300 gas stipend
        royaltySplit.distribute(address(0));
        assertEq(teamSafe.balance, 0.0335 ether);
        assertEq(arbSafe.balance, 0.0335 ether);
    }

    function test_metadataFreeze() public {
        vm.startPrank(owner);
        nft.setBaseURI("ipfs://final/");
        nft.freezeMetadata();
        vm.expectRevert(MilordsNFT.Frozen.selector);
        nft.setBaseURI("ipfs://other/");
        vm.stopPrank();
        uint256 id = _convert(alice, 1 ether);
        assertEq(nft.tokenURI(id), "ipfs://final/1");
    }

    function test_governor_proposeAndVote() public {
        MilordGovernor gov = new MilordGovernor(nft);
        _convert(alice, 1 ether);
        _convert(bob, 1 ether);
        vm.warp(block.timestamp + 1);
        address[] memory t = new address[](1);
        uint256[] memory v = new uint256[](1);
        bytes[] memory c = new bytes[](1);
        t[0] = address(0xdead);
        c[0] = "";
        vm.prank(alice);
        uint256 pid = gov.propose(t, v, c, "signal: enable strategy 2");
        vm.warp(block.timestamp + 1 days + 1);
        vm.prank(alice);
        gov.castVote(pid, 1);
        vm.prank(bob);
        gov.castVote(pid, 0);
        (uint256 against, uint256 forVotes,) = gov.proposalVotes(pid);
        assertEq(forVotes, 1);
        assertEq(against, 1);
    }
}
