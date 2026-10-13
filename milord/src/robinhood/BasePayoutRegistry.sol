// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title BasePayoutRegistry
/// @notice Lets a Robinhood Chain holder name the Base address that should receive their
///         pARB allocations. Needed when the holder is a smart wallet (e.g. a Safe) whose
///         address is not controlled on Base. Default (unset) = the same address.
///         Changes apply to allocations computed after the change; already published
///         allocations are never moved.
contract BasePayoutRegistry {
    mapping(address holder => address) internal _payout;

    event PayoutAddressSet(address indexed holder, address indexed baseRecipient);

    function setPayoutAddress(address baseRecipient) external {
        _payout[msg.sender] = baseRecipient; // zero resets to default
        emit PayoutAddressSet(msg.sender, baseRecipient);
    }

    function payoutAddress(address holder) external view returns (address) {
        address p = _payout[holder];
        return p == address(0) ? holder : p;
    }
}
