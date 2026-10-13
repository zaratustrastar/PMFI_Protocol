// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {MilordsNFT} from "./MilordsNFT.sol";

/// @title StrategyRegistry
/// @notice Characteristics, strategies and per-NFT strategy preferences. Kept outside the
///         NFT so it can be extended or replaced without touching the collection.
///
///  - Characteristics and strategies are append-only lists (IDs start at 1). Entries can be
///    deactivated, never deleted, so history stays readable.
///  - Every unordered pair of characteristics maps deterministically to a strategy ID.
///    Unmapped pairs, or pairs mapped to a disabled strategy, resolve to `defaultStrategyId`
///    (Arbitrage in V1). Selection is free; there is no activation payment.
///  - A selection belongs to (holder, activation). It stops applying when the NFT changes
///    hands or is reissued from inventory, so nobody inherits another holder's preference.
///    The new holder may make their own selection.
///  - The first selection ever made for a token is kept as its origin record. Every
///    selection is appended to the token's history.
///  - Strategies describe where rewards come from (chain, vault, reward asset); pARB on Base
///    is one entry, not a hard-coded assumption.
contract StrategyRegistry is Ownable2Step {
    struct Characteristic {
        string name;
        bool active;
    }

    struct Strategy {
        string name;
        bool enabled;
        uint256 chainId;
        address vault;
        address rewardAsset;
    }

    struct Selection {
        uint32 charA;
        uint32 charB;
        uint32 strategyId;
        uint64 activation;
        uint64 timestamp;
        address holder;
    }

    MilordsNFT public immutable nft;

    Characteristic[] internal _chars; // index 0 unused
    Strategy[] internal _strategies; // index 0 unused
    uint256 public defaultStrategyId;

    mapping(bytes32 pairKey => uint256 strategyId) public pairStrategy;
    mapping(uint256 tokenId => Selection) internal _current;
    mapping(uint256 tokenId => Selection) internal _origin;
    mapping(uint256 tokenId => Selection[]) internal _history;

    event CharacteristicAdded(uint256 indexed id, string name);
    event CharacteristicActiveSet(uint256 indexed id, bool active);
    event StrategyAdded(
        uint256 indexed id, string name, uint256 chainId, address vault, address rewardAsset, bool enabled
    );
    event StrategyEnabledSet(uint256 indexed id, bool enabled);
    event DefaultStrategySet(uint256 indexed id);
    event PairMapped(uint256 indexed charA, uint256 indexed charB, uint256 indexed strategyId);
    event Selected(
        uint256 indexed tokenId,
        address indexed holder,
        uint256 charA,
        uint256 charB,
        uint256 strategyId,
        uint256 activation,
        bool origin
    );

    error Invalid();
    error NotHolder();

    constructor(address owner_, MilordsNFT nft_) Ownable(owner_) {
        nft = nft_;
        _chars.push(Characteristic("", false));
        _strategies.push(Strategy("", false, 0, address(0), address(0)));
    }

    // ───────────────────────────── holder ─────────────────────────────

    function select(uint256 tokenId, uint256 charA, uint256 charB) external returns (uint256 strategyId) {
        if (nft.ownerOf(tokenId) != msg.sender || !nft.isActive(tokenId)) revert NotHolder();
        if (charA == charB || !_isActiveChar(charA) || !_isActiveChar(charB)) revert Invalid();
        (uint256 a, uint256 b) = charA < charB ? (charA, charB) : (charB, charA);
        strategyId = resolve(a, b);
        Selection memory s = Selection({
            charA: uint32(a),
            charB: uint32(b),
            strategyId: uint32(strategyId),
            activation: uint64(nft.activationCount(tokenId)),
            timestamp: uint64(block.timestamp),
            holder: msg.sender
        });
        _current[tokenId] = s;
        _history[tokenId].push(s);
        bool isOrigin = _origin[tokenId].timestamp == 0;
        if (isOrigin) _origin[tokenId] = s;
        emit Selected(tokenId, msg.sender, a, b, strategyId, s.activation, isOrigin);
    }

    // ───────────────────────────── views ─────────────────────────────

    /// @notice Strategy for a characteristic pair, falling back to the default.
    function resolve(uint256 charA, uint256 charB) public view returns (uint256) {
        uint256 sid = pairStrategy[_pairKey(charA, charB)];
        if (sid == 0 || !_strategies[sid].enabled) return defaultStrategyId;
        return sid;
    }

    /// @notice Effective preference for a token. `personal` is false when the stored
    ///         selection belongs to a previous holder or activation, or the NFT is inactive.
    function currentPreference(uint256 tokenId)
        external
        view
        returns (bool personal, uint256 charA, uint256 charB, uint256 strategyId)
    {
        Selection memory s = _current[tokenId];
        if (
            s.timestamp != 0 && nft.isActive(tokenId) && nft.ownerOf(tokenId) == s.holder
                && nft.activationCount(tokenId) == s.activation
        ) {
            uint256 sid = s.strategyId;
            if (!_strategies[sid].enabled) sid = resolve(s.charA, s.charB);
            return (true, s.charA, s.charB, sid);
        }
        return (false, 0, 0, defaultStrategyId);
    }

    function origin(uint256 tokenId) external view returns (Selection memory) {
        return _origin[tokenId];
    }

    function history(uint256 tokenId) external view returns (Selection[] memory) {
        return _history[tokenId];
    }

    function characteristicCount() external view returns (uint256) {
        return _chars.length - 1;
    }

    function strategyCount() external view returns (uint256) {
        return _strategies.length - 1;
    }

    function characteristic(uint256 id) external view returns (Characteristic memory) {
        return _chars[id];
    }

    function strategy(uint256 id) external view returns (Strategy memory) {
        return _strategies[id];
    }

    // ───────────────────────────── admin ─────────────────────────────

    function addCharacteristic(string calldata name) external onlyOwner returns (uint256 id) {
        if (bytes(name).length == 0) revert Invalid();
        _chars.push(Characteristic(name, true));
        id = _chars.length - 1;
        emit CharacteristicAdded(id, name);
    }

    function setCharacteristicActive(uint256 id, bool active) external onlyOwner {
        if (id == 0 || id >= _chars.length) revert Invalid();
        _chars[id].active = active;
        emit CharacteristicActiveSet(id, active);
    }

    function addStrategy(string calldata name, uint256 chainId, address vault, address rewardAsset, bool enabled)
        external
        onlyOwner
        returns (uint256 id)
    {
        if (bytes(name).length == 0) revert Invalid();
        _strategies.push(Strategy(name, enabled, chainId, vault, rewardAsset));
        id = _strategies.length - 1;
        emit StrategyAdded(id, name, chainId, vault, rewardAsset, enabled);
    }

    function setStrategyEnabled(uint256 id, bool enabled) external onlyOwner {
        if (id == 0 || id >= _strategies.length) revert Invalid();
        if (!enabled && id == defaultStrategyId) revert Invalid(); // keep a live default
        _strategies[id].enabled = enabled;
        emit StrategyEnabledSet(id, enabled);
    }

    function setDefaultStrategy(uint256 id) external onlyOwner {
        if (id == 0 || id >= _strategies.length || !_strategies[id].enabled) revert Invalid();
        defaultStrategyId = id;
        emit DefaultStrategySet(id);
    }

    function setPairStrategy(uint256 charA, uint256 charB, uint256 strategyId) external onlyOwner {
        if (charA == charB || charA == 0 || charB == 0 || charA >= _chars.length || charB >= _chars.length) {
            revert Invalid();
        }
        if (strategyId >= _strategies.length) revert Invalid();
        pairStrategy[_pairKey(charA, charB)] = strategyId;
        (uint256 a, uint256 b) = charA < charB ? (charA, charB) : (charB, charA);
        emit PairMapped(a, b, strategyId);
    }

    function _isActiveChar(uint256 id) internal view returns (bool) {
        return id != 0 && id < _chars.length && _chars[id].active;
    }

    function _pairKey(uint256 a, uint256 b) internal pure returns (bytes32) {
        return a < b ? keccak256(abi.encode(a, b)) : keccak256(abi.encode(b, a));
    }
}
