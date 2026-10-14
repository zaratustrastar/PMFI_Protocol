// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {Actions} from "@uniswap/v4-periphery/src/libraries/Actions.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

/// @notice LOCAL/TEST STAND-IN for Hookr-1's HookrLauncher family ownership + HookrFamilyLock. It exposes the same
///         lock surface as the verified HookrFamilyLock ABI (`lock`, `lockOf`, `collect`, `release`) over a real
///         Uniswap v4 position held through the PositionManager, so the fee collector and the economics can be
///         tested against real v4 fee accounting. Hookr's own contracts hold the position in production.
///         Behaviour mirrored from Hookr's docs: only the family owner locks; once locked nobody can withdraw or
///         add liquidity; only the beneficiary can `collect` fees; `release` only after the unlock block.
contract MockHookrFamilyLock {
    uint64 public constant FOREVER = type(uint64).max;

    IPositionManager public immutable positionManager;
    address public immutable familyOwner;

    struct Family {
        uint256[] positions; // member index -> PositionManager token id
        address beneficiary;
        uint64 unlockBlock;
    }

    mapping(bytes32 => Family) internal _families;

    event FamilyLocked(bytes32 indexed familyId, address indexed locker, address indexed beneficiary, uint64 unlockBlock);

    error NotBeneficiary(bytes32 familyId, address caller);
    error NotLockable(bytes32 familyId, address caller);
    error InvalidBeneficiary(address beneficiary);
    error StillLocked(bytes32 familyId, uint256 unlockBlock);

    constructor(IPositionManager positionManager_, address familyOwner_) {
        positionManager = positionManager_;
        familyOwner = familyOwner_;
    }

    /// @notice Stand-in for the launcher: record a position this contract already holds as a family member.
    function addMember(bytes32 familyId, uint256 tokenId) external {
        if (msg.sender != familyOwner || _families[familyId].beneficiary != address(0)) revert NotLockable(familyId, msg.sender);
        require(IERC721(address(positionManager)).ownerOf(tokenId) == address(this), "not held");
        _families[familyId].positions.push(tokenId);
    }

    function lock(bytes32 familyId, address beneficiary, uint64 unlockBlock) external {
        Family storage f = _families[familyId];
        if (msg.sender != familyOwner || f.beneficiary != address(0) || f.positions.length == 0) revert NotLockable(familyId, msg.sender);
        if (beneficiary == address(0) || beneficiary == address(this)) revert InvalidBeneficiary(beneficiary);
        require(unlockBlock > block.number, "unlock block");
        f.beneficiary = beneficiary;
        f.unlockBlock = unlockBlock;
        emit FamilyLocked(familyId, msg.sender, beneficiary, unlockBlock);
    }

    function lockOf(bytes32 familyId) external view returns (address beneficiary, uint64 unlockBlock) {
        return (_families[familyId].beneficiary, _families[familyId].unlockBlock);
    }

    function position(bytes32 familyId, uint8 member) external view returns (uint256) {
        return _families[familyId].positions[member];
    }

    /// @notice Fees only: a zero-liquidity decrease collects accrued fees and pays them to the beneficiary.
    function collect(bytes32 familyId, uint8 member, uint8 claims) external returns (uint256 amount0, uint256 amount1) {
        Family storage f = _families[familyId];
        if (msg.sender != f.beneficiary) revert NotBeneficiary(familyId, msg.sender);
        require(claims == 0, "mock: claims payout not modelled");
        uint256 tokenId = f.positions[member];
        (PoolKey memory key,) = positionManager.getPoolAndPositionInfo(tokenId);
        uint256 b0 = key.currency0.balanceOf(msg.sender);
        uint256 b1 = key.currency1.balanceOf(msg.sender);
        bytes memory actions = abi.encodePacked(uint8(Actions.DECREASE_LIQUIDITY), uint8(Actions.TAKE_PAIR));
        bytes[] memory params = new bytes[](2);
        params[0] = abi.encode(tokenId, uint256(0), uint128(0), uint128(0), bytes(""));
        params[1] = abi.encode(key.currency0, key.currency1, msg.sender);
        positionManager.modifyLiquidities(abi.encode(actions, params), block.timestamp);
        amount0 = key.currency0.balanceOf(msg.sender) - b0;
        amount1 = key.currency1.balanceOf(msg.sender) - b1;
    }

    function release(bytes32 familyId, address) external view returns (address) {
        Family storage f = _families[familyId];
        if (msg.sender != f.beneficiary) revert NotBeneficiary(familyId, msg.sender);
        revert StillLocked(familyId, f.unlockBlock); // FOREVER never releases; the mock models only permanent locks
    }

    /// @dev The mock receives the PositionManager NFT at mint.
    function onERC721Received(address, address, uint256, bytes calldata) external pure returns (bytes4) {
        return this.onERC721Received.selector;
    }
}
