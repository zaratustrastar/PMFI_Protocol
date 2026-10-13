// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MILORDToken} from "../src/robinhood/MILORDToken.sol";
import {MilordsNFT} from "../src/robinhood/MilordsNFT.sol";
import {MilordConverter} from "../src/robinhood/MilordConverter.sol";
import {RevenueSplitter} from "../src/robinhood/RevenueSplitter.sol";
import {BuybackBurner, ISwapAdapter} from "../src/robinhood/BuybackBurner.sol";
import {StrategyRegistry} from "../src/robinhood/StrategyRegistry.sol";
import {BasePayoutRegistry} from "../src/robinhood/BasePayoutRegistry.sol";
import {MilordGovernor} from "../src/robinhood/MilordGovernor.sol";
import {IMILORD} from "../src/interfaces/IMILORD.sol";
import {MockSwapAdapter} from "../src/mocks/MockSwapAdapter.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";
import {PMFIArbVaultV3} from "../src/base/PMFIArbVaultV3.sol";
import {ParbFundingAdapter, IParbVault} from "../src/base/ParbFundingAdapter.sol";
import {ShareDistributor} from "../src/base/ShareDistributor.sol";

/// @notice LOCAL ONLY (anvil, chain id 31337). Deploys both the Robinhood Chain side and
///         the Base side onto one local chain with mocks, using anvil's public test keys.
///         Refuses to run on any other chain.
///
///   anvil &
///   forge script script/DeployLocal.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
///
/// Roles (anvil accounts): 0 deployer/owner, 1 team Safe, 2 arbitrage Safe,
/// 3 quote signer, 4 report signer, 5 operator (buyback, funding, root publisher).
contract DeployLocal is Script {
    uint256 constant PK0 = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    struct Roles {
        address owner;
        address team;
        address arb;
        address quoteSigner;
        address reportSigner;
        address operator;
    }

    function run() external {
        require(block.chainid == 31337, "local anvil only");
        // anvil's well-known public test keys #1..#5
        Roles memory r = Roles(
            vm.addr(PK0),
            vm.addr(0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d),
            vm.addr(0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a),
            vm.addr(0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6),
            vm.addr(0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a),
            vm.addr(0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba)
        );

        vm.startBroadcast(PK0);

        // ── Robinhood Chain side ──
        MILORDToken token = new MILORDToken(r.owner);
        RevenueSplitter royaltySplit =
            new RevenueSplitter(r.owner, _two(r.team, 5000, "team", r.arb, 5000, "arbitrage"));
        RevenueSplitter entrySplit = new RevenueSplitter(r.owner, _two(r.arb, 7000, "arbitrage", r.team, 3000, "team"));
        MilordsNFT nft = new MilordsNFT(r.owner, address(royaltySplit), "ipfs://PENDING-METADATA/");
        MilordConverter conv =
            new MilordConverter(r.owner, IMILORD(address(token)), nft, address(entrySplit), r.quoteSigner);
        nft.setConverter(address(conv));

        MockSwapAdapter swap = new MockSwapAdapter(20_000_000 ether); // local test rate: 1 ETH = 20M MILORD
        token.transfer(address(swap), 500_000_000 ether);
        BuybackBurner burner = new BuybackBurner(r.owner, IMILORD(address(token)), ISwapAdapter(address(swap)));
        burner.setOperator(r.operator, true);

        RevenueSplitter.Payee[] memory t = new RevenueSplitter.Payee[](3);
        t[0] = RevenueSplitter.Payee(address(burner), 3334, 0, 10000, "buyback-burn");
        t[1] = RevenueSplitter.Payee(r.team, 3333, 0, 10000, "team");
        t[2] = RevenueSplitter.Payee(r.arb, 3333, 0, 10000, "arbitrage");
        RevenueSplitter taxSplit = new RevenueSplitter(r.owner, t);

        StrategyRegistry registry = new StrategyRegistry(r.owner, nft);
        for (uint256 i = 1; i <= 8; ++i) {
            registry.addCharacteristic(string.concat("Characteristic ", vm.toString(i)));
        }
        BasePayoutRegistry payout = new BasePayoutRegistry();
        MilordGovernor gov = new MilordGovernor(nft);

        // ── Base side (same local chain) ──
        MockUSDC usdc = new MockUSDC();
        PMFIArbVaultV3 vault = new PMFIArbVaultV3(
            "PMFI Arb (MILORD)", "pARB", address(usdc), r.reportSigner, r.team, r.arb, 10_000_000e6, 0
        );
        ShareDistributor dist = new ShareDistributor(r.owner, IERC20(address(vault)), r.operator);
        ParbFundingAdapter adapter =
            new ParbFundingAdapter(r.owner, IERC20(address(usdc)), IParbVault(address(vault)), address(dist));
        adapter.setOperator(r.operator, true);
        vault.setDepositorRestricted(true);
        vault.setAllowedDepositor(address(adapter), true);
        vault.setReportCooldown(0);
        vault.setMinimumIdleAmount(0);
        dist.setActivationDelay(1 hours);

        uint256 arbId = registry.addStrategy("pArbitrage", block.chainid, address(vault), address(vault), true);
        registry.setDefaultStrategy(arbId);

        vm.stopBroadcast();

        string memory k = "d";
        vm.serializeUint(k, "chainId", block.chainid);
        vm.serializeAddress(k, "MILORD", address(token));
        vm.serializeAddress(k, "Milords", address(nft));
        vm.serializeAddress(k, "MilordConverter", address(conv));
        vm.serializeAddress(k, "EntryFeeSplitter", address(entrySplit));
        vm.serializeAddress(k, "RoyaltySplitter", address(royaltySplit));
        vm.serializeAddress(k, "TradingTaxSplitter", address(taxSplit));
        vm.serializeAddress(k, "BuybackBurner", address(burner));
        vm.serializeAddress(k, "MockSwapAdapter", address(swap));
        vm.serializeAddress(k, "StrategyRegistry", address(registry));
        vm.serializeAddress(k, "BasePayoutRegistry", address(payout));
        vm.serializeAddress(k, "MilordGovernor", address(gov));
        vm.serializeAddress(k, "USDC", address(usdc));
        vm.serializeAddress(k, "pARBVault", address(vault));
        vm.serializeAddress(k, "ShareDistributor", address(dist));
        vm.serializeAddress(k, "ParbFundingAdapter", address(adapter));
        vm.serializeAddress(k, "owner", r.owner);
        vm.serializeAddress(k, "teamSafe", r.team);
        vm.serializeAddress(k, "arbitrageSafe", r.arb);
        vm.serializeAddress(k, "quoteSigner", r.quoteSigner);
        vm.serializeAddress(k, "reportSigner", r.reportSigner);
        string memory json = vm.serializeAddress(k, "operator", r.operator);
        vm.writeJson(json, "./sim/deployments.local.json");
        console2.log("wrote sim/deployments.local.json");
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
}
