// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {SingleChainFixture} from "../utils/SingleChainFixture.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {StrategyRegistry} from "../../src/robinhood/StrategyRegistry.sol";
import {RevenueSplitter} from "../../src/robinhood/RevenueSplitter.sol";
import {Mock18DecimalStable} from "../../src/mocks/Mock18DecimalStable.sol";
import {DeployRobinhood} from "../../script/DeployRobinhood.s.sol";

contract MockWethUsdgPool {
    address public token0;
    address public token1;

    constructor(address a, address b) {
        (token0, token1) = a < b ? (a, b) : (b, a);
    }
}

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

    function deployWith(Config memory c) external returns (Deployment memory) {
        return _deployMilord(c);
    }

    /// Atomic launch: the protocol is deployed against MilordAtomicLaunch.predictedToken(), which has no code yet.
    function test_deployAgainstPredictedToken() public {
        Config memory c = cfg;
        c.token = makeAddr("predictedMilord");
        c.tokenHolder = address(0);
        Deployment memory x = this.deployWith(c);
        assertEq(address(x.token), c.token);
        assertEq(address(x.converter.token()), c.token);
        assertEq(address(x.burner.token()), c.token);
        assertEq(c.token.code.length, 0, "nothing was deployed at the predicted address");
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
        address weth = makeAddr("weth");
        vm.setEnv("MILORD_FUNDING_POOL", vm.toString(address(new MockWethUsdgPool(weth, address(usdg)))));
        vm.setEnv("MILORD_WETH", vm.toString(weth));
        vm.setEnv("MILORD_PUBLISHER", vm.toString(makeAddr("publisher")));
        vm.setEnv("MILORD_ROUTER_PAUSER", vm.toString(makeAddr("monitor")));
        vm.setEnv("MILORD_DEPLOYMENTS_OUT", "./deployments/test-robinhood.json");
        DeployRobinhood s = new DeployRobinhood();
        Deployment memory x = s.run();
        assertEq(address(x.vault.usdc()), address(usdg));
        assertEq(x.vault.owner(), safe);
        assertEq(x.converter.pendingOwner(), safe);

        // Fee -> USDG -> pARB is automatic: every splitter's arbitrage share goes to the router, never to a Safe.
        address router = address(x.router);
        assertTrue(router != address(0), "router deployed");
        assertEq(x.entrySplit.payee(0).account, router, "entry fee 70% -> router");
        assertEq(x.royaltySplit.payee(1).account, router, "royalty 50% -> router");
        assertEq(x.taxSplit.payee(2).account, router, "trading tax 1/3 -> router");
        assertTrue(x.adapter.isOperator(router), "router may fund the vault");
        assertEq(x.router.pendingOwner(), arbSafe, "router offered to the Arbitrage Funding Safe");
        assertTrue(x.router.isPauser(makeAddr("monitor")));
        assertEq(x.distributor.publisher(), makeAddr("publisher"), "publisher has its own key");
        assertEq(x.router.minSwapWei(), 0.05 ether);
        assertEq(x.router.maxSwapWei(), 2 ether);
        assertEq(x.router.dailyCapWei(), 10 ether);

        // Refusals: no funding pool (would silently fall back to manual Safe funding) and a shared keeper/publisher key.
        vm.setEnv("MILORD_PUBLISHER", vm.toString(operator));
        vm.expectRevert(bytes("keeper and publisher need separate keys (nonces)"));
        s.run();
    }
}
