// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Test-only stand-in for Paxos USDG: 6 decimals, plus the issuer controls the real
///         token has (pause all transfers, freeze an address). Used to test how the vault and
///         distributor behave when the issuer exercises them.
contract MockUSDG is ERC20 {
    bool public paused;
    mapping(address => bool) public isFrozen;

    constructor() ERC20("Global Dollar (mock)", "USDG") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    function setPaused(bool p) external {
        paused = p;
    }

    function setFrozen(address who, bool f) external {
        isFrozen[who] = f;
    }

    function _update(address from, address to, uint256 value) internal override {
        require(!paused, "USDG: paused");
        require(!isFrozen[from] && !isFrozen[to], "USDG: frozen");
        super._update(from, to, value);
    }
}
