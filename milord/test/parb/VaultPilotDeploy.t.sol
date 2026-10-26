// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PMFIArbVaultV3} from "../../src/parb/PMFIArbVaultV3.sol";
import {MockUSDG} from "../../src/mocks/MockUSDG.sol";
import {DeployVaultPilot} from "../../script/DeployVaultPilot.s.sol";

/// Vault-only Robinhood pilot: deploys PMFIArbVaultV3 and nothing else, restricted to named depositors, owned by the
/// Safe, and refused on 4663 without the pilot approval hash.
contract PilotHarness is DeployVaultPilot {
    function deployWith(PilotConfig memory c) external returns (PMFIArbVaultV3) {
        return _deploy(c);
    }
}

contract VaultPilotDeployTest is Test {
    address internal constant MAINNET_USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address safe = makeAddr("ownerSafe");
    address teamSafe = makeAddr("teamSafe");
    address servicer = makeAddr("servicer");
    uint256 signerKey = 0xA11CE;
    address reportSigner = vm.addr(0xA11CE);
    address depositor = makeAddr("bobo");
    MockUSDG usdg;

    function setUp() public {
        vm.chainId(46630);
        usdg = new MockUSDG();
        vm.setEnv("MILORD_OWNER_SAFE", vm.toString(safe));
        vm.setEnv("MILORD_TEAM_SAFE", vm.toString(teamSafe));
        vm.setEnv("MILORD_SERVICER", vm.toString(servicer));
        vm.setEnv("MILORD_REPORT_SIGNER", vm.toString(reportSigner));
        vm.setEnv("MILORD_VAULT_ASSET", vm.toString(address(usdg)));
        vm.setEnv("MILORD_VAULT_CAP", "1000000000");
        vm.setEnv("MILORD_PILOT_DEPOSITORS", vm.toString(depositor));
        vm.setEnv("MILORD_DEPLOYMENTS_OUT", "./deployments/test-robinhood-pilot.json");
        vm.setEnv("MILORD_LOCAL_FORK", "no");
        vm.setEnv("MILORD_PILOT_APPROVAL", vm.toString(bytes32(0)));
    }

    function test_deploysOnlyTheVault_restrictedAndOwnedBySafe() public {
        DeployVaultPilot s = new DeployVaultPilot();
        PMFIArbVaultV3 v = s.run();
        assertEq(address(v.usdc()), address(usdg));
        assertEq(v.owner(), safe);
        assertEq(v.feeRecipient(), teamSafe);
        assertEq(v.arbServicerWallet(), servicer);
        assertEq(v.reportSigner(), reportSigner);
        assertEq(v.maxTotalDeposits(), 1000e6);
        assertEq(v.perfFeeBps(), 0);
        assertEq(v.officialPPS(), 1e6);
        assertTrue(v.depositorRestricted());
        assertTrue(v.isAllowedDepositor(depositor));
        assertFalse(v.isAllowedDepositor(makeAddr("stranger")));
        string memory json = vm.readFile("./deployments/test-robinhood-pilot.json");
        assertEq(vm.parseJsonAddress(json, ".pARBVault"), address(v));
        assertEq(vm.parseJsonString(json, ".domainSalt"), "PMFIArbVaultV3.milord.v1");
    }

    function test_strangerCannotDeposit_depositorCan() public {
        PMFIArbVaultV3 v = new DeployVaultPilot().run();
        usdg.mint(makeAddr("stranger"), 10e6);
        vm.startPrank(makeAddr("stranger"));
        usdg.approve(address(v), 10e6);
        vm.expectRevert();
        v.requestDeposit(10e6, makeAddr("stranger"));
        vm.stopPrank();
        usdg.mint(depositor, 10e6);
        vm.startPrank(depositor);
        usdg.approve(address(v), 10e6);
        v.requestDeposit(10e6, depositor);
        vm.stopPrank();
    }

    function test_reportSignedForBaseIsRejected() public {
        PMFIArbVaultV3 v = new DeployVaultPilot().run();
        vm.warp(block.timestamp + 2 hours); // past the report cooldown
        PMFIArbVaultV3.ReportData memory data = PMFIArbVaultV3.ReportData(0, block.timestamp, block.timestamp + 600, 1);
        vm.chainId(8453);
        bytes32 baseHash = _digest(v, data);
        vm.chainId(46630);
        (uint8 bv, bytes32 br, bytes32 bs) = vm.sign(signerKey, baseHash);
        vm.expectRevert(bytes("Invalid signer"));
        v.report(data, abi.encodePacked(br, bs, bv), 0, 0);
        (uint8 rv, bytes32 rr, bytes32 rs) = vm.sign(signerKey, _digest(v, data));
        v.report(data, abi.encodePacked(rr, rs, rv), 0, 0);
    }

    function _digest(PMFIArbVaultV3 v, PMFIArbVaultV3.ReportData memory data) internal view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", v.reportStructHash(data)));
    }

    function test_refusesOtherChains() public {
        DeployVaultPilot s = new DeployVaultPilot();
        vm.chainId(8453);
        vm.expectRevert(bytes("Robinhood Chain (46630 / 4663) only"));
        s.run();
    }

    // Refusal cases use a config struct instead of env: forge runs tests in parallel and env is process-wide.
    function test_mainnetNeedsPilotApprovalHash() public {
        PilotHarness h = new PilotHarness();
        DeployVaultPilot.PilotConfig memory c = h.readConfig();
        vm.etch(MAINNET_USDG, address(usdg).code);
        c.asset = MAINNET_USDG;
        vm.chainId(4663);
        vm.expectRevert(bytes("vault pilot not approved for this configuration hash"));
        h.deployWith(c);
    }

    function test_refusesUnrestrictedOrSharedKeys() public {
        PilotHarness h = new PilotHarness();
        DeployVaultPilot.PilotConfig memory c = h.readConfig();
        c.servicer = reportSigner;
        vm.expectRevert(bytes("servicer and report signer need separate keys"));
        h.deployWith(c);
        c = h.readConfig();
        c.cap = 0;
        vm.expectRevert(bytes("MILORD_VAULT_CAP must be > 0"));
        h.deployWith(c);
        c = h.readConfig();
        c.depositors = new address[](0);
        vm.expectRevert(bytes("MILORD_PILOT_DEPOSITORS: the pilot vault is restricted to named depositors"));
        h.deployWith(c);
        c = h.readConfig();
        c.owner = servicer;
        vm.expectRevert(bytes("owner must not be an engine key"));
        h.deployWith(c);
    }
}
