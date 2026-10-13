// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Hashes} from "@openzeppelin/contracts/utils/cryptography/Hashes.sol";

/// Minimal test Merkle tree using OZ commutative keccak (compatible with MerkleProof
/// and with leaves in StandardMerkleTree format).
library Merkle {
    function leaf(address account, uint256 cumulative) internal pure returns (bytes32) {
        return keccak256(bytes.concat(keccak256(abi.encode(account, cumulative))));
    }

    function root(bytes32[] memory leaves) internal pure returns (bytes32) {
        bytes32[] memory level = leaves;
        while (level.length > 1) level = _next(level);
        return level[0];
    }

    function proof(bytes32[] memory leaves, uint256 index) internal pure returns (bytes32[] memory p) {
        bytes32[] memory tmp = new bytes32[](32);
        uint256 n;
        bytes32[] memory level = leaves;
        while (level.length > 1) {
            uint256 sib = index ^ 1;
            if (sib < level.length) tmp[n++] = level[sib];
            level = _next(level);
            index /= 2;
        }
        p = new bytes32[](n);
        for (uint256 i; i < n; ++i) {
            p[i] = tmp[i];
        }
    }

    function _next(bytes32[] memory level) private pure returns (bytes32[] memory up) {
        up = new bytes32[]((level.length + 1) / 2);
        for (uint256 i; i < up.length; ++i) {
            uint256 l = 2 * i;
            up[i] = l + 1 < level.length ? Hashes.commutativeKeccak256(level[l], level[l + 1]) : level[l];
        }
    }
}
