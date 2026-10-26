// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @notice PRIVATE-FORK REHEARSAL ONLY (fork/rehearse-full-launch.sh). Exact-input buys and sells straight on the
///         PoolManager, standing in for traders on the Hookr MILORD pool. Not part of the protocol, never deployed to
///         a real chain. Holds nothing between calls.
contract RehearsalTrader is IUnlockCallback {
    IPoolManager public immutable pm;

    constructor(IPoolManager pm_) {
        pm = pm_;
    }

    receive() external payable {}

    /// ETH in (msg.value) -> token out to `to`.
    function buy(PoolKey calldata key, address to) external payable returns (uint256 out) {
        out = _run(key, true, msg.value, to);
    }

    /// `amountIn` tokens (pulled from the caller) -> ETH out to `to`.
    function sell(PoolKey calldata key, uint256 amountIn, address to) external returns (uint256 out) {
        IERC20(Currency.unwrap(key.currency1)).transferFrom(msg.sender, address(this), amountIn);
        out = _run(key, false, amountIn, to);
    }

    function _run(PoolKey calldata key, bool buy_, uint256 amountIn, address to) internal returns (uint256 out) {
        BalanceDelta d = abi.decode(pm.unlock(abi.encode(key, buy_, amountIn)), (BalanceDelta));
        int128 got = buy_ ? d.amount1() : d.amount0();
        out = got > 0 ? uint256(uint128(got)) : 0;
        if (buy_) {
            IERC20(Currency.unwrap(key.currency1)).transfer(to, out);
        } else {
            (bool ok,) = to.call{value: out}("");
            require(ok, "eth transfer");
        }
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(pm), "not pool manager");
        (PoolKey memory key, bool buy_, uint256 amountIn) = abi.decode(data, (PoolKey, bool, uint256));
        BalanceDelta d = pm.swap(
            key,
            SwapParams({
                zeroForOne: buy_,
                amountSpecified: -int256(amountIn),
                sqrtPriceLimitX96: buy_ ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            ""
        );
        _settle(key.currency0, d.amount0());
        _settle(key.currency1, d.amount1());
        return abi.encode(d);
    }

    function _settle(Currency c, int128 delta) internal {
        if (delta < 0) {
            uint256 owe = uint256(uint128(-delta));
            if (c.isAddressZero()) {
                pm.settle{value: owe}();
            } else {
                pm.sync(c);
                IERC20(Currency.unwrap(c)).transfer(address(pm), owe);
                pm.settle();
            }
        } else if (delta > 0) {
            pm.take(c, address(this), uint256(uint128(delta)));
        }
    }
}
