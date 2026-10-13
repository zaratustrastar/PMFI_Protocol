// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {SingleChainFixture} from "../utils/SingleChainFixture.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {StrategyRegistry} from "../../src/robinhood/StrategyRegistry.sol";
import {RevenueSplitter} from "../../src/robinhood/RevenueSplitter.sol";
import {Mock18DecimalStable} from "../../src/mocks/Mock18DecimalStable.sol";
import {DeployRobinhood} from "../../script/DeployRobinhood.s.sol";

/// Deployment of the whole protocol, including pARB and its distributor, on Robinhood Chain.
contract RobinhoodDeploymentTest is SingleChainFixture {
    function test_everythingOnOneChain_wiredToUsdg() public view {
        assertEq(block.chainid, ROBINHOOD_TESTNET);
        assertEq(address(d.vault.usdc()), address(usdg), "vault asset is USDG");
        assertEq(d.vault.perfFeeBps(), 0);
        assertEq(d.vault.officialPPS(), 1e6);
        assertTrue(d.vault.depositorRestricted());
        assertTrue(d.vault.isAllowedDepositor(address(d.adapter)));
        assertEq(address(d.adapter.usdc()), address(usdg));
        assertEq(address(d.adapter.vault()), address(d.vault));
        assertEq(d.adapter.distributor(), address(d.distributor));
        assertEq(address(d.distributor.parb()), address(d.vault));
        assertEq(d.distributor.publisher(), operator);
        assertEq(d.vault.arbServicerWallet(), servicer);
        assertEq(d.vault.reportSigner(), cfg.reportSigner);
        assertEq(d.nft.converter(), address(d.converter));
        assertEq(d.token.totalSupply(), 6_767_000_000 ether);
    }

    function test_strategyRegistryPointsAtLocalVault() public view {
        StrategyRegistry.Strategy memory s = d.registry.strategy(d.registry.defaultStrategyId());
        assertEq(s.chainId, ROBINHOOD_TESTNET, "rewards on the same chain, no bridge");
        assertEq(s.vault, address(d.vault));
        assertEq(s.rewardAsset, address(d.vault));
        assertEq(d.registry.characteristicCount(), 8);
    }

    function test_ownershipHandedToSafe() public view {
        assertEq(d.vault.owner(), safe);
        assertEq(d.converter.owner(), safe);
        assertEq(d.distributor.owner(), safe);
        assertEq(d.adapter.owner(), safe);
        assertEq(d.nft.owner(), safe);
        assertEq(d.entrySplit.owner(), safe);
    }

    function test_productionSplitterBounds() public {
        RevenueSplitter.Payee memory arb = d.entrySplit.payee(0);
        assertEq(arb.account, arbSafe);
        assertEq(arb.bps, 7000);
        assertEq(arb.minBps, 6000);
        assertEq(arb.maxBps, 8000);
        uint16[] memory b = new uint16[](2);
        b[0] = 9000;
        b[1] = 1000;
        vm.prank(safe);
        vm.expectRevert(RevenueSplitter.BadConfig.selector);
        d.entrySplit.setShares(b);
    }

    function test_vaultRejectsNon6DecimalAsset() public {
        Mock18DecimalStable m = new Mock18DecimalStable();
        vm.expectRevert(bytes("Asset must have 6 decimals"));
        new PMFIArbVaultV3("x", "x", address(m), cfg.reportSigner, teamSafe, servicer, 1, 0);
        Config memory c = cfg;
        c.asset = address(m);
        vm.expectRevert(bytes("vault asset must have 6 decimals"));
        this.deployWith(c);
    }

    function deployWith(Config memory c) external {
        _deployMilord(c);
    }

    function test_reportSignedForBaseIsRejectedOnRobinhood() public {
        PMFIArbVaultV3.ReportData memory data = PMFIArbVaultV3.ReportData(0, _now(), _now() + 600, 1);
        bytes memory baseSig = _signReport(data, 8453);
        vm.expectRevert(bytes("Invalid signer"));
        d.vault.report(data, baseSig, 0, 0);
        d.vault.report(data, _signReport(data, ROBINHOOD_TESTNET), 0, 0);
    }

    function test_deployScriptRefusesMainnet() public {
        DeployRobinhood s = new DeployRobinhood();
        vm.chainId(4663);
        vm.expectRevert(bytes("mainnet deployment not approved"));
        s.run();
        vm.chainId(8453);
        vm.expectRevert(bytes("Robinhood Chain testnet only"));
        s.run();
    }

    function test_deployScriptOnTestnetFromEnv() public {
        vm.setEnv("MILORD_OWNER_SAFE", vm.toString(safe));
        vm.setEnv("MILORD_TEAM_SAFE", vm.toString(teamSafe));
        vm.setEnv("MILORD_ARB_SAFE", vm.toString(arbSafe));
        vm.setEnv("MILORD_SERVICER", vm.toString(servicer));
        vm.setEnv("MILORD_QUOTE_SIGNER", vm.toString(cfg.quoteSigner));
        vm.setEnv("MILORD_REPORT_SIGNER", vm.toString(cfg.reportSigner));
        vm.setEnv("MILORD_OPERATOR", vm.toString(operator));
        vm.setEnv("MILORD_TOKEN_HOLDER", vm.toString(treasury));
        vm.setEnv("MILORD_VAULT_ASSET", vm.toString(address(usdg)));
        vm.setEnv("MILORD_SWAP_ADAPTER", vm.toString(address(swap)));
        vm.setEnv("MILORD_BASE_URI", "ipfs://x/");
        vm.setEnv("MILORD_CHARACTERISTICS", "C1,C2,C3,C4,C5,C6,C7,C8");
        vm.setEnv("MILORD_VAULT_CAP", "1000000000000");
        DeployRobinhood s = new DeployRobinhood();
        Deployment memory x = s.run();
        assertEq(address(x.vault.usdc()), address(usdg));
        assertEq(x.vault.owner(), safe);
        assertEq(x.converter.pendingOwner(), safe);
    }
}
