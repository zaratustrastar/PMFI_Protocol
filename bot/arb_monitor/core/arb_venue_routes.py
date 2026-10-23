"""MILORD (robinhood mode) venue recall routes, checked against the venues' official documentation (B9).

Polymarket: collateral has been pUSD since the CLOB V2 upgrade (2026-04-28). The legacy recall script moves
USDC.e (Safe -> EOA -> Relay), so robinhood mode refuses it. The supported route is Polymarket's bridge:
POST /withdraw (toChainId 4663, toTokenAddress USDG) and one Safe pUSD transfer to the returned address
(trading_bot/poly_bridge_withdraw.mjs). It stays disabled until an owner-approved small withdrawal has
verified it (ARB_POLY_BRIDGE_ROUTE_VERIFIED=true, read by the script).

Kalshi: the public API documents GET /portfolio/withdrawals only; crypto withdrawals are a UI flow (prior
crypto deposit required, Zero Hash in the US). The engine's POST /portfolio/balance/withdrawals is not in the
documentation, so robinhood mode does not call it unless ARB_KALSHI_API_WITHDRAW_VERIFIED=true; otherwise a
Kalshi recall is an operator task.

Base mode (the live PMFI engine) never reaches this module.
"""
import json
import os
import subprocess
from pathlib import Path

POLY_BRIDGE = "polymarket_bridge"


def poly_recall_route() -> str:
    return os.environ.get("ARB_POLY_WITHDRAW_ROUTE", "").strip().lower()


def kalshi_api_withdraw_allowed() -> bool:
    return os.environ.get("ARB_KALSHI_API_WITHDRAW_VERIFIED") == "true"


def to_raw(amount_usdc: float) -> int:
    raw = int(round(float(amount_usdc) * 1_000_000))
    if raw <= 0:
        raise ValueError("amount must be positive")
    return raw


def poly_bridge(action: str, timeout: int = 240, **data) -> dict:
    """Run trading_bot/poly_bridge_withdraw.mjs. Returns its last JSON line; exit 3 means outcome unknown."""
    script = Path(__file__).resolve().parents[3] / "trading_bot" / "poly_bridge_withdraw.mjs"
    r = subprocess.run(["node", str(script)], input=json.dumps({"action": action, **data}),
                       capture_output=True, text=True, timeout=timeout)
    lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(f"poly_bridge_withdraw {action}: no output (exit {r.returncode}): {(r.stderr or '')[:300]}")
    out = json.loads(lines[-1])
    out["_exit"] = r.returncode
    # the bridge address is printed to stderr before the Safe transaction (recovery via /status)
    for l in (r.stderr or "").splitlines():
        try:
            j = json.loads(l)
            if isinstance(j, dict) and j.get("bridgeAddress"):
                out.setdefault("bridgeAddress", j["bridgeAddress"])
        except ValueError:
            pass
    return out


def withdraw_polymarket_bridge(amount_usdc: float, runner=None) -> tuple[bool, str, float, str | None]:
    """(ok, reference_or_error, amount_usdc, bridge_address). ok=False with reference starting 'ambiguous' when
    the Safe transfer may have been submitted (never retried automatically)."""
    raw = to_raw(amount_usdc)
    runner = runner or poly_bridge
    try:
        out = runner("withdraw", amountRaw=str(raw))
    except Exception as e:  # timeout or crash after the bridge address may have been funded
        return False, f"ambiguous: {type(e).__name__}: {str(e)[:200]}", 0.0, None
    if out.get("txHash"):
        return True, out["txHash"], raw / 1_000_000, out.get("bridgeAddress")
    if out.get("unknown") or out.get("_exit") == 3:
        return False, f"ambiguous: Safe pUSD transfer outcome unknown ({out.get('error', '')[:200]})", 0.0, out.get("bridgeAddress")
    return False, str(out.get("error") or out)[:300], 0.0, out.get("bridgeAddress")
