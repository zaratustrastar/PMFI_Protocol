// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {ParbFixture} from "../utils/ParbFixture.sol";
import {Merkle} from "../utils/Merkle.sol";
import {MockUSDC} from "../../src/mocks/MockUSDC.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {ParbFundingAdapter} from "../../src/parb/ParbFundingAdapter.sol";
import {ShareDistributor} from "../../src/parb/ShareDistributor.sol";

/// Drives funding, honest reports with P&L, weekly cumulative roots and claims.
contract DistHandler is Test {
    ParbFixtureView f;
    MockUSDC usdc;
    PMFIArbVaultV3 vault;
    ParbFundingAdapter adapter;
    ShareDistributor dist;
    address op;
    address publisher;

    address[3] public holders = [address(0xA1), address(0xB2), address(0xC3)];
    uint256[3] public cumulative;
    uint256 public allocatedTotal;
    uint64 public epoch;
    uint256 refs;
    uint256[] pendingBatches;
    bytes32[] lastLeaves;

    constructor(ParbFixtureView f_) {
        f = f_;
        (usdc, vault, adapter, dist, op, publisher) = f.parts();
    }

    function fund(uint256 amount) external {
        amount = bound(amount, 10e6, 50_000e6);
        usdc.mint(address(adapter), amount);
        vm.prank(op);
        pendingBatches.push(adapter.fund(keccak256(abi.encode(++refs)), amount));
    }

    function report(int256 pnlBps) external {
        pnlBps = bound(pnlBps, -500, 500);
        f.reportWithPnl(pnlBps);
        for (uint256 i; i < pendingBatches.length; ++i) {
            if (adapter.batch(pendingBatches[i]).settledAt == 0) {
                (,,,, PMFIArbVaultV3.RequestStatus st,) =
                    vault.depositRequests(adapter.batch(pendingBatches[i]).requestId);
                if (st == PMFIArbVaultV3.RequestStatus.CLAIMABLE) adapter.settle(pendingBatches[i]);
            }
        }
    }

    /// Allocate everything currently unallocated across holders with arbitrary weights.
    function publish(uint256 w0, uint256 w1, uint256 w2) external {
        uint256 avail = vault.balanceOf(address(dist)) + dist.totalClaimed() - allocatedTotal;
        if (avail == 0) return;
        w0 = bound(w0, 0, 100);
        w1 = bound(w1, 0, 100);
        w2 = bound(w2, 1, 100);
        uint256 sum = w0 + w1 + w2;
        uint256 a0 = (avail * w0) / sum;
        uint256 a1 = (avail * w1) / sum;
        uint256 a2 = (avail * w2) / sum; // floor; dust carries forward
        cumulative[0] += a0;
        cumulative[1] += a1;
        cumulative[2] += a2;
        allocatedTotal += a0 + a1 + a2;
        delete lastLeaves;
        for (uint256 i; i < 3; ++i) {
            lastLeaves.push(Merkle.leaf(holders[i], cumulative[i]));
        }
        vm.prank(publisher);
        dist.proposeRoot(Merkle.root(lastLeaves), allocatedTotal, ++epoch, 0);
        vm.warp(vm.getBlockTimestamp() + 1 days);
        dist.activateRoot();
    }

    function claim(uint256 who) external {
        who = who % 3;
        if (lastLeaves.length == 0) return;
        if (cumulative[who] <= dist.claimed(holders[who])) return;
        dist.claim(holders[who], cumulative[who], Merkle.proof(lastLeaves, who));
    }
}

interface ParbFixtureView {
    function parts()
        external
        view
        returns (MockUSDC, PMFIArbVaultV3, ParbFundingAdapter, ShareDistributor, address, address);
    function reportWithPnl(int256 bps) external;
}

contract DistributionInvariantTest is ParbFixture, ParbFixtureView {
    DistHandler h;

    function setUp() public override {
        super.setUp();
        vm.startPrank(owner);
        vault.setMaxPpsMoveBps(5000);
        vault.setMinimumIdleAmount(0);
        vm.stopPrank();
        h = new DistHandler(this);
        targetContract(address(h));
    }

    function parts()
        external
        view
        returns (MockUSDC, PMFIArbVaultV3, ParbFundingAdapter, ShareDistributor, address, address)
    {
        return (usdc, vault, adapter, dist, fundingOperator, publisher);
    }

    function reportWithPnl(int256 bps) external {
        if (offchain > 0) {
            int256 d = (int256(offchain) * bps) / 10000;
            _pnl(d);
        }
        vm.warp(vm.getBlockTimestamp() + 1 hours);
        _honestReport();
        uint256 before = usdc.balanceOf(servicer);
        vault.tend();
        offchain += usdc.balanceOf(servicer) - before;
    }

    /// The distributor can always pay every outstanding allocation.
    function invariant_distributorSolvent() public view {
        (, uint256 total,,,) = dist.active();
        assertGe(vault.balanceOf(address(dist)) + dist.totalClaimed(), total);
        assertLe(dist.totalClaimed(), total);
    }

    /// Shares minted to the distributor equal shares the adapter recorded (no double counting).
    function invariant_sharesTrackedOnce() public view {
        assertEq(vault.balanceOf(address(dist)) + dist.totalClaimed(), adapter.totalSharesMinted());
    }

    /// Claims never exceed a holder's cumulative allocation.
    function invariant_claimsBounded() public view {
        for (uint256 i; i < 3; ++i) {
            assertLe(dist.claimed(h.holders(i)), h.cumulative(i));
        }
    }

    /// Vault USDC always covers money that is not the vault's to deploy.
    function invariant_vaultReservesCovered() public view {
        assertGe(usdc.balanceOf(address(vault)), vault.totalPendingDepositAssets() + vault.totalClaimableRedeemAssets());
    }

    /// Unclaimed-deposit share counter matches the CLAIMABLE requests.
    function invariant_unclaimedShareCounter() public view {
        uint256 sum;
        uint256 n = vault.depositRequestCount();
        for (uint256 i; i < n; ++i) {
            (,, uint256 assets,, PMFIArbVaultV3.RequestStatus st, uint256 pps) = vault.depositRequests(i);
            if (st == PMFIArbVaultV3.RequestStatus.CLAIMABLE) sum += (assets * 1e18) / pps;
        }
        assertEq(vault.totalUnclaimedDepositShares(), sum);
    }

    /// PPS equals net backing / outstanding shares (within rounding) after every report.
    function invariant_ppsConsistent() public view {
        uint256 outstanding = vault.circulatingSupply() + vault.totalUnclaimedDepositShares();
        if (outstanding == 0 || vault.lastReportNonce() == 0) return;
        uint256 implied = (vault.lastReportedBackingAssets() * 1e18) / outstanding;
        // lastReportedBackingAssets is post-processing; allow 1 unit rounding per share-precision
        assertApproxEqAbs(implied, vault.officialPPS(), 2);
    }
}
