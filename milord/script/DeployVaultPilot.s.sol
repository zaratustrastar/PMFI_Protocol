// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {PMFIArbVaultV3} from "../src/parb/PMFIArbVaultV3.sol";

/// @notice Vault-only deployment of the existing PMFIArbVaultV3 on Robinhood Chain, for the controlled real-capital
///         pilot of the PMFI arbitrage engine (docs/VAULT_PILOT_RUNBOOK.md). It deploys ONE contract, the vault, and
///         nothing else: no MILORD, Hookr, NFTs, splitters, router, adapter or distributor.
///
///   Same vault as the full launch (MilordDeployer): PMFIArbVaultV3 with the hardened V3 accounting, USDG (6 decimals),
///   chain-bound report signatures (ReportDataV2: vault, chainId, DOMAIN_SALT "PMFIArbVaultV3.milord.v1").
///   Pilot differences: deposits are restricted to MILORD_PILOT_DEPOSITORS (the owner's own wallets) and the cap is
///   the pilot amount, both changeable later by the owner Safe.
///
///   Modes:
///   - TESTNET (46630).
///   - PRIVATE FORK of Robinhood mainnet (4663): MILORD_LOCAL_FORK=yes and the public Anvil account #0 as broadcaster.
///   - MAINNET (4663): refused unless MILORD_PILOT_APPROVAL equals this run's configuration hash (printed by a dry run
///     without --broadcast), the broadcaster is not the public Anvil key, the owner is a deployed Safe and the asset is
///     USDG. MILORD_PILOT_APPROVAL is separate from MILORD_MAINNET_APPROVAL so neither approval can authorise the other.
///
///   Dry run:   forge script script/DeployVaultPilot.s.sol --rpc-url $ROBINHOOD_RPC --sender $DEPLOYER
///   Required env: MILORD_OWNER_SAFE (vault owner), MILORD_TEAM_SAFE (fee recipient), MILORD_SERVICER (engine wallet
///   that receives tend() capital), MILORD_REPORT_SIGNER (NAV reporter key), MILORD_VAULT_ASSET (USDG),
///   MILORD_VAULT_CAP (asset units, e.g. 1000e6), MILORD_PILOT_DEPOSITORS (comma-separated addresses).
///   Optional: MILORD_PILOT_PERF_FEE_BPS (0), MILORD_PILOT_NAME ("PMFI Arb (Robinhood)"), MILORD_PILOT_SYMBOL ("pARB"),
///   MILORD_DEPLOYMENTS_OUT (./deployments/robinhood-pilot-<chain>.json).
///   No private keys are read from env; sign with --ledger / --account.
contract DeployVaultPilot is Script {
    uint256 constant ROBINHOOD_TESTNET = 46630;
    uint256 constant ROBINHOOD_MAINNET = 4663;
    address internal constant ANVIL_0 = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;
    address internal constant MAINNET_USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    uint256 internal constant MAX_PILOT_PERF_FEE_BPS = 2000;

    struct PilotConfig {
        address owner;
        address feeRecipient;
        address servicer;
        address reportSigner;
        address asset;
        uint256 cap;
        uint256 perfFeeBps;
        address[] depositors;
        string name;
        string symbol;
    }

    bytes32 public configHash;

    function run() external returns (PMFIArbVaultV3 vault) {
        return _deploy(readConfig());
    }

    function _deploy(PilotConfig memory c) internal returns (PMFIArbVaultV3 vault) {
        _validate(c);
        _gate(c);
        string memory out = manifestPath();
        console2.log("vault asset", c.asset, IERC20Metadata(c.asset).symbol());
        uint256 startBlock = block.number;
        vm.startBroadcast();
        vault = new PMFIArbVaultV3(c.name, c.symbol, c.asset, c.reportSigner, c.feeRecipient, c.servicer, c.cap, c.perfFeeBps);
        vault.setDepositorRestricted(true);
        for (uint256 i; i < c.depositors.length; ++i) vault.setAllowedDepositor(c.depositors[i], true);
        vault.transferOwnership(c.owner); // Ownable (one step), as in MilordDeployer
        vm.stopBroadcast();
        _record(c, vault, startBlock, out);
    }

    function readConfig() public view returns (PilotConfig memory c) {
        c.owner = vm.envAddress("MILORD_OWNER_SAFE");
        c.feeRecipient = vm.envAddress("MILORD_TEAM_SAFE");
        c.servicer = vm.envAddress("MILORD_SERVICER");
        c.reportSigner = vm.envAddress("MILORD_REPORT_SIGNER");
        c.asset = vm.envAddress("MILORD_VAULT_ASSET");
        c.cap = vm.envUint("MILORD_VAULT_CAP");
        c.perfFeeBps = vm.envOr("MILORD_PILOT_PERF_FEE_BPS", uint256(0));
        c.depositors = vm.envAddress("MILORD_PILOT_DEPOSITORS", ",");
        c.name = vm.envOr("MILORD_PILOT_NAME", string("PMFI Arb (Robinhood)"));
        c.symbol = vm.envOr("MILORD_PILOT_SYMBOL", string("pARB"));
    }

    function _validate(PilotConfig memory c) internal view {
        require(block.chainid == ROBINHOOD_TESTNET || block.chainid == ROBINHOOD_MAINNET, "Robinhood Chain (46630 / 4663) only");
        require(c.cap > 0, "MILORD_VAULT_CAP must be > 0");
        require(c.perfFeeBps <= MAX_PILOT_PERF_FEE_BPS, "perf fee above the vault's 20% maximum");
        require(c.depositors.length > 0, "MILORD_PILOT_DEPOSITORS: the pilot vault is restricted to named depositors");
        require(c.servicer != c.reportSigner, "servicer and report signer need separate keys");
        require(c.owner != c.servicer && c.owner != c.reportSigner, "owner must not be an engine key");
        require(IERC20Metadata(c.asset).decimals() == 6, "vault asset must have 6 decimals");
        for (uint256 i; i < c.depositors.length; ++i) require(c.depositors[i] != address(0), "zero depositor");
    }

    function _gate(PilotConfig memory c) internal {
        configHash = keccak256(
            abi.encode(
                "PMFIArbVaultV3.pilot",
                block.chainid,
                keccak256(abi.encode(c.owner, c.feeRecipient, c.servicer, c.reportSigner, c.asset, c.cap, c.perfFeeBps)),
                keccak256(abi.encode(c.depositors, c.name, c.symbol))
            )
        );
        console2.log("vault pilot configuration hash:");
        console2.logBytes32(configHash);
        if (block.chainid != ROBINHOOD_MAINNET) return;
        if (_localFork()) {
            require(msg.sender == ANVIL_0, "local fork: broadcast with the public Anvil account #0 only");
            return;
        }
        require(vm.envOr("MILORD_PILOT_APPROVAL", bytes32(0)) == configHash, "vault pilot not approved for this configuration hash");
        require(msg.sender != ANVIL_0, "mainnet: never the public Anvil key");
        require(c.owner.code.length > 0 && c.feeRecipient.code.length > 0, "mainnet: owner and fee recipient must be deployed Safes");
        require(c.asset == MAINNET_USDG, "mainnet: vault asset must be USDG");
    }

    function _localFork() internal view returns (bool) {
        return keccak256(bytes(vm.envOr("MILORD_LOCAL_FORK", string("no")))) == keccak256("yes");
    }

    /// Same rule as DeployRobinhood.manifestPath: only under ./deployments, never overwritten on mainnet.
    function manifestPath() public returns (string memory out) {
        out = vm.envOr("MILORD_DEPLOYMENTS_OUT", string.concat("./deployments/robinhood-pilot-", vm.toString(block.chainid), ".json"));
        require(
            (vm.indexOf(out, "./deployments/") == 0 || vm.indexOf(out, "deployments/") == 0)
                && vm.indexOf(out, "..") == type(uint256).max,
            "MILORD_DEPLOYMENTS_OUT must be a file under ./deployments (foundry fs_permissions)"
        );
        if (block.chainid == ROBINHOOD_MAINNET && !_localFork()) {
            require(!vm.exists(out), "mainnet: deployments manifest already exists; never overwritten");
        }
    }

    /// The engine reads these (ARB_VAULT_ADDRESS, ARB_VAULT_ASSET, ARB_VAULT_CHAIN_ID, ARB_VAULT_DOMAIN_SALT).
    function _record(PilotConfig memory c, PMFIArbVaultV3 vault, uint256 startBlock, string memory out) internal {
        string memory k = "pilot";
        vm.serializeUint(k, "chainId", block.chainid);
        vm.serializeUint(k, "startBlock", startBlock);
        vm.serializeAddress(k, "pARBVault", address(vault));
        vm.serializeAddress(k, "USDG", c.asset);
        vm.serializeAddress(k, "owner", c.owner);
        vm.serializeAddress(k, "feeRecipient", c.feeRecipient);
        vm.serializeAddress(k, "servicer", c.servicer);
        vm.serializeAddress(k, "reportSigner", c.reportSigner);
        vm.serializeAddress(k, "depositors", c.depositors);
        vm.serializeUint(k, "cap", c.cap);
        vm.serializeUint(k, "perfFeeBps", c.perfFeeBps);
        vm.serializeBytes32(k, "configHash", configHash);
        string memory json = vm.serializeString(k, "domainSalt", "PMFIArbVaultV3.milord.v1");
        vm.writeJson(json, out);
        console2.log("wrote", out);
    }
}
