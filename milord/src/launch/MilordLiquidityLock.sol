// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {Actions} from "@uniswap/v4-periphery/src/libraries/Actions.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {IERC721Receiver} from "@openzeppelin/contracts/token/ERC721/IERC721Receiver.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

/// @notice Permanent lock for the launch liquidity position(s). Holds Uniswap v4 position NFTs and can only
///         collect their trading fees, to one fixed recipient. There is no function to remove liquidity,
///         transfer the NFT or change the recipient, so the launch liquidity cannot be pulled.
///         Fees are ordinary LP fees (ETH from buys, MILORD from sells), kept separate from Hookr project taxes.
contract MilordLiquidityLock is IERC721Receiver {
    IPositionManager public immutable positionManager;
    address public immutable feeRecipient;
    uint256[] public lockedPositions;
    mapping(uint256 => bool) internal _registered;

    event Locked(uint256 indexed tokenId, address from);
    event FeesCollected(uint256 indexed tokenId, address recipient);

    constructor(IPositionManager positionManager_, address feeRecipient_) {
        require(feeRecipient_ != address(0), "zero recipient");
        positionManager = positionManager_;
        feeRecipient = feeRecipient_;
    }

    function onERC721Received(address, address from, uint256 tokenId, bytes calldata) external returns (bytes4) {
        require(msg.sender == address(positionManager), "only v4 positions");
        _register(tokenId, from);
        return IERC721Receiver.onERC721Received.selector;
    }

    /// @notice Records a position minted straight to this contract (PositionManager mints without a callback).
    function register(uint256 tokenId) external {
        require(isLocked(tokenId), "not owned");
        _register(tokenId, msg.sender);
    }

    /// @notice A position is locked exactly when this contract owns it; nothing here can transfer it out.
    function isLocked(uint256 tokenId) public view returns (bool) {
        return IERC721(address(positionManager)).ownerOf(tokenId) == address(this);
    }

    function _register(uint256 tokenId, address from) internal {
        if (_registered[tokenId]) return;
        _registered[tokenId] = true;
        lockedPositions.push(tokenId);
        emit Locked(tokenId, from);
    }

    /// @notice Permissionless: sends accrued LP fees of a locked position to `feeRecipient`.
    function collectFees(uint256 tokenId) external {
        require(isLocked(tokenId), "not locked");
        (PoolKey memory key,) = positionManager.getPoolAndPositionInfo(tokenId);
        bytes memory actions = abi.encodePacked(uint8(Actions.DECREASE_LIQUIDITY), uint8(Actions.TAKE_PAIR));
        bytes[] memory params = new bytes[](2);
        params[0] = abi.encode(tokenId, uint256(0), uint128(0), uint128(0), bytes("")); // zero liquidity: fees only
        params[1] = abi.encode(key.currency0, key.currency1, feeRecipient);
        positionManager.modifyLiquidities(abi.encode(actions, params), block.timestamp);
        emit FeesCollected(tokenId, feeRecipient);
    }

    function lockedCount() external view returns (uint256) {
        return lockedPositions.length;
    }
}
