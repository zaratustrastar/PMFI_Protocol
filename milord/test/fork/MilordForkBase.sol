// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

// Robinhood Chain (4663) mainnet-FORK integration base. Runs only inside `forge test --fork-url <local anvil fork>`:
// every transaction below executes in Foundry's in-memory EVM over forked state. Nothing is signed or broadcast, no
// private key is used, and the deployed Hookr-1 contracts are called as they are on mainnet (no mocks).
//
// Without a 4663 fork (plain `forge test`, CI) every test in test/fork/ is skipped in setUp.
// Start it with fork/run-fork-tests.sh, which also proves the RPC is a local Anvil fork and not a remote node.

import {Test, console2, Vm} from "forge-std/Test.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {IHookrLauncher} from "./hookr/interfaces/IHookrLauncher.sol";
import {IHookrRegistry} from "./hookr/interfaces/IHookrRegistry.sol";
import {IHookrFamilyLock} from "./hookr/interfaces/IHookrFamilyLock.sol";
import {IHookrLaunchFee} from "./hookr/interfaces/IHookrLaunchFee.sol";
import {HookrTypes} from "./hookr/types/HookrTypes.sol";
import {IHookrRulesFork, IHookrDirectionalTaxFork, IHookrTaxQueueFork, IHookrTreasuryFork} from "./HookrForkInterfaces.sol";

import {MILORDToken} from "../../src/robinhood/MILORDToken.sol";
import {BuybackBurner, ISwapAdapter} from "../../src/robinhood/BuybackBurner.sol";
import {RevenueSplitter} from "../../src/robinhood/RevenueSplitter.sol";
import {V4SwapAdapter} from "../../src/launch/V4SwapAdapter.sol";
import {HookrLockFeeCollector, IHookrFamilyLock as ICollectorLock} from "../../src/launch/HookrLockFeeCollector.sol";
import {IMILORD} from "../../src/interfaces/IMILORD.sol";

/// Stands in for ArbSys (0x64), which an Anvil fork does not emulate. Hookr reads it only through HookrClock
/// (arb recapture and pending-owner bookkeeping), neither of which the MILORD launch uses.
contract ArbSysForkStub {
    function arbBlockNumber() external view returns (uint256) {
        return block.number;
    }
}

abstract contract MilordForkBase is Test, IUnlockCallback {
    using PoolIdLibrary for PoolKey;

    // ---------------------------------------------------------------- deployed Hookr-1 (Robinhood Chain 4663)
    IHookrLauncher internal constant LAUNCHER = IHookrLauncher(0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4);
    IHookrFamilyLock internal constant LOCK = IHookrFamilyLock(0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b);
    address internal constant ROOT = 0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc;
    address internal constant RULES_A = 0x4a71c270b1520F94064268102AebD744b0DE7942;
    address internal constant RULES_B = 0x10f684361b35C8411a2c07F68A6fB1F4B7e555F7;
    address internal constant TAX_A = 0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F;
    address internal constant TAX_B = 0x99012422e1acBdf53C42BB378eb2f4e468005d3c;
    address internal constant ARBSYS = address(0x64);
    Currency internal constant ETH = Currency.wrap(address(0));
    bytes32 internal constant SWAP_TOPIC = keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)");

    // ---------------------------------------------------------------- approved MILORD launch terms (do not change)
    uint256 internal constant SUPPLY = 6_767_000_000 ether;
    int24 internal constant TICK_UPPER = 203280; // opening price: ~10 ETH FDV
    int24 internal constant TICK_LOWER = 134220; // range top ~1,000x the opening price
    int24 internal constant TICK_SPACING = 60;
    uint24 internal constant BASE = 3000; // 0.3% LP fee
    uint24 internal constant TAX_PIPS = 15_000; // 1.5% buy and sell, in ETH
    uint16 internal constant SHARE_BPS = 2000; // Hookr commission 20%
    uint256 internal constant PIPS = 1_000_000;

    // ---------------------------------------------------------------- actors (no keys: in-memory addresses only)
    address internal safe = makeAddr("teamSafeStandIn"); // launches and owns the family before the lock
    address internal team = makeAddr("teamSafePayee");
    address internal arbSafe = makeAddr("arbitrageFundingSafe");
    address internal operator = makeAddr("buybackOperator");

    // ---------------------------------------------------------------- live modules and our contracts
    IPoolManager internal pm;
    IHookrRegistry internal registry;
    address internal rules;
    address internal tax;
    MILORDToken internal milord;
    BuybackBurner internal burner;
    RevenueSplitter internal taxSplit;
    V4SwapAdapter internal adapter;
    HookrLockFeeCollector internal collector;

    // ---------------------------------------------------------------- launch state
    bytes32 internal familyId;
    PoolKey internal key;
    PoolId internal poolId;
    uint256 internal launchBlock;
    uint256 internal launchTs;
    uint256 internal guardEnd; // 0 = no guard
    uint24 internal snipePips;
    uint16 internal snipeSeconds;
    uint256 internal launchFeePaid;
    uint256 internal launchDust;
    IHookrTaxQueueFork internal buyQueue;
    IHookrTaxQueueFork internal sellQueue;

    // per-swap LP fee bookkeeping from PoolManager Swap events (ETH side), the Option D split's input
    uint256 internal lpFeeEthTotal;
    uint256 internal lpSurchargeEthTotal;
    uint256 internal lpFeeMilordTotal;

    struct LaunchOpts {
        uint24 snipePips; // 0 = Anti-Snipe off (the approved launch)
        uint16 snipeSeconds;
        uint256 guardBlocks; // parent (L1) blocks on mainnet; fork blocks here
        uint256 lockEndAfterGuard; // principal lock knob: blocks after the guard (0 = no knob)
    }

    receive() external payable {}

    /// Skips unless this is a 4663 fork with Hookr-1 code present; then wires the live modules.
    function _forkSetUp() internal returns (bool) {
        if (block.chainid != 4663 || address(LAUNCHER).code.length == 0) {
            vm.skip(true, "fork-only: run fork/run-fork-tests.sh (local Anvil fork of Robinhood Chain)");
            return false;
        }
        vm.etch(ARBSYS, address(new ArbSysForkStub()).code);
        pm = LAUNCHER.poolManager();
        registry = LAUNCHER.registry();
        rules = vm.envOr("HOOKR_RULES", _admitted(RULES_A, RULES_B));
        tax = vm.envOr("HOOKR_TAX", _admitted(TAX_A, TAX_B));
        vm.deal(address(this), 1000 ether);
        vm.deal(safe, 1 ether); // launch fee (at most 0.01 ETH) and nothing else: the founding position takes 0 ETH
        _deployMilord();
        return true;
    }

    function _admitted(address a, address b) internal view returns (address) {
        return registry.admission(ROOT, a).implementation == a ? a : b;
    }

    /// Our real contracts, as MilordDeployer wires them: the token (100% to the Safe), BuybackBurner, and the
    /// trading-tax splitter (1/3 buyback-burn, 1/3 Team Safe, 1/3 Arbitrage Funding Safe).
    function _deployMilord() internal {
        milord = new MILORDToken(safe);
        burner = new BuybackBurner(safe, IMILORD(address(milord)), ISwapAdapter(address(0)));
        RevenueSplitter.Payee[] memory t = new RevenueSplitter.Payee[](3);
        t[0] = RevenueSplitter.Payee(address(burner), 3334, 3000, 4000, "buyback-burn");
        t[1] = RevenueSplitter.Payee(team, 3333, 3000, 4000, "team");
        t[2] = RevenueSplitter.Payee(arbSafe, 3333, 3000, 4000, "arbitrage");
        taxSplit = new RevenueSplitter(safe, t);
    }

    // ---------------------------------------------------------------- launch through the live HookrLauncher

    function _launch(LaunchOpts memory o) internal {
        snipePips = o.snipePips;
        snipeSeconds = o.snipeSeconds;
        guardEnd = o.guardBlocks == 0 ? 0 : block.number + o.guardBlocks;
        IHookrRegistry.Admission memory ra = registry.admission(ROOT, rules);
        IHookrRegistry.Admission memory aa = registry.admission(ROOT, tax);

        uint160 sqrtU = TickMath.getSqrtPriceAtTick(TICK_UPPER);
        uint160 sqrtL = TickMath.getSqrtPriceAtTick(TICK_LOWER);
        uint256 amount = milord.balanceOf(safe);
        assertEq(amount, SUPPLY, "Safe holds 100% of supply before launch");
        uint128 liquidity = uint128(FullMath.mulDiv(amount, 1 << 96, sqrtU - sqrtL));

        IHookrLauncher.Member[] memory members = new IHookrLauncher.Member[](1);
        members[0].quote = ETH;
        members[0].tickSpacing = TICK_SPACING;
        members[0].sqrtPriceX96 = sqrtU;
        members[0].tickLower = TICK_LOWER;
        members[0].tickUpper = TICK_UPPER;
        members[0].liquidity = liquidity;
        members[0].amount0Max = 0; // 0 ETH
        members[0].amount1Max = uint128(amount);
        members[0].config = HookrTypes.PoolConfig({
            subject: Currency.wrap(address(milord)),
            quote: ETH,
            rules: rules,
            advisory: tax,
            liquidityOwner: address(0), // the launcher sets itself
            baseLpFeePips: BASE,
            caps: ra.caps,
            rulesGasLimit: ra.gasLimit,
            advisoryGasLimit: aa.gasLimit,
            advisoryPhases: HookrTypes.BEFORE_SWAP | HookrTypes.AFTER_SWAP, // both directions taxed
            advisoryFailOpen: false,
            policyId: bytes32(0)
        });
        HookrTypes.RulesConfig memory c; // every Hook Block off unless set here
        c.guardEndBlock = uint40(guardEnd);
        c.maxFeePips = BASE; // no dynamic fee
        c.snipeTaxPips = o.snipePips;
        c.snipeDecaySeconds = o.snipeSeconds;
        c.protocolShareBps = SHARE_BPS;
        members[0].rules = c;

        IHookrLauncher.MemberKnobs[] memory knobs = new IHookrLauncher.MemberKnobs[](1);
        if (o.lockEndAfterGuard != 0) knobs[0].lockEndBlock = uint64((guardEnd == 0 ? block.number : guardEnd) + o.lockEndAfterGuard);

        // Tax + Conversion, abi.encode(Config) (320 bytes = no decay schedule, no session tiers): flat 1.5% buy and
        // 1.5% sell in ETH, no conversion route, creator share paid as ETH to the trading-tax splitter.
        bytes[] memory advisory = new bytes[](1);
        advisory[0] = abi.encode(
            TAX_PIPS, bytes32(0), address(taxSplit), address(0), uint32(0),
            TAX_PIPS, bytes32(0), address(taxSplit), address(0), uint32(0)
        );
        assertEq(advisory[0].length, 320, "Tax + Conversion config is the flat 320-byte form");

        IHookrLauncher.LaunchParams memory p;
        p.token = IHookrLauncher.Token(address(milord), "", "", 0, bytes32(0));
        p.root = ROOT;
        p.members = members;
        p.knobs = knobs;
        p.advisoryConfigs = advisory;
        p.maxRetainedBps = 10_000; // existing-token families take only 10,000
        p.deadline = block.timestamp + 1 hours;

        (launchFeePaid,,,,) = IHookrLaunchFee(address(LAUNCHER)).launchFee();
        uint256 safeEthBefore = safe.balance;
        vm.startPrank(safe);
        milord.approve(address(LAUNCHER), amount);
        (familyId,,) = LAUNCHER.launchFamily{value: launchFeePaid}(p);
        vm.stopPrank();
        assertEq(safeEthBefore - safe.balance, launchFeePaid, "the Safe spent only the launch fee in ETH");
        launchBlock = block.number;
        launchTs = block.timestamp;
        key = LAUNCHER.position(familyId, 0).key;
        poolId = key.toId();

        // Burn the rounding remainder the position could not take (HookrLaunchKit._burnLaunchRemainder).
        launchDust = milord.balanceOf(safe);
        if (launchDust > 0) {
            vm.prank(safe);
            milord.burn(launchDust);
        }

        IHookrDirectionalTaxFork.Terms memory tt = IHookrDirectionalTaxFork(tax).terms(ROOT, poolId);
        buyQueue = IHookrTaxQueueFork(tt.buyQueue);
        sellQueue = IHookrTaxQueueFork(tt.sellQueue);

        adapter = _deployAdapter();
        vm.prank(safe);
        burner.setInitialAdapter(address(adapter));
        vm.prank(safe);
        burner.setOperator(operator, true);
        collector = new HookrLockFeeCollector(
            ICollectorLock(address(LOCK)), familyId, IMILORD(address(milord)), payable(address(burner)), pm
        );
    }

    function _deployAdapter() internal returns (V4SwapAdapter) {
        PoolKey[] memory keys = new PoolKey[](1);
        keys[0] = key;
        return new V4SwapAdapter(pm, keys);
    }

    function _approvedLaunch() internal {
        _launch(LaunchOpts({snipePips: 0, snipeSeconds: 0, guardBlocks: 0, lockEndAfterGuard: 0}));
    }

    /// The Anti-Snipe candidate under exploration: 40% falling linearly to 0 over 60 minutes, guard 300 parent blocks.
    function _antiSnipeLaunch() internal {
        _launch(
            LaunchOpts({
                snipePips: uint24(vm.envOr("SNIPE_PIPS", uint256(400_000))),
                snipeSeconds: uint16(vm.envOr("SNIPE_SECONDS", uint256(3600))),
                guardBlocks: vm.envOr("GUARD_BLOCKS", uint256(300)),
                lockEndAfterGuard: vm.envOr("LOCK_MARGIN_BLOCKS", uint256(7200))
            })
        );
    }

    // ---------------------------------------------------------------- swaps straight on the PoolManager (unauthenticated)

    struct SwapResult {
        BalanceDelta delta; // the caller's delta, Hookr takes included
        uint24 lpFee; // pips the pool charged (Swap event)
        uint256 poolIn; // input the pool itself swapped (Swap event)
        uint256 poolOut; // output the pool paid before any Hookr take (Swap event)
        uint256 lpFeeAmt; // estimated LP fee, input currency
    }

    function _swap(bool buy, uint256 amountIn) internal returns (SwapResult memory r) {
        vm.recordLogs();
        r.delta = abi.decode(pm.unlock(abi.encode(uint8(0), buy, amountIn)), (BalanceDelta));
        _readSwapLog(r, buy);
    }

    function _buyExactOut(uint256 milordOut) internal returns (SwapResult memory r) {
        vm.recordLogs();
        r.delta = abi.decode(pm.unlock(abi.encode(uint8(1), true, milordOut)), (BalanceDelta));
        _readSwapLog(r, true);
    }

    function _readSwapLog(SwapResult memory r, bool buy) internal {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(pm) || logs[i].topics[0] != SWAP_TOPIC) continue;
            if (logs[i].topics[1] != PoolId.unwrap(poolId)) continue;
            (int128 a0, int128 a1,,,, uint24 f) = abi.decode(logs[i].data, (int128, int128, uint160, uint128, int24, uint24));
            r.lpFee = f;
            // In the event, a negative amount is what the pool received.
            r.poolIn = buy ? uint256(uint128(-a0)) : uint256(uint128(-a1));
            r.poolOut = buy ? uint256(uint128(a1)) : uint256(uint128(a0));
            r.lpFeeAmt = FullMath.mulDivRoundingUp(r.poolIn, f, PIPS);
            if (buy) {
                lpFeeEthTotal += r.lpFeeAmt;
                lpSurchargeEthTotal += r.lpFeeAmt * (f - BASE) / f;
            } else {
                lpFeeMilordTotal += r.lpFeeAmt;
            }
        }
    }

    /// op 0: exact-input swap, op 1: exact-output buy, op 2: add outside liquidity just under the price
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(pm), "not pool manager");
        (uint8 op, bool buy, uint256 amount) = abi.decode(data, (uint8, bool, uint256));
        BalanceDelta d;
        if (op == 2) {
            (, int24 tick,,) = _slot0();
            int24 lower = (tick / TICK_SPACING) * TICK_SPACING - TICK_SPACING;
            (d,) = pm.modifyLiquidity(key, ModifyLiquidityParams(lower, lower + TICK_SPACING, int256(amount), bytes32(0)), "");
        } else {
            d = pm.swap(
                key,
                SwapParams({
                    zeroForOne: buy,
                    amountSpecified: op == 0 ? -int256(amount) : int256(amount),
                    sqrtPriceLimitX96: buy ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
                }),
                ""
            );
        }
        _settleDelta(key.currency0, d.amount0());
        _settleDelta(key.currency1, d.amount1());
        return abi.encode(d);
    }

    function _settleDelta(Currency c, int128 delta) internal {
        if (delta < 0) {
            uint256 owe = uint256(uint128(-delta));
            if (c.isAddressZero()) {
                pm.settle{value: owe}();
            } else {
                pm.sync(c);
                milord.transfer(address(pm), owe);
                pm.settle();
            }
        } else if (delta > 0) {
            pm.take(c, address(this), uint256(uint128(delta)));
        }
    }

    // ---------------------------------------------------------------- reads

    function _slot0() internal view returns (uint160, int24, uint24, uint24) {
        return StateLibrary.getSlot0(pm, poolId);
    }

    /// Fully diluted value in wei of ETH at the pool price: supply / (MILORD per ETH).
    function _fdvWei() internal view returns (uint256) {
        (uint160 sqrtP,,,) = _slot0();
        uint256 priceX96 = FullMath.mulDiv(sqrtP, sqrtP, 1 << 96); // MILORD per ETH, Q96
        return FullMath.mulDiv(milord.totalSupply(), 1 << 96, priceX96);
    }

    function _protocolClaim() internal view returns (uint256) {
        return IHookrRulesFork(rules).claimable(ETH, IHookrRulesFork(rules).protocolRecipient());
    }

    /// The Snipe pips HookrRules charges `elapsed` seconds after launch (linear, floor 0), before the guard ends.
    function _snipeAt(uint256 elapsed) internal view returns (uint256) {
        if (guardEnd == 0 || block.number >= guardEnd || snipePips == 0) return 0;
        return elapsed >= snipeSeconds ? 0 : uint256(snipePips) * (snipeSeconds - elapsed) / snipeSeconds;
    }

    function _expectedLpFee(uint256 elapsed) internal view returns (uint24) {
        uint256 s = _snipeAt(elapsed);
        return uint24(BASE + s - s * SHARE_BPS / 10_000);
    }

    function _logLaunch() internal view {
        IHookrRegistry.Admission memory ra = registry.admission(ROOT, rules);
        IHookrRegistry.Admission memory aa = registry.admission(ROOT, tax);
        console2.log("HookrRules in use", rules);
        console2.log("  admission caps: maxLpFeePips / maxQuoteTakePips", ra.caps.maxLpFeePips, ra.caps.maxQuoteTakePips);
        console2.log("Tax + Conversion in use", tax);
        console2.log("  admission caps: maxLpFeePips / maxQuoteTakePips", aa.caps.maxLpFeePips, aa.caps.maxQuoteTakePips);
        console2.log("  advisory phase mask", aa.phaseMask);
        console2.log("launch fee paid (wei)", launchFeePaid);
        console2.log("family id");
        console2.logBytes32(familyId);
        console2.log("pool id");
        console2.logBytes32(PoolId.unwrap(poolId));
        console2.log("key.fee / tickSpacing", key.fee, uint256(int256(key.tickSpacing)));
        console2.log("buy queue", address(buyQueue), "sell queue", address(sellQueue));
        console2.log("protocol recipient", IHookrRulesFork(rules).protocolRecipient());
        console2.log("launch rounding dust burned (wei)", launchDust);
    }
}
