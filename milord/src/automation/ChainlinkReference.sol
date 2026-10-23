// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPriceReference} from "./IPriceReference.sol";

interface IAggregatorV3 {
    function decimals() external view returns (uint8);
    function latestRoundData()
        external
        view
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound);
}

/// @notice ETH/USD push-oracle reference (Chainlink AggregatorV3 interface), expressed in a USD stablecoin with
///         `assetDecimals`. Refuses stale, non-positive or incomplete rounds. Treats the stablecoin as $1, so it is
///         used only as a second, independent check next to the pool TWAP (DualReference), never alone.
contract ChainlinkReference is IPriceReference {
    IAggregatorV3 public immutable feed;
    uint256 public immutable maxAge;
    uint8 public immutable assetDecimals;

    error StalePrice(uint256 updatedAt);
    error BadPrice(int256 answer);

    constructor(IAggregatorV3 feed_, uint256 maxAge_, uint8 assetDecimals_) {
        require(maxAge_ > 0 && maxAge_ <= 1 days, "age");
        feed = feed_;
        maxAge = maxAge_;
        assetDecimals = assetDecimals_;
    }

    function assetPerEth() external view returns (uint256) {
        (uint80 roundId, int256 answer,, uint256 updatedAt, uint80 answeredInRound) = feed.latestRoundData();
        if (answer <= 0) revert BadPrice(answer);
        if (updatedAt == 0 || answeredInRound < roundId || block.timestamp - updatedAt > maxAge) {
            revert StalePrice(updatedAt);
        }
        uint8 fd = feed.decimals();
        uint256 p = uint256(answer);
        return fd >= assetDecimals ? p / 10 ** (fd - assetDecimals) : p * 10 ** (assetDecimals - fd);
    }
}
