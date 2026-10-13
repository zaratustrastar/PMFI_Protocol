// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {RobinhoodFixture} from "../utils/RobinhoodFixture.sol";
import {MILORDToken} from "../../src/robinhood/MILORDToken.sol";
import {MilordsNFT} from "../../src/robinhood/MilordsNFT.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";

contract ConverterHandler is Test {
    MILORDToken token;
    MilordsNFT nft;
    MilordConverter conv;
    uint256 pk;
    address treasury;
    address owner;
    address[] public actors;
    uint256 public nonce;
    uint256 public burnedByHandler;

    constructor(MILORDToken t, MilordsNFT n, MilordConverter c, uint256 pk_, address treasury_, address owner_) {
        token = t;
        nft = n;
        conv = c;
        pk = pk_;
        treasury = treasury_;
        owner = owner_;
        for (uint256 i; i < 5; ++i) {
            actors.push(address(uint160(0xA000 + i)));
        }
    }

    function convert(uint256 actorSeed, uint256 price) external {
        address a = actors[actorSeed % actors.length];
        price = bound(price, 1, 10 ether);
        vm.prank(treasury);
        token.transfer(a, 1_000_000 ether);
        vm.deal(a, 20 ether);
        vm.prank(a);
        token.approve(address(conv), type(uint256).max);
        MilordConverter.EntryQuote memory q = MilordConverter.EntryQuote(
            a, price, uint64(block.timestamp), uint64(block.timestamp + 60), keccak256(abi.encode(++nonce))
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, conv.quoteDigest(q));
        uint256 fee = conv.entryFeeFor(price);
        vm.prank(a);
        conv.convert{value: fee}(q, abi.encodePacked(r, s, v), fee);
    }

    function redeem(uint256 actorSeed, uint256 idSeed) external {
        address a = actors[actorSeed % actors.length];
        uint256 bal = nft.balanceOf(a);
        if (bal == 0) return;
        uint256 minted = nft.totalMinted();
        for (uint256 i; i < minted; ++i) {
            uint256 id = ((idSeed + i) % minted) + 1;
            if (nft.ownerOf(id) == a) {
                vm.prank(a);
                conv.redeem(id);
                return;
            }
        }
    }

    function transferNft(uint256 fromSeed, uint256 toSeed, uint256 idSeed) external {
        address from = actors[fromSeed % actors.length];
        address to = actors[toSeed % actors.length];
        uint256 minted = nft.totalMinted();
        if (minted == 0) return;
        uint256 id = (idSeed % minted) + 1;
        if (nft.ownerOf(id) != from) return;
        vm.prank(from);
        nft.transferFrom(from, to, id);
    }

    function changeExitBurn(uint16 bps) external {
        (uint16 e, uint16 x, uint256 m) = conv.feeParams();
        bps = uint16(bound(bps, 0, x));
        vm.prank(owner);
        conv.reduceFeeParams(MilordConverter.FeeParams(e, bps, m));
    }

    function warp(uint256 s) external {
        vm.warp(block.timestamp + bound(s, 1, 1 days));
    }
}

contract ConverterInvariantTest is RobinhoodFixture {
    ConverterHandler h;
    uint256 initialSupply;

    function setUp() public override {
        super.setUp();
        initialSupply = token.totalSupply();
        h = new ConverterHandler(token, nft, conv, signerPk, treasury, owner);
        targetContract(address(h));
    }

    /// Escrow always covers every circulating Milord.
    function invariant_escrowCoversBackedNfts() public view {
        assertGe(token.balanceOf(address(conv)), conv.backedCount() * M);
    }

    function invariant_backedEqualsCirculating() public view {
        assertEq(conv.backedCount(), nft.activeSupply());
        assertEq(nft.totalMinted(), nft.activeSupply() + conv.inventorySize());
    }

    function invariant_votesEqualCirculating() public view {
        assertEq(nft.getVotes(address(conv)), 0);
        uint256 sum;
        for (uint256 i; i < 5; ++i) {
            sum += nft.getVotes(h.actors(i));
        }
        assertEq(sum, nft.activeSupply());
    }

    function invariant_supplyOnlyFallsByExitBurns() public view {
        assertEq(initialSupply - token.totalSupply(), conv.totalMilordBurnedOnExit());
    }

    function invariant_idsWithinRange() public view {
        assertLe(nft.totalMinted(), 6767);
        assertEq(conv.nextMintId(), nft.totalMinted() + 1);
    }
}
