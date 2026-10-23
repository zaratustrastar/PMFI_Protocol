// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @notice Test-only swap venue: pays `tokensPerEth` (scaled 1e18) from its own balance.
contract MockSwapAdapter {
    uint256 public tokensPerEth;

    constructor(uint256 tokensPerEth_) {
        tokensPerEth = tokensPerEth_;
    }

    function setRate(uint256 r) external {
        tokensPerEth = r;
    }

    function swapExactETHForToken(address tokenOut, uint256 minOut, address recipient, uint256 deadline)
        external
        payable
        returns (uint256 out)
    {
        require(block.timestamp <= deadline, "deadline");
        out = (msg.value * tokensPerEth) / 1e18;
        require(out >= minOut, "slippage");
        IERC20(tokenOut).transfer(recipient, out);
    }
}
