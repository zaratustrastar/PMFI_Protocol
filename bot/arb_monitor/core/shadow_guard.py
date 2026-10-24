"""Execution mode and the shadow-mode write barrier.

ARB_EXECUTION_MODE:
  live    the engine funds venues, places orders, reports NAV, tends the vault (unchanged behaviour).
  shadow  read-only: public price discovery, live order books, fee and edge calculations, sizing on a
          notional amount and an isolated shadow journal. Nothing that changes state outside the shadow
          journal may run: no vault report or tend, no funding / bridge / withdrawal / approval, no
          JSON-RPC transaction broadcast, no venue order or cancel, no ledger or positions-DB write.

Default: shadow in robinhood mode (the pilot), live in base mode (the existing PMFI engine).

The execution loop runs a separate read-only cycle in shadow mode (arb_execution_loop._shadow_cycle).
assert_not_shadow() is the second line of defence, called at every state-changing choke point (RPC
broadcast, order placement and cancel, venue withdrawals, bridge and redemption transports, ledger and
positions-DB writes), so a code path that is reached by mistake fails instead of acting.
"""

import os

WRITE_RPC_METHODS = frozenset({"eth_sendRawTransaction", "eth_sendTransaction", "eth_signTransaction"})


class ShadowModeViolation(RuntimeError):
    """A state-changing action was attempted while ARB_EXECUTION_MODE=shadow."""


def execution_mode() -> str:
    from ..config import is_robinhood_funding

    default = "shadow" if is_robinhood_funding() else "live"
    mode = os.environ.get("ARB_EXECUTION_MODE", default).strip().lower()
    if mode not in ("live", "shadow"):
        raise ValueError(f"ARB_EXECUTION_MODE={mode!r}; expected live or shadow")
    return mode


def is_shadow() -> bool:
    return execution_mode() == "shadow"


def assert_not_shadow(action: str) -> None:
    if is_shadow():
        raise ShadowModeViolation(f"shadow mode: refused {action}")


def assert_rpc_allowed(method: str) -> None:
    if method in WRITE_RPC_METHODS:
        assert_not_shadow(f"JSON-RPC {method}")


def shadow_capital_usdc() -> float:
    """Notional capital the shadow cycle sizes against (no account balances are read)."""
    from ..config import ARB_MAX_PAIR_USDC

    return float(os.environ.get("ARB_SHADOW_CAPITAL_USDC", str(ARB_MAX_PAIR_USDC)))
