// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";
import {Votes} from "@openzeppelin/contracts/governance/utils/Votes.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";

/// @title MilordsNFT
/// @notice 6,767 persistent Milord identities (token IDs 1..6767).
///
///  - Only the converter can bring an identity into circulation (`issue`) or return it to
///    inventory (`retire`). Inventory = NFTs owned by the converter.
///  - Inventory NFTs carry no votes and do not count toward the vote total supply.
///  - Every circulating NFT is one vote. Holders are self-delegated automatically on receipt,
///    so votes work without an extra transaction (holders may re-delegate).
///  - The governance clock is block.timestamp: on Robinhood Chain (Arbitrum Orbit)
///    block.number returns an L1 estimate, not the L2 height.
///  - `activationCount` increments each time an identity is issued, so per-holder settings
///    stored elsewhere (strategy preferences) can be invalidated on reissue.
///  - ERC-2981 royalty info with a hard ceiling. ERC-6551 needs no NFT changes: any ERC-721
///    can be bound to token-bound accounts through the canonical registry.
contract MilordsNFT is ERC721, ERC2981, Votes, Ownable2Step {
    using Strings for uint256;

    uint256 public constant MAX_SUPPLY = 6767;
    uint96 public constant MAX_ROYALTY_BPS = 1000; // 10% hard ceiling
    uint96 public constant INITIAL_ROYALTY_BPS = 670; // 6.7%

    address public converter;
    uint256 public totalMinted;
    mapping(uint256 tokenId => uint256) public activationCount;

    string private _baseTokenURI;
    bool public metadataFrozen;

    event ConverterSet(address indexed converter);
    event Issued(uint256 indexed tokenId, address indexed to, uint256 activation);
    event Retired(uint256 indexed tokenId, address indexed from);
    event RoyaltyUpdated(address indexed receiver, uint96 bps);
    event BaseURIUpdated(string baseURI);
    event MetadataFrozen();

    error NotConverter();
    error ConverterAlreadySet();
    error InvalidTokenId();
    error RoyaltyTooHigh();
    error Frozen();

    modifier onlyConverter() {
        if (msg.sender != converter) revert NotConverter();
        _;
    }

    constructor(address owner_, address royaltyReceiver, string memory baseURI_)
        ERC721("Milords", "MILORDS")
        EIP712("Milords", "1")
        Ownable(owner_)
    {
        _setDefaultRoyalty(royaltyReceiver, INITIAL_ROYALTY_BPS);
        _baseTokenURI = baseURI_;
        emit RoyaltyUpdated(royaltyReceiver, INITIAL_ROYALTY_BPS);
    }

    // ───────────────────────────── converter hooks ─────────────────────────────

    /// @notice One-time wiring of the converter. Cannot be changed afterwards.
    function setConverter(address converter_) external onlyOwner {
        if (converter != address(0)) revert ConverterAlreadySet();
        require(converter_ != address(0), "zero");
        converter = converter_;
        emit ConverterSet(converter_);
    }

    /// @notice Put identity `tokenId` into circulation for `to`. Mints it the first time,
    ///         otherwise moves it out of converter inventory.
    function issue(address to, uint256 tokenId) external onlyConverter {
        if (tokenId == 0 || tokenId > MAX_SUPPLY) revert InvalidTokenId();
        address current = _ownerOf(tokenId);
        if (current == address(0)) {
            totalMinted += 1;
            _mint(to, tokenId);
        } else {
            require(current == converter, "not in inventory");
            _transfer(converter, to, tokenId);
        }
        uint256 activation = ++activationCount[tokenId];
        emit Issued(tokenId, to, activation);
    }

    /// @notice Move `tokenId` from its holder into converter inventory. The converter
    ///         checks that the caller of redeem() is the holder before calling this.
    function retire(uint256 tokenId, address from) external onlyConverter {
        _transfer(from, converter, tokenId);
        emit Retired(tokenId, from);
    }

    // ───────────────────────────── views ─────────────────────────────

    /// @notice True when the NFT is held outside converter inventory (reward/vote eligible).
    function isActive(uint256 tokenId) public view returns (bool) {
        address o = _ownerOf(tokenId);
        return o != address(0) && o != converter;
    }

    /// @notice Number of NFTs currently in circulation (excludes converter inventory).
    function activeSupply() external view returns (uint256) {
        return totalMinted - (converter == address(0) ? 0 : balanceOf(converter));
    }

    function _baseURI() internal view override returns (string memory) {
        return _baseTokenURI;
    }

    // ───────────────────────────── admin ─────────────────────────────

    function setRoyalty(address receiver, uint96 bps) external onlyOwner {
        if (bps > MAX_ROYALTY_BPS) revert RoyaltyTooHigh();
        _setDefaultRoyalty(receiver, bps);
        emit RoyaltyUpdated(receiver, bps);
    }

    function setBaseURI(string calldata baseURI_) external onlyOwner {
        if (metadataFrozen) revert Frozen();
        _baseTokenURI = baseURI_;
        emit BaseURIUpdated(baseURI_);
    }

    function freezeMetadata() external onlyOwner {
        metadataFrozen = true;
        emit MetadataFrozen();
    }

    // ───────────────────────────── votes ─────────────────────────────

    function clock() public view override returns (uint48) {
        return uint48(block.timestamp);
    }

    // solhint-disable-next-line func-name-mixedcase
    function CLOCK_MODE() public pure override returns (string memory) {
        return "mode=timestamp";
    }

    /// @dev Converter inventory carries no voting units.
    function _getVotingUnits(address account) internal view override returns (uint256) {
        if (account == converter) return 0;
        return balanceOf(account);
    }

    /// @dev Voting units follow circulating ownership; converter is treated like address(0)
    ///      so inventory is removed from the vote total supply.
    function _update(address to, uint256 tokenId, address auth) internal override returns (address from) {
        from = super._update(to, tokenId, auth);
        address vFrom = from == converter ? address(0) : from;
        address vTo = to == converter ? address(0) : to;
        if (vFrom != vTo) {
            _transferVotingUnits(vFrom, vTo, 1);
        }
        if (vTo != address(0) && delegates(vTo) == address(0)) {
            _delegate(vTo, vTo);
        }
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, ERC2981) returns (bool) {
        return super.supportsInterface(interfaceId);
    }
}
