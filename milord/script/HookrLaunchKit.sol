// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Vm} from "forge-std/Vm.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {IPositionManager} from "@uniswap/v4-periphery/src/interfaces/IPositionManager.sol";
import {Actions} from "@uniswap/v4-periphery/src/libraries/Actions.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

/// @notice Shared launch steps for tests, the local deploy and (as a reference) the production Safe batch:
///           1. a native-ETH / MILORD v4 pool behind the Hookr Tax + Conversion hook (Hookr opens it on mainnet),
///           2. one MILORD-only concentrated position below the opening price, minted through PositionManager,
///           3. the position NFT handed to its owner (the liquidity lock).
///         Local v4 contracts are the official Uniswap build artifacts; Permit2 must already be at its canonical
///         address (tests etch it, run-local.sh sets it on anvil).
interface IMILORDBurn {
    function burn(uint256 value) external;
}

abstract contract HookrLaunchKit {
    using PoolIdLibrary for PoolKey;
    Vm private constant _vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address internal constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    uint160 internal constant TAX_HOOK_FLAGS = uint160(
        Hooks.BEFORE_SWAP_FLAG | Hooks.AFTER_SWAP_FLAG | Hooks.BEFORE_SWAP_RETURNS_DELTA_FLAG
            | Hooks.AFTER_SWAP_RETURNS_DELTA_FLAG
    );

    struct V4 {
        IPoolManager poolManager;
        IPositionManager positionManager;
        address stateView;
        address quoter;
        address weth;
    }

    /// Launch parameters. Final allocation is 100% of supply (docs/launch/LAUNCH_LIQUIDITY.md); lock, LP fee and commission need owner approval.
    struct LaunchParams {
        uint256 milordForLiquidity; // MILORD placed in the single-sided position
        int24 tickUpper; // opening tick: price = lowest MILORD price, all liquidity is MILORD
        int24 tickLower; // highest MILORD price covered by the position
        uint24 lpFee; // ordinary LP fee in hundredths of a bip (separate from project tax)
        int24 tickSpacing;
    }

    function _deployV4Local(address owner) internal returns (V4 memory v) {
        require(PERMIT2.code.length > 0, "Permit2 missing at canonical address");
        v.poolManager = IPoolManager(_vm.deployCode("node_modules/@uniswap/v4-core/out/PoolManager.sol/PoolManager.json", abi.encode(owner)));
        v.weth = _vm.deployCode("node_modules/@uniswap/v4-periphery/foundry-out/WETH.sol/WETH.default.json");
        v.positionManager = IPositionManager(
            _vm.deployCode(
                "node_modules/@uniswap/v4-periphery/foundry-out/PositionManager.sol/PositionManager.json",
                abi.encode(address(v.poolManager), PERMIT2, uint256(300_000), address(0), v.weth)
            )
        );
        v.stateView = _vm.deployCode(
            "node_modules/@uniswap/v4-periphery/foundry-out/StateView.sol/StateView.json", abi.encode(address(v.poolManager))
        );
        v.quoter = _vm.deployCode(
            "node_modules/@uniswap/v4-periphery/foundry-out/V4Quoter.sol/V4Quoter.json", abi.encode(address(v.poolManager))
        );
    }

    function _milordKey(address milord, address hook, LaunchParams memory p) internal pure returns (PoolKey memory) {
        return PoolKey({
            currency0: Currency.wrap(address(0)),
            currency1: Currency.wrap(milord),
            fee: p.lpFee,
            tickSpacing: p.tickSpacing,
            hooks: IHooks(hook)
        });
    }

    /// @notice Opens the (empty) market at the top of the position: tick == tickUpper, so the position holds
    ///         only MILORD and the first buy moves the price into it.
    function _openMarket(IPoolManager pm, PoolKey memory key, LaunchParams memory p) internal {
        require(p.tickUpper % p.tickSpacing == 0 && p.tickLower % p.tickSpacing == 0, "ticks off spacing");
        require(p.tickLower < p.tickUpper, "range");
        pm.initialize(key, TickMath.getSqrtPriceAtTick(p.tickUpper));
    }

    /// @notice Mints the MILORD-only position from the caller's MILORD (Permit2 approval) to `owner_`.
    /// @return tokenId the position NFT id
    function _addMilordOnlyLiquidity(V4 memory v, PoolKey memory key, LaunchParams memory p, address owner_)
        internal
        returns (uint256 tokenId)
    {
        address milord = Currency.unwrap(key.currency1);
        uint128 liquidity = LiquidityAmounts.getLiquidityForAmount1(
            TickMath.getSqrtPriceAtTick(p.tickLower), TickMath.getSqrtPriceAtTick(p.tickUpper), p.milordForLiquidity
        );
        IERC20(milord).approve(PERMIT2, p.milordForLiquidity);
        IAllowanceTransfer(PERMIT2).approve(
            milord, address(v.positionManager), uint160(p.milordForLiquidity), uint48(block.timestamp + 1 hours)
        );
        bytes memory actions = abi.encodePacked(uint8(Actions.MINT_POSITION), uint8(Actions.SETTLE_PAIR));
        bytes[] memory params = new bytes[](2);
        params[0] = abi.encode(key, p.tickLower, p.tickUpper, uint256(liquidity), uint128(0), uint128(p.milordForLiquidity), owner_, bytes(""));
        params[1] = abi.encode(key.currency0, key.currency1);
        tokenId = v.positionManager.nextTokenId();
        v.positionManager.modifyLiquidities(abi.encode(actions, params), block.timestamp + 1 hours);
    }

    /// @notice Burns the MILORD the position could not take (integer rounding of liquidity: at most a few tens of
    ///         thousands of wei), so the account that seeded 100% of supply ends with exactly 0 MILORD.
    /// `holder` is the account that seeded the position and sends the burn (the broadcaster in a script).
    function _burnLaunchRemainder(address milord, address holder, uint256 maxDust) internal returns (uint256 dust) {
        dust = IERC20(milord).balanceOf(holder);
        require(dust <= maxDust, "remainder is not rounding dust");
        if (dust > 0) IMILORDBurn(milord).burn(dust);
    }

    /// @notice Two-sided ETH / token position (used locally for the ETH -> USDG route).
    function _addEthTokenLiquidity(
        V4 memory v,
        PoolKey memory key,
        int24 tickLower,
        int24 tickUpper,
        uint256 ethAmount,
        uint256 tokenAmount,
        address owner_
    ) internal returns (uint256 tokenId) {
        address token = Currency.unwrap(key.currency1);
        (uint160 sqrtP,,,) = _slot0(v.poolManager, key);
        uint128 liquidity = LiquidityAmounts.getLiquidityForAmounts(
            sqrtP, TickMath.getSqrtPriceAtTick(tickLower), TickMath.getSqrtPriceAtTick(tickUpper), ethAmount, tokenAmount
        );
        IERC20(token).approve(PERMIT2, tokenAmount);
        IAllowanceTransfer(PERMIT2).approve(token, address(v.positionManager), uint160(tokenAmount), uint48(block.timestamp + 1 hours));
        bytes memory actions = abi.encodePacked(uint8(Actions.MINT_POSITION), uint8(Actions.SETTLE_PAIR), uint8(Actions.SWEEP));
        bytes[] memory params = new bytes[](3);
        params[0] = abi.encode(key, tickLower, tickUpper, uint256(liquidity), uint128(ethAmount), uint128(tokenAmount), owner_, bytes(""));
        params[1] = abi.encode(key.currency0, key.currency1);
        params[2] = abi.encode(key.currency0, owner_);
        tokenId = v.positionManager.nextTokenId();
        v.positionManager.modifyLiquidities{value: ethAmount}(abi.encode(actions, params), block.timestamp + 1 hours);
    }

    function _slot0(IPoolManager pm, PoolKey memory key)
        internal
        view
        returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)
    {
        return StateLibrary.getSlot0(pm, key.toId());
    }
}
