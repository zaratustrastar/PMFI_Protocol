"""arb_allowlist.py — per-chain token / bridge-contract / venue-deposit allowlists.

Fail-closed checks applied to every Robinhood-mode funding tx and to bridge quote responses:
  * (chain, token) must be allowlisted: USDG on 4663, USDC on Base, USDC.e on Polygon (addresses
    from config) plus ARB_ALLOWED_TOKENS_EXTRA;
  * bridge spenders / routers / deposit targets must be in ARB_BRIDGE_ALLOWLIST for that chain
    (empty allowlist = every bridge tx refused);
  * approvals: approve(spender, amount) on the origin token, spender allowlisted,
    0 < amount <= quoted input amount (never unlimited);
  * deposit tx `to` allowlisted and not the token itself; native value 0 unless
    ARB_BRIDGE_ALLOW_NATIVE_VALUE=true;
  * when the quote response names a recipient / destination chain / token, it must match;
  * venue deposit addresses are tied to a chain + token and validated at startup.
"""

import os
import re

from ..config import (
    usdg_address,
    robinhood_chain_id,
    usdc_base_address,
    usdc_polygon_address,
    allowed_tokens_extra_raw,
    bridge_allowlist_raw,
    bridge_allow_native_value,
    venue_deposit_declaration,
    poly_rh_deposit_addr,
    is_robinhood_funding,
)

BASE_CHAIN_ID = 8453
POLYGON_CHAIN_ID = 137
SELECTOR_APPROVE = "095ea7b3"
SELECTOR_TRANSFER = "a9059cbb"
ZERO = "0x" + "0" * 40
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class AllowlistError(RuntimeError):
    pass


def is_address(a) -> bool:
    return isinstance(a, str) and bool(_ADDR_RE.match(a)) and a.lower() != ZERO


def _parse_pairs(raw: str, errors: list | None = None) -> set:
    out = set()
    for item in (raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            chain, addr = item.split(":", 1)
            chain_i = int(chain.strip())
            addr = addr.strip()
            if not is_address(addr):
                raise ValueError("bad address")
            out.add((chain_i, addr.lower()))
        except Exception:
            if errors is not None:
                errors.append(f"invalid allowlist entry {item!r} (expected chainId:0xaddress)")
    return out


def allowed_tokens() -> set:
    base = {
        (robinhood_chain_id(), usdg_address().lower()),
        (BASE_CHAIN_ID, usdc_base_address().lower()),
        (POLYGON_CHAIN_ID, usdc_polygon_address().lower()),
    }
    return base | _parse_pairs(allowed_tokens_extra_raw())


def allowed_bridge_contracts() -> set:
    return _parse_pairs(bridge_allowlist_raw())


def require_token(chain_id: int, token: str) -> None:
    if (int(chain_id), str(token).lower()) not in allowed_tokens():
        raise AllowlistError(f"token {token} on chain {chain_id} is not allowlisted")


def require_bridge_contract(chain_id: int, addr: str, role: str = "bridge contract") -> None:
    allowed = allowed_bridge_contracts()
    if not allowed:
        raise AllowlistError("ARB_BRIDGE_ALLOWLIST is empty — bridge txs refused")
    if (int(chain_id), str(addr).lower()) not in allowed:
        raise AllowlistError(f"{role} {addr} on chain {chain_id} is not in ARB_BRIDGE_ALLOWLIST")


def _int(v) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def decode_erc20_call(data: str, selector: str) -> tuple[str, int] | None:
    """Return (address, amount) for an exact `selector(address,uint256)` calldata, else None."""
    d = str(data or "").lower()
    if d.startswith("0x"):
        d = d[2:]
    if len(d) != 8 + 128 or not d.startswith(selector):
        return None
    word_addr, word_amt = d[8:72], d[72:136]
    if word_addr[:24] != "0" * 24:
        return None
    return "0x" + word_addr[24:], int(word_amt, 16)


def validate_bridge_txs(txs: list, chain_id: int, token: str, input_raw: int) -> None:
    """Validate a quote's tx list: approvals first, the value-moving deposit last."""
    require_token(chain_id, token)
    if not txs:
        raise AllowlistError("quote has no transactions")
    allow_value = bridge_allow_native_value()
    for tx in txs:
        if not is_address(tx.get("to")):
            raise AllowlistError(f"quote tx has invalid `to`: {tx.get('to')!r}")
        if _int(tx.get("value")) != 0 and not allow_value:
            raise AllowlistError(f"quote tx carries native value {tx.get('value')}")
        cid = tx.get("chainId")
        if cid not in (None, "") and _int(cid) != int(chain_id):
            raise AllowlistError(f"quote tx chainId {cid} != origin chain {chain_id}")
    for tx in txs[:-1]:
        if tx["to"].lower() != token.lower():
            raise AllowlistError(f"unexpected pre-deposit tx to {tx['to']} (only origin-token approvals allowed)")
        dec = decode_erc20_call(tx.get("data", ""), SELECTOR_APPROVE)
        if not dec:
            raise AllowlistError("pre-deposit tx is not an exact approve(address,uint256)")
        spender, amount = dec
        require_bridge_contract(chain_id, spender, "approve spender")
        if amount <= 0 or amount > int(input_raw):
            raise AllowlistError(f"approve amount {amount} not in (0, input {input_raw}] (unlimited/excess approval)")
    final = txs[-1]
    if final["to"].lower() == token.lower():
        raise AllowlistError("deposit tx targets the token contract directly")
    require_bridge_contract(chain_id, final["to"], "deposit tx `to`")


def validate_route(route: dict | None, *, recipient: str, origin_chain: int, origin_token: str,
                   dest_chain: int, dest_token: str) -> None:
    """Check whatever destination details the quote response exposes (absent fields are skipped)."""
    route = route or {}
    checks = (
        ("recipient", recipient, str),
        ("origin_chain", origin_chain, int),
        ("origin_token", origin_token, str),
        ("dest_chain", dest_chain, int),
        ("dest_token", dest_token, str),
    )
    for field, expected, typ in checks:
        got = route.get(field)
        if got in (None, ""):
            continue
        if typ is int:
            ok = _int(got) == int(expected)
        else:
            ok = str(got).lower() == str(expected).lower()
        if not ok:
            raise AllowlistError(f"quote {field}={got} != expected {expected}")


# ---------------------------------------------------------------------------
# Venue deposit addresses
# ---------------------------------------------------------------------------

def venue_route(venue: str) -> tuple[int, str]:
    """(chain, token) a venue deposit address must accept in robinhood mode."""
    if venue == "polymarket":
        return robinhood_chain_id(), usdg_address()
    if venue == "kalshi":
        return BASE_CHAIN_ID, usdc_base_address()
    raise AllowlistError(f"no V1 deposit route for venue {venue!r}")


def _forbidden_deposit_targets() -> set:
    s = {a for _, a in allowed_tokens()} | {a for _, a in allowed_bridge_contracts()}
    for env in ("ARB_SERVICER_WALLET", "ARB_VAULT_V2_ADDRESS"):
        v = os.environ.get(env, "")
        if v:
            s.add(v.lower())
    return s


def _check_declaration(venue: str, chain_id: int, token: str, required: bool) -> list:
    errs = []
    d_chain, d_token = venue_deposit_declaration(venue)
    if required and (not d_chain or not d_token):
        prefix = {"polymarket": "POLY_RH_DEPOSIT", "kalshi": "KALSHI_BASE_DEPOSIT"}[venue]
        errs.append(f"{prefix}_CHAIN_ID and {prefix}_TOKEN must be set (tie the {venue} deposit address to a chain+token)")
    if d_chain and (not d_chain.isdigit() or int(d_chain) != int(chain_id)):
        errs.append(f"{venue} deposit declared chain {d_chain} but the V1 route sends on chain {chain_id}")
    if d_token and d_token.lower() != str(token).lower():
        errs.append(f"{venue} deposit declared token {d_token} but the V1 route sends {token}")
    return errs


def require_venue_deposit(venue: str, chain_id: int, token: str, to: str) -> None:
    """Per-send check: the (chain, token) is the venue's V1 route and `to` is a plausible deposit address."""
    exp_chain, exp_token = venue_route(venue)
    if int(chain_id) != int(exp_chain) or str(token).lower() != exp_token.lower():
        raise AllowlistError(f"{venue} deposit on chain {chain_id} token {token} is not the V1 route ({exp_chain}, {exp_token})")
    require_token(chain_id, token)
    if not is_address(to):
        raise AllowlistError(f"{venue} deposit address {to!r} is not a valid address")
    if to.lower() in _forbidden_deposit_targets():
        raise AllowlistError(f"{venue} deposit address {to} collides with a token/bridge/servicer/vault address")
    errs = _check_declaration(venue, chain_id, token, required=False)
    if errs:
        raise AllowlistError("; ".join(errs))


def validate_funding_config(kalshi_deposit_addr: str | None = None) -> list[str]:
    """Startup validation. Returns a list of problems; empty list = OK.

    In robinhood mode a non-empty list must block trading (arb_execution_loop does this).
    """
    if not is_robinhood_funding():
        return []
    errs: list[str] = []
    _parse_pairs(bridge_allowlist_raw(), errs)
    _parse_pairs(allowed_tokens_extra_raw(), errs)
    bridges = allowed_bridge_contracts()
    if not bridges:
        errs.append("ARB_BRIDGE_ALLOWLIST is empty — Kalshi funding and Base→USDG sweeps would be refused")
    else:
        chains = {c for c, _ in bridges}
        for need in (robinhood_chain_id(), BASE_CHAIN_ID):
            if need not in chains:
                errs.append(f"ARB_BRIDGE_ALLOWLIST has no entry for chain {need}")
    if not is_address(usdg_address()):
        errs.append(f"ARB_USDG_ADDRESS {usdg_address()!r} is not a valid address")
    if kalshi_deposit_addr is None:
        kalshi_deposit_addr = os.environ.get("KALSHI_BASE_DEPOSIT_ADDR", "")
    for venue, addr in (("polymarket", poly_rh_deposit_addr()), ("kalshi", kalshi_deposit_addr)):
        chain_id, token = venue_route(venue)
        if not addr:
            errs.append(f"{venue} deposit address not set")
        elif not is_address(addr):
            errs.append(f"{venue} deposit address {addr!r} is not a valid address")
        elif addr.lower() in _forbidden_deposit_targets():
            errs.append(f"{venue} deposit address {addr} collides with a token/bridge/servicer/vault address")
        errs.extend(_check_declaration(venue, chain_id, token, required=True))
    if poly_rh_deposit_addr() and kalshi_deposit_addr and poly_rh_deposit_addr().lower() == kalshi_deposit_addr.lower():
        errs.append("polymarket and kalshi deposit addresses are identical")
    return errs
