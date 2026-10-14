// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {MockUSDG} from "../src/mocks/MockUSDG.sol";
import {MockHookrTaxHook} from "../src/mocks/MockHookrTaxHook.sol";
import {V4SwapAdapter} from "../src/launch/V4SwapAdapter.sol";
import {MockHookrFamilyLock} from "../src/mocks/MockHookrFamilyLock.sol";
import {HookrLockFeeCollector, IHookrFamilyLock} from "../src/launch/HookrLockFeeCollector.sol";
import {UniV3LocalKit} from "./UniV3LocalKit.sol";
import {UniV3SwapVenue, IUniswapV3PoolSwap, IWETH9} from "../src/automation/UniV3SwapVenue.sol";
import {UniV3TwapReference, IUniswapV3PoolOracle} from "../src/automation/UniV3TwapReference.sol";
import {IMILORD} from "../src/interfaces/IMILORD.sol";
import {MilordDeployer} from "./MilordDeployer.sol";
import {HookrLaunchKit} from "./HookrLaunchKit.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";

/// @notice LOCAL ONLY (anvil, chain id 31337). Deploys the whole protocol on one chain as on Robinhood Chain,
///         plus a Hookr-style launch on the official Uniswap v4 build: PoolManager, PositionManager, a native
///         ETH / MILORD pool behind MockHookrTaxHook (stand-in for Hookr's Tax + Conversion block), the
///         MILORD-only position (100% of supply) held by MockHookrFamilyLock (stand-in for Hookr's launcher and
///         HookrFamilyLock) and locked forever with HookrLockFeeCollector as beneficiary, V4SwapAdapter as the
///         buyback route, and the pARB automation: an official Uniswap V3 WETH/USDG pool (mock USDG),
///         UniV3SwapVenue, a 30-minute UniV3TwapReference and ArbitrageFundingRouter.
///         Permit2 must be at its canonical address first (run-local.sh sets it). Anvil test keys only.
///
/// Roles (anvil accounts): 0 deployer/owner, 1 team Safe, 2 arbitrage Safe (also the engine's servicer wallet
/// locally), 3 quote signer, 4 report signer, 5 operator, 6 Hookr treasury stand-in.
///
/// Launch numbers as approved by the owner (2026-10-14): 100% of supply in the position, ~10 ETH opening FDV,
/// a range up to ~1,000x the opening price, 0.3% LP fee, 1.5% / 1.5% tax, 20% platform commission (to verify on Hookr).
contract DeployLocal is Script, MilordDeployer, HookrLaunchKit, UniV3LocalKit {
    uint256 constant PK0 = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    address constant CREATE2_DEPLOYER = 0x4e59b44847b379578588920cA78FbF26c0B4956C;

    function run() external {
        require(block.chainid == 31337, "local anvil only");
        address me = vm.addr(PK0);
        address arb = vm.addr(0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a);
        address hookrTreasury = vm.addr(0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e);

        vm.startBroadcast(PK0);
        MockUSDG usdg = new MockUSDG();

        // ── WETH/USDG market (stand-in for the Robinhood Uniswap V3 pool) and the automation route ──
        V4 memory v = _deployV4Local(me);
        usdg.mint(me, 5_000_000e6);
        V3Market memory m3 = _deployV3EthUsdg(v.weth, address(usdg), 2_500, 2_000 ether, 5_000_000e6, me);
        UniV3SwapVenue fundingVenue = new UniV3SwapVenue(IUniswapV3PoolSwap(m3.pool), IWETH9(v.weth));
        UniV3TwapReference twap = new UniV3TwapReference(IUniswapV3PoolOracle(m3.pool), v.weth, 30 minutes, 100);

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
        c.swapAdapter = address(0); // set once the pool exists (BuybackBurner.setInitialAdapter)
        c.baseURI = "ipfs://PENDING-METADATA/";
        c.characteristics = new string[](8);
        for (uint256 i; i < 8; ++i) c.characteristics[i] = string.concat("Characteristic ", vm.toString(i + 1));
        c.vaultCap = 10_000_000e6;
        c.rootActivationDelay = 1 hours;
        c.splitterMin = [uint16(6000), 4000, 2500];
        c.splitterMax = [uint16(8000), 6000, 4000];
        c.fundingVenue = address(fundingVenue);
        c.fundingReference = address(twap);
        c.routerAmounts = [uint256(0.01 ether), 5 ether, 20 ether]; // local: low threshold so the small week-3 revenue is swapped
        c.routerSlippageBps = 100;
        c.routerInterval = 0; // local: the simulation runs whenever it wants; production uses 1 hour
        c.routerPauser = c.operator;

        Deployment memory d = _deployMilord(c);
        d.vault.setReportCooldown(0); // local: report as often as the simulation wants
        d.vault.setMinimumIdleAmount(0);

        // ── Hookr-style market ──
        bytes memory args = abi.encode(v.poolManager, uint16(150), uint16(150), uint16(2000), hookrTreasury, address(d.taxSplit));
        (address hookAddr, bytes32 salt) = HookMiner.find(CREATE2_DEPLOYER, TAX_HOOK_FLAGS, type(MockHookrTaxHook).creationCode, args);
        MockHookrTaxHook hook =
            new MockHookrTaxHook{salt: salt}(v.poolManager, 150, 150, 2000, hookrTreasury, address(d.taxSplit));
        require(address(hook) == hookAddr, "hook address");

        LaunchParams memory p = LaunchParams({
            milordForLiquidity: d.token.balanceOf(me), // 100% of supply (owner decision 2026-10-14)
            tickUpper: 203_280,
            tickLower: 203_280 - 69_060,
            lpFee: 3000,
            tickSpacing: 60
        });
        PoolKey memory mk = _milordKey(address(d.token), hookAddr, p);
        _openMarket(v.poolManager, mk, p);
        bytes32 familyId = keccak256("MILORD launch family (local)");
        MockHookrFamilyLock familyLock = new MockHookrFamilyLock(v.positionManager, me);
        uint256 positionId = _addMilordOnlyLiquidity(v, mk, p, address(familyLock));
        uint256 launchDust = _burnLaunchRemainder(address(d.token), me, 1e6);
        require(d.token.balanceOf(me) == 0, "deployer must hold no MILORD");
        familyLock.addMember(familyId, positionId);
        HookrLockFeeCollector collector = new HookrLockFeeCollector(
            IHookrFamilyLock(address(familyLock)), familyId, IMILORD(address(d.token)), payable(address(d.burner)), v.poolManager
        );
        familyLock.lock(familyId, address(collector), familyLock.FOREVER()); // LOCAL mock only; no real lock executed

        PoolKey[] memory keys = new PoolKey[](1);
        keys[0] = mk;
        V4SwapAdapter route = new V4SwapAdapter(v.poolManager, keys);
        d.burner.setInitialAdapter(address(route));
        PoolSwapTest router = new PoolSwapTest(v.poolManager); // local stand-in for the Hookr / Universal Router UI
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
        vm.serializeAddress(k, "StrategyRegistry", address(d.registry));
        vm.serializeAddress(k, "MilordGovernor", address(d.governor));
        vm.serializeAddress(k, "USDG", address(usdg));
        vm.serializeAddress(k, "pARBVault", address(d.vault));
        vm.serializeAddress(k, "ShareDistributor", address(d.distributor));
        vm.serializeAddress(k, "ParbFundingAdapter", address(d.adapter));
        vm.serializeAddress(k, "PoolManager", address(v.poolManager));
        vm.serializeAddress(k, "PositionManager", address(v.positionManager));
        vm.serializeAddress(k, "StateView", v.stateView);
        vm.serializeAddress(k, "V4Quoter", v.quoter);
        vm.serializeAddress(k, "HookrTaxHook", hookAddr);
        vm.serializeAddress(k, "HookrFamilyLock", address(familyLock));
        vm.serializeBytes32(k, "familyId", familyId);
        vm.serializeAddress(k, "LockFeeCollector", address(collector));
        vm.serializeAddress(k, "ArbitrageFundingRouter", address(d.router));
        vm.serializeAddress(k, "FundingVenue", address(fundingVenue));
        vm.serializeAddress(k, "TwapReference", address(twap));
        vm.serializeAddress(k, "WETH", v.weth);
        vm.serializeAddress(k, "V3Factory", m3.factory);
        vm.serializeAddress(k, "WethUsdgPool", m3.pool);
        vm.serializeAddress(k, "V3Helper", address(m3.helper));
        vm.serializeUint(k, "launchPositionId", positionId);
        vm.serializeAddress(k, "V4SwapAdapter", address(route));
        vm.serializeAddress(k, "SwapRouter", address(router));
        vm.serializeAddress(k, "hookrTreasury", hookrTreasury);
        vm.serializeUint(k, "lpFee", p.lpFee);
        vm.serializeInt(k, "tickSpacing", p.tickSpacing);
        vm.serializeInt(k, "tickLower", p.tickLower);
        vm.serializeInt(k, "tickUpper", p.tickUpper);
        vm.serializeUint(k, "milordForLiquidity", p.milordForLiquidity);
        vm.serializeUint(k, "launchDustBurned", launchDust);
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
