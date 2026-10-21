// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console2, Vm} from "forge-std/Test.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IHookrLauncher} from "../../src/vendor/hookr/interfaces/IHookrLauncher.sol";
import {IHookrLaunchFee} from "../../src/vendor/hookr/interfaces/IHookrLaunchFee.sol";
import {IHookrTaxQueueFork} from "../../src/vendor/hookr/IHookrExtras.sol";
import {MilordAtomicLaunch} from "../../src/launch/MilordAtomicLaunch.sol";
import {MILORDToken} from "../../src/robinhood/MILORDToken.sol";
import {BuybackBurner, ISwapAdapter} from "../../src/robinhood/BuybackBurner.sol";
import {RevenueSplitter} from "../../src/robinhood/RevenueSplitter.sol";
import {HookrLockFeeCollector, IHookrFamilyLock as ICollectorLock} from "../../src/launch/HookrLockFeeCollector.sol";
import {IMILORD} from "../../src/interfaces/IMILORD.sol";
import {MilordForkBase} from "./MilordForkBase.sol";

/// Priority 1 (production readiness): MILORD is created and launched in ONE transaction by MilordAtomicLaunch, called
/// by the Team Safe, so the ETH/MILORD Hookr pool cannot be pre-empted. Then the Safe accepts the family and, in a
/// later transaction, locks it for good. Also: exactly when principal becomes withdrawable, and the missed-handover
/// contingency (priority 2).
contract MilordAtomicLaunchFork is MilordForkBase {
    using PoolIdLibrary for PoolKey;

    uint32 internal constant LOCK_BLOCKS = 7200; // temporary principal lock: ~24 h of 12 s parent blocks
    MilordAtomicLaunch internal helper;

    function setUp() public {
        _forkSetUp();
    }

    function _config(uint24 snipe, uint16 secs, uint32 guard)
        internal
        view
        returns (MilordAtomicLaunch.Config memory c)
    {
        c.launcher = LAUNCHER;
        c.root = ROOT;
        c.rules = rules;
        c.tax = tax;
        c.owner = safe;
        c.salt = keccak256("MILORD");
        c.maxLaunchFee = 0.01 ether;
        c.lockBlocks = LOCK_BLOCKS;
        c.snipePips = snipe;
        c.snipeSeconds = secs;
        c.guardBlocks = guard;
    }

    /// Deploys the helper and everything that needs the token address, against the PREDICTED address (no code yet).
    function _prepare(MilordAtomicLaunch.Config memory c) internal {
        helper = new MilordAtomicLaunch(c);
        address predicted = helper.predictedToken();
        assertEq(predicted.code.length, 0, "MILORD does not exist before the launch transaction");
        burner = new BuybackBurner(safe, IMILORD(predicted), ISwapAdapter(address(0)));
        RevenueSplitter.Payee[] memory t = new RevenueSplitter.Payee[](3);
        t[0] = RevenueSplitter.Payee(address(burner), 3334, 3000, 4000, "buyback-burn");
        t[1] = RevenueSplitter.Payee(team, 3333, 3000, 4000, "team");
        t[2] = RevenueSplitter.Payee(arbSafe, 3333, 3000, 4000, "arbitrage");
        taxSplit = new RevenueSplitter(safe, t);
    }

    /// The Safe's launch transaction: one call. Wires the base's handles to the result.
    function _safeLaunch() internal {
        uint256 fee = helper.launchFee();
        vm.prank(safe);
        familyId = helper.launch{value: fee}(address(taxSplit), block.timestamp + 1 hours);
        launchFeePaid = fee;
        launchBlock = block.number;
        launchTs = block.timestamp;
        milord = MILORDToken(address(helper.token()));
        key = LAUNCHER.position(familyId, 0).key;
        poolId = key.toId();
        buyQueue = IHookrTaxQueueFork(helper.buyQueue());
        sellQueue = IHookrTaxQueueFork(helper.sellQueue());
        guardEnd = helper.guardEndBlock();
        MilordAtomicLaunch.Config memory c = helper.config();
        snipePips = c.snipePips;
        snipeSeconds = c.snipeSeconds;
        adapter = _deployAdapter();
        collector = new HookrLockFeeCollector(
            ICollectorLock(address(LOCK)), familyId, IMILORD(address(milord)), payable(address(burner)), pm
        );
    }

    function _accept() internal {
        vm.prank(safe);
        LAUNCHER.acceptFamily(familyId);
    }

    function _lockForever() internal {
        vm.startPrank(safe);
        LAUNCHER.transferFamily(familyId, address(LOCK), 0);
        LOCK.lock(familyId, address(collector), type(uint64).max);
        vm.stopPrank();
    }

    // ---------------------------------------------------------------- pre-emption

    /// Before the launch transaction no one can open the pool: HookrLauncher refuses a token without code, and MILORD
    /// has none until the Safe's call creates it.
    function test_cannotPreemptBeforeLaunch() public {
        _prepare(_config(0, 0, 0));
        address squatter = makeAddr("squatter");
        vm.deal(squatter, 1 ether);
        IHookrLauncher.LaunchParams memory p = _squatParams(helper.predictedToken());
        vm.prank(squatter);
        vm.expectRevert(IHookrLauncher.InvalidFunding.selector);
        LAUNCHER.launchFamily{value: 0.1 ether + helper.launchFee()}(p);

        _safeLaunch(); // and our launch still succeeds
        assertEq(address(milord), helper.predictedToken());
    }

    function _squatParams(address token) internal view returns (IHookrLauncher.LaunchParams memory p) {
        IHookrLauncher.Member[] memory m = new IHookrLauncher.Member[](1);
        m[0].quote = ETH;
        m[0].tickSpacing = TICK_SPACING;
        m[0].sqrtPriceX96 = TickMath.getSqrtPriceAtTick(0);
        m[0].tickLower = 600;
        m[0].tickUpper = 1200;
        m[0].liquidity = 1e15;
        m[0].amount0Max = 0.1 ether;
        m[0].config.rules = rules;
        m[0].config.baseLpFeePips = BASE;
        m[0].config.caps = registry.admission(ROOT, rules).caps;
        m[0].config.rulesGasLimit = registry.admission(ROOT, rules).gasLimit;
        m[0].rules.maxFeePips = BASE;
        m[0].rules.protocolShareBps = SHARE_BPS;
        p.token = IHookrLauncher.Token(token, "", "", 0, bytes32(0));
        p.root = ROOT;
        p.members = m;
        p.knobs = new IHookrLauncher.MemberKnobs[](1);
        p.advisoryConfigs = new bytes[](1);
        p.maxRetainedBps = 10_000;
        p.deadline = block.timestamp + 1 hours;
    }

    // ---------------------------------------------------------------- the flat launch, end to end

    function test_atomicFlatLaunch() public {
        _prepare(_config(0, 0, 0));
        uint256 safeEth = safe.balance;
        _safeLaunch();

        assertEq(safeEth - safe.balance, launchFeePaid, "the Safe spent only Hookr's launch fee");
        assertEq(milord.totalSupply(), milord.balanceOf(address(pm)), "every MILORD is in the pool");
        assertGt(milord.totalSupply(), SUPPLY - 1e6, "only rounding dust burned");
        assertEq(milord.balanceOf(address(helper)), 0);
        assertEq(milord.balanceOf(safe), 0);
        uint256 fdv = _fdvWei();
        assertGe(fdv, 9.9 ether);
        assertLe(fdv, 10.2 ether);
        (address pending,) = LAUNCHER.pendingFamilyOwner(familyId);
        assertEq(pending, safe, "family offered to the Safe");
        assertEq(LAUNCHER.familyOwner(familyId), address(helper));
        assertEq(LAUNCHER.principalLockedUntil(familyId), launchBlock + LOCK_BLOCKS, "temporary lock = launch + 7200");
        assertEq(helper.lockEndBlock(), launchBlock + LOCK_BLOCKS);

        // Flat 1.5% from the first trade, through the atomic launch's pool.
        SwapResult memory r = _swap(true, 1 ether);
        assertEq(r.lpFee, BASE);
        assertApproxEqAbs(buyQueue.pending(), 0.015 ether, 10);

        _accept();
        assertEq(LAUNCHER.familyOwner(familyId), safe);
        _lockForever();
        (address ben, uint64 until) = LOCK.lockOf(familyId);
        assertEq(ben, address(collector));
        assertEq(until, type(uint64).max);
        console2.log("token", address(milord));
        console2.log("launch fee / lockEnd", launchFeePaid, helper.lockEndBlock());
    }

    /// Fees earned before the Safe accepts are paid to the helper on acceptance; anyone can forward them to the Safe.
    function test_feesBeforeAcceptReachSafe() public {
        _prepare(_config(0, 0, 0));
        _safeLaunch();
        _swap(true, 2 ether);
        vm.roll(block.number + 1);
        _swap(false, milord.balanceOf(address(this)) / 2);
        uint256 e0 = safe.balance;
        uint256 m0 = milord.balanceOf(safe);
        _accept();
        assertApproxEqAbs(address(helper).balance, lpFeeEthTotal, 10, "ETH fees paid to the helper on accept");
        vm.prank(makeAddr("anyone"));
        helper.sweep();
        assertApproxEqAbs(safe.balance - e0, lpFeeEthTotal, 10, "forwarded to the Safe");
        assertApproxEqAbs(milord.balanceOf(safe) - m0, lpFeeMilordTotal, 10);
        assertEq(address(helper).balance, 0);
    }

    // ---------------------------------------------------------------- refusals

    function test_onlySafeOnce() public {
        _prepare(_config(0, 0, 0));
        uint256 fee = helper.launchFee();
        vm.deal(makeAddr("stranger"), 1 ether);
        vm.prank(makeAddr("stranger"));
        vm.expectRevert(MilordAtomicLaunch.NotOwner.selector);
        helper.launch{value: fee}(address(taxSplit), block.timestamp + 1 hours);
        _safeLaunch();
        vm.prank(safe);
        vm.expectRevert(MilordAtomicLaunch.AlreadyLaunched.selector);
        helper.launch{value: fee}(address(taxSplit), block.timestamp + 1 hours);
    }

    function test_refusesBadInputs() public {
        _prepare(_config(0, 0, 0));
        uint256 fee = helper.launchFee();
        vm.prank(safe);
        vm.expectRevert(MilordAtomicLaunch.BadConfig.selector);
        helper.launch{value: fee}(makeAddr("eoaRecipient"), block.timestamp + 1 hours); // tax recipient must be a contract

        vm.prank(safe);
        vm.expectRevert(abi.encodeWithSelector(MilordAtomicLaunch.WrongValue.selector, fee + 1, fee));
        helper.launch{value: fee + 1}(address(taxSplit), block.timestamp + 1 hours);

        // Hookr raises its launch fee above our cap: refused.
        vm.mockCall(
            address(LAUNCHER),
            abi.encodeWithSelector(IHookrLaunchFee.launchFee.selector),
            abi.encode(uint256(0.02 ether), address(0), uint256(0), address(0), uint256(0))
        );
        vm.prank(safe);
        vm.expectRevert(abi.encodeWithSelector(MilordAtomicLaunch.LaunchFeeTooHigh.selector, 0.02 ether, 0.01 ether));
        helper.launch{value: 0.02 ether}(address(taxSplit), block.timestamp + 1 hours);
        vm.clearMockedCalls();

        // Constructor: Anti-Snipe needs all three knobs; the lock must be set and within Hookr's horizon.
        vm.expectRevert(MilordAtomicLaunch.BadConfig.selector);
        new MilordAtomicLaunch(_config(400_000, 0, 300));
        MilordAtomicLaunch.Config memory c = _config(0, 0, 0);
        c.lockBlocks = 0;
        vm.expectRevert(MilordAtomicLaunch.BadConfig.selector);
        new MilordAtomicLaunch(c);
        c.lockBlocks = 100_001;
        vm.expectRevert(MilordAtomicLaunch.BadConfig.selector);
        new MilordAtomicLaunch(c);
    }

    /// A launch whose Hookr terms do not verify reverts as a whole: nothing is created, nothing is launched.
    function test_verificationFailureRevertsEverything() public {
        _prepare(_config(0, 0, 0));
        address predicted = helper.predictedToken();
        // Make HookrRules report a Hookr minimum fee for every pool: the helper's check 6 must stop the launch.
        vm.mockCall(rules, abi.encodeWithSignature("minimumFee(bytes32)"), abi.encode(uint16(1)));
        uint256 fee = helper.launchFee();
        vm.prank(safe);
        vm.expectRevert(); // Mismatch(6) from the helper, or earlier inside Hookr: either way the whole launch reverts
        helper.launch{value: fee}(address(taxSplit), block.timestamp + 1 hours);
        vm.clearMockedCalls();
        assertEq(predicted.code.length, 0, "no token was left behind");
        assertFalse(helper.launched());
    }

    // ---------------------------------------------------------------- Anti-Snipe candidate through the helper

    function test_atomicAntiSnipeCandidate() public {
        _prepare(_config(400_000, 3600, 300));
        _safeLaunch();
        assertEq(guardEnd, launchBlock + 300, "guard counted from the launch block");
        assertEq(helper.lockEndBlock(), guardEnd + LOCK_BLOCKS, "temporary lock runs past the guard");
        assertEq(LAUNCHER.principalLockedUntil(familyId), guardEnd + LOCK_BLOCKS);
        vm.warp(launchTs + 900);
        vm.roll(block.number + 1);
        SwapResult memory r = _swap(true, 0.1 ether);
        assertEq(r.lpFee, _expectedLpFee(900), "Snipe at 15 min: 30%, LP part 24% + 0.3%");
    }

    // ---------------------------------------------------------------- when principal becomes withdrawable (priority 2)

    /// Principal is withdrawable by the family OWNER from block principalLockedUntil on, never before, and never once
    /// HookrFamilyLock holds the family.
    function test_withdrawableExactlyAtLockEnd() public {
        _prepare(_config(0, 0, 0));
        _safeLaunch();
        uint128 liq = LAUNCHER.position(familyId, 0).liquidity;
        uint256 until = LAUNCHER.principalLockedUntil(familyId);

        // While the helper owns the family: it has no withdraw path at all.
        vm.roll(until + 10);
        vm.prank(safe);
        vm.expectRevert();
        LAUNCHER.withdraw(familyId, 0, liq, 0, 0, safe, block.timestamp);

        _accept();
        vm.roll(until - 1);
        vm.prank(safe);
        vm.expectRevert(abi.encodeWithSelector(IHookrLauncher.PrincipalLocked.selector, familyId, uint8(0), until));
        LAUNCHER.withdraw(familyId, 0, 1, 0, 0, safe, block.timestamp);

        vm.roll(until);
        uint256 snap = vm.snapshotState();
        vm.prank(safe);
        (, uint256 a1) = LAUNCHER.withdraw(familyId, 0, liq / 1000, 0, 0, safe, block.timestamp);
        assertGt(a1, 0, "at principalLockedUntil the owner CAN withdraw: lock before this block");
        vm.revertToState(snap);

        // Missed-handover contingency: the lock still works after the temporary lock ended, and then nothing can leave.
        vm.roll(until + 50_000);
        _lockForever();
        vm.prank(safe);
        vm.expectRevert();
        LAUNCHER.withdraw(familyId, 0, liq, 0, 0, safe, block.timestamp);
        console2.log("principalLockedUntil (parent block)", until);
    }

    // ---------------------------------------------------------------- Option D through the helper (priority 4)

    /// The reconciliation (offchain/option-d-reconcile.mjs) trusts HookrLauncher's LPFeesCollected events as what the
    /// helper and the Safe received. Check that against balances and against the Swap-event estimate, end to end.
    function test_optionD_lpFeesCollectedEventsMatchPayouts() public {
        _prepare(_config(400_000, 3600, 300));
        _safeLaunch();
        _swap(true, 0.5 ether); // before the Safe accepts: paid to the helper on acceptance
        vm.recordLogs();
        _accept();
        (uint256 e1, uint256 m1) = _sumFeeEvents(address(helper));
        assertEq(address(helper).balance, e1, "helper received exactly what LPFeesCollected says");
        helper.sweep();
        for (uint256 i = 1; i < 6; i++) {
            vm.warp(launchTs + i * 600);
            vm.roll(block.number + 10);
            _swap(true, 0.5 ether);
        }
        _swap(false, milord.balanceOf(address(this)) / 4);
        vm.roll(guardEnd);
        uint256 s0 = safe.balance;
        uint256 sm0 = milord.balanceOf(safe);
        vm.recordLogs();
        _lockForever();
        (uint256 e2, uint256 m2) = _sumFeeEvents(safe);
        assertEq(safe.balance - s0, e2, "Safe received exactly what LPFeesCollected says");
        assertEq(milord.balanceOf(safe) - sm0, m2);
        assertApproxEqAbs(e1 + e2, lpFeeEthTotal, 12, "events = Swap-event estimate (ETH)");
        assertApproxEqAbs(m1 + m2, lpFeeMilordTotal, 12, "events = Swap-event estimate (MILORD)");
        assertGt(lpSurchargeEthTotal, (e1 + e2) * 9 / 10, "the Snipe part dominates the window");
        console2.log("window ETH fees (events) / Snipe LP part", e1 + e2, lpSurchargeEthTotal);
    }

    bytes32 internal constant LP_FEES_TOPIC =
        keccak256("LPFeesCollected(bytes32,uint8,bytes32,address,address,uint256,uint256)");

    function _sumFeeEvents(address expectRecipient) internal returns (uint256 eth, uint256 mil) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(LAUNCHER) || logs[i].topics[0] != LP_FEES_TOPIC) continue;
            if (logs[i].topics[1] != familyId) continue;
            (, address recipient, uint256 a0, uint256 a1) = abi.decode(logs[i].data, (uint8, address, uint256, uint256));
            assertEq(recipient, expectRecipient, "fees paid to the previous owner");
            eth += a0;
            mil += a1;
        }
    }
}
