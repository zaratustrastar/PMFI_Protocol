// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";

/// @title MILORDToken
/// @notice Plain OpenZeppelin ERC-20. Fixed 6,767,000,000 supply minted once, no owner, no mint
///         function, no transfer tax, no transfer hooks. Anyone may burn their own tokens,
///         which reduces totalSupply().
contract MILORDToken is ERC20, ERC20Burnable, ERC20Permit {
    uint256 public constant INITIAL_SUPPLY = 6_767_000_000 ether;

    constructor(address initialHolder) ERC20("MILORD", "MILORD") ERC20Permit("MILORD") {
        require(initialHolder != address(0), "MILORD: zero holder");
        _mint(initialHolder, INITIAL_SUPPLY);
    }
}
