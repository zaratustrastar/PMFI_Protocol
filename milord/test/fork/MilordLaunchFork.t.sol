// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {console2} from "forge-std/Test.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {IHookrLauncher} from "../../src/vendor/hookr/interfaces/IHookrLauncher.sol";
import {HookrTypes} from "../../src/vendor/hookr/types/HookrTypes.sol";
import {IHookrRulesFork, IHookrDirectionalTaxFork, IHookrTaxQueueFork, IHookrTreasuryFork} from "../../src/vendor/hookr/IHookrExtras.sol";
import {MilordForkBase} from "./MilordForkBase.sol";

/// Priority 1: the approved MILORD launch (no Anti-Snipe) through the live HookrLauncher on a Robinhood fork.
contract MilordLaunchFork is MilordForkBase {
    function setUp() public {
        if (!_forkSetUp()) return;
        _approvedLaunch();
        _logLaunch();
    }

    /// The existing MILORD ERC-20 opens through the launcher; 100% of supply is in one MILORD-only position; 0 ETH.
    function test_launch_fullSupply_zeroEth() public view {
        IHookrLauncher.Position memory pos = LAUNCHER.position(familyId, 0);
        assertGt(pos.liquidity, 0, "founding position exists");
        assertEq(pos.tickLower, TICK_LOWER);
        assertEq(pos.tickUpper, TICK_UPPER);
        assertEq(Currency.unwrap(key.currency0), address(0), "ETH is currency0");
        assertEq(Currency.unwrap(key.currency1), address(milord), "MILORD is currency1");
        assertEq(address(key.hooks), ROOT, "pool hook is HookrRoot");
        assertEq(int256(key.tickSpacing), int256(TICK_SPACING));
        assertEq(LAUNCHER.familyOwner(familyId), safe, "the Safe owns the family until it locks it");

        assertLt(launchDust, 1e6, "the position took all but integer-rounding dust");
        assertEq(milord.balanceOf(safe), 0, "deployer/Safe keeps 0 MILORD");
        assertEq(milord.totalSupply(), SUPPLY - launchDust, "supply = 6.767B minus burned dust");
        assertEq(milord.balanceOf(address(pm)), SUPPLY - launchDust, "every remaining MILORD sits in the pool");

        (uint160 sqrtP, int24 tick,,) = _slot0();
        assertEq(sqrtP, TickMath.getSqrtPriceAtTick(TICK_UPPER), "opens at the top of the range (MILORD-only)");
        assertEq(tick, TICK_UPPER);
        console2.log("MILORD in founding position (wei)", SUPPLY - launchDust);
        console2.log("principalLockedUntil", LAUNCHER.principalLockedUntil(familyId));
    }

    /// Opening FDV ~10 ETH (approved 2026-10-14).
    function test_openingFdv() public view {
        uint256 fdv = _fdvWei();
        console2.log("opening FDV (wei)", fdv);
        assertGe(fdv, 9.9 ether, "FDV >= 9.9 ETH");
        assertLe(fdv, 10.2 ether, "FDV <= 10.2 ETH");
    }

    /// What Hookr froze is exactly what we passed: every other Hook Block off, 20% commission, no Hookr minimum fee,
    /// flat 1.5%/1.5% tax (no decay, no session tiers), tax paid as ETH to our splitter.
    function test_frozenTerms() public view {
        HookrTypes.RulesConfig memory c = IHookrRulesFork(rules).config(poolId);
        assertEq(c.guardEndBlock, 0, "no guard");
        assertEq(c.snipeTaxPips, 0, "no Snipe");
        assertEq(c.maxFeePips, BASE, "no dynamic fee");
        assertEq(c.burnBps + c.lpBps + c.potBps + c.royaltyBps, 0, "Auto Burn, LP Rewards, pot, royalty off");
        assertEq(c.protocolShareBps, SHARE_BPS, "Hookr commission 20%");
        assertEq(c.maxBuyQuoteAmount, 0, "no buy cap");
        assertEq(IHookrRulesFork(rules).minimumFee(poolId), 0, "no Hookr minimum fee on this pool");

        IHookrDirectionalTaxFork.Terms memory t = IHookrDirectionalTaxFork(tax).terms(ROOT, poolId);
        assertTrue(t.bound);
        assertFalse(t.decaying, "no launch tax decay: flat from trade one");
        assertFalse(t.tiered, "no session tiers");
        assertEq(t.buyTaxPips, TAX_PIPS);
        assertEq(t.sellTaxPips, TAX_PIPS);
        assertEq(IHookrDirectionalTaxFork(tax).taxAt(ROOT, poolId, true, launchBlock), TAX_PIPS, "1.5% buy at launch block");
        assertEq(IHookrDirectionalTaxFork(tax).taxAt(ROOT, poolId, false, launchBlock), TAX_PIPS, "1.5% sell at launch block");

        _checkQueue(buyQueue, true);
        _checkQueue(sellQueue, false);
    }

    function _checkQueue(IHookrTaxQueueFork q, bool isBuy) internal view {
        IHookrTaxQueueFork.QueueTerms memory qt = q.terms();
        assertEq(qt.isBuy, isBuy);
        assertEq(qt.assetRecipient, address(taxSplit), "creator share goes to the trading-tax splitter");
        assertEq(qt.routeId, bytes32(0), "no conversion: paid in ETH");
        assertEq(Currency.unwrap(qt.quote), address(0), "quote is ETH");
        assertEq(qt.protocolShareBps, SHARE_BPS, "Hookr takes 20% of the tax");
        assertEq(qt.protocolRecipient, IHookrRulesFork(rules).protocolRecipient());
    }

    /// First trade, in the launch block: exactly 1.5% tax + 0.3% LP fee, no surcharge, no Hookr take beyond the tax.
    function test_firstBuy_flatTaxNoSurcharge() public {
        uint256 fdv0 = _fdvWei();
        uint256 protocol0 = _protocolClaim();
        SwapResult memory r = _swap(true, 1 ether);
        uint256 taxTaken = buyQueue.pending();
        assertEq(uint256(uint128(-r.delta.amount0())), 1 ether, "buyer paid exactly 1 ETH");
        assertEq(r.lpFee, BASE, "LP fee 0.3%, no surcharge");
        assertApproxEqAbs(taxTaken, 1 ether * uint256(TAX_PIPS) / PIPS, 10, "1.5% buy tax");
        assertEq(_protocolClaim() - protocol0, 0, "no Hookr rules take on a plain buy");
        assertApproxEqAbs(r.poolIn, 1 ether - taxTaken, 10, "pool swapped the rest");
        uint256 fdv1 = _fdvWei();
        console2.log("MILORD received for 1 ETH", uint256(uint128(r.delta.amount1())));
        console2.log("FDV after first 1 ETH buy (wei)", fdv1);
        console2.log("price move (bps)", (fdv1 - fdv0) * 10_000 / fdv0);
    }

    /// Sell: 1.5% of the ETH leaving the pool, LP fee 0.3% in MILORD, no Hookr rules take.
    function test_sell_flatTax() public {
        _swap(true, 2 ether);
        vm.roll(block.number + 1);
        vm.warp(block.timestamp + 12);
        uint256 protocol0 = _protocolClaim();
        SwapResult memory r = _swap(false, milord.balanceOf(address(this)) / 2);
        uint256 taxTaken = sellQueue.pending();
        uint256 received = uint256(uint128(r.delta.amount0()));
        assertEq(r.lpFee, BASE, "LP fee 0.3%");
        assertApproxEqAbs(taxTaken, r.poolOut * TAX_PIPS / PIPS, 10, "1.5% of the gross ETH out");
        assertApproxEqAbs(received + taxTaken, r.poolOut, 10, "seller nets gross minus tax");
        assertEq(_protocolClaim() - protocol0, 0, "no Hookr rules take on a sell");
        console2.log("sell gross ETH / tax / net", r.poolOut, taxTaken, received);
    }

    /// Exact-output buys are allowed without a guard and pay the same 1.5% of the gross spend.
    function test_exactOutputBuy_flatTax() public {
        SwapResult memory r = _buyExactOut(1_000_000 ether);
        uint256 spent = uint256(uint128(-r.delta.amount0()));
        assertEq(uint256(uint128(r.delta.amount1())), 1_000_000 ether, "received exactly 1M MILORD");
        assertEq(r.lpFee, BASE);
        assertApproxEqAbs(buyQueue.pending(), spent * TAX_PIPS / PIPS, 10, "1.5% of the gross spend");
    }

    /// Where the tax actually goes: the queues pay 80% to our splitter (thirds: buyback-burn, Team, Arbitrage) and 20%
    /// to Hookr's protocol destination. Both payouts are permissionless.
    function test_taxPayout_recipients() public {
        _swap(true, 3 ether);
        vm.roll(block.number + 1);
        _swap(false, milord.balanceOf(address(this)) / 3);
        vm.roll(block.number + 1);

        uint256 buyTax = buyQueue.pending();
        uint256 sellTax = sellQueue.pending();
        address protocolTo = _protocolDestination();
        uint256 p0 = protocolTo.balance;

        vm.startPrank(makeAddr("anyone"));
        uint256 c1 = buyQueue.payCreator();
        uint256 c2 = sellQueue.payCreator();
        uint256 h1 = buyQueue.sweepProtocol();
        uint256 h2 = sellQueue.sweepProtocol();
        vm.stopPrank();

        assertEq(c1 + h1, buyTax, "buy tax fully paid out");
        assertEq(c2 + h2, sellTax, "sell tax fully paid out");
        assertEq(h1, buyTax * SHARE_BPS / 10_000, "Hookr 20% of buy tax (rounded down)");
        assertEq(h2, sellTax * SHARE_BPS / 10_000, "Hookr 20% of sell tax (rounded down)");
        assertEq(address(taxSplit).balance, c1 + c2, "80% reached the trading-tax splitter as ETH");
        assertEq(protocolTo.balance - p0, h1 + h2, "20% reached Hookr's protocol destination");

        uint256 burnerBefore = burner.totalEthReceived();
        taxSplit.distribute(address(0));
        uint256 total = c1 + c2;
        assertEq(burner.totalEthReceived() - burnerBefore, total * 3334 / 10_000, "1/3 to buyback-burn");
        assertEq(team.balance, total * 3333 / 10_000, "1/3 to Team Safe");
        assertEq(arbSafe.balance, total - total * 3334 / 10_000 - total * 3333 / 10_000, "1/3 to Arbitrage Funding Safe");
        console2.log("buy tax / sell tax", buyTax, sellTax);
        console2.log("to splitter / to Hookr", c1 + c2, h1 + h2);
        console2.log("Hookr protocol destination", protocolTo);
    }

    /// Launch-order risk: anyone may open the ETH/MILORD Hookr pool first with an ETH-only position (no MILORD
    /// needed), at any price. Our launch then reverts PoolExists. Mainnet mitigation: deploy MILORD and launch in one
    /// transaction (Safe MultiSend: CREATE2-deploy the token, approve, launchFamily).
    function test_poolSquattingRisk() public {
        _deployMilord(); // a fresh, not yet launched MILORD
        address squatter = makeAddr("squatter");
        vm.deal(squatter, 1 ether);
        IHookrLauncher.Member[] memory m = new IHookrLauncher.Member[](1);
        m[0].quote = ETH;
        m[0].tickSpacing = TICK_SPACING;
        m[0].sqrtPriceX96 = TickMath.getSqrtPriceAtTick(0); // a junk price
        m[0].tickLower = 600;
        m[0].tickUpper = 1200; // above the price: ETH only
        m[0].liquidity = 1e15;
        m[0].amount0Max = 0.1 ether;
        m[0].amount1Max = 0;
        m[0].config.rules = rules;
        m[0].config.baseLpFeePips = BASE;
        m[0].config.caps = registry.admission(ROOT, rules).caps;
        m[0].config.rulesGasLimit = registry.admission(ROOT, rules).gasLimit;
        m[0].rules.maxFeePips = BASE;
        m[0].rules.protocolShareBps = SHARE_BPS;
        IHookrLauncher.LaunchParams memory p;
        p.token = IHookrLauncher.Token(address(milord), "", "", 0, bytes32(0));
        p.root = ROOT;
        p.members = m;
        p.knobs = new IHookrLauncher.MemberKnobs[](1);
        p.advisoryConfigs = new bytes[](1);
        p.maxRetainedBps = 10_000;
        p.deadline = block.timestamp + 1 hours;
        uint256 fee = launchFeePaid;
        vm.prank(squatter);
        try LAUNCHER.launchFamily{value: 0.1 ether + fee}(p) {
            console2.log("RISK CONFIRMED: an outsider opened the ETH/MILORD pool with 0 MILORD");
            vm.expectRevert();
            this.launchApproved();
        } catch (bytes memory err) {
            console2.log("outsider pre-emption refused by the launcher");
            console2.logBytes(err);
        }
    }

    function launchApproved() external {
        require(msg.sender == address(this));
        _approvedLaunch();
    }

    function _protocolDestination() internal view returns (address) {
        address r = IHookrRulesFork(rules).protocolRecipient();
        if (r.code.length == 0) return r;
        return IHookrTreasuryFork(r).target();
    }
}
