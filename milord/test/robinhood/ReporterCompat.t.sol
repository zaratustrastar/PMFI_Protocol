// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {MockUSDG} from "../../src/mocks/MockUSDG.sol";

/// The existing Python reporter (bot/arb_monitor/core/arb_reporter.py), configured only through
/// env vars for Robinhood Chain, produces signatures the V3 vault accepts.
/// Fixture: python3 milord/offchain/gen_reporter_fixture.py
contract ReporterCompatTest is Test {
    function test_pythonReporterSignatureAcceptedOnRobinhood() public {
        string memory j = vm.readFile("test/fixtures/reporter_v3_robinhood.json");
        address vaultAddr = vm.parseJsonAddress(j, ".vault");
        vm.chainId(vm.parseJsonUint(j, ".chainId"));
        vm.warp(vm.parseJsonUint(j, ".timestamp") + 10);
        MockUSDG usdg = new MockUSDG();
        deployCodeTo(
            "PMFIArbVaultV3.sol:PMFIArbVaultV3",
            abi.encode(
                "PMFI Arb (MILORD)",
                "pARB",
                address(usdg),
                vm.parseJsonAddress(j, ".signer"),
                address(1),
                address(2),
                1e12,
                uint256(0)
            ),
            vaultAddr
        );
        PMFIArbVaultV3 vault = PMFIArbVaultV3(vaultAddr);
        assertEq(keccak256(bytes(vm.parseJsonString(j, ".salt"))), vault.DOMAIN_SALT());
        PMFIArbVaultV3.ReportData memory data = PMFIArbVaultV3.ReportData(
            vm.parseJsonUint(j, ".reportedAssets"),
            vm.parseJsonUint(j, ".timestamp"),
            vm.parseJsonUint(j, ".deadline"),
            vm.parseJsonUint(j, ".nonce")
        );
        vault.report(data, vm.parseJsonBytes(j, ".signature"), 0, 0);
        assertEq(vault.lastReportNonce(), 1);
    }
}
