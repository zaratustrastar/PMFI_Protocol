// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {MilordDeployer} from "../../script/MilordDeployer.sol";
import {MilordConverter} from "../../src/robinhood/MilordConverter.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {MockUSDG} from "../../src/mocks/MockUSDG.sol";
import {MockSwapAdapter} from "../../src/mocks/MockSwapAdapter.sol";

/// Full MILORD deployment on one chain configured as Robinhood Chain testnet (46630),
/// with mock USDG as the vault asset, through the same deployer the scripts use.
abstract contract SingleChainFixture is Test, MilordDeployer {
    uint256 internal constant M = 1_000_000 ether;
    uint256 internal constant ROBINHOOD_TESTNET = 46630;
    uint256 internal quotePk = 0xA11CE;
    uint256 internal reportPk = 0xB0B;

    address internal safe = makeAddr("teamSafe(owner)");
    address internal teamSafe = makeAddr("teamSafe");
    address internal arbSafe = makeAddr("arbitrageSafe");
    address internal servicer = makeAddr("engineServicer");
    address internal operator = makeAddr("operator");
    address internal treasury = makeAddr("tokenHolder");
    address internal dex = makeAddr("ethUsdgDex");

    MockUSDG internal usdg;
    MockSwapAdapter internal swap;
    Deployment internal d;
    Config internal cfg;
    uint256 internal quoteNonce;
    uint256 internal reportNonce;

    function setUp() public virtual {
        vm.chainId(ROBINHOOD_TESTNET);
        vm.warp(1_798_502_400); // a Monday 00:00 UTC
        usdg = new MockUSDG();
        swap = new MockSwapAdapter(20_000_000 ether);

        cfg.deployer = address(this);
        cfg.owner = safe;
        cfg.teamSafe = teamSafe;
        cfg.arbitrageSafe = arbSafe;
        cfg.servicer = servicer;
        cfg.quoteSigner = vm.addr(quotePk);
        cfg.reportSigner = vm.addr(reportPk);
        cfg.operator = operator;
        cfg.tokenHolder = treasury;
        cfg.asset = address(usdg);
        cfg.swapAdapter = address(swap);
        cfg.baseURI = "ipfs://test/";
        cfg.characteristics = new string[](8);
        for (uint256 i; i < 8; ++i) cfg.characteristics[i] = string.concat("Characteristic ", vm.toString(i + 1));
        cfg.vaultCap = 10_000_000e6;
        cfg.rootActivationDelay = 1 days;
        cfg.splitterMin = [uint16(6000), 4000, 2500];
        cfg.splitterMax = [uint16(8000), 6000, 4000];
        d = _deployMilord(cfg);

        // Safe accepts every Ownable2Step handover
        vm.startPrank(safe);
        d.nft.acceptOwnership();
        d.converter.acceptOwnership();
        d.entrySplit.acceptOwnership();
        d.royaltySplit.acceptOwnership();
        d.taxSplit.acceptOwnership();
        d.burner.acceptOwnership();
        d.registry.acceptOwnership();
        d.distributor.acceptOwnership();
        d.adapter.acceptOwnership();
        d.vault.setReportCooldown(0);
        vm.stopPrank();

        vm.prank(treasury);
        d.token.transfer(address(swap), 500_000_000 ether);
    }

    function _now() internal view returns (uint256) {
        return vm.getBlockTimestamp();
    }

    function _convert(address who, uint256 weiPerMillion) internal returns (uint256 id) {
        vm.prank(treasury);
        d.token.transfer(who, M);
        vm.deal(who, who.balance + 1 ether);
        MilordConverter.EntryQuote memory q = MilordConverter.EntryQuote(
            who, weiPerMillion, uint64(_now()), uint64(_now() + 300), keccak256(abi.encode(++quoteNonce))
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(quotePk, d.converter.quoteDigest(q));
        uint256 fee = d.converter.entryFeeFor(weiPerMillion);
        vm.startPrank(who);
        d.token.approve(address(d.converter), M);
        id = d.converter.convert{value: fee}(q, abi.encodePacked(r, s, v), fee);
        vm.stopPrank();
    }

    function _signReport(PMFIArbVaultV3.ReportData memory data, uint256 chainId) internal view returns (bytes memory) {
        bytes32 h = keccak256(
            abi.encode(
                d.vault.REPORT_TYPEHASH(),
                data.reportedAssets,
                data.timestamp,
                data.deadline,
                data.nonce,
                address(d.vault),
                chainId,
                d.vault.DOMAIN_SALT()
            )
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(reportPk, MessageHashUtils.toEthSignedMessageHash(h));
        return abi.encodePacked(r, s, v);
    }

    /// Honest report: vault USDG + engine-held USDG (venue balances modelled as servicer balance).
    function _report() internal {
        PMFIArbVaultV3.ReportData memory data = PMFIArbVaultV3.ReportData(
            usdg.balanceOf(address(d.vault)) + usdg.balanceOf(servicer), _now(), _now() + 600, ++reportNonce
        );
        d.vault.report(data, _signReport(data, block.chainid), 0, 0);
    }
}
