// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {FullMath} from "@uniswap/v4-core/src/libraries/FullMath.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {IHookrLauncher} from "../vendor/hookr/interfaces/IHookrLauncher.sol";
import {IHookrRegistry} from "../vendor/hookr/interfaces/IHookrRegistry.sol";
import {IHookrLaunchFee} from "../vendor/hookr/interfaces/IHookrLaunchFee.sol";
import {HookrTypes} from "../vendor/hookr/types/HookrTypes.sol";
import {IHookrRulesFork, IHookrDirectionalTaxFork, IHookrTaxQueueFork} from "../vendor/hookr/IHookrExtras.sol";
import {MILORDToken} from "../robinhood/MILORDToken.sol";

/// @title MilordAtomicLaunch
/// @notice Creates MILORD and opens its Hookr-1 market in ONE transaction, so no one can open the ETH/MILORD Hookr
///         pool first: HookrLauncher only accepts an existing token that already has code, and MILORD has none until
///         this call. The token address is fixed in advance (CREATE2 from this contract), so the rest of the
///         protocol can be deployed against it before launch.
///
///         One call, by `owner` (the Team Safe) only, once:
///           1. deploys MILORDToken (6,767,000,000, minted to this contract) at `predictedToken()`;
///           2. launches it through HookrLauncher.launchFamily with the approved terms below, 100% of supply in one
///              MILORD-only position and 0 ETH (only Hookr's launch fee, capped by `maxLaunchFee`);
///           3. verifies on chain what Hookr froze (price, range, fee, tax, commission, recipients, no surcharge
///              beyond the configured Anti-Snipe, no Hookr minimum fee, principal lock) and reverts otherwise;
///           4. burns the integer-rounding remainder, so no one holds MILORD outside the pool;
///           5. offers the family to `owner` (HookrLauncher.transferFamily). The Safe then calls acceptFamily.
///
///         The approved tokenomics are constants and cannot be changed by configuration. Configurable: the Hookr
///         addresses, the trading-tax recipient, the optional Anti-Snipe (a CANDIDATE, off unless set), the temporary
///         principal lock and the launch-fee cap. No owner functions exist after the launch apart from forwarding
///         fees this contract receives when the Safe accepts the family (`sweep`, to the Safe, callable by anyone).
contract MilordAtomicLaunch {
    using PoolIdLibrary for PoolKey;

    // ---------------------------------------------------------------- approved tokenomics (2026-10-14)
    uint256 public constant SUPPLY = 6_767_000_000 ether;
    int24 public constant TICK_UPPER = 203280; // opening price, ~10 ETH FDV
    int24 public constant TICK_LOWER = 134220; // range top ~1,000x
    int24 public constant TICK_SPACING = 60;
    uint24 public constant BASE_LP_FEE_PIPS = 3000; // 0.3%
    uint24 public constant TAX_PIPS = 15_000; // 1.5% buy and sell, in ETH
    uint16 public constant PROTOCOL_SHARE_BPS = 2000; // Hookr commission 20%
    uint256 public constant MAX_DUST = 1e6; // wei of MILORD the position may leave unplaced (rounding)
    uint256 public constant MAX_LOCK_BLOCKS = 100_000; // HookrLauncher's lockEndBlock horizon

    struct Config {
        IHookrLauncher launcher;
        address root;
        address rules;
        address tax; // Tax + Conversion (HookrDirectionalTax)
        address owner; // Team Safe: the only caller of launch(), receives the family
        bytes32 salt; // CREATE2 salt of the token
        uint256 maxLaunchFee; // wei; launch() reverts if Hookr's fee is higher
        uint32 lockBlocks; // temporary principal lock after the guard (or after launch): parent-chain blocks, > 0
        uint24 snipePips; // Anti-Snipe start (0 = off: the approved flat launch)
        uint16 snipeSeconds; // linear decay to 0
        uint32 guardBlocks; // Anti-Snipe guard, parent-chain blocks (0 when snipePips is 0)
    }

    Config internal _c;
    bool public launched;
    MILORDToken public token;
    bytes32 public familyId;
    PoolId public poolId;
    address public buyQueue;
    address public sellQueue;
    uint256 public guardEndBlock;
    uint256 public lockEndBlock;

    event Launched(
        address indexed token,
        bytes32 indexed familyId,
        PoolId poolId,
        address buyQueue,
        address sellQueue,
        uint256 guardEndBlock,
        uint256 lockEndBlock,
        uint256 launchFee,
        uint256 dustBurned
    );
    event Swept(address indexed to, uint256 eth, uint256 milord);

    error NotOwner();
    error AlreadyLaunched();
    error BadConfig();
    error NotAdmitted(address module);
    error LaunchFeeTooHigh(uint256 fee, uint256 maxFee);
    error WrongValue(uint256 sent, uint256 fee);
    error Mismatch(uint8 check);

    constructor(Config memory c) {
        if (
            address(c.launcher).code.length == 0 || c.root == address(0) || c.rules == address(0) || c.tax == address(0)
                || c.owner == address(0) || c.lockBlocks == 0 || c.lockBlocks > MAX_LOCK_BLOCKS
                || (c.snipePips == 0) != (c.guardBlocks == 0) || (c.snipePips == 0) != (c.snipeSeconds == 0)
                || uint256(c.guardBlocks) + c.lockBlocks > MAX_LOCK_BLOCKS
                || uint256(BASE_LP_FEE_PIPS) + c.snipePips > 600_000
        ) revert BadConfig();
        _c = c;
    }

    function config() external view returns (Config memory) {
        return _c;
    }

    /// @notice MILORD's address before it exists. Deploy the rest of the protocol against it.
    function predictedToken() public view returns (address) {
        bytes32 h = keccak256(abi.encodePacked(type(MILORDToken).creationCode, abi.encode(address(this))));
        return address(uint160(uint256(keccak256(abi.encodePacked(bytes1(0xff), address(this), _c.salt, h)))));
    }

    /// @notice Hookr's current launch fee; send exactly this as msg.value.
    function launchFee() public view returns (uint256 fee) {
        (fee,,,,) = IHookrLaunchFee(address(_c.launcher)).launchFee();
    }

    /// @param taxRecipient The trading-tax RevenueSplitter (1/3 buyback-burn, 1/3 Team, 1/3 Arbitrage). Frozen by Hookr.
    /// @param deadline Hookr's launch deadline (block.timestamp).
    function launch(address taxRecipient, uint256 deadline) external payable returns (bytes32) {
        Config memory c = _c;
        if (msg.sender != c.owner) revert NotOwner();
        if (launched) revert AlreadyLaunched();
        if (taxRecipient.code.length == 0) revert BadConfig();
        launched = true;
        uint256 fee = launchFee();
        if (fee > c.maxLaunchFee) revert LaunchFeeTooHigh(fee, c.maxLaunchFee);
        if (msg.value != fee) revert WrongValue(msg.value, fee);

        MILORDToken t = new MILORDToken{salt: c.salt}(address(this));
        token = t;
        guardEndBlock = c.guardBlocks == 0 ? 0 : block.number + c.guardBlocks;
        lockEndBlock = (guardEndBlock == 0 ? block.number : guardEndBlock) + c.lockBlocks;

        IHookrLauncher.LaunchParams memory p = _params(c, address(t), taxRecipient, deadline);
        t.approve(address(c.launcher), SUPPLY);
        bytes32 fid;
        (fid,,) = c.launcher.launchFamily{value: fee}(p);
        familyId = fid;

        uint256 dust = t.balanceOf(address(this));
        if (dust > MAX_DUST) revert Mismatch(1);
        if (dust != 0) t.burn(dust);
        _verify(c, t, fid, taxRecipient);

        c.launcher.transferFamily(fid, c.owner, 0);
        emit Launched(address(t), fid, poolId, buyQueue, sellQueue, guardEndBlock, lockEndBlock, fee, dust);
        return fid;
    }

    /// @notice Forwards ETH and MILORD this contract received (fees paid to it when the Safe accepts the family) to
    ///         the Safe. Callable by anyone; the destination is fixed.
    function sweep() external {
        uint256 eth = address(this).balance;
        uint256 m = address(token) == address(0) ? 0 : token.balanceOf(address(this));
        if (m != 0) token.transfer(_c.owner, m);
        if (eth != 0) {
            (bool ok,) = _c.owner.call{value: eth}("");
            require(ok, "owner refused ETH");
        }
        emit Swept(_c.owner, eth, m);
    }

    receive() external payable {}

    // ---------------------------------------------------------------- internals

    function _params(Config memory c, address t, address taxRecipient, uint256 deadline)
        internal
        view
        returns (IHookrLauncher.LaunchParams memory p)
    {
        IHookrRegistry registry = c.launcher.registry();
        IHookrRegistry.Admission memory ra = registry.admission(c.root, c.rules);
        IHookrRegistry.Admission memory aa = registry.admission(c.root, c.tax);
        if (ra.implementation != c.rules) revert NotAdmitted(c.rules);
        if (aa.implementation != c.tax) revert NotAdmitted(c.tax);

        uint160 sqrtU = TickMath.getSqrtPriceAtTick(TICK_UPPER);
        uint160 sqrtL = TickMath.getSqrtPriceAtTick(TICK_LOWER);
        IHookrLauncher.Member[] memory m = new IHookrLauncher.Member[](1);
        m[0].quote = Currency.wrap(address(0));
        m[0].tickSpacing = TICK_SPACING;
        m[0].sqrtPriceX96 = sqrtU;
        m[0].tickLower = TICK_LOWER;
        m[0].tickUpper = TICK_UPPER;
        m[0].liquidity = uint128(FullMath.mulDiv(SUPPLY, 1 << 96, sqrtU - sqrtL));
        m[0].amount0Max = 0; // 0 ETH
        m[0].amount1Max = uint128(SUPPLY);
        m[0].config = HookrTypes.PoolConfig({
            subject: Currency.wrap(t),
            quote: Currency.wrap(address(0)),
            rules: c.rules,
            advisory: c.tax,
            liquidityOwner: address(0), // the launcher sets itself
            baseLpFeePips: BASE_LP_FEE_PIPS,
            caps: ra.caps,
            rulesGasLimit: ra.gasLimit,
            advisoryGasLimit: aa.gasLimit,
            advisoryPhases: HookrTypes.BEFORE_SWAP | HookrTypes.AFTER_SWAP,
            advisoryFailOpen: false,
            policyId: bytes32(0)
        });
        m[0].rules.guardEndBlock = uint40(guardEndBlock);
        m[0].rules.maxFeePips = BASE_LP_FEE_PIPS; // no dynamic fee
        m[0].rules.snipeTaxPips = c.snipePips;
        m[0].rules.snipeDecaySeconds = c.snipeSeconds;
        m[0].rules.protocolShareBps = PROTOCOL_SHARE_BPS;

        IHookrLauncher.MemberKnobs[] memory k = new IHookrLauncher.MemberKnobs[](1);
        k[0].lockEndBlock = uint64(lockEndBlock);

        bytes[] memory adv = new bytes[](1);
        adv[0] = taxConfig(taxRecipient);

        p.token = IHookrLauncher.Token(t, "", "", 0, bytes32(0));
        p.root = c.root;
        p.members = m;
        p.knobs = k;
        p.advisoryConfigs = adv;
        p.maxRetainedBps = 10_000;
        p.deadline = deadline;
    }

    /// @notice Tax + Conversion bind data: abi.encode(Config{buy, sell}), 320 bytes. Flat 1.5% each way from the launch
    ///         block (no decay schedule, no session tiers), no conversion route: the creator share is paid in ETH.
    function taxConfig(address taxRecipient) public pure returns (bytes memory) {
        bytes memory leg = abi.encode(TAX_PIPS, bytes32(0), taxRecipient, address(0), uint32(0));
        return bytes.concat(leg, leg);
    }

    function _verify(Config memory c, MILORDToken t, bytes32 fid, address taxRecipient) internal {
        IHookrLauncher.Position memory pos = c.launcher.position(fid, 0);
        PoolKey memory key = pos.key;
        if (
            Currency.unwrap(key.currency0) != address(0) || Currency.unwrap(key.currency1) != address(t)
                || address(key.hooks) != c.root || key.tickSpacing != TICK_SPACING || pos.tickLower != TICK_LOWER
                || pos.tickUpper != TICK_UPPER || pos.liquidity == 0
        ) revert Mismatch(2);
        PoolId id = key.toId();
        poolId = id;
        IPoolManager pm = c.launcher.poolManager();
        (uint160 sqrtP,,,) = StateLibrary.getSlot0(pm, id);
        if (sqrtP != TickMath.getSqrtPriceAtTick(TICK_UPPER)) revert Mismatch(3);
        if (t.balanceOf(address(pm)) != t.totalSupply() || t.balanceOf(address(this)) != 0) revert Mismatch(4);

        HookrTypes.RulesConfig memory rc = IHookrRulesFork(c.rules).config(id);
        if (
            rc.guardEndBlock != guardEndBlock || rc.maxFeePips != BASE_LP_FEE_PIPS || rc.snipeTaxPips != c.snipePips
                || rc.snipeFloorPips != 0 || rc.snipeDecaySeconds != c.snipeSeconds || rc.dynamicFeeSens != 0
                || rc.burnBps != 0 || rc.lpBps != 0 || rc.potBps != 0 || rc.royaltyBps != 0
                || rc.protocolShareBps != PROTOCOL_SHARE_BPS || rc.maxBuyQuoteAmount != 0 || rc.integrator != address(0)
        ) revert Mismatch(5);
        if (IHookrRulesFork(c.rules).minimumFee(id) != 0) revert Mismatch(6);

        IHookrDirectionalTaxFork.Terms memory tt = IHookrDirectionalTaxFork(c.tax).terms(c.root, id);
        if (!tt.bound || tt.decaying || tt.tiered || tt.buyTaxPips != TAX_PIPS || tt.sellTaxPips != TAX_PIPS) {
            revert Mismatch(7);
        }
        _checkQueue(tt.buyQueue, true, c.rules, taxRecipient);
        _checkQueue(tt.sellQueue, false, c.rules, taxRecipient);
        buyQueue = tt.buyQueue;
        sellQueue = tt.sellQueue;

        if (c.launcher.principalLockedUntil(fid) < lockEndBlock) revert Mismatch(9);
        if (c.launcher.familyOwner(fid) != address(this)) revert Mismatch(10);
    }

    function _checkQueue(address q, bool isBuy, address rules, address taxRecipient) internal view {
        IHookrTaxQueueFork.QueueTerms memory qt = IHookrTaxQueueFork(q).terms();
        if (
            qt.isBuy != isBuy || qt.assetRecipient != taxRecipient || qt.routeId != bytes32(0)
                || Currency.unwrap(qt.quote) != address(0) || qt.protocolShareBps != PROTOCOL_SHARE_BPS
                || qt.protocolRecipient != IHookrRulesFork(rules).protocolRecipient()
        ) revert Mismatch(8);
    }
}
