"""arb_liquidity.py — Centralized liquidity state for pARB V2."""

from dataclasses import dataclass

from ..config import ARB_IDLE_TARGET_BPS, ARB_SAFETY_BUFFER_USDC


def log(msg: str):
    print(f"💧 [Liquidity] {msg}")


@dataclass
class LiquidityState:
    idle_available: float = 0.0
    total_assets: float = 0.0
    pending_redeem_value: float = 0.0
    required_idle: float = 0.0
    deployable_capital: float = 0.0

    free_cash: float = 0.0
    settled_proceeds: float = 0.0
    pending_withdrawals: float = 0.0
    platform_cash: float = 0.0
    coverable_cash: float = 0.0

    shortfall: float = 0.0
    under_pressure: bool = False
    unwind_needed: bool = False
    ok: bool = True


def compute_liquidity_state(vault_address: str, servicer_wallet: str) -> LiquidityState:
    state = LiquidityState()

    if not vault_address:
        log("⚠️ vault_address not set — liquidity state unavailable")
        state.ok = False
        return state

    try:
        from .arb_reporter import _read_vault_state, _read_usdc_balance
        from .arb_nav import _get_settled_pnl, _get_servicer_balances
        from .arb_withdrawals import get_pending_withdrawal_usdc, mark_withdrawals_arrived

        vault_state = _read_vault_state(vault_address)
        if not vault_state["ok"]:
            log("⚠️ Could not read vault state — liquidity unknown")
            state.ok = False
            return state

        official_pps_raw = vault_state["official_pps"]
        idle_balance = vault_state["idle_balance_usdc"]
        pending_redeem_shares = vault_state["pending_redeem_shares"]

        state.idle_available = idle_balance
        state.pending_redeem_value = (pending_redeem_shares * official_pps_raw) / 1e18 / 1e6

        servicer_usdc = 0.0
        if servicer_wallet:
            try:
                servicer_usdc = _read_usdc_balance(servicer_wallet)
            except Exception as e:
                log(f"⚠️ Could not read servicer balance: {e}")
        state.free_cash = servicer_usdc

        try:
            settled = _get_settled_pnl()
            state.settled_proceeds = max(0.0, settled)
        except Exception as e:
            log(f"⚠️ Could not read settled PnL: {e}")
            state.settled_proceeds = 0.0

        try:
            poly_cash, kalshi_cash, opinion_cash = _get_servicer_balances()
            state.platform_cash = max(0.0, poly_cash) + max(0.0, kalshi_cash) + max(0.0, opinion_cash)
        except Exception as e:
            log(f"⚠️ Could not read platform cash: {e}")
            state.platform_cash = 0.0

        # First reconcile mature recalls against current servicer balance, then read pending amount.
        try:
            mark_withdrawals_arrived(servicer_usdc)
            state.pending_withdrawals = max(0.0, get_pending_withdrawal_usdc())
        except Exception as e:
            log(f"⚠️ Could not read pending recalls: {e}")
            state.pending_withdrawals = 0.0

        state.total_assets = (
            idle_balance
            + servicer_usdc
            + state.settled_proceeds
            + state.platform_cash
        )

        idle_from_target = state.total_assets * ARB_IDLE_TARGET_BPS / 10_000
        idle_from_redeems = state.pending_redeem_value + ARB_SAFETY_BUFFER_USDC
        state.required_idle = max(idle_from_target, idle_from_redeems)

        state.deployable_capital = max(0.0, servicer_usdc - state.required_idle)

        state.shortfall = max(0.0, state.pending_redeem_value - idle_balance)
        state.under_pressure = state.shortfall > 0.0

        # Coverage available without forced unwind:
        # - 90% of free cash
        # - settled proceeds
        # - withdrawals already initiated and in flight
        state.coverable_cash = servicer_usdc * 0.9 + state.settled_proceeds + state.pending_withdrawals

        if state.under_pressure:
            state.unwind_needed = state.shortfall > state.coverable_cash

        state.ok = True

        log(
            f"idle={state.idle_available:.2f} required={state.required_idle:.2f} "
            f"pending_redeem={state.pending_redeem_value:.2f} shortfall={state.shortfall:.2f} "
            f"free_cash={state.free_cash:.2f} settled={state.settled_proceeds:.2f} "
            f"pending_withdrawals={state.pending_withdrawals:.2f} platform_cash={state.platform_cash:.2f} "
            f"coverable={state.coverable_cash:.2f} deployable={state.deployable_capital:.2f} "
            f"under_pressure={state.under_pressure} unwind_needed={state.unwind_needed}"
        )

    except Exception as e:
        log(f"❌ compute_liquidity_state failed: {e}")
        state.ok = False

    return state