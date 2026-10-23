// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Test-only 18-decimal stablecoin, used to prove the vault rejects non-6-decimal assets.
contract Mock18DecimalStable is ERC20 {
    constructor() ERC20("Mock 18", "M18") {}
}
