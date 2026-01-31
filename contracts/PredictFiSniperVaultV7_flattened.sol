// SPDX-License-Identifier: MIT
// Flattened contract for Basescan verification
// Compiler: 0.8.20, Optimization: 200 runs, EVM: Paris

pragma solidity ^0.8.20;

// OpenZeppelin Contracts (last updated v5.0.0) (utils/Context.sol)
abstract contract Context {
    function _msgSender() internal view virtual returns (address) {
        return msg.sender;
    }

    function _msgData() internal view virtual returns (bytes calldata) {
        return msg.data;
    }

    function _contextSuffixLength() internal view virtual returns (uint256) {
        return 0;
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (access/Ownable.sol)
abstract contract Ownable is Context {
    address private _owner;

    error OwnableUnauthorizedAccount(address account);
    error OwnableInvalidOwner(address owner);

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    constructor(address initialOwner) {
        if (initialOwner == address(0)) {
            revert OwnableInvalidOwner(address(0));
        }
        _transferOwnership(initialOwner);
    }

    modifier onlyOwner() {
        _checkOwner();
        _;
    }

    function owner() public view virtual returns (address) {
        return _owner;
    }

    function _checkOwner() internal view virtual {
        if (owner() != _msgSender()) {
            revert OwnableUnauthorizedAccount(_msgSender());
        }
    }

    function renounceOwnership() public virtual onlyOwner {
        _transferOwnership(address(0));
    }

    function transferOwnership(address newOwner) public virtual onlyOwner {
        if (newOwner == address(0)) {
            revert OwnableInvalidOwner(address(0));
        }
        _transferOwnership(newOwner);
    }

    function _transferOwnership(address newOwner) internal virtual {
        address oldOwner = _owner;
        _owner = newOwner;
        emit OwnershipTransferred(oldOwner, newOwner);
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/ReentrancyGuard.sol)
abstract contract ReentrancyGuard {
    uint256 private constant NOT_ENTERED = 1;
    uint256 private constant ENTERED = 2;

    uint256 private _status;

    error ReentrancyGuardReentrantCall();

    constructor() {
        _status = NOT_ENTERED;
    }

    modifier nonReentrant() {
        _nonReentrantBefore();
        _;
        _nonReentrantAfter();
    }

    function _nonReentrantBefore() private {
        if (_status == ENTERED) {
            revert ReentrancyGuardReentrantCall();
        }
        _status = ENTERED;
    }

    function _nonReentrantAfter() private {
        _status = NOT_ENTERED;
    }

    function _reentrancyGuardEntered() internal view returns (bool) {
        return _status == ENTERED;
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (token/ERC20/IERC20.sol)
interface IERC20 {
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 value) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 value) external returns (bool);
    function transferFrom(address from, address to, uint256 value) external returns (bool);
}

// OpenZeppelin Contracts (last updated v5.0.0) (token/ERC20/extensions/IERC20Metadata.sol)
interface IERC20Metadata is IERC20 {
    function name() external view returns (string memory);
    function symbol() external view returns (string memory);
    function decimals() external view returns (uint8);
}

// OpenZeppelin Contracts (last updated v5.0.0) (interfaces/draft-IERC6093.sol)
interface IERC20Errors {
    error ERC20InsufficientBalance(address sender, uint256 balance, uint256 needed);
    error ERC20InvalidSender(address sender);
    error ERC20InvalidReceiver(address receiver);
    error ERC20InsufficientAllowance(address spender, uint256 allowance, uint256 needed);
    error ERC20InvalidApprover(address approver);
    error ERC20InvalidSpender(address spender);
}

interface IERC721Errors {
    error ERC721InvalidOwner(address owner);
    error ERC721NonexistentToken(uint256 tokenId);
    error ERC721IncorrectOwner(address sender, uint256 tokenId, address owner);
    error ERC721InvalidSender(address sender);
    error ERC721InvalidReceiver(address receiver);
    error ERC721InsufficientApproval(address operator, uint256 tokenId);
    error ERC721InvalidApprover(address approver);
    error ERC721InvalidOperator(address operator);
}

interface IERC1155Errors {
    error ERC1155InsufficientBalance(address sender, uint256 balance, uint256 needed, uint256 tokenId);
    error ERC1155InvalidSender(address sender);
    error ERC1155InvalidReceiver(address receiver);
    error ERC1155MissingApprovalForAll(address operator, address owner);
    error ERC1155InvalidApprover(address approver);
    error ERC1155InvalidOperator(address operator);
    error ERC1155InvalidArrayLength(uint256 idsLength, uint256 valuesLength);
}

// OpenZeppelin Contracts (last updated v5.0.0) (token/ERC20/ERC20.sol)
abstract contract ERC20 is Context, IERC20, IERC20Metadata, IERC20Errors {
    mapping(address account => uint256) private _balances;
    mapping(address account => mapping(address spender => uint256)) private _allowances;

    uint256 private _totalSupply;
    string private _name;
    string private _symbol;

    constructor(string memory name_, string memory symbol_) {
        _name = name_;
        _symbol = symbol_;
    }

    function name() public view virtual returns (string memory) {
        return _name;
    }

    function symbol() public view virtual returns (string memory) {
        return _symbol;
    }

    function decimals() public view virtual returns (uint8) {
        return 18;
    }

    function totalSupply() public view virtual returns (uint256) {
        return _totalSupply;
    }

    function balanceOf(address account) public view virtual returns (uint256) {
        return _balances[account];
    }

    function transfer(address to, uint256 value) public virtual returns (bool) {
        address owner = _msgSender();
        _transfer(owner, to, value);
        return true;
    }

    function allowance(address owner, address spender) public view virtual returns (uint256) {
        return _allowances[owner][spender];
    }

    function approve(address spender, uint256 value) public virtual returns (bool) {
        address owner = _msgSender();
        _approve(owner, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) public virtual returns (bool) {
        address spender = _msgSender();
        _spendAllowance(from, spender, value);
        _transfer(from, to, value);
        return true;
    }

    function _transfer(address from, address to, uint256 value) internal {
        if (from == address(0)) {
            revert ERC20InvalidSender(address(0));
        }
        if (to == address(0)) {
            revert ERC20InvalidReceiver(address(0));
        }
        _update(from, to, value);
    }

    function _update(address from, address to, uint256 value) internal virtual {
        if (from == address(0)) {
            _totalSupply += value;
        } else {
            uint256 fromBalance = _balances[from];
            if (fromBalance < value) {
                revert ERC20InsufficientBalance(from, fromBalance, value);
            }
            unchecked {
                _balances[from] = fromBalance - value;
            }
        }

        if (to == address(0)) {
            unchecked {
                _totalSupply -= value;
            }
        } else {
            unchecked {
                _balances[to] += value;
            }
        }

        emit Transfer(from, to, value);
    }

    function _mint(address account, uint256 value) internal {
        if (account == address(0)) {
            revert ERC20InvalidReceiver(address(0));
        }
        _update(address(0), account, value);
    }

    function _burn(address account, uint256 value) internal {
        if (account == address(0)) {
            revert ERC20InvalidSender(address(0));
        }
        _update(account, address(0), value);
    }

    function _approve(address owner, address spender, uint256 value) internal {
        _approve(owner, spender, value, true);
    }

    function _approve(address owner, address spender, uint256 value, bool emitEvent) internal virtual {
        if (owner == address(0)) {
            revert ERC20InvalidApprover(address(0));
        }
        if (spender == address(0)) {
            revert ERC20InvalidSpender(address(0));
        }
        _allowances[owner][spender] = value;
        if (emitEvent) {
            emit Approval(owner, spender, value);
        }
    }

    function _spendAllowance(address owner, address spender, uint256 value) internal virtual {
        uint256 currentAllowance = allowance(owner, spender);
        if (currentAllowance != type(uint256).max) {
            if (currentAllowance < value) {
                revert ERC20InsufficientAllowance(spender, currentAllowance, value);
            }
            unchecked {
                _approve(owner, spender, currentAllowance - value, false);
            }
        }
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (token/ERC20/extensions/IERC20Permit.sol)
interface IERC20Permit {
    function permit(
        address owner,
        address spender,
        uint256 value,
        uint256 deadline,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external;
    function nonces(address owner) external view returns (uint256);
    function DOMAIN_SEPARATOR() external view returns (bytes32);
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/Address.sol)
library Address {
    error AddressInsufficientBalance(address account);
    error AddressEmptyCode(address target);
    error FailedInnerCall();

    function sendValue(address payable recipient, uint256 amount) internal {
        if (address(this).balance < amount) {
            revert AddressInsufficientBalance(address(this));
        }

        (bool success, ) = recipient.call{value: amount}("");
        if (!success) {
            revert FailedInnerCall();
        }
    }

    function functionCall(address target, bytes memory data) internal returns (bytes memory) {
        return functionCallWithValue(target, data, 0);
    }

    function functionCallWithValue(address target, bytes memory data, uint256 value) internal returns (bytes memory) {
        if (address(this).balance < value) {
            revert AddressInsufficientBalance(address(this));
        }
        (bool success, bytes memory returndata) = target.call{value: value}(data);
        return verifyCallResultFromTarget(target, success, returndata);
    }

    function functionStaticCall(address target, bytes memory data) internal view returns (bytes memory) {
        (bool success, bytes memory returndata) = target.staticcall(data);
        return verifyCallResultFromTarget(target, success, returndata);
    }

    function functionDelegateCall(address target, bytes memory data) internal returns (bytes memory) {
        (bool success, bytes memory returndata) = target.delegatecall(data);
        return verifyCallResultFromTarget(target, success, returndata);
    }

    function verifyCallResultFromTarget(
        address target,
        bool success,
        bytes memory returndata
    ) internal view returns (bytes memory) {
        if (!success) {
            _revert(returndata);
        } else {
            if (returndata.length == 0 && target.code.length == 0) {
                revert AddressEmptyCode(target);
            }
            return returndata;
        }
    }

    function verifyCallResult(bool success, bytes memory returndata) internal pure returns (bytes memory) {
        if (!success) {
            _revert(returndata);
        } else {
            return returndata;
        }
    }

    function _revert(bytes memory returndata) private pure {
        if (returndata.length > 0) {
            assembly {
                let returndata_size := mload(returndata)
                revert(add(32, returndata), returndata_size)
            }
        } else {
            revert FailedInnerCall();
        }
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (token/ERC20/utils/SafeERC20.sol)
library SafeERC20 {
    using Address for address;

    error SafeERC20FailedOperation(address token);
    error SafeERC20FailedDecreaseAllowance(address spender, uint256 currentAllowance, uint256 requestedDecrease);

    function safeTransfer(IERC20 token, address to, uint256 value) internal {
        _callOptionalReturn(token, abi.encodeCall(token.transfer, (to, value)));
    }

    function safeTransferFrom(IERC20 token, address from, address to, uint256 value) internal {
        _callOptionalReturn(token, abi.encodeCall(token.transferFrom, (from, to, value)));
    }

    function safeIncreaseAllowance(IERC20 token, address spender, uint256 value) internal {
        uint256 oldAllowance = token.allowance(address(this), spender);
        forceApprove(token, spender, oldAllowance + value);
    }

    function safeDecreaseAllowance(IERC20 token, address spender, uint256 requestedDecrease) internal {
        unchecked {
            uint256 currentAllowance = token.allowance(address(this), spender);
            if (currentAllowance < requestedDecrease) {
                revert SafeERC20FailedDecreaseAllowance(spender, currentAllowance, requestedDecrease);
            }
            forceApprove(token, spender, currentAllowance - requestedDecrease);
        }
    }

    function forceApprove(IERC20 token, address spender, uint256 value) internal {
        bytes memory approvalCall = abi.encodeCall(token.approve, (spender, value));

        if (!_callOptionalReturnBool(token, approvalCall)) {
            _callOptionalReturn(token, abi.encodeCall(token.approve, (spender, 0)));
            _callOptionalReturn(token, approvalCall);
        }
    }

    function _callOptionalReturn(IERC20 token, bytes memory data) private {
        bytes memory returndata = address(token).functionCall(data);
        if (returndata.length != 0 && !abi.decode(returndata, (bool))) {
            revert SafeERC20FailedOperation(address(token));
        }
    }

    function _callOptionalReturnBool(IERC20 token, bytes memory data) private returns (bool) {
        (bool success, bytes memory returndata) = address(token).call(data);
        return success && (returndata.length == 0 || abi.decode(returndata, (bool))) && address(token).code.length > 0;
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/cryptography/ECDSA.sol)
library ECDSA {
    enum RecoverError {
        NoError,
        InvalidSignature,
        InvalidSignatureLength,
        InvalidSignatureS
    }

    error ECDSAInvalidSignature();
    error ECDSAInvalidSignatureLength(uint256 length);
    error ECDSAInvalidSignatureS(bytes32 s);

    function tryRecover(bytes32 hash, bytes memory signature) internal pure returns (address, RecoverError, bytes32) {
        if (signature.length == 65) {
            bytes32 r;
            bytes32 s;
            uint8 v;
            assembly {
                r := mload(add(signature, 0x20))
                s := mload(add(signature, 0x40))
                v := byte(0, mload(add(signature, 0x60)))
            }
            return tryRecover(hash, v, r, s);
        } else {
            return (address(0), RecoverError.InvalidSignatureLength, bytes32(signature.length));
        }
    }

    function recover(bytes32 hash, bytes memory signature) internal pure returns (address) {
        (address recovered, RecoverError error, bytes32 errorArg) = tryRecover(hash, signature);
        _throwError(error, errorArg);
        return recovered;
    }

    function tryRecover(bytes32 hash, bytes32 r, bytes32 vs) internal pure returns (address, RecoverError, bytes32) {
        unchecked {
            bytes32 s = vs & bytes32(0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff);
            uint8 v = uint8((uint256(vs) >> 255) + 27);
            return tryRecover(hash, v, r, s);
        }
    }

    function recover(bytes32 hash, bytes32 r, bytes32 vs) internal pure returns (address) {
        (address recovered, RecoverError error, bytes32 errorArg) = tryRecover(hash, r, vs);
        _throwError(error, errorArg);
        return recovered;
    }

    function tryRecover(
        bytes32 hash,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) internal pure returns (address, RecoverError, bytes32) {
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return (address(0), RecoverError.InvalidSignatureS, s);
        }

        address signer = ecrecover(hash, v, r, s);
        if (signer == address(0)) {
            return (address(0), RecoverError.InvalidSignature, bytes32(0));
        }

        return (signer, RecoverError.NoError, bytes32(0));
    }

    function recover(bytes32 hash, uint8 v, bytes32 r, bytes32 s) internal pure returns (address) {
        (address recovered, RecoverError error, bytes32 errorArg) = tryRecover(hash, v, r, s);
        _throwError(error, errorArg);
        return recovered;
    }

    function _throwError(RecoverError error, bytes32 errorArg) private pure {
        if (error == RecoverError.NoError) {
            return;
        } else if (error == RecoverError.InvalidSignature) {
            revert ECDSAInvalidSignature();
        } else if (error == RecoverError.InvalidSignatureLength) {
            revert ECDSAInvalidSignatureLength(uint256(errorArg));
        } else if (error == RecoverError.InvalidSignatureS) {
            revert ECDSAInvalidSignatureS(errorArg);
        }
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/cryptography/MessageHashUtils.sol)
library MessageHashUtils {
    function toEthSignedMessageHash(bytes32 messageHash) internal pure returns (bytes32 digest) {
        assembly {
            mstore(0x00, "\x19Ethereum Signed Message:\n32")
            mstore(0x1c, messageHash)
            digest := keccak256(0x00, 0x3c)
        }
    }

    function toEthSignedMessageHash(bytes memory message) internal pure returns (bytes32) {
        return
            keccak256(bytes.concat("\x19Ethereum Signed Message:\n", bytes(Strings.toString(message.length)), message));
    }

    function toDataWithIntendedValidatorHash(address validator, bytes memory data) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(hex"19_00", validator, data));
    }

    function toTypedDataHash(bytes32 domainSeparator, bytes32 structHash) internal pure returns (bytes32 digest) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, hex"19_01")
            mstore(add(ptr, 0x02), domainSeparator)
            mstore(add(ptr, 0x22), structHash)
            digest := keccak256(ptr, 0x42)
        }
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/math/Math.sol)
library Math {
    error MathOverflowedMulDiv();

    enum Rounding {
        Floor,
        Ceil,
        Trunc,
        Expand
    }

    function log256(uint256 value) internal pure returns (uint256) {
        uint256 result = 0;
        unchecked {
            if (value >> 128 > 0) {
                value >>= 128;
                result += 16;
            }
            if (value >> 64 > 0) {
                value >>= 64;
                result += 8;
            }
            if (value >> 32 > 0) {
                value >>= 32;
                result += 4;
            }
            if (value >> 16 > 0) {
                value >>= 16;
                result += 2;
            }
            if (value >> 8 > 0) {
                result += 1;
            }
        }
        return result;
    }

    function log10(uint256 value) internal pure returns (uint256) {
        uint256 result = 0;
        unchecked {
            if (value >= 10 ** 64) {
                value /= 10 ** 64;
                result += 64;
            }
            if (value >= 10 ** 32) {
                value /= 10 ** 32;
                result += 32;
            }
            if (value >= 10 ** 16) {
                value /= 10 ** 16;
                result += 16;
            }
            if (value >= 10 ** 8) {
                value /= 10 ** 8;
                result += 8;
            }
            if (value >= 10 ** 4) {
                value /= 10 ** 4;
                result += 4;
            }
            if (value >= 10 ** 2) {
                value /= 10 ** 2;
                result += 2;
            }
            if (value >= 10 ** 1) {
                result += 1;
            }
        }
        return result;
    }

    function log10(uint256 value, Rounding rounding) internal pure returns (uint256) {
        unchecked {
            uint256 result = log10(value);
            return result + (unsignedRoundsUp(rounding) && 10 ** result < value ? 1 : 0);
        }
    }

    function unsignedRoundsUp(Rounding rounding) internal pure returns (bool) {
        return uint8(rounding) % 2 == 1;
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/math/SignedMath.sol)
library SignedMath {
    function abs(int256 n) internal pure returns (uint256) {
        unchecked {
            return uint256(n >= 0 ? n : -n);
        }
    }
}

// OpenZeppelin Contracts (last updated v5.0.0) (utils/Strings.sol)
library Strings {
    bytes16 private constant HEX_DIGITS = "0123456789abcdef";
    uint8 private constant ADDRESS_LENGTH = 20;

    error StringsInsufficientHexLength(uint256 value, uint256 length);

    function toString(uint256 value) internal pure returns (string memory) {
        unchecked {
            uint256 length = Math.log10(value) + 1;
            string memory buffer = new string(length);
            uint256 ptr;
            assembly {
                ptr := add(buffer, add(32, length))
            }
            while (true) {
                ptr--;
                assembly {
                    mstore8(ptr, byte(mod(value, 10), HEX_DIGITS))
                }
                value /= 10;
                if (value == 0) break;
            }
            return buffer;
        }
    }

    function toStringSigned(int256 value) internal pure returns (string memory) {
        return string.concat(value < 0 ? "-" : "", toString(SignedMath.abs(value)));
    }

    function toHexString(uint256 value) internal pure returns (string memory) {
        unchecked {
            return toHexString(value, Math.log256(value) + 1);
        }
    }

    function toHexString(uint256 value, uint256 length) internal pure returns (string memory) {
        uint256 localValue = value;
        bytes memory buffer = new bytes(2 * length + 2);
        buffer[0] = "0";
        buffer[1] = "x";
        for (uint256 i = 2 * length + 1; i > 1; --i) {
            buffer[i] = HEX_DIGITS[localValue & 0xf];
            localValue >>= 4;
        }
        if (localValue != 0) {
            revert StringsInsufficientHexLength(value, length);
        }
        return string(buffer);
    }

    function toHexString(address addr) internal pure returns (string memory) {
        return toHexString(uint256(uint160(addr)), ADDRESS_LENGTH);
    }

    function toChecksumHexString(address addr) internal pure returns (string memory) {
        bytes memory buffer = bytes(toHexString(addr));
        uint256 hashValue;
        assembly {
            hashValue := shr(96, keccak256(add(buffer, 0x22), 40))
        }
        for (uint256 i = 2; i < 42; ++i) {
            if (hashValue & (1 << (261 - i * 4)) != 0 && uint8(buffer[i]) > 96) {
                buffer[i] = bytes1(uint8(buffer[i]) - 32);
            }
        }
        return string(buffer);
    }

    function equal(string memory a, string memory b) internal pure returns (bool) {
        return bytes(a).length == bytes(b).length && keccak256(bytes(a)) == keccak256(bytes(b));
    }
}

/**
 * @title PredictFiSniperVaultV7
 * @notice pSNIPER vault with 100% Polymarket forwarding and 3-state asset tracking
 */
contract PredictFiSniperVaultV7 is ERC20, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using ECDSA for bytes32;
    using MessageHashUtils for bytes32;

    bytes32 public constant NAV_TYPEHASH = keccak256(
        "NavDataV7(uint256 totalAssets,uint256 creditedCash,uint256 creditedPositions,uint256 pendingCredit,uint256 inFlightOnChain,uint256 timestamp,uint256 deadline,uint256 roundId,address vault,uint256 chainId,bytes32 domainSalt)"
    );
    
    bytes32 public constant DOMAIN_SALT = keccak256("PredictFiSniperVaultV7.v1");
    
    uint256 public constant MAX_NAV_AGE = 300;
    uint256 public constant MIN_NAV_INTERVAL = 60;
    uint256 public constant STALE_NAV_GRACE = 900;
    uint256 public constant WITHDRAWAL_TAX_BPS = 0;
    uint256 public constant USDC_DECIMALS = 6;
    uint256 public constant NAV_PRECISION = 1e18;
    uint256 public constant WITHDRAWAL_EXPIRY = 7 days;
    
    uint256 public constant PENDING_RATIO_PAUSE = 3000;
    uint256 public constant PENDING_RATIO_CAP = 1000;
    uint256 public constant MAX_DEPOSIT_DURING_LIMBO = 1000 * 1e6;
    uint256 public constant CLAIM_SLIPPAGE_BPS = 50;
    
    address public constant POLYMARKET_BASE_DEPOSIT = 0x2b20920A00D705043260eBFE6561bC96FBd84dBE;

    IERC20 public immutable usdc;
    address public immutable navSigner;
    address public taxCollector;
    address public polymarketWallet;
    
    uint256 public lastRoundId;
    uint256 public lastNavTimestamp;
    
    uint256 public maxDepositPerWallet;
    uint256 public maxTotalDeposits;
    
    uint256 public expectedAssets;
    uint256 public maxLossBps;
    
    uint256 public totalForwardedToPolymarket;
    
    uint256 public correctionNonce;
    
    uint256 public lastAcceptedTotalAssets;
    uint256 public lastAcceptedTimestamp;
    
    mapping(address => uint256) public walletDeposits;
    
    bool public paused;
    bool public depositsThrottled;

    struct WithdrawalRequest {
        address user;
        uint256 shares;
        uint256 usdcLocked;
        uint256 requestTime;
        bool claimed;
    }
    
    WithdrawalRequest[] public withdrawalQueue;
    uint256 public nextWithdrawalIndex;
    uint256 public totalPendingShares;
    
    mapping(address => uint256[]) public userWithdrawals;

    struct NavDataV7 {
        uint256 totalAssets;
        uint256 creditedCash;
        uint256 creditedPositions;
        uint256 pendingCredit;
        uint256 inFlightOnChain;
        uint256 timestamp;
        uint256 deadline;
        uint256 roundId;
    }

    event Deposit(address indexed user, uint256 usdcAmount, uint256 sharesReceived, uint256 navUsed);
    event WithdrawalRequested(uint256 indexed requestId, address indexed user, uint256 shares, uint256 expectedUsdc);
    event WithdrawalClaimed(uint256 indexed requestId, address indexed user, uint256 shares, uint256 usdcReceived, uint256 taxPaid, uint256 navUsed);
    event WithdrawalCancelled(uint256 indexed requestId, address indexed user, uint256 sharesReturned);
    event NavUpdated(uint256 totalAssets, uint256 creditedCash, uint256 creditedPositions, uint256 pendingCredit, uint256 roundId, uint256 timestamp);
    event BufferRefilled(uint256 amount, string source);
    event TaxCollectorUpdated(address indexed oldCollector, address indexed newCollector);
    event PolymarketWalletUpdated(address indexed oldWallet, address indexed newWallet);
    event CapsUpdated(uint256 perWallet, uint256 total);
    event MaxLossUpdated(uint256 oldMaxLoss, uint256 newMaxLoss);
    event Paused(bool isPaused);
    event DepositsThrottled(bool isThrottled);
    event EmergencyWithdraw(address indexed to, uint256 amount);
    event CorrectionExpectedAssets(uint256 indexed nonce, uint256 oldValue, uint256 newValue, string reason);
    event CorrectionTotalForwarded(uint256 indexed nonce, uint256 oldValue, uint256 newValue, string reason);
    event CorrectionTradingLoss(uint256 indexed nonce, uint256 lossAmount, uint256 oldExpected, uint256 newExpected);
    event CorrectionTradingGain(uint256 indexed nonce, uint256 gainAmount, uint256 oldExpected, uint256 newExpected);

    constructor(
        address _usdc,
        address _navSigner,
        address _taxCollector,
        address _polymarketWallet,
        uint256 _maxPerWallet,
        uint256 _maxTotal,
        uint256 _maxLossBps
    ) ERC20("PredictFi Sniper", "pSNIPER") Ownable(msg.sender) {
        require(_usdc != address(0), "Invalid USDC");
        require(_navSigner != address(0), "Invalid signer");
        require(_taxCollector != address(0), "Invalid tax collector");
        require(_polymarketWallet != address(0), "Invalid PM wallet");
        require(_maxLossBps <= 5000, "Max loss cannot exceed 50%");
        
        usdc = IERC20(_usdc);
        navSigner = _navSigner;
        taxCollector = _taxCollector;
        polymarketWallet = _polymarketWallet;
        maxDepositPerWallet = _maxPerWallet;
        maxTotalDeposits = _maxTotal;
        maxLossBps = _maxLossBps;
        
        lastRoundId = 0;
        lastNavTimestamp = 0;
        expectedAssets = 0;
        totalForwardedToPolymarket = 0;
    }

    modifier whenNotPaused() {
        require(!paused, "Vault is paused");
        _;
    }
    
    modifier whenPaused() {
        require(paused, "Vault must be paused");
        _;
    }
    
    modifier whenDepositsNotThrottled() {
        require(!depositsThrottled, "Deposits throttled");
        _;
    }

    function deposit(
        uint256 usdcAmount,
        NavDataV7 calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused whenDepositsNotThrottled {
        require(usdcAmount > 0, "Amount must be > 0");
        
        _verifyAndApplyNav(navData, signature);
        
        require(expectedAssets + usdcAmount <= maxTotalDeposits, "Exceeds total cap");
        
        if (navData.totalAssets > 0) {
            uint256 pendingRatioBps = (navData.pendingCredit * 10000) / navData.totalAssets;
            if (pendingRatioBps > PENDING_RATIO_CAP) {
                require(usdcAmount <= MAX_DEPOSIT_DURING_LIMBO, "Deposit capped during limbo");
            }
        }
        
        uint256 nav = _calculateNav(navData.totalAssets);
        
        uint256 sharesToMint = (usdcAmount * NAV_PRECISION) / nav;
        require(sharesToMint > 0, "Shares too small");
        
        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);
        
        usdc.safeTransfer(POLYMARKET_BASE_DEPOSIT, usdcAmount);
        totalForwardedToPolymarket += usdcAmount;
        
        expectedAssets += usdcAmount;
        walletDeposits[msg.sender] += usdcAmount;
        
        _mint(msg.sender, sharesToMint);
        
        emit Deposit(msg.sender, usdcAmount, sharesToMint, nav);
    }
    
    function requestWithdraw(
        uint256 shareAmount,
        NavDataV7 calldata navData,
        bytes calldata signature
    ) external nonReentrant whenNotPaused returns (uint256 requestId) {
        require(shareAmount > 0, "Amount must be > 0");
        require(balanceOf(msg.sender) >= shareAmount, "Insufficient shares");
        
        _verifyAndApplyNav(navData, signature);
        
        uint256 nav = _calculateNav(navData.totalAssets);
        uint256 usdcLocked = (shareAmount * nav) / NAV_PRECISION;
        
        _transfer(msg.sender, address(this), shareAmount);
        
        requestId = withdrawalQueue.length;
        withdrawalQueue.push(WithdrawalRequest({
            user: msg.sender,
            shares: shareAmount,
            usdcLocked: usdcLocked,
            requestTime: block.timestamp,
            claimed: false
        }));
        
        userWithdrawals[msg.sender].push(requestId);
        totalPendingShares += shareAmount;
        
        emit WithdrawalRequested(requestId, msg.sender, shareAmount, usdcLocked);
    }
    
    function claim(uint256 requestId) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage request = withdrawalQueue[requestId];
        
        require(request.user == msg.sender, "Not your request");
        require(!request.claimed, "Already claimed");
        require(block.timestamp <= request.requestTime + WITHDRAWAL_EXPIRY, "Request expired");
        
        lastRoundId += 1;
        
        uint256 grossUsdc = request.usdcLocked;
        uint256 vaultBalance = usdc.balanceOf(address(this));
        
        uint256 minRequired = grossUsdc - (grossUsdc * CLAIM_SLIPPAGE_BPS) / 10000;
        require(vaultBalance >= minRequired, "Not enough USDC in buffer. Try again later when positions are liquidated.");
        
        uint256 actualGross = vaultBalance >= grossUsdc ? grossUsdc : vaultBalance;
        uint256 tax = (actualGross * WITHDRAWAL_TAX_BPS) / 10000;
        uint256 netUsdc = actualGross - tax;
        
        request.claimed = true;
        totalPendingShares -= request.shares;
        
        if (requestId == nextWithdrawalIndex) {
            while (nextWithdrawalIndex < withdrawalQueue.length && 
                   withdrawalQueue[nextWithdrawalIndex].claimed) {
                nextWithdrawalIndex++;
            }
        }
        
        _burn(address(this), request.shares);
        
        if (grossUsdc > expectedAssets) {
            expectedAssets = 0;
        } else {
            expectedAssets -= grossUsdc;
        }
        
        uint256 depositReduction = (walletDeposits[msg.sender] * request.shares) / 
            (balanceOf(msg.sender) + request.shares + 1);
        if (depositReduction > walletDeposits[msg.sender]) {
            depositReduction = walletDeposits[msg.sender];
        }
        walletDeposits[msg.sender] -= depositReduction;
        
        usdc.safeTransfer(taxCollector, tax);
        usdc.safeTransfer(msg.sender, netUsdc);
        
        uint256 effectiveNav = (grossUsdc * NAV_PRECISION) / request.shares;
        emit WithdrawalClaimed(requestId, msg.sender, request.shares, netUsdc, tax, effectiveNav);
    }
    
    function cancelExpiredWithdrawal(uint256 requestId) external nonReentrant {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage request = withdrawalQueue[requestId];
        
        require(request.user == msg.sender, "Not your request");
        require(!request.claimed, "Already claimed");
        require(block.timestamp > request.requestTime + WITHDRAWAL_EXPIRY, "Not expired yet");
        
        request.claimed = true;
        totalPendingShares -= request.shares;
        
        _transfer(address(this), msg.sender, request.shares);
        
        emit WithdrawalCancelled(requestId, msg.sender, request.shares);
    }

    function _verifyAndApplyNav(NavDataV7 calldata navData, bytes calldata signature) internal {
        require(block.timestamp <= navData.deadline, "NAV expired");
        require(block.timestamp - navData.timestamp <= MAX_NAV_AGE, "NAV too old");
        require(navData.roundId > lastRoundId, "RoundId must increase");
        
        require(navData.timestamp >= lastNavTimestamp + MIN_NAV_INTERVAL || lastNavTimestamp == 0, 
                "NAV update too frequent");
        
        uint256 computedTotal = navData.creditedCash + navData.creditedPositions + 
                                navData.pendingCredit + navData.inFlightOnChain;
        require(computedTotal == navData.totalAssets, "Asset breakdown mismatch");
        
        bytes32 structHash = keccak256(abi.encode(
            NAV_TYPEHASH,
            navData.totalAssets,
            navData.creditedCash,
            navData.creditedPositions,
            navData.pendingCredit,
            navData.inFlightOnChain,
            navData.timestamp,
            navData.deadline,
            navData.roundId,
            address(this),
            block.chainid,
            DOMAIN_SALT
        ));
        bytes32 digest = structHash.toEthSignedMessageHash();
        address signer = digest.recover(signature);
        
        require(signer == navSigner, "Invalid NAV signer");
        
        if (expectedAssets > 0 && totalSupply() > 0) {
            uint256 minAllowedAssets = (expectedAssets * (10000 - maxLossBps)) / 10000;
            require(navData.totalAssets >= minAllowedAssets, "Conservation bound violated");
        }
        
        lastRoundId = navData.roundId;
        lastNavTimestamp = navData.timestamp;
        
        lastAcceptedTotalAssets = navData.totalAssets;
        lastAcceptedTimestamp = block.timestamp;
        
        emit NavUpdated(
            navData.totalAssets,
            navData.creditedCash,
            navData.creditedPositions,
            navData.pendingCredit,
            navData.roundId,
            navData.timestamp
        );
    }
    
    function _calculateNav(uint256 totalAssets) internal view returns (uint256) {
        uint256 supply = totalSupply();
        if (supply == 0) {
            return 10**6;
        }
        return (totalAssets * NAV_PRECISION) / supply;
    }

    function previewDeposit(uint256 usdcAmount, uint256 totalAssets) external view returns (uint256 shares) {
        uint256 nav = _calculateNav(totalAssets);
        shares = (usdcAmount * NAV_PRECISION) / nav;
    }
    
    function previewRedeem(uint256 shareAmount, uint256 totalAssets) external view returns (uint256 netUsdc, uint256 tax) {
        uint256 nav = _calculateNav(totalAssets);
        uint256 grossUsdc = (shareAmount * nav) / NAV_PRECISION;
        tax = (grossUsdc * WITHDRAWAL_TAX_BPS) / 10000;
        netUsdc = grossUsdc - tax;
    }
    
    function getVaultState() external view returns (
        uint256 _lastRoundId,
        uint256 _lastNavTimestamp,
        uint256 _totalSupply,
        uint256 _vaultBuffer,
        uint256 _expectedAssets,
        uint256 _totalForwarded,
        uint256 _totalPendingShares,
        uint256 _pendingWithdrawalsCount,
        bool _paused,
        bool _depositsThrottled,
        uint256 _maxLossBps
    ) {
        uint256 pendingCount = 0;
        for (uint256 i = nextWithdrawalIndex; i < withdrawalQueue.length; i++) {
            if (!withdrawalQueue[i].claimed) pendingCount++;
        }
        
        return (
            lastRoundId,
            lastNavTimestamp,
            totalSupply(),
            usdc.balanceOf(address(this)),
            expectedAssets,
            totalForwardedToPolymarket,
            totalPendingShares,
            pendingCount,
            paused,
            depositsThrottled,
            maxLossBps
        );
    }
    
    function getPendingWithdrawalShares() external view returns (uint256) {
        return totalPendingShares;
    }
    
    function getUserWithdrawals(address user) external view returns (uint256[] memory) {
        return userWithdrawals[user];
    }
    
    function getWithdrawalRequest(uint256 requestId) external view returns (
        address user,
        uint256 shares,
        uint256 usdcLocked,
        uint256 requestTime,
        bool claimed,
        bool expired
    ) {
        require(requestId < withdrawalQueue.length, "Invalid request");
        WithdrawalRequest storage r = withdrawalQueue[requestId];
        return (
            r.user,
            r.shares,
            r.usdcLocked,
            r.requestTime,
            r.claimed,
            block.timestamp > r.requestTime + WITHDRAWAL_EXPIRY
        );
    }
    
    function getRemainingAllowance(address wallet) external view returns (uint256) {
        if (walletDeposits[wallet] >= maxDepositPerWallet) return 0;
        return maxDepositPerWallet - walletDeposits[wallet];
    }
    
    function getRemainingCapacity() external view returns (uint256) {
        if (expectedAssets >= maxTotalDeposits) return 0;
        return maxTotalDeposits - expectedAssets;
    }

    function pause() external onlyOwner {
        paused = true;
        emit Paused(true);
    }
    
    function unpause() external onlyOwner {
        paused = false;
        emit Paused(false);
    }
    
    function throttleDeposits(bool throttle) external onlyOwner {
        depositsThrottled = throttle;
        emit DepositsThrottled(throttle);
    }
    
    function setTaxCollector(address newCollector) external onlyOwner {
        require(newCollector != address(0), "Invalid collector");
        address old = taxCollector;
        taxCollector = newCollector;
        emit TaxCollectorUpdated(old, newCollector);
    }
    
    function setPolymarketWallet(address newWallet) external onlyOwner {
        require(newWallet != address(0), "Invalid wallet");
        address old = polymarketWallet;
        polymarketWallet = newWallet;
        emit PolymarketWalletUpdated(old, newWallet);
    }
    
    function setCaps(uint256 perWallet, uint256 total) external onlyOwner {
        maxDepositPerWallet = perWallet;
        maxTotalDeposits = total;
        emit CapsUpdated(perWallet, total);
    }
    
    function setMaxLoss(uint256 newMaxLossBps) external onlyOwner {
        require(newMaxLossBps <= 5000, "Max loss cannot exceed 50%");
        uint256 old = maxLossBps;
        maxLossBps = newMaxLossBps;
        emit MaxLossUpdated(old, newMaxLossBps);
    }
    
    function refillBuffer(uint256 amount) external onlyOwner {
        usdc.safeTransferFrom(msg.sender, address(this), amount);
        emit BufferRefilled(amount, "owner");
    }
    
    function emergencyWithdraw(address to, uint256 amount) external onlyOwner whenPaused {
        require(to != address(0), "Invalid recipient");
        usdc.safeTransfer(to, amount);
        emit EmergencyWithdraw(to, amount);
    }
    
    function correctExpectedAssets(uint256 newValue, string calldata reason) external onlyOwner {
        uint256 oldValue = expectedAssets;
        expectedAssets = newValue;
        correctionNonce++;
        emit CorrectionExpectedAssets(correctionNonce, oldValue, newValue, reason);
    }
    
    function correctTotalForwarded(uint256 newValue, string calldata reason) external onlyOwner {
        uint256 oldValue = totalForwardedToPolymarket;
        totalForwardedToPolymarket = newValue;
        correctionNonce++;
        emit CorrectionTotalForwarded(correctionNonce, oldValue, newValue, reason);
    }
    
    function recordTradingLoss(uint256 lossAmount) external onlyOwner {
        uint256 oldExpected = expectedAssets;
        if (lossAmount > expectedAssets) {
            expectedAssets = 0;
        } else {
            expectedAssets -= lossAmount;
        }
        correctionNonce++;
        emit CorrectionTradingLoss(correctionNonce, lossAmount, oldExpected, expectedAssets);
    }
    
    function recordTradingGain(uint256 gainAmount) external onlyOwner {
        uint256 oldExpected = expectedAssets;
        expectedAssets += gainAmount;
        correctionNonce++;
        emit CorrectionTradingGain(correctionNonce, gainAmount, oldExpected, expectedAssets);
    }
}
