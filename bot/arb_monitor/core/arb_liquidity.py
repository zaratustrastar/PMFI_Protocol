"""arb_liquidity.py — Centralized liquidity state for pARB V2 / V3.

Fail-closed contract (C1): callers must check `trading_allowed` before deploying capital.
  ok=False               vault state unreadable (as before)
  trading_allowed=False  vault state unreadable OR any liquidity input failed to read;
                         `blocked_reason` says why.
`ok` keeps its legacy meaning (vault state readable) so the existing gates in the execution
loop still run; a component read failure no longer silently becomes 0 in coverable cash.
"""

from dataclasses import dataclass, field

from ..config import ARB_IDLE_TARGET_BPS, ARB_SAFETY_BUFFER_USDC, is_robinhood_funding


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
    # Kept for log/back-compat only; always 0. Historical settled PnL is NOT cash: realised
    # proceeds are already inside platform/servicer cash, so counting it again in coverable
    # cash (and total assets) double counted it.
    settled_proceeds: float = 0.0
    pending_withdrawals: float = 0.0
    platform_cash: float = 0.0
    coverable_cash: float = 0.0

    shortfall: float = 0.0
    under_pressure: bool = False
    unwind_needed: bool = False
    ok: bool = True
    trading_allowed: bool = True
    blocked_reason: str = ""
    failed_components: list = field(default_factory=list)


def _block(state: LiquidityState, component: str, err) -> None:
    state.failed_components.append(f"{component}: {err}")
    state.trading_allowed = False
    state.blocked_reason = "liquidity input unreadable: " + "; ".join(state.failed_components)
    log(f"⚠️ Could not read {component}: {err} — trading blocked this cycle")


def compute_liquidity_state(vault_address: str, servicer_wallet: str) -> LiquidityState:
    state = LiquidityState()

    if not vault_address:
        log("⚠️ vault_address not set — liquidity state unavailable")
        state.ok = False
        state.trading_allowed = False
        state.blocked_reason = "vault_address not set"
        return state

    try:
        from .arb_reporter import _read_vault_state, _read_usdc_balance
        from .arb_nav import _get_servicer_balances
        from .arb_withdrawals import get_pending_withdrawal_usdc, mark_withdrawals_arrived

        vault_state = _read_vault_state(vault_address)
        if not vault_state["ok"]:
            log("⚠️ Could not read vault state — liquidity unknown")
            state.ok = False
            state.trading_allowed = False
            state.blocked_reason = f"vault state unreadable: {vault_state.get('error', '')}"
            return state

        official_pps_raw = vault_state["official_pps"]
        idle_balance = vault_state["idle_balance_usdc"]
        pending_redeem_shares = vault_state["pending_redeem_shares"]

        state.idle_available = idle_balance
        state.pending_redeem_value = (pending_redeem_shares * official_pps_raw) / 1e18 / 1e6

        servicer_usdc = 0.0
        base_usdc = None
        if servicer_wallet:
            try:
                if is_robinhood_funding():
                    # Servicer cash = USDG on 4663; Base USDC only matters for Kalshi recalls.
                    from .arb_funder import get_servicer_cash_usdc, _get_usdc_balance
                    servicer_usdc = get_servicer_cash_usdc(servicer_wallet)
                    base_usdc = _get_usdc_balance(servicer_wallet)
                else:
                    servicer_usdc = _read_usdc_balance(servicer_wallet)
            except Exception as e:
                _block(state, "servicer_balance", e)
        state.free_cash = servicer_usdc

        try:
            poly_cash, kalshi_cash, opinion_cash = _get_servicer_balances(strict=True)
            state.platform_cash = max(0.0, poly_cash) + max(0.0, kalshi_cash) + max(0.0, opinion_cash)
        except Exception as e:
            _block(state, "platform_cash", e)
            state.platform_cash = 0.0

        # First reconcile mature recalls against current servicer balance, then read pending amount.
        try:
            if state.trading_allowed:   # never reconcile against a balance that failed to read
                mark_withdrawals_arrived(servicer_usdc, base_usdc)
            state.pending_withdrawals = max(0.0, get_pending_withdrawal_usdc())
        except Exception as e:
            _block(state, "pending_recalls", e)
            state.pending_withdrawals = 0.0

        state.total_assets = (
            idle_balance
            + servicer_usdc
            + state.platform_cash
            + state.pending_withdrawals
        )

        idle_from_target = state.total_assets * ARB_IDLE_TARGET_BPS / 10_000
        idle_from_redeems = state.pending_redeem_value + ARB_SAFETY_BUFFER_USDC
        state.required_idle = max(idle_from_target, idle_from_redeems)

        state.deployable_capital = max(0.0, servicer_usdc - state.required_idle)
        if not state.trading_allowed:
            state.deployable_capital = 0.0

        state.shortfall = max(0.0, state.pending_redeem_value - idle_balance)
        state.under_pressure = state.shortfall > 0.0

        # Coverage available without forced unwind:
        # - 90% of free cash
        # - withdrawals already initiated and in flight
        # (settled PnL is history, not cash — no longer counted)
        state.coverable_cash = servicer_usdc * 0.9 + state.pending_withdrawals

        if state.under_pressure:
            state.unwind_needed = state.shortfall > state.coverable_cash

        state.ok = True

        log(
            f"idle={state.idle_available:.2f} required={state.required_idle:.2f} "
            f"pending_redeem={state.pending_redeem_value:.2f} shortfall={state.shortfall:.2f} "
            f"free_cash={state.free_cash:.2f} "
            f"pending_withdrawals={state.pending_withdrawals:.2f} platform_cash={state.platform_cash:.2f} "
            f"coverable={state.coverable_cash:.2f} deployable={state.deployable_capital:.2f} "
            f"under_pressure={state.under_pressure} unwind_needed={state.unwind_needed} "
            f"trading_allowed={state.trading_allowed}"
        )

    except Exception as e:
        log(f"❌ compute_liquidity_state failed: {e}")
        state.ok = False
        state.trading_allowed = False
        state.blocked_reason = f"compute_liquidity_state failed: {e}"

    return state
