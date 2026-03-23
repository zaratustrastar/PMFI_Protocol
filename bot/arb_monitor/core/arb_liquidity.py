"""arb_liquidity.py — Centralized liquidity state for pARB V2.

Computes the full set of metrics required for liquidity-aware vault management:

    idleAvailable      — USDC currently idle inside the vault contract
    requiredIdle       — max(idleTargetBps * totalAssets, pendingRedeemValue + safetyBuffer)
    pendingRedeemValue — USDC owed to all queued redeemers at current officialPPS
    deployableCapital  — max(0, servicer_usdc - requiredIdle)
    freeCash           — servicer wallet USDC on Base
    settledProceeds    — realized (settled) PnL from closed positions
    shortfall          — max(0, pendingRedeemValue - idleAvailable)
    underPressure      — shortfall > 0
    unwindNeeded       — shortfall cannot be covered by free cash + settled proceeds

Single source of truth — imported by arb_funder, arb_reporter, arb_execution_loop.
Never raises; returns LiquidityState(ok=False) on any error.
"""

import os
from dataclasses import dataclass, field

from ..config import ARB_IDLE_TARGET_BPS, ARB_SAFETY_BUFFER_USDC


def log(msg: str):
    print(f"💧 [Liquidity] {msg}")


@dataclass
class LiquidityState:
    # ── vault-side ────────────────────────────────────────────────────────────
    idle_available: float      = 0.0   # USDC idle in vault contract right now
    total_assets: float        = 0.0   # conservative estimate of all assets

    # ── redeem pressure ───────────────────────────────────────────────────────
    pending_redeem_value: float = 0.0  # USDC owed at current officialPPS

    # ── target / deployable ───────────────────────────────────────────────────
    required_idle: float       = 0.0   # max(target %, pending + buffer)
    deployable_capital: float  = 0.0   # servicer_usdc beyond required_idle

    # ── cash layers (waterfall) ───────────────────────────────────────────────
    free_cash: float           = 0.0   # servicer wallet USDC on Base
    settled_proceeds: float    = 0.0   # settled / realized PnL from positions

    # ── flags ─────────────────────────────────────────────────────────────────
    shortfall: float           = 0.0   # pending_redeem_value - idle_available (≥ 0)
    under_pressure: bool       = False # shortfall > 0
    unwind_needed: bool        = False # shortfall > free_cash + settled proceeds
    ok: bool                   = True  # False means compute failed (treat as unknown)


def compute_liquidity_state(vault_address: str, servicer_wallet: str) -> LiquidityState:
    """Compute the full liquidity state from on-chain vault data + servicer wallet + DB.

    Args:
        vault_address:   deployed PMFIArbVaultV2 address (empty → ok=False)
        servicer_wallet: servicer wallet address on Base (empty → free_cash=0)

    Returns LiquidityState. Never raises.
    """
    state = LiquidityState()

    if not vault_address:
        log("⚠️ vault_address not set — liquidity state unavailable")
        state.ok = False
        return state

    try:
        # ── 1. Read vault on-chain state ──────────────────────────────────────
        from .arb_reporter import _read_vault_state, _read_usdc_balance
        vault_state = _read_vault_state(vault_address)
        if not vault_state["ok"]:
            log("⚠️ Could not read vault state — liquidity unknown")
            state.ok = False
            return state

        official_pps_raw      = vault_state["official_pps"]        # wei (USDC × 1e6 per 1e18 shares)
        idle_balance          = vault_state["idle_balance_usdc"]    # already in USDC float
        pending_redeem_shares = vault_state["pending_redeem_shares"] # raw token units (1e18)

        state.idle_available       = idle_balance
        state.pending_redeem_value = (pending_redeem_shares * official_pps_raw) / 1e18 / 1e6

        # ── 2. Servicer wallet free cash ──────────────────────────────────────
        servicer_usdc = 0.0
        if servicer_wallet:
            try:
                servicer_usdc = _read_usdc_balance(servicer_wallet)
            except Exception as e:
                log(f"⚠️ Could not read servicer balance: {e}")
        state.free_cash = servicer_usdc

        # ── 3. Settled proceeds (realized PnL from closed arb positions) ──────
        try:
            from .arb_nav import _get_settled_pnl
            settled = _get_settled_pnl()
            state.settled_proceeds = max(0.0, settled)
        except Exception as e:
            log(f"⚠️ Could not read settled PnL: {e}")
            state.settled_proceeds = 0.0

        # ── 4. Total asset estimate (conservative, no position marks) ─────────
        # idle_balance already excludes claimable redeems (contract handles that)
        state.total_assets = idle_balance + servicer_usdc + state.settled_proceeds

        # ── 5. Required idle = max(target %, pending redeems + safety buffer) ─
        idle_from_target  = state.total_assets * ARB_IDLE_TARGET_BPS / 10_000
        idle_from_redeems = state.pending_redeem_value + ARB_SAFETY_BUFFER_USDC
        state.required_idle = max(idle_from_target, idle_from_redeems)

        # ── 6. Deployable = servicer cash beyond required idle ─────────────────
        state.deployable_capital = max(0.0, servicer_usdc - state.required_idle)

        # ── 7. Pressure / unwind flags ────────────────────────────────────────
        state.shortfall      = max(0.0, state.pending_redeem_value - idle_balance)
        state.under_pressure = state.shortfall > 0.0
        if state.under_pressure:
            # Can we cover it with servicer free cash + settled proceeds?
            coverable = servicer_usdc * 0.9 + state.settled_proceeds
            state.unwind_needed = state.shortfall > coverable

        state.ok = True

        log(
            f"idle={state.idle_available:.2f} required={state.required_idle:.2f} "
            f"pending_redeem={state.pending_redeem_value:.2f} shortfall={state.shortfall:.2f} "
            f"free_cash={state.free_cash:.2f} settled={state.settled_proceeds:.2f} "
            f"deployable={state.deployable_capital:.2f} "
            f"under_pressure={state.under_pressure} unwind_needed={state.unwind_needed}"
        )

    except Exception as e:
        log(f"❌ compute_liquidity_state failed: {e}")
        state.ok = False

    return state
