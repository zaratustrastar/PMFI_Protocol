// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {IMILORD} from "../interfaces/IMILORD.sol";

/// @notice Hookr-1 HookrFamilyLock (0xfAFEcE64099C1E791C4B46189c277f00c0c7e00b on Robinhood Chain), as published
///         in its verified ABI. Only the lock's beneficiary may call `collect`.
interface IHookrFamilyLock {
    function collect(bytes32 familyId, uint8 member, uint8 claims) external returns (uint256 amount0, uint256 amount1);
    function lockOf(bytes32 familyId) external view returns (address beneficiary, uint64 unlockBlock);
}

interface IBuybackBurnerSink {
    function burnHeldTokens() external returns (uint256 amount);
}

/// @notice Beneficiary of the MILORD launch family in Hookr's official launch lock. Anyone can trigger fee
///         collection; everything collected leaves immediately:
///           - ETH LP fees  -> BuybackBurner (buyback-and-burn budget)
///           - MILORD LP fees -> BuybackBurner, then burned there with `burnHeldTokens` (a true ERC-20 burn)
///         No owner, no configuration, no withdraw path. Hookr's lock only pays fees to its beneficiary, never
///         principal, and this contract can only pass on what it receives. If Hookr pays fees as PoolManager
///         claims (ERC-6909), `redeemClaims` turns them into tokens first.
contract HookrLockFeeCollector is IUnlockCallback {
    IHookrFamilyLock public immutable lock;
    bytes32 public immutable familyId;
    IMILORD public immutable milord;
    address payable public immutable burner;
    IPoolManager public immutable poolManager;

    uint256 public totalEthForwarded;
    uint256 public totalMilordBurned;

    event Collected(uint8 indexed member, uint256 amount0, uint256 amount1);
    event Forwarded(uint256 eth, uint256 milordBurned);
    event ClaimsRedeemed(address indexed currency, uint256 amount);

    error NotPoolManager();

    constructor(IHookrFamilyLock lock_, bytes32 familyId_, IMILORD milord_, address payable burner_, IPoolManager poolManager_) {
        require(address(lock_) != address(0) && address(milord_) != address(0) && burner_ != address(0), "zero");
        lock = lock_;
        familyId = familyId_;
        milord = milord_;
        burner = burner_;
        poolManager = poolManager_;
    }

    receive() external payable {}

    /// @notice Collect the fees of one pool of the family (`member`, 0 for a single-pool launch) and pass them on.
    function collect(uint8 member) external returns (uint256 amount0, uint256 amount1) {
        (amount0, amount1) = lock.collect(familyId, member, 0);
        emit Collected(member, amount0, amount1);
        forward();
    }

    /// @notice Send held ETH to the burner and burn held MILORD through the burner. Permissionless.
    function forward() public returns (uint256 eth, uint256 burned) {
        eth = address(this).balance;
        if (eth > 0) {
            (bool ok,) = burner.call{value: eth}("");
            require(ok, "burner refused ETH");
            totalEthForwarded += eth;
        }
        uint256 m = milord.balanceOf(address(this));
        if (m > 0) {
            require(milord.transfer(burner, m), "transfer");
            burned = IBuybackBurnerSink(burner).burnHeldTokens();
            totalMilordBurned += m;
        }
        if (eth > 0 || m > 0) emit Forwarded(eth, m);
    }

    /// @notice Convert this contract's PoolManager claims (ERC-6909) for `currency` into tokens, then forward.
    function redeemClaims(Currency currency) external {
        uint256 amount = poolManager.balanceOf(address(this), currency.toId());
        if (amount == 0) return;
        poolManager.unlock(abi.encode(currency, amount));
        emit ClaimsRedeemed(Currency.unwrap(currency), amount);
        forward();
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        (Currency currency, uint256 amount) = abi.decode(data, (Currency, uint256));
        poolManager.burn(address(this), currency.toId(), amount);
        poolManager.take(currency, address(this), amount);
        return "";
    }
}
