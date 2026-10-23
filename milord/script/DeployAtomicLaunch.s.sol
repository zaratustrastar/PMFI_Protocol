// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {MilordAtomicLaunch} from "../src/launch/MilordAtomicLaunch.sol";
import {IHookrLauncher} from "../src/vendor/hookr/interfaces/IHookrLauncher.sol";

/// @notice Step 1 of the atomic launch (docs/launch/ATOMIC_LAUNCH.md): deploys MilordAtomicLaunch with the approved,
///         flat-launch configuration and prints the predicted MILORD address that DeployRobinhood.s.sol needs as
///         MILORD_PREDICTED_TOKEN. Deploying the helper creates no token and moves no funds; the launch itself is the
///         Team Safe's transaction (offchain/safe-launch-batch.mjs) and is NOT executed here.
///
///         Hookr-1 exists only on Robinhood mainnet (4663). Private fork: MILORD_LOCAL_FORK=yes AND the public Anvil
///         account #0 as the broadcaster. Mainnet: refused unless MILORD_MAINNET_APPROVAL equals the configuration hash
///         a dry run prints, and never with the Anvil key. Mainnet deployment is not approved yet.
///
///   Env: MILORD_TEAM_SAFE (owner), MILORD_SALT (bytes32, optional), MILORD_MAX_LAUNCH_FEE (wei, default 0.01 ETH),
///        MILORD_LOCK_BLOCKS (parent blocks, default 7200). Hookr addresses default to the verified Hookr-1 set
///        (HOOKR_LAUNCHER, HOOKR_ROOT, HOOKR_RULES, HOOKR_DIRECTIONAL_TAX override them).
///        Anti-Snipe stays OFF (unapproved candidate): MILORD_SNIPE_PIPS/SECONDS/GUARD_BLOCKS are refused unless 0.
contract DeployAtomicLaunch is Script {
    address internal constant ANVIL_0 = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;

    function run() external returns (MilordAtomicLaunch helper) {
        bool fork = keccak256(bytes(vm.envOr("MILORD_LOCAL_FORK", string("no")))) == keccak256("yes");
        if (block.chainid == 4663 && fork) {
            require(msg.sender == ANVIL_0, "local fork: broadcast with the public Anvil account #0 only");
        } else if (block.chainid != 4663) {
            require(block.chainid == 31337, "local Anvil (fork of 4663, or 31337) only");
        }
        require(
            vm.envOr("MILORD_SNIPE_PIPS", uint256(0)) == 0 && vm.envOr("MILORD_SNIPE_SECONDS", uint256(0)) == 0
                && vm.envOr("MILORD_GUARD_BLOCKS", uint256(0)) == 0,
            "Anti-Snipe is not approved: flat launch only"
        );
        MilordAtomicLaunch.Config memory c;
        c.launcher = IHookrLauncher(vm.envOr("HOOKR_LAUNCHER", 0x1cf2748A6670572425eAbf9Ab9e5BC4eb2eB8Ef4));
        c.root = vm.envOr("HOOKR_ROOT", 0x89c9FB50d1f03192230FFB1c279BB6E7d3Fe6aCc);
        c.rules = vm.envOr("HOOKR_RULES", 0x4a71c270b1520F94064268102AebD744b0DE7942);
        c.tax = vm.envOr("HOOKR_DIRECTIONAL_TAX", 0xE73fAD0d773b9571a7a9d2A1edc01132fF5f1e1F); // A; never HookrPartnerTax
        c.owner = vm.envAddress("MILORD_TEAM_SAFE");
        c.salt = vm.envOr("MILORD_SALT", keccak256("MILORD"));
        c.maxLaunchFee = vm.envOr("MILORD_MAX_LAUNCH_FEE", uint256(0.01 ether));
        c.lockBlocks = uint32(vm.envOr("MILORD_LOCK_BLOCKS", uint256(7200)));
        require(c.owner.code.length > 0 || block.chainid == 31337, "MILORD_TEAM_SAFE must be a deployed Safe");
        bytes32 h = keccak256(abi.encode(block.chainid, c));
        console2.log("MilordAtomicLaunch configuration hash:");
        console2.logBytes32(h);
        if (block.chainid == 4663 && !fork) {
            // Mainnet: only with the owner's approval of exactly this configuration (dry run prints the hash).
            require(vm.envOr("MILORD_MAINNET_APPROVAL", bytes32(0)) == h, "mainnet deployment not approved for this configuration hash");
            require(msg.sender != ANVIL_0, "mainnet: never the public Anvil key");
        }

        vm.startBroadcast();
        helper = new MilordAtomicLaunch(c);
        vm.stopBroadcast();

        address predicted = helper.predictedToken();
        require(predicted.code.length == 0, "predicted MILORD address already has code");
        console2.log("MilordAtomicLaunch", address(helper));
        console2.log("MILORD_PREDICTED_TOKEN", predicted);
        console2.log("current Hookr launch fee (wei)", helper.launchFee());
    }
}
