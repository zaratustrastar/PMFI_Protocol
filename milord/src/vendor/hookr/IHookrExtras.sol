// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {HookrTypes} from "./types/HookrTypes.sol";

// Hand-written subsets of Hookr-1 contracts the fork suite reads, from hookr-contracts@7e771b3 (hookr-1/src/core/
// HookrRules.sol, hook-blocks/directional-tax/src/HookrDirectionalTax.sol and HookrTaxQueue.sol).

interface IHookrRulesFork {
    function configSchemaHash() external pure returns (bytes32);
    function protocolRecipient() external view returns (address);
    function claimable(Currency currency, address beneficiary) external view returns (uint256);
    function config(PoolId id) external view returns (HookrTypes.RulesConfig memory);
    function minimumFee(PoolId id) external view returns (uint16);
    function protocolShareFloor() external view returns (uint16);
    function removalLockedUntil(PoolId id) external view returns (uint256 untilBlock);
}

interface IHookrDirectionalTaxFork {
    struct Terms {
        address buyQueue;
        uint24 buyTaxPips;
        bool bound;
        bool tiered;
        bool decaying;
        address sellQueue;
        uint24 sellTaxPips;
    }

    function terms(address root, PoolId id) external view returns (Terms memory);
    function taxAt(address root, PoolId id, bool isBuy, uint256 blockNumber) external view returns (uint24);
    function minProtocolShareBps() external view returns (uint16);
    function MAX_TAX_PIPS() external view returns (uint24);
}

interface IHookrTaxQueueFork {
    struct QueueTerms {
        address root;
        address rules;
        PoolId poolId;
        Currency quote;
        bool isBuy;
        address protocolRecipient;
        uint16 protocolShareBps;
        bytes32 routeId;
        address assetRecipient;
        address recoveryRecipient;
        uint32 staleRecoveryDelay;
    }

    struct Ledger {
        uint256 protocolOwed;
        uint256 creatorOwed;
        uint256 income;
        uint256 protocolIncome;
        uint256 creatorIncome;
        uint256 protocolSwept;
        uint256 creatorConverted;
        uint256 creatorPaid;
        uint256 outputDelivered;
    }

    function terms() external view returns (QueueTerms memory);
    function ledger() external view returns (Ledger memory);
    function pending() external view returns (uint256);
    function settle() external returns (uint256 income);
    function payCreator() external returns (uint256 amount);
    function sweepProtocol() external returns (uint256 amount);
}

/// Treasury view used to find where Hookr's protocol share is actually paid.
interface IHookrTreasuryFork {
    function target() external view returns (address);
}
