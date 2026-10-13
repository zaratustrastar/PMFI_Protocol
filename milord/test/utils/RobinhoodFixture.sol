// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {MILORDToken} from "../../src/robinhood/MILORDToken.sol";
import {MilordsNFT} from "../../src/robinhood/MilordsNFT.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";
import {RevenueSplitter} from "../../src/robinhood/RevenueSplitter.sol";
import {BuybackBurner, ISwapAdapter} from "../../src/robinhood/BuybackBurner.sol";
import {StrategyRegistry} from "../../src/robinhood/StrategyRegistry.sol";
import {IMILORD} from "../../src/interfaces/IMILORD.sol";
import {MockSwapAdapter} from "../../src/mocks/MockSwapAdapter.sol";

/// Shared deployment of the Robinhood Chain side with mocks.
abstract contract RobinhoodFixture is Test {
    uint256 internal constant M = 1_000_000 ether;
    uint256 internal signerPk = 0xA11CE;
    address internal quoteSigner;

    address internal owner = makeAddr("teamSafeOwner");
    address internal teamSafe = makeAddr("teamSafe");
    address internal arbSafe = makeAddr("arbitrageFundingSafe");
    address internal treasury = makeAddr("treasury");
    address internal operator = makeAddr("operator");

    MILORDToken internal token;
    MilordsNFT internal nft;
    MilordConverter internal conv;
    RevenueSplitter internal entrySplit;
    RevenueSplitter internal royaltySplit;
    RevenueSplitter internal taxSplit;
    BuybackBurner internal burner;
    MockSwapAdapter internal swap;
    StrategyRegistry internal registry;

    uint256 internal nonceCounter;

    function setUp() public virtual {
        quoteSigner = vm.addr(signerPk);
        token = new MILORDToken(treasury);

        royaltySplit = new RevenueSplitter(owner, _two(teamSafe, 5000, "team", arbSafe, 5000, "arbitrage"));
        entrySplit = new RevenueSplitter(owner, _two(arbSafe, 7000, "arbitrage", teamSafe, 3000, "team"));

        nft = new MilordsNFT(owner, address(royaltySplit), "ipfs://placeholder/");
        conv = new MilordConverter(owner, IMILORD(address(token)), nft, address(entrySplit), quoteSigner);
        vm.prank(owner);
        nft.setConverter(address(conv));

        swap = new MockSwapAdapter(1_000_000_000 ether); // 1 ETH -> 1B MILORD (test price only)
        burner = new BuybackBurner(owner, IMILORD(address(token)), ISwapAdapter(address(swap)));
        vm.prank(owner);
        burner.setOperator(operator, true);
        vm.prank(treasury);
        token.transfer(address(swap), 1_000_000_000 ether);

        RevenueSplitter.Payee[] memory t = new RevenueSplitter.Payee[](3);
        t[0] = RevenueSplitter.Payee(address(burner), 3334, 0, 10000, "buyback-burn");
        t[1] = RevenueSplitter.Payee(teamSafe, 3333, 0, 10000, "team");
        t[2] = RevenueSplitter.Payee(arbSafe, 3333, 0, 10000, "arbitrage");
        taxSplit = new RevenueSplitter(owner, t);

        registry = new StrategyRegistry(owner, nft);
        vm.startPrank(owner);
        // labels are placeholders until the 8 characteristic names are supplied
        for (uint256 i = 1; i <= 8; ++i) {
            registry.addCharacteristic(string.concat("Characteristic ", vm.toString(i)));
        }
        uint256 arb = registry.addStrategy("pArbitrage", 8453, address(0xBEEF), address(0xBEEF), true);
        registry.setDefaultStrategy(arb);
        vm.stopPrank();
    }

    function _two(address a, uint16 ab, string memory al, address b, uint16 bb, string memory bl)
        internal
        pure
        returns (RevenueSplitter.Payee[] memory p)
    {
        p = new RevenueSplitter.Payee[](2);
        p[0] = RevenueSplitter.Payee(a, ab, 0, 10000, al);
        p[1] = RevenueSplitter.Payee(b, bb, 0, 10000, bl);
    }

    // ───────── quote helpers ─────────

    function _quote(address account, uint256 weiPerMillion)
        internal
        returns (MilordConverter.EntryQuote memory q, bytes memory sig)
    {
        q = MilordConverter.EntryQuote({
            account: account,
            weiPerMillion: weiPerMillion,
            issuedAt: uint64(block.timestamp),
            expiresAt: uint64(block.timestamp + 5 minutes),
            nonce: keccak256(abi.encode("q", ++nonceCounter))
        });
        sig = _sign(q, signerPk);
    }

    function _sign(MilordConverter.EntryQuote memory q, uint256 pk) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, conv.quoteDigest(q));
        return abi.encodePacked(r, s, v);
    }

    function _fund(address who, uint256 milord, uint256 eth) internal {
        vm.prank(treasury);
        token.transfer(who, milord);
        vm.deal(who, who.balance + eth);
        vm.prank(who);
        token.approve(address(conv), type(uint256).max);
    }

    /// Convert for `who` at `price` wei per 1M MILORD. Funds them first.
    function _convert(address who, uint256 price) internal returns (uint256 id) {
        _fund(who, M, 1 ether);
        (MilordConverter.EntryQuote memory q, bytes memory sig) = _quote(who, price);
        uint256 fee = conv.entryFeeFor(price);
        vm.prank(who);
        id = conv.convert{value: fee}(q, sig, fee);
    }
}
