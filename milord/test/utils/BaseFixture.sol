// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MockUSDC} from "../../src/mocks/MockUSDC.sol";
import {PMFIArbVaultV3} from "../../src/base/PMFIArbVaultV3.sol";
import {ParbFundingAdapter, IParbVault} from "../../src/base/ParbFundingAdapter.sol";
import {ShareDistributor} from "../../src/base/ShareDistributor.sol";

abstract contract BaseFixture is Test {
    uint256 internal reporterPk = 0xB0B;
    address internal reporter;
    address internal owner = makeAddr("baseOwner");
    address internal servicer = makeAddr("arbServicer");
    address internal feeRecipient = makeAddr("perfFeeRecipient");
    address internal fundingOperator = makeAddr("fundingOperator");
    address internal publisher = makeAddr("rootPublisher");

    MockUSDC internal usdc;
    PMFIArbVaultV3 internal vault;
    ParbFundingAdapter internal adapter;
    ShareDistributor internal dist;
    uint256 internal reportNonce;

    function setUp() public virtual {
        vm.warp(1_700_000_000);
        reporter = vm.addr(reporterPk);
        usdc = new MockUSDC();
        vm.prank(owner);
        vault = new PMFIArbVaultV3(
            "PMFI Arb (MILORD)", "pARB", address(usdc), reporter, feeRecipient, servicer, 10_000_000e6, 0
        );
        dist = new ShareDistributor(owner, IERC20(address(vault)), publisher);
        adapter = new ParbFundingAdapter(owner, IERC20(address(usdc)), IParbVault(address(vault)), address(dist));
        vm.startPrank(owner);
        adapter.setOperator(fundingOperator, true);
        vault.setReportCooldown(0);
        vault.setDepositorRestricted(true);
        vault.setAllowedDepositor(address(adapter), true);
        vm.stopPrank();
    }

    function _sigFor(address v, bytes32 salt, PMFIArbVaultV3.ReportData memory d) internal view returns (bytes memory) {
        bytes32 structHash = keccak256(
            abi.encode(
                vault.REPORT_TYPEHASH(), d.reportedAssets, d.timestamp, d.deadline, d.nonce, v, block.chainid, salt
            )
        );
        (uint8 vv, bytes32 r, bytes32 s) = vm.sign(reporterPk, MessageHashUtils.toEthSignedMessageHash(structHash));
        return abi.encodePacked(r, s, vv);
    }

    function _reportData(uint256 assets) internal returns (PMFIArbVaultV3.ReportData memory d) {
        uint256 t = vm.getBlockTimestamp();
        d = PMFIArbVaultV3.ReportData(assets, t, t + 600, ++reportNonce);
    }

    function _report(uint256 assets) internal {
        PMFIArbVaultV3.ReportData memory d = _reportData(assets);
        vault.report(d, _sigFor(address(vault), vault.DOMAIN_SALT(), d), 0, 0);
    }

    /// Off-chain assets (servicer + venues) tracked by the test so reports are honest.
    uint256 internal offchain;

    function _honestReport() internal {
        _report(usdc.balanceOf(address(vault)) + offchain);
    }

    function _tendAll() internal {
        uint256 before = usdc.balanceOf(servicer);
        vm.warp(vm.getBlockTimestamp() + 61);
        vault.tend();
        offchain += usdc.balanceOf(servicer) - before;
    }

    /// Simulate trading result on the off-chain side (profit or loss), in USDC units.
    function _pnl(int256 delta) internal {
        offchain = delta >= 0 ? offchain + uint256(delta) : offchain - uint256(-delta);
    }

    function _fundBatch(bytes32 ref, uint256 amount) internal returns (uint256 batchId) {
        usdc.mint(address(adapter), amount);
        vm.prank(fundingOperator);
        batchId = adapter.fund(ref, amount);
    }
}
