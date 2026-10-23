// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Governor} from "@openzeppelin/contracts/governance/Governor.sol";
import {GovernorSettings} from "@openzeppelin/contracts/governance/extensions/GovernorSettings.sol";
import {GovernorCountingSimple} from "@openzeppelin/contracts/governance/extensions/GovernorCountingSimple.sol";
import {GovernorVotes} from "@openzeppelin/contracts/governance/extensions/GovernorVotes.sol";
import {
    GovernorVotesQuorumFraction
} from "@openzeppelin/contracts/governance/extensions/GovernorVotesQuorumFraction.sol";
import {IVotes} from "@openzeppelin/contracts/governance/utils/IVotes.sol";

/// @title MilordGovernor
/// @notice Stock OpenZeppelin Governor: one circulating Milord = one vote, converter
///         inventory excluded (handled by MilordsNFT voting units). Timestamp clock.
///
///         V1 recommendation: use it (or Snapshot reading the same IVotes data) for
///         advisory/signalling votes and do NOT make it owner of financial contracts until
///         reviewed. Even as owner it could not reach escrowed principal or distributed
///         pARB, because no contract exposes a path to them.
contract MilordGovernor is
    Governor,
    GovernorSettings,
    GovernorCountingSimple,
    GovernorVotes,
    GovernorVotesQuorumFraction
{
    constructor(IVotes milords)
        Governor("MilordGovernor")
        GovernorSettings(1 days, 5 days, 1) // delay, period (seconds, timestamp clock), threshold 1 NFT
        GovernorVotes(milords)
        GovernorVotesQuorumFraction(10) // 10% of circulating Milords

    {}

    function votingDelay() public view override(Governor, GovernorSettings) returns (uint256) {
        return super.votingDelay();
    }

    function votingPeriod() public view override(Governor, GovernorSettings) returns (uint256) {
        return super.votingPeriod();
    }

    function quorum(uint256 timepoint) public view override(Governor, GovernorVotesQuorumFraction) returns (uint256) {
        return super.quorum(timepoint);
    }

    function proposalThreshold() public view override(Governor, GovernorSettings) returns (uint256) {
        return super.proposalThreshold();
    }
}
