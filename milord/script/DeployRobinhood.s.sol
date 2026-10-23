// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {MilordDeployer} from "./MilordDeployer.sol";
import {UniV3SwapVenue, IUniswapV3PoolSwap, IWETH9} from "../src/automation/UniV3SwapVenue.sol";
import {UniV3TwapReference, IUniswapV3PoolOracle} from "../src/automation/UniV3TwapReference.sol";

/// @notice Robinhood Chain deployment from environment configuration.
///
///   Modes (docs/MAINNET_RUNBOOK.md):
///   - TESTNET (46630).
///   - PRIVATE FORK of Robinhood mainnet (4663) for fork/rehearse-full-launch.sh: MILORD_LOCAL_FORK=yes and the
///     public Anvil account #0 as broadcaster (a key that must never hold mainnet funds).
///   - MAINNET (4663): refused unless MILORD_MAINNET_APPROVAL equals this run's configuration hash (printed by a
///     dry run without --broadcast) and the broadcaster is not the public Anvil key, every Safe is a deployed
///     contract and the vault asset is USDG. The hash binds every address and parameter below, so an approval
///     cannot be reused for a different configuration. Mainnet deployment is NOT approved yet.
///
///   Dry run (no broadcast) against a testnet RPC:
///     forge script script/DeployRobinhood.s.sol --rpc-url $ROBINHOOD_TESTNET_RPC --sender $DEPLOYER
///
///   Required env: MILORD_OWNER_SAFE, MILORD_TEAM_SAFE, MILORD_ARB_SAFE, MILORD_SERVICER,
///   MILORD_QUOTE_SIGNER, MILORD_REPORT_SIGNER, MILORD_OPERATOR (keeper key), MILORD_PUBLISHER (epoch publisher key),
///   MILORD_ROUTER_PAUSER (monitor key), MILORD_VAULT_ASSET (USDG), MILORD_BASE_URI,
///   MILORD_CHARACTERISTICS (8 comma-separated labels), MILORD_VAULT_CAP (asset units),
///   MILORD_FUNDING_POOL (Uniswap V3 WETH/USDG pool), MILORD_WETH,
///   and either MILORD_PREDICTED_TOKEN (atomic launch: MilordAtomicLaunch.predictedToken()) or MILORD_TOKEN_HOLDER.
///   Optional: MILORD_SWAP_ADAPTER (normally unset until the Hookr market opens), MILORD_ROUTER_MIN_WEI /
///   _MAX_WEI / _DAILY_WEI (default 0.05 / 2 / 10 ETH), MILORD_ROUTER_SLIPPAGE_BPS (100), MILORD_ROUTER_INTERVAL (3600),
///   MILORD_TWAP_WINDOW (1800 s), MILORD_TWAP_MAX_TICKS (100), MILORD_DEPLOYMENTS_OUT (./deployments/robinhood-<chain>.json).
///   The three splitters' arbitrage share always goes to ArbitrageFundingRouter (never a manual Safe transfer);
///   the router belongs to the Arbitrage Funding Safe after it accepts ownership.
///   No private keys are read from env; sign with --ledger / --account.
contract DeployRobinhood is Script, MilordDeployer {
    uint256 constant ROBINHOOD_TESTNET = 46630;
    uint256 constant ROBINHOOD_MAINNET = 4663;

    address internal constant ANVIL_0 = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;
    address internal constant MAINNET_USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    bytes32 public configHash;

    function run() external returns (Deployment memory d) {
        require(block.chainid == ROBINHOOD_TESTNET || block.chainid == ROBINHOOD_MAINNET, "Robinhood Chain (46630 / 4663) only");
        return _run();
    }

    /// Mainnet and private-fork gates, after the configuration is read (the hash covers all of it).
    function _gate(Config memory c, address pool, address weth, uint32 window, int24 maxTicks) internal {
        configHash = keccak256(
            abi.encode(
                keccak256(abi.encode(block.chainid, c.owner, c.teamSafe, c.arbitrageSafe, c.servicer, c.quoteSigner, c.reportSigner)),
                keccak256(abi.encode(c.operator, c.publisher, c.token, c.tokenHolder, c.asset, c.swapAdapter, c.routerPauser)),
                keccak256(abi.encode(c.baseURI, c.characteristics, c.vaultCap, c.rootActivationDelay, c.splitterMin, c.splitterMax)),
                keccak256(abi.encode(pool, weth, c.routerAmounts, c.routerSlippageBps, c.routerInterval, window, maxTicks))
            )
        );
        console2.log("MILORD deployment configuration hash:");
        console2.logBytes32(configHash);
        if (block.chainid != ROBINHOOD_MAINNET) return;
        if (keccak256(bytes(vm.envOr("MILORD_LOCAL_FORK", string("no")))) == keccak256("yes")) {
            require(msg.sender == ANVIL_0, "local fork: broadcast with the public Anvil account #0 only");
            return;
        }
        require(vm.envOr("MILORD_MAINNET_APPROVAL", bytes32(0)) == configHash, "mainnet deployment not approved for this configuration hash");
        require(msg.sender != ANVIL_0, "mainnet: never the public Anvil key");
        require(c.owner.code.length > 0 && c.teamSafe.code.length > 0 && c.arbitrageSafe.code.length > 0, "mainnet: Safes must be deployed contracts");
        require(c.asset == MAINNET_USDG, "mainnet: vault asset must be USDG");
        require(c.token != address(0) && c.token.code.length == 0, "mainnet: MILORD_PREDICTED_TOKEN required and must not exist yet (atomic launch)");
    }

    function _run() internal returns (Deployment memory d) {
        Config memory c;
        c.owner = vm.envAddress("MILORD_OWNER_SAFE");
        c.teamSafe = vm.envAddress("MILORD_TEAM_SAFE");
        c.arbitrageSafe = vm.envAddress("MILORD_ARB_SAFE");
        c.servicer = vm.envAddress("MILORD_SERVICER");
        c.quoteSigner = vm.envAddress("MILORD_QUOTE_SIGNER");
        c.reportSigner = vm.envAddress("MILORD_REPORT_SIGNER");
        c.operator = vm.envAddress("MILORD_OPERATOR");
        c.publisher = vm.envAddress("MILORD_PUBLISHER");
        require(c.publisher != c.operator, "keeper and publisher need separate keys (nonces)");
        c.token = vm.envOr("MILORD_PREDICTED_TOKEN", address(0)); // atomic launch: MilordAtomicLaunch.predictedToken()
        c.tokenHolder = c.token == address(0) ? vm.envAddress("MILORD_TOKEN_HOLDER") : address(0);
        c.asset = vm.envAddress("MILORD_VAULT_ASSET");
        c.swapAdapter = vm.envOr("MILORD_SWAP_ADAPTER", address(0)); // normally unset: route set after the Hookr market opens
        c.baseURI = vm.envString("MILORD_BASE_URI");
        c.characteristics = vm.envString("MILORD_CHARACTERISTICS", ",");
        c.vaultCap = vm.envUint("MILORD_VAULT_CAP");
        c.rootActivationDelay = 1 days;
        // proposed production splitter bounds (docs/OPEN_DECISIONS.md P2)
        c.splitterMin = [uint16(6000), 4000, 2500];
        c.splitterMax = [uint16(8000), 6000, 4000];
        // ETH -> USDG -> pARB automation (docs/launch/SAFE_CONFIGURATION.md §3). Required: without it the splitters
        // would pay the Arbitrage Safe and funding would be manual.
        address pool = vm.envAddress("MILORD_FUNDING_POOL");
        address weth = vm.envAddress("MILORD_WETH");
        c.routerAmounts = [
            vm.envOr("MILORD_ROUTER_MIN_WEI", uint256(0.05 ether)),
            vm.envOr("MILORD_ROUTER_MAX_WEI", uint256(2 ether)),
            vm.envOr("MILORD_ROUTER_DAILY_WEI", uint256(10 ether))
        ];
        c.routerSlippageBps = uint16(vm.envOr("MILORD_ROUTER_SLIPPAGE_BPS", uint256(100)));
        c.routerInterval = uint32(vm.envOr("MILORD_ROUTER_INTERVAL", uint256(1 hours)));
        c.routerPauser = vm.envAddress("MILORD_ROUTER_PAUSER");
        uint32 window = uint32(vm.envOr("MILORD_TWAP_WINDOW", uint256(30 minutes)));
        int24 maxTicks = int24(int256(vm.envOr("MILORD_TWAP_MAX_TICKS", uint256(100))));

        _gate(c, pool, weth, window, maxTicks);
        console2.log("vault asset", c.asset, IERC20Metadata(c.asset).symbol());
        uint256 startBlock = block.number;
        vm.startBroadcast();
        (, c.deployer,) = vm.readCallers(); // the broadcasting account
        UniV3SwapVenue venue = new UniV3SwapVenue(IUniswapV3PoolSwap(pool), IWETH9(weth));
        require(venue.token() == c.asset, "funding pool is not WETH/vault asset");
        c.fundingVenue = address(venue);
        c.fundingReference = address(new UniV3TwapReference(IUniswapV3PoolOracle(pool), weth, window, maxTicks));
        d = _deployMilord(c);
        vm.stopBroadcast();
        require(address(d.router) != address(0), "router not deployed");
        _record(c, d, pool, weth, startBlock);
    }

    /// Writes the addresses the services read (offchain/services: DEPLOYMENTS_FILE). The Hookr keys (familyId, poolId,
    /// tax queues) are added after the launch by offchain/record-launch.mjs.
    function _record(Config memory c, Deployment memory d, address pool, address weth, uint256 startBlock) internal {
        string memory k = "robinhood";
        vm.serializeUint(k, "chainId", block.chainid);
        vm.serializeUint(k, "startBlock", startBlock);
        vm.serializeAddress(k, "MILORD", address(d.token));
        vm.serializeAddress(k, "Milords", address(d.nft));
        vm.serializeAddress(k, "MilordConverter", address(d.converter));
        vm.serializeAddress(k, "EntryFeeSplitter", address(d.entrySplit));
        vm.serializeAddress(k, "RoyaltySplitter", address(d.royaltySplit));
        vm.serializeAddress(k, "TradingTaxSplitter", address(d.taxSplit));
        vm.serializeAddress(k, "BuybackBurner", address(d.burner));
        vm.serializeAddress(k, "StrategyRegistry", address(d.registry));
        vm.serializeAddress(k, "MilordGovernor", address(d.governor));
        vm.serializeAddress(k, "pARBVault", address(d.vault));
        vm.serializeAddress(k, "ShareDistributor", address(d.distributor));
        vm.serializeAddress(k, "ParbFundingAdapter", address(d.adapter));
        vm.serializeAddress(k, "ArbitrageFundingRouter", address(d.router));
        vm.serializeAddress(k, "FundingVenue", c.fundingVenue);
        vm.serializeAddress(k, "TwapReference", c.fundingReference);
        vm.serializeAddress(k, "WethUsdgPool", pool);
        vm.serializeAddress(k, "WETH", weth);
        vm.serializeAddress(k, "USDG", c.asset);
        vm.serializeAddress(k, "owner", c.owner);
        vm.serializeAddress(k, "teamSafe", c.teamSafe);
        vm.serializeAddress(k, "arbitrageSafe", c.arbitrageSafe);
        vm.serializeAddress(k, "servicer", c.servicer);
        vm.serializeAddress(k, "quoteSigner", c.quoteSigner);
        vm.serializeAddress(k, "reportSigner", c.reportSigner);
        vm.serializeAddress(k, "publisher", c.publisher);
        vm.serializeAddress(k, "routerPauser", c.routerPauser);
        string memory json = vm.serializeAddress(k, "operator", c.operator);
        string memory out = vm.envOr(
            "MILORD_DEPLOYMENTS_OUT", string.concat("./deployments/robinhood-", vm.toString(block.chainid), ".json")
        );
        vm.writeJson(json, out);
        console2.log("wrote", out);
    }
}
