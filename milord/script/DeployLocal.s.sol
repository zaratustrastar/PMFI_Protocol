// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {MockSwapAdapter} from "../src/mocks/MockSwapAdapter.sol";
import {MockUSDG} from "../src/mocks/MockUSDG.sol";
import {MilordDeployer} from "./MilordDeployer.sol";

/// @notice LOCAL ONLY (anvil, chain id 31337). Deploys the whole protocol, including the pARB
///         vault and distributor, on one chain as on Robinhood Chain, with a mock USDG and a
///         mock swap venue. Uses anvil's public test keys. Refuses any other chain.
///
///   anvil &
///   forge script script/DeployLocal.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
///
/// Roles (anvil accounts): 0 deployer/owner, 1 team Safe, 2 arbitrage Safe (also the engine's
/// servicer wallet locally), 3 quote signer, 4 report signer, 5 operator.
contract DeployLocal is Script, MilordDeployer {
    uint256 constant PK0 = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    function run() external {
        require(block.chainid == 31337, "local anvil only");
        address me = vm.addr(PK0);
        address arb = vm.addr(0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a);

        vm.startBroadcast(PK0);
        MockUSDG usdg = new MockUSDG();
        MockSwapAdapter swap = new MockSwapAdapter(20_000_000 ether); // local test rate: 1 ETH = 20M MILORD

        Config memory c;
        c.deployer = me;
        c.owner = me;
        c.teamSafe = vm.addr(0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d);
        c.arbitrageSafe = arb;
        c.servicer = arb;
        c.quoteSigner = vm.addr(0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6);
        c.reportSigner = vm.addr(0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a);
        c.operator = vm.addr(0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba);
        c.tokenHolder = me;
        c.asset = address(usdg);
        c.swapAdapter = address(swap);
        c.baseURI = "ipfs://PENDING-METADATA/";
        c.characteristics = new string[](8);
        for (uint256 i; i < 8; ++i) c.characteristics[i] = string.concat("Characteristic ", vm.toString(i + 1));
        c.vaultCap = 10_000_000e6;
        c.rootActivationDelay = 1 hours;

        Deployment memory d = _deployMilord(c);
        d.token.transfer(address(swap), 500_000_000 ether); // liquidity for the mock buyback venue
        d.vault.setReportCooldown(0); // local: report as often as the simulation wants
        d.vault.setMinimumIdleAmount(0);
        vm.stopBroadcast();

        string memory k = "d";
        vm.serializeUint(k, "chainId", block.chainid);
        vm.serializeAddress(k, "MILORD", address(d.token));
        vm.serializeAddress(k, "Milords", address(d.nft));
        vm.serializeAddress(k, "MilordConverter", address(d.converter));
        vm.serializeAddress(k, "EntryFeeSplitter", address(d.entrySplit));
        vm.serializeAddress(k, "RoyaltySplitter", address(d.royaltySplit));
        vm.serializeAddress(k, "TradingTaxSplitter", address(d.taxSplit));
        vm.serializeAddress(k, "BuybackBurner", address(d.burner));
        vm.serializeAddress(k, "MockSwapAdapter", address(swap));
        vm.serializeAddress(k, "StrategyRegistry", address(d.registry));
        vm.serializeAddress(k, "MilordGovernor", address(d.governor));
        vm.serializeAddress(k, "USDG", address(usdg));
        vm.serializeAddress(k, "pARBVault", address(d.vault));
        vm.serializeAddress(k, "ShareDistributor", address(d.distributor));
        vm.serializeAddress(k, "ParbFundingAdapter", address(d.adapter));
        vm.serializeAddress(k, "owner", c.owner);
        vm.serializeAddress(k, "teamSafe", c.teamSafe);
        vm.serializeAddress(k, "arbitrageSafe", c.arbitrageSafe);
        vm.serializeAddress(k, "quoteSigner", c.quoteSigner);
        vm.serializeAddress(k, "reportSigner", c.reportSigner);
        string memory json = vm.serializeAddress(k, "operator", c.operator);
        vm.writeJson(json, "./sim/deployments.local.json");
        console2.log("wrote sim/deployments.local.json");
    }
}
