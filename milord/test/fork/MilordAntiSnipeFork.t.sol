// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console2} from "forge-std/Test.sol";
import {IHookrLauncher} from "./hookr/interfaces/IHookrLauncher.sol";
import {HookrTypes} from "./hookr/types/HookrTypes.sol";
import {IHookrRulesFork, IHookrDirectionalTaxFork} from "./HookrForkInterfaces.sol";
import {MilordForkBase} from "./MilordForkBase.sol";

/// Priority 2: Hookr's native Anti-Snipe on an existing-token launch (EXPLORATION, not an approved launch setting):
/// 40% falling linearly to 0 over 60 minutes, guard 300 blocks, with the permanent 1.5% Tax + Conversion on top.
/// Env overrides: SNIPE_PIPS, SNIPE_SECONDS, GUARD_BLOCKS, LOCK_MARGIN_BLOCKS.
contract MilordAntiSnipeFork is MilordForkBase {
    uint256 internal constant G = 0.1 ether;

    function setUp() public {
        if (!_forkSetUp()) return;
        _antiSnipeLaunch();
        _logLaunch();
    }

    /// Native Anti-Snipe is accepted on an existing-token family, frozen as passed, and locks principal for the guard.
    function test_existingTokenAntiSnipeAccepted() public view {
        HookrTypes.RulesConfig memory c = IHookrRulesFork(rules).config(poolId);
        assertEq(c.snipeTaxPips, snipePips, "Snipe start frozen as passed");
        assertEq(c.snipeDecaySeconds, snipeSeconds, "decay seconds frozen as passed");
        assertEq(c.snipeFloorPips, 0, "decays to zero");
        assertEq(c.guardEndBlock, guardEnd, "guard end frozen");
        assertEq(c.protocolShareBps, SHARE_BPS);
        assertEq(IHookrRulesFork(rules).minimumFee(poolId), 0, "no Hookr minimum fee");
        assertGe(LAUNCHER.principalLockedUntil(familyId), guardEnd, "principal locked through the guard");
        assertEq(milord.balanceOf(safe), 0, "100% of supply still seeded");
        console2.log("guardEnd / principalLockedUntil", guardEnd, LAUNCHER.principalLockedUntil(familyId));
    }

    /// Exact decay formula: Snipe(t) = floor(start * (D - t) / D) for t < D (t = block.timestamp - launch timestamp),
    /// 0 from t = D; LP fee = 0.3% + Snipe - floor(Snipe * 20%); Hookr's quote take = floor(Snipe * 20%); the 1.5% tax is
    /// charged on top, unchanged, on the gross.
    function test_decayFormulaAndTakes() public {
        uint256[9] memory ts = [uint256(0), 1, 600, 1200, 1800, 2700, 3599, 3600, 3700];
        console2.log("t(s) | Snipe pips | LP fee pips | Hookr take wei | tax wei | pool input wei  (per 0.1 ETH buy)");
        for (uint256 i; i < ts.length; i++) {
            vm.warp(launchTs + ts[i]);
            vm.roll(block.number + 1);
            assertLt(block.number, guardEnd, "still inside the guard");
            uint256 s = _snipeAt(ts[i]);
            uint256 p0 = _protocolClaim();
            uint256 q0 = buyQueue.pending();
            SwapResult memory r = _swap(true, G);
            uint256 hookr = _protocolClaim() - p0;
            uint256 taxed = buyQueue.pending() - q0;

            assertEq(uint256(uint128(-r.delta.amount0())), G, "buyer pays the gross");
            assertEq(r.lpFee, _expectedLpFee(ts[i]), "LP fee = base + 80% of Snipe");
            assertApproxEqAbs(hookr, G * (s * SHARE_BPS / 10_000) / PIPS, 10, "Hookr takes 20% of Snipe");
            assertApproxEqAbs(taxed, G * uint256(TAX_PIPS) / PIPS, 10, "1.5% tax unchanged under Anti-Snipe");
            assertApproxEqAbs(r.poolIn, G - hookr - taxed, 10, "pool input = gross - takes");
            assertEq(IHookrDirectionalTaxFork(tax).taxAt(ROOT, poolId, true, block.number), TAX_PIPS);
            console2.log(ts[i], s, r.lpFee);
            console2.log("    ", hookr, taxed, r.poolIn);
        }
    }

    /// Maximum surcharge: HookrRules refuses base + Snipe above 60% (Snipe <= 597,000 pips at a 0.3% base), and the
    /// admission caps bound the LP part and the quote take. 40% must be accepted.
    function test_maxSurchargeBounds() public {
        uint256 ok400 = _tryLaunch(400_000);
        uint256 ok597 = _tryLaunch(597_000);
        uint256 ok597b = _tryLaunch(597_001);
        uint256 ok600 = _tryLaunch(600_000);
        console2.log("Snipe 40.0% accepted", ok400);
        console2.log("Snipe 59.7% accepted", ok597);
        console2.log("Snipe 59.7001% accepted", ok597b);
        console2.log("Snipe 60.0% accepted", ok600);
        assertEq(ok400, 1, "40% Anti-Snipe is launchable");
        assertEq(ok597b, 0, "above 59.7% is refused");
        assertEq(ok600, 0);
    }

    function _tryLaunch(uint24 pips) internal returns (uint256 accepted) {
        uint256 snap = vm.snapshotState();
        try this.launchWith(pips) {
            accepted = 1;
        } catch {}
        vm.revertToState(snap);
    }

    function launchWith(uint24 pips) external {
        require(msg.sender == address(this));
        _deployMilord();
        _launch(LaunchOpts({snipePips: pips, snipeSeconds: 3600, guardBlocks: 300, lockEndAfterGuard: 7200}));
    }

    /// The guard, not the clock, ends the Snipe: from guardEndBlock on, buys pay base only even mid-decay. On mainnet
    /// the guard counts parent-chain (L1) blocks, ~12 s each, so 300 blocks ~ 60 min.
    function test_guardEndStopsSnipe() public {
        vm.roll(guardEnd);
        vm.warp(launchTs + 1800);
        uint256 p0 = _protocolClaim();
        SwapResult memory r = _swap(true, G);
        assertEq(r.lpFee, BASE, "base fee only after the guard");
        assertEq(_protocolClaim() - p0, 0, "no Hookr Snipe take after the guard");
    }

    /// During the guard: exact-output buys and outside liquidity are refused; sells pay no Snipe.
    function test_guardRules() public {
        vm.expectRevert();
        this.exactOutBuy();
        vm.expectRevert();
        this.outsideAdd();

        _swap(true, 1 ether);
        vm.roll(block.number + 1);
        uint256 p0 = _protocolClaim();
        uint256 q0 = sellQueue.pending();
        SwapResult memory r = _swap(false, milord.balanceOf(address(this)) / 2);
        assertEq(r.lpFee, BASE, "sells pay no Snipe");
        assertEq(_protocolClaim() - p0, 0, "no Hookr take on sells");
        assertApproxEqAbs(sellQueue.pending() - q0, r.poolOut * TAX_PIPS / PIPS, 10, "1.5% sell tax");

        vm.roll(guardEnd);
        SwapResult memory x = _buyExactOut(1_000_000 ether);
        assertEq(x.lpFee, BASE, "exact-output buys work after the guard");
    }

    function exactOutBuy() external {
        _buyExactOut(1_000_000 ether);
    }

    function outsideAdd() external {
        pm.unlock(abi.encode(uint8(2), false, uint256(1e18)));
    }

    /// Our BuybackBurner route (V4SwapAdapter, exact input, unauthenticated, straight on the PoolManager) still trades
    /// during the guard, and pays the Snipe like any buyer.
    function test_buybackRouteWorksInGuard() public {
        vm.deal(operator, 1 ether);
        uint256 supply0 = milord.totalSupply();
        vm.prank(operator);
        uint256 out = adapter.swapExactETHForToken{value: 0.05 ether}(address(milord), 1, address(burner), block.timestamp);
        assertGt(out, 0);
        vm.prank(operator);
        burner.burnHeldTokens();
        assertEq(milord.totalSupply(), supply0 - out, "bought MILORD burned");
    }

    /// Where Snipe revenue goes: 80% stays in the pool as LP fee owed to the family owner (the founding position is
    /// the only in-range liquidity in the guard); 20% is credited to Hookr's protocol recipient in HookrRules.
    function test_snipeRevenueDestination() public {
        uint256 p0 = _protocolClaim();
        uint256 gross;
        for (uint256 i; i < 6; i++) {
            vm.warp(launchTs + i * 600);
            vm.roll(block.number + 10);
            _swap(true, 0.5 ether);
            gross += 0.5 ether;
        }
        uint256 hookrSnipe = _protocolClaim() - p0;
        uint256 eth0 = safe.balance;
        vm.prank(safe);
        (uint256 a0,) = LAUNCHER.withdraw(familyId, 0, 0, 0, 0, safe, block.timestamp); // fees only
        assertEq(safe.balance - eth0, a0);
        assertApproxEqAbs(a0, lpFeeEthTotal, 12, "owner's ETH fees = LP fees from Swap events");
        console2.log("gross buys (wei)", gross);
        console2.log("LP fees to family owner: base part", lpFeeEthTotal - lpSurchargeEthTotal);
        console2.log("LP fees to family owner: Snipe part (post-commission)", lpSurchargeEthTotal);
        console2.log("Hookr 20% of Snipe (Rules claim)", hookrSnipe);
        console2.log("1.5% tax to buy queue", buyQueue.pending());
    }

    /// Tax + Conversion's own decay cannot replace Anti-Snipe: its ceiling is 10%.
    function test_taxDecayCappedAtTenPercent() public view {
        assertEq(IHookrDirectionalTaxFork(tax).MAX_TAX_PIPS(), 100_000);
    }
}
