// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable, Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IMILORD} from "../interfaces/IMILORD.sol";
import {MilordsNFT} from "./MilordsNFT.sol";

/// @title MilordConverter
/// @notice Two-way conversion between 1,000,000 MILORD and one Milord NFT, and the escrow
///         for that principal.
///
///  Entry  (MILORD -> NFT): caller deposits exactly 1,000,000 MILORD (escrowed, not burned)
///         plus an ETH fee = max(entryFeeBps x ETH value of 1M MILORD, minEntryFeeWei).
///         The ETH value comes from a short-lived quote signed by an approved quote signer.
///         There is no on-chain fallback price: without a valid quote, entry is impossible.
///  Exit   (NFT -> MILORD): the holder returns the NFT to inventory, receives
///         PRINCIPAL x (1 - exitBurnBps) and PRINCIPAL x exitBurnBps is burned.
///
///  Custody invariants:
///   - MILORD.balanceOf(this) >= backedCount x PRINCIPAL at all times.
///   - No function moves MILORD out of this contract except redeem() (to the redeeming
///     holder) and the exit burn. No owner, governance or fee path can touch principal.
///   - Redemptions cannot be paused.
///   - No MILORD is ever minted (the token has no mint function).
contract MilordConverter is Ownable2Step, ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    uint256 public constant PRINCIPAL = 1_000_000 ether;
    uint16 public constant BPS = 10_000;

    // Hard ceilings (cannot be changed). Increases toward them are time-delayed.
    uint16 public constant MAX_ENTRY_FEE_BPS = 2_000; // 20% of 1M-MILORD ETH value
    uint256 public constant MAX_MIN_ENTRY_FEE = 0.02 ether;
    uint16 public constant MAX_EXIT_BURN_BPS = 1_000; // 10% of principal
    uint256 public constant PARAM_INCREASE_DELAY = 2 days;
    uint256 public constant MAX_QUOTE_LIFETIME_CAP = 30 minutes;

    bytes32 public constant QUOTE_TYPEHASH =
        keccak256("EntryQuote(address account,uint256 weiPerMillion,uint64 issuedAt,uint64 expiresAt,bytes32 nonce)");

    struct EntryQuote {
        address account; // the only address that may use this quote
        uint256 weiPerMillion; // ETH (wei) value of 1,000,000 MILORD
        uint64 issuedAt;
        uint64 expiresAt;
        bytes32 nonce; // single use
    }

    struct FeeParams {
        uint16 entryFeeBps;
        uint16 exitBurnBps;
        uint256 minEntryFeeWei;
    }

    struct PendingParams {
        FeeParams params;
        uint64 eta;
    }

    IMILORD public immutable token;
    MilordsNFT public immutable nft;

    FeeParams public feeParams;
    PendingParams public pendingParams;

    /// Receives entry-fee ETH (a RevenueSplitter configured 70% arbitrage / 30% team).
    address public entryFeeRecipient;
    PendingAddress public pendingEntryFeeRecipient;

    struct PendingAddress {
        address account;
        uint64 eta;
    }

    mapping(address => bool) public isQuoteSigner;
    uint256 public maxQuoteLifetime = 5 minutes;
    mapping(bytes32 nonce => bool) public quoteUsed;

    bool public entryPaused;

    uint256 public backedCount;
    uint256 public nextMintId = 1;
    uint256[] internal _inventory;

    // lifetime accounting
    uint256 public totalEntryFeesWei;
    uint256 public totalMilordBurnedOnExit;
    uint256 public totalConversions;
    uint256 public totalRedemptions;

    event Converted(
        uint256 indexed tokenId, address indexed account, uint256 feeWei, uint256 weiPerMillion, bytes32 quoteNonce
    );
    event Redeemed(uint256 indexed tokenId, address indexed account, uint256 released, uint256 burned);
    event FeeParamsQueued(FeeParams params, uint64 eta);
    event FeeParamsUpdated(FeeParams params);
    event QuoteSignerSet(address indexed signer, bool allowed);
    event MaxQuoteLifetimeSet(uint256 seconds_);
    event EntryPausedSet(bool paused);
    event EntryFeeRecipientQueued(address account, uint64 eta);
    event EntryFeeRecipientSet(address account);

    error EntryPaused();
    error SupplyExhausted();
    error BadQuote();
    error QuoteExpired();
    error QuoteReused();
    error FeeAboveMax(uint256 fee, uint256 maxFee);
    error InsufficientFee(uint256 fee, uint256 paid);
    error NotHolder();
    error BadParams();
    error TooEarly();
    error PrincipalMismatch();

    constructor(address owner_, IMILORD token_, MilordsNFT nft_, address entryFeeRecipient_, address quoteSigner_)
        Ownable(owner_)
        EIP712("MilordConverter", "1")
    {
        require(address(token_) != address(0) && address(nft_) != address(0), "zero");
        require(entryFeeRecipient_ != address(0), "zero fee recipient");
        token = token_;
        nft = nft_;
        entryFeeRecipient = entryFeeRecipient_;
        feeParams = FeeParams({entryFeeBps: 1_000, exitBurnBps: 1_000, minEntryFeeWei: 0.002 ether});
        if (quoteSigner_ != address(0)) {
            isQuoteSigner[quoteSigner_] = true;
            emit QuoteSignerSet(quoteSigner_, true);
        }
        emit FeeParamsUpdated(feeParams);
        emit EntryFeeRecipientSet(entryFeeRecipient_);
    }

    // ───────────────────────────── entry ─────────────────────────────

    /// @notice ETH fee for a given reference price: max(entryFeeBps x price, minimum).
    function entryFeeFor(uint256 weiPerMillion) public view returns (uint256) {
        uint256 pct = (weiPerMillion * feeParams.entryFeeBps) / BPS;
        return pct > feeParams.minEntryFeeWei ? pct : feeParams.minEntryFeeWei;
    }

    function quoteDigest(EntryQuote calldata q) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(abi.encode(QUOTE_TYPEHASH, q.account, q.weiPerMillion, q.issuedAt, q.expiresAt, q.nonce))
        );
    }

    /// @notice Convert exactly 1,000,000 MILORD into one Milord.
    /// @param q        price quote signed by an approved quote signer, bound to msg.sender
    /// @param sig      signature over q
    /// @param maxFee   caller's ceiling on the ETH fee (slippage protection)
    function convert(EntryQuote calldata q, bytes calldata sig, uint256 maxFee)
        external
        payable
        nonReentrant
        returns (uint256 tokenId)
    {
        if (entryPaused) revert EntryPaused();
        uint256 fee = _verifyQuote(q, sig);
        if (fee > maxFee) revert FeeAboveMax(fee, maxFee);
        if (msg.value < fee) revert InsufficientFee(fee, msg.value);

        // identity: reuse inventory first, otherwise mint the next never-issued ID
        uint256 invLen = _inventory.length;
        if (invLen > 0) {
            tokenId = _inventory[invLen - 1];
            _inventory.pop();
        } else {
            if (nextMintId > nft.MAX_SUPPLY()) revert SupplyExhausted();
            tokenId = nextMintId++;
        }

        // escrow exactly PRINCIPAL
        uint256 before = token.balanceOf(address(this));
        IERC20(address(token)).safeTransferFrom(msg.sender, address(this), PRINCIPAL);
        if (token.balanceOf(address(this)) - before != PRINCIPAL) revert PrincipalMismatch();

        backedCount += 1;
        totalConversions += 1;
        totalEntryFeesWei += fee;

        nft.issue(msg.sender, tokenId);

        (bool ok,) = entryFeeRecipient.call{value: fee}("");
        require(ok, "fee transfer failed");
        if (msg.value > fee) {
            (bool r,) = msg.sender.call{value: msg.value - fee}("");
            require(r, "refund failed");
        }

        emit Converted(tokenId, msg.sender, fee, q.weiPerMillion, q.nonce);
    }

    function _verifyQuote(EntryQuote calldata q, bytes calldata sig) internal returns (uint256 fee) {
        if (q.account != msg.sender || q.weiPerMillion == 0) revert BadQuote();
        if (q.issuedAt > block.timestamp || q.expiresAt < block.timestamp) revert QuoteExpired();
        if (q.expiresAt - q.issuedAt > maxQuoteLifetime) revert QuoteExpired();
        if (quoteUsed[q.nonce]) revert QuoteReused();
        address signer = ECDSA.recover(quoteDigest(q), sig);
        if (!isQuoteSigner[signer]) revert BadQuote();
        quoteUsed[q.nonce] = true;
        fee = entryFeeFor(q.weiPerMillion);
    }

    // ───────────────────────────── exit ─────────────────────────────

    /// @notice Return a Milord to inventory and receive the principal minus the exit burn.
    ///         Never pausable.
    function redeem(uint256 tokenId) external nonReentrant returns (uint256 released) {
        if (nft.ownerOf(tokenId) != msg.sender) revert NotHolder();
        nft.retire(tokenId, msg.sender);
        _inventory.push(tokenId);
        backedCount -= 1;
        totalRedemptions += 1;

        uint256 burned = (PRINCIPAL * feeParams.exitBurnBps) / BPS;
        released = PRINCIPAL - burned;
        if (burned > 0) {
            token.burn(burned);
            totalMilordBurnedOnExit += burned;
        }
        IERC20(address(token)).safeTransfer(msg.sender, released);
        emit Redeemed(tokenId, msg.sender, released, burned);
    }

    // ───────────────────────────── admin (bounded) ─────────────────────────────

    /// @notice Lower any fee parameter immediately (emergency reductions).
    function reduceFeeParams(FeeParams calldata p) external onlyOwner {
        FeeParams memory c = feeParams;
        if (p.entryFeeBps > c.entryFeeBps || p.exitBurnBps > c.exitBurnBps || p.minEntryFeeWei > c.minEntryFeeWei) {
            revert BadParams();
        }
        feeParams = p;
        delete pendingParams;
        emit FeeParamsUpdated(p);
    }

    /// @notice Queue any change (including increases) within the hard ceilings.
    function queueFeeParams(FeeParams calldata p) external onlyOwner {
        if (
            p.entryFeeBps > MAX_ENTRY_FEE_BPS || p.exitBurnBps > MAX_EXIT_BURN_BPS
                || p.minEntryFeeWei > MAX_MIN_ENTRY_FEE
        ) {
            revert BadParams();
        }
        uint64 eta = uint64(block.timestamp + PARAM_INCREASE_DELAY);
        pendingParams = PendingParams(p, eta);
        emit FeeParamsQueued(p, eta);
    }

    function executeFeeParams() external onlyOwner {
        PendingParams memory pp = pendingParams;
        if (pp.eta == 0) revert BadParams();
        if (block.timestamp < pp.eta) revert TooEarly();
        feeParams = pp.params;
        delete pendingParams;
        emit FeeParamsUpdated(pp.params);
    }

    function setQuoteSigner(address signer, bool allowed) external onlyOwner {
        require(signer != address(0), "zero");
        isQuoteSigner[signer] = allowed;
        emit QuoteSignerSet(signer, allowed);
    }

    function setMaxQuoteLifetime(uint256 seconds_) external onlyOwner {
        if (seconds_ == 0 || seconds_ > MAX_QUOTE_LIFETIME_CAP) revert BadParams();
        maxQuoteLifetime = seconds_;
        emit MaxQuoteLifetimeSet(seconds_);
    }

    /// @notice Pause / unpause NEW conversions. Redemptions are unaffected.
    function setEntryPaused(bool paused) external onlyOwner {
        entryPaused = paused;
        emit EntryPausedSet(paused);
    }

    function queueEntryFeeRecipient(address account) external onlyOwner {
        require(account != address(0), "zero");
        uint64 eta = uint64(block.timestamp + PARAM_INCREASE_DELAY);
        pendingEntryFeeRecipient = PendingAddress(account, eta);
        emit EntryFeeRecipientQueued(account, eta);
    }

    function executeEntryFeeRecipient() external onlyOwner {
        PendingAddress memory p = pendingEntryFeeRecipient;
        if (p.account == address(0)) revert BadParams();
        if (block.timestamp < p.eta) revert TooEarly();
        entryFeeRecipient = p.account;
        delete pendingEntryFeeRecipient;
        emit EntryFeeRecipientSet(p.account);
    }

    /// @notice Recover tokens sent here by mistake. MILORD can never be recovered, so
    ///         escrowed principal is untouchable.
    function rescueERC20(IERC20 other, address to, uint256 amount) external onlyOwner {
        require(address(other) != address(token), "principal");
        other.safeTransfer(to, amount);
    }

    // ───────────────────────────── views ─────────────────────────────

    function inventory() external view returns (uint256[] memory) {
        return _inventory;
    }

    function inventorySize() external view returns (uint256) {
        return _inventory.length;
    }

    /// @notice Escrowed MILORD that backs circulating NFTs.
    function requiredEscrow() public view returns (uint256) {
        return backedCount * PRINCIPAL;
    }

    /// @notice How many more NFTs could exist simultaneously given current MILORD supply.
    ///         After burns, totalSupply / PRINCIPAL < 6,767, so not every identity can be backed.
    function maxSimultaneouslyBacked() external view returns (uint256) {
        uint256 bySupply = token.totalSupply() / PRINCIPAL;
        uint256 cap = nft.MAX_SUPPLY();
        return bySupply < cap ? bySupply : cap;
    }

    function previewRedeem() external view returns (uint256 released, uint256 burned) {
        burned = (PRINCIPAL * feeParams.exitBurnBps) / BPS;
        released = PRINCIPAL - burned;
    }
}
