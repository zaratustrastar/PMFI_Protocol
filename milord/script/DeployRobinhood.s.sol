// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {MilordDeployer} from "./MilordDeployer.sol";

/// @notice Robinhood Chain deployment from environment configuration.
///
///   TESTNET (46630) only in this version. Mainnet (4663) is refused until the owner approves
///   a mainnet deployment and the audit is done; see docs/MAINNET_RUNBOOK.md.
///
///   Dry run (no broadcast) against a testnet RPC:
///     forge script script/DeployRobinhood.s.sol --rpc-url $ROBINHOOD_TESTNET_RPC --sender $DEPLOYER
///
///   Required env: MILORD_OWNER_SAFE, MILORD_TEAM_SAFE, MILORD_ARB_SAFE, MILORD_SERVICER,
///   MILORD_QUOTE_SIGNER, MILORD_REPORT_SIGNER, MILORD_OPERATOR, MILORD_TOKEN_HOLDER,
///   MILORD_VAULT_ASSET (USDG on the target chain), MILORD_SWAP_ADAPTER, MILORD_BASE_URI,
///   MILORD_CHARACTERISTICS (8 comma-separated labels), MILORD_VAULT_CAP (asset units).
///   No private keys are read from env; sign with --ledger / --account.
contract DeployRobinhood is Script, MilordDeployer {
    uint256 constant ROBINHOOD_TESTNET = 46630;
    uint256 constant ROBINHOOD_MAINNET = 4663;

    function run() external returns (Deployment memory d) {
        require(block.chainid != ROBINHOOD_MAINNET, "mainnet deployment not approved");
        require(block.chainid == ROBINHOOD_TESTNET, "Robinhood Chain testnet only");

        Config memory c;
        c.owner = vm.envAddress("MILORD_OWNER_SAFE");
        c.teamSafe = vm.envAddress("MILORD_TEAM_SAFE");
        c.arbitrageSafe = vm.envAddress("MILORD_ARB_SAFE");
        c.servicer = vm.envAddress("MILORD_SERVICER");
        c.quoteSigner = vm.envAddress("MILORD_QUOTE_SIGNER");
        c.reportSigner = vm.envAddress("MILORD_REPORT_SIGNER");
        c.operator = vm.envAddress("MILORD_OPERATOR");
        c.tokenHolder = vm.envAddress("MILORD_TOKEN_HOLDER");
        c.asset = vm.envAddress("MILORD_VAULT_ASSET");
        c.swapAdapter = vm.envAddress("MILORD_SWAP_ADAPTER");
        c.baseURI = vm.envString("MILORD_BASE_URI");
        c.characteristics = vm.envString("MILORD_CHARACTERISTICS", ",");
        c.vaultCap = vm.envUint("MILORD_VAULT_CAP");
        c.rootActivationDelay = 1 days;
        // proposed production splitter bounds (docs/OPEN_DECISIONS.md P2)
        c.splitterMin = [uint16(6000), 4000, 2500];
        c.splitterMax = [uint16(8000), 6000, 4000];

        console2.log("vault asset", c.asset, IERC20Metadata(c.asset).symbol());
        vm.startBroadcast();
        (, c.deployer,) = vm.readCallers(); // the broadcasting account
        d = _deployMilord(c);
        vm.stopBroadcast();
    }
}
