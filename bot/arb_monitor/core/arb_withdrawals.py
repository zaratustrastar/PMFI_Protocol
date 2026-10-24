"""arb_withdrawals.py — Platform cash withdrawal functions for pARB waterfall.

Called by run_withdrawal_waterfall() in arb_reporter.py when the vault needs
to pull cash back from trading platforms to cover pending redemptions.

This module also persists withdrawal intents so liquidity logic can count
"in-flight" recalls before funds land on Base.
"""

import json
import os
import time
from typing import Any

import requests

from ..config import (
    KALSHI_BASE_URL,
    OPINION_BASE_URL,
    is_robinhood_funding,
    opinion_enabled,
    robinhood_chain_id,
    usdg_address,
    across_api_url,
    relay_api_url,
    rh_sweep_min_usdc,
    usdc_base_address,
    usdc_polygon_address,
    bridge_max_fee_bps,
    arrival_tolerance_bps,
    inflight_stale_secs,
)
from . import arb_ledger as _L
from .arb_http import request_json

POLY_CLOB_URL = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
RELAY_API_URL = "https://api.relay.link"
BASE_CHAIN_ID = 8453

BSC_CHAIN_ID = 56
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
LIFI_API = "https://li.quest/v1"
BSC_RPC = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.binance.org")

WITHDRAWAL_LEDGER_PATH = os.environ.get(
    "ARB_WITHDRAWAL_LEDGER_PATH",
    os.path.expanduser("~/PolyNotifyBot/data/arb_withdrawals_ledger.json"),
)

# Outbound/inbound bridge transfers (robinhood mode): recorded when sent, cleared on arrival,
# counted as in-transit by arb_nav.compute_nav so NAV does not dip while money is in a bridge.
BRIDGE_LEDGER_PATH = os.environ.get(
    "ARB_BRIDGE_LEDGER_PATH",
    os.path.join(os.path.dirname(WITHDRAWAL_LEDGER_PATH), "arb_bridge_ledger.json"),
)
# Bridges with no status API (Polymarket deposit address) are assumed landed after this.
BRIDGE_DEFAULT_ETA_SECS = int(os.environ.get("ARB_BRIDGE_ETA_SECS", "1800"))

POLY_ESTIMATED_SETTLEMENT_SECS = int(os.environ.get("ARB_POLY_WITHDRAW_SETTLEMENT_SECS", "1800"))
KALSHI_ESTIMATED_SETTLEMENT_SECS = int(os.environ.get("ARB_KALSHI_WITHDRAW_SETTLEMENT_SECS", "3600"))
OPINION_ESTIMATED_SETTLEMENT_SECS = int(os.environ.get("ARB_OPINION_WITHDRAW_SETTLEMENT_SECS", "2700"))


def log(msg: str):
    print(f"💸 [ArbWithdrawals] {msg}")


def _ensure_ledger_dir(path: str | None = None) -> None:
    os.makedirs(os.path.dirname(path or WITHDRAWAL_LEDGER_PATH), exist_ok=True)


# All ledger access goes through arb_ledger (fcntl lock around every load-modify-save).
# _load_ledger/_save_ledger keep their legacy signatures for callers and tests.

def _load_ledger(path: str | None = None) -> list[dict[str, Any]]:
    path = path or WITHDRAWAL_LEDGER_PATH
    try:
        return _L.read_rows(path)
    except Exception as e:
        log(f"⚠️ Could not load ledger {path}: {e}")
        return []


def _save_ledger(rows: list[dict[str, Any]], path: str | None = None) -> None:
    path = path or WITHDRAWAL_LEDGER_PATH
    try:
        _L.write_rows(path, rows)
    except Exception as e:
        log(f"⚠️ Could not save ledger {path}: {e}")


def get_ledger_snapshot() -> dict:
    """Read-only, lock-consistent copy of every funding ledger (for NAV / monitoring)."""
    return {
        "withdrawals": _L.read_rows(WITHDRAWAL_LEDGER_PATH),
        "bridges": _L.read_rows(BRIDGE_LEDGER_PATH),
        "venue_deposits": _L.read_rows(_L.venue_deposit_path()),
        "intents": _L.read_rows(_L.intents_path()),
        "taken_at": int(time.time()),
    }


# Rows in these states still represent money owed to the servicer (counted in NAV / liquidity).
COUNTED_STATUSES = ("pending", "stale")


def _dest_chain_token(dest: str | None) -> tuple[int, str]:
    if dest == "rh_usdg":
        return robinhood_chain_id(), usdg_address()
    return BASE_CHAIN_ID, usdc_base_address()


def _arrived(balance: float, before: float, amount: float, out: float, inn: float) -> tuple[bool, float]:
    """Delta-based arrival: balance rose by >= amount − fee tolerance once our own known
    outflows are added back and other known inflows are taken out."""
    adjusted = balance - before + out - inn
    need = amount * (1 - arrival_tolerance_bps() / 10_000) - 0.01
    return adjusted >= need, adjusted


def record_withdrawal_intent(
    platform: str,
    amount_usdc: float,
    reference: str,
    eta_seconds: int,
    dest: str | None = None,
    dest_balance_before: float | None = None,
    idem_key: str | None = None,
) -> None:
    """dest (robinhood mode): "rh_usdg" = lands as USDG on 4663, "base_usdc" = lands as USDC
    on Base and is then bridged home by sweep_base_usdc_to_robinhood(). None = legacy.

    dest_balance_before: destination balance snapshot taken BEFORE the recall was requested;
    rows that carry it are settled by balance delta (not by ETA). ETA is only an alert threshold.
    """
    now = time.time()
    row = {
        "platform": platform,
        "amount_usdc": round(float(amount_usdc), 6),
        "reference": str(reference),
        "status": "pending",
        "created_at": int(now),
        "eta_at": int(now) + max(60, int(eta_seconds)),
        "completed_at": None,
        "failed_at": None,
    }
    if dest:
        row["dest"] = dest
    if dest_balance_before is not None:
        row["dest_balance_before"] = round(float(dest_balance_before), 6)
        row["snapshot_ts"] = now
    if idem_key:
        row["idem_key"] = idem_key
    _L.append_row(WITHDRAWAL_LEDGER_PATH, row)
    log(
        f"📝 Recorded withdrawal intent: platform={platform} amount={amount_usdc:.4f} "
        f"ref={reference} eta={eta_seconds}s"
    )


def update_withdrawal(idem_key: str, **fields) -> None:
    def fn(rows):
        for row in rows:
            if row.get("idem_key") == idem_key:
                row.update(fields)
    _L.update_rows(WITHDRAWAL_LEDGER_PATH, fn)


def mark_withdrawal_failed(reference: str) -> None:
    now = int(time.time())

    def fn(rows):
        for row in rows:
            if (row.get("reference") == reference or row.get("idem_key") == reference) \
                    and row.get("status") == "pending":
                row["status"] = "failed"
                row["failed_at"] = now
    _L.update_rows(WITHDRAWAL_LEDGER_PATH, fn)


def mark_withdrawals_arrived(servicer_balance_usdc: float, base_usdc_balance: float | None = None) -> float:
    """Settle pending recalls that have landed. Returns the amount settled.

    servicer_balance_usdc is servicer cash on the funding chain (USDG on 4663 in robinhood
    mode). Rows with dest="base_usdc" are compared against base_usdc_balance instead and are
    skipped when it is not given.

    * Rows with a dest_balance_before snapshot (robinhood mode): delta-based — the destination
      balance must have risen by the amount (minus ARB_ARRIVAL_TOLERANCE_BPS) since the snapshot,
      after adding back our own journaled outflows and removing other known inflows. Oldest
      first; a delta consumed by one row is not reused by the next. No ETA gate.
    * Legacy rows (no snapshot, base mode): unchanged — after ETA, balance >= amount.
    """
    now = time.time()
    journal = _L.read_rows(_L.cashflow_path())
    inflows: list = []

    def fn(rows):
        total = 0.0
        consumed: dict = {}
        for row in sorted(rows, key=lambda r: float(r.get("snapshot_ts") or r.get("created_at") or 0)):
            st = row.get("status")
            amt = float(row.get("amount_usdc", 0.0))
            if row.get("dest_balance_before") is None:
                if st != "pending" or now < int(row.get("eta_at", 0)):
                    continue
                if row.get("dest") == "base_usdc":
                    if base_usdc_balance is None:
                        continue
                    balance = base_usdc_balance
                else:
                    balance = servicer_balance_usdc
                if balance + 1e-9 >= amt:
                    row["status"] = "completed"
                    row["completed_at"] = int(now)
                    total += amt
                continue
            if st not in COUNTED_STATUSES:
                continue
            balance = base_usdc_balance if row.get("dest") == "base_usdc" else servicer_balance_usdc
            if balance is None:
                continue
            chain, token = _dest_chain_token(row.get("dest"))
            out, inn = _L.flows_since(chain, token, float(row.get("snapshot_ts") or 0), journal)
            inn += consumed.get((chain, token.lower()), 0.0)
            ok, adjusted = _arrived(balance, float(row["dest_balance_before"]), amt, out, inn)
            if ok:
                row["status"] = "completed"
                row["completed_at"] = int(now)
                row["arrival_delta_usdc"] = round(adjusted, 6)
                total += amt
                consumed[(chain, token.lower())] = consumed.get((chain, token.lower()), 0.0) + amt
                inflows.append((chain, token, amt, row.get("reference")))
        return total

    total = _L.update_rows(WITHDRAWAL_LEDGER_PATH, fn)
    for chain, token, amt, ref in inflows:
        _L.record_cashflow(chain, token, amt, "recall_arrived", ref)
    return total


def get_pending_withdrawal_usdc() -> float:
    """Pending recalls still owed. Robinhood mode: rows older than ARB_INFLIGHT_STALE_SECS become
    "stale" — still counted, alerted, never dropped. Base mode keeps the legacy 24h expiry."""
    now = int(time.time())
    stale_after = inflight_stale_secs()
    rh = is_robinhood_funding()

    def fn(rows):
        total = 0.0
        for row in rows:
            st = row.get("status")
            amt = float(row.get("amount_usdc", 0.0) or 0.0)
            if st == "stale":
                total += amt
                continue
            if st != "pending":
                continue
            if now > int(row.get("created_at", 0)) + stale_after:
                if rh:
                    row["status"] = "stale"
                    row["stale_at"] = now
                    log(f"🚨 STALE recall {row.get('platform')} {amt:.4f} ref={row.get('reference')} "
                        f"pending > {stale_after}s — still counted, manual review needed")
                    total += amt
                else:
                    row["status"] = "failed"
                    row["failed_at"] = now
                continue
            total += amt
        return total

    return round(_L.update_rows(WITHDRAWAL_LEDGER_PATH, fn), 6)


def get_pending_withdrawal_usdc_by_dest() -> dict:
    """Pending (incl. stale) recall totals split by landing place: {"base_usdc": x, "servicer": y}."""
    get_pending_withdrawal_usdc()  # applies the stale transition
    out = {"base_usdc": 0.0, "servicer": 0.0}
    for row in _load_ledger():
        if row.get("status") not in COUNTED_STATUSES:
            continue
        key = "base_usdc" if row.get("dest") == "base_usdc" else "servicer"
        out[key] += float(row.get("amount_usdc") or 0.0)
    return {k: round(v, 6) for k, v in out.items()}


def get_pending_withdrawals_split() -> dict:
    """Pending (incl. stale) recalls split for NAV:
      servicer            — lands in servicer funding-chain cash (rh_usdg / legacy)
      base_usdc_tracked   — Kalshi recalls with a balance snapshot: settled by delta, so while
                            pending they are NOT yet in the Base balance → count in full
      base_usdc_legacy    — snapshot-less base_usdc rows (keep max(0, legacy − base_usdc) netting)
    """
    get_pending_withdrawal_usdc()
    out = {"servicer": 0.0, "base_usdc_tracked": 0.0, "base_usdc_legacy": 0.0}
    for row in _load_ledger():
        if row.get("status") not in COUNTED_STATUSES:
            continue
        amt = float(row.get("amount_usdc") or 0.0)
        if row.get("dest") != "base_usdc":
            out["servicer"] += amt
        elif row.get("dest_balance_before") is not None:
            out["base_usdc_tracked"] += amt
        else:
            out["base_usdc_legacy"] += amt
    return {k: round(v, 6) for k, v in out.items()}


def _complete_base_withdrawals_upto(amount_usdc: float) -> float:
    """Legacy rows only (no balance snapshot): mark pending dest=base_usdc recalls completed
    (oldest first) up to amount_usdc. Snapshot rows are settled by mark_withdrawals_arrived."""
    now = int(time.time())

    def fn(rows):
        left = float(amount_usdc)
        done = 0.0
        for row in sorted(rows, key=lambda r: int(r.get("created_at") or 0)):
            if row.get("status") != "pending" or row.get("dest") != "base_usdc" \
                    or row.get("dest_balance_before") is not None:
                continue
            amt = float(row.get("amount_usdc") or 0.0)
            if amt > left + 1e-9:
                break
            row["status"] = "completed"
            row["completed_at"] = now
            left -= amt
            done += amt
        return done

    return round(_L.update_rows(WITHDRAWAL_LEDGER_PATH, fn), 6)


# ---------------------------------------------------------------------------
# Bridge / venue-deposit in-transit ledgers (robinhood mode)
# ---------------------------------------------------------------------------
# Row lifecycle: written only after the value-moving tx's receipt is status 1 (receipt="success")
# or the receipt wait timed out (receipt="unconfirmed", still counted, flagged). Reverted txs are
# never recorded. status: pending → completed | refunded | reverted, or → stale (still counted).

_DIRECTION_ORIGIN = {
    "rh_to_base": "rh", "rh_to_polymarket": "rh", "base_to_rh": "base",
}


def _chain_token_for(side: str) -> tuple[int, str]:
    return (robinhood_chain_id(), usdg_address()) if side == "rh" else (BASE_CHAIN_ID, usdc_base_address())


def record_bridge_transfer(
    direction: str,
    provider: str,
    reference: str,
    amount_usdc: float,
    origin_chain_id: int,
    eta_seconds: int | None = None,
    *,
    tx_hash: str | None = None,
    receipt: str | None = None,
    dest_balance_before: float | None = None,
    expected_out_usdc: float | None = None,
    idem_key: str | None = None,
) -> None:
    """Record money that has left a servicer balance and not yet landed anywhere we read.

    direction: "rh_to_base" | "base_to_rh" | "rh_to_polymarket"
    provider:  "across" | "relay" | "polymarket_bridge"
    amount_usdc: amount that LEFT the origin balance (fees show up as a loss on arrival).
    Idempotent on idem_key (reconciliation may try to ledger the same intent twice).
    """
    now = time.time()
    row = {
        "direction": direction,
        "provider": provider,
        "reference": str(reference),
        "amount_usdc": round(float(amount_usdc), 6),
        "origin_chain_id": int(origin_chain_id),
        "status": "pending",
        "created_at": int(now),
        "eta_at": int(now) + int(eta_seconds if eta_seconds is not None else BRIDGE_DEFAULT_ETA_SECS),
        "completed_at": None,
    }
    for k, v in (("tx_hash", tx_hash), ("receipt", receipt), ("expected_out_usdc", expected_out_usdc),
                 ("idem_key", idem_key)):
        if v is not None:
            row[k] = v
    if dest_balance_before is not None:
        row["dest_balance_before"] = round(float(dest_balance_before), 6)
        row["snapshot_ts"] = now

    def fn(rows):
        if idem_key and any(r.get("idem_key") == idem_key for r in rows):
            return False
        rows.append(row)
        return True

    if _L.update_rows(BRIDGE_LEDGER_PATH, fn):
        flag = " [RECEIPT UNCONFIRMED]" if receipt == "unconfirmed" else ""
        log(f"📝 Bridge in-transit: {direction} via {provider} {amount_usdc:.4f} ref={reference}{flag}")


def mark_bridge_arrived(reference: str, status: str = "completed") -> None:
    now = int(time.time())
    hit: list = []

    def fn(rows):
        for row in rows:
            if row.get("reference") == str(reference) and row.get("status") in COUNTED_STATUSES:
                row["status"] = status
                row["completed_at"] = now
                hit.append(dict(row))
    _L.update_rows(BRIDGE_LEDGER_PATH, fn)
    for row in hit:
        log(f"✅ Bridge {reference} → {status}")
        _journal_bridge_settlement(row, status)


def _journal_bridge_settlement(row: dict, status: str) -> None:
    """Known servicer inflow when a bridge lands (dest side) or is refunded/reverted (origin side)."""
    try:
        direction = row.get("direction")
        origin = _DIRECTION_ORIGIN.get(direction)
        if origin is None:
            return
        amt = float(row.get("amount_usdc") or 0.0)
        if status == "completed":
            if direction == "rh_to_polymarket":
                return   # lands on Polymarket, not in a servicer balance
            dest = "base" if origin == "rh" else "rh"
            chain, token = _chain_token_for(dest)
            _L.record_cashflow(chain, token, float(row.get("expected_out_usdc") or amt), "bridge_arrived",
                               row.get("reference"))
        elif status in ("refunded", "reverted"):
            chain, token = _chain_token_for(origin)
            _L.record_cashflow(chain, token, amt, f"bridge_{status}", row.get("reference"))
    except Exception as e:
        log(f"⚠️ Could not journal bridge settlement: {e}")


def record_venue_deposit(
    venue: str,
    reference: str,
    amount_usdc: float,
    chain_id: int,
    *,
    dest_balance_before: float | None = None,
    receipt: str | None = None,
    tx_hash: str | None = None,
    idem_key: str | None = None,
    eta_seconds: int | None = None,
) -> None:
    """Direct venue deposit (e.g. Base USDC → Kalshi) in transit until the venue balance moves."""
    now = time.time()
    row = {
        "venue": venue, "reference": str(reference), "amount_usdc": round(float(amount_usdc), 6),
        "chain_id": int(chain_id), "status": "pending", "created_at": int(now),
        "eta_at": int(now) + int(eta_seconds if eta_seconds is not None else BRIDGE_DEFAULT_ETA_SECS),
        "completed_at": None, "receipt": receipt, "tx_hash": tx_hash, "idem_key": idem_key,
    }
    if dest_balance_before is not None:
        row["dest_balance_before"] = round(float(dest_balance_before), 6)
        row["snapshot_ts"] = now

    def fn(rows):
        if idem_key and any(r.get("idem_key") == idem_key for r in rows):
            return False
        rows.append(row)
        return True

    if _L.update_rows(_L.venue_deposit_path(), fn):
        log(f"📝 Venue deposit in-transit: {venue} {amount_usdc:.4f} ref={reference}"
            + (" [RECEIPT UNCONFIRMED]" if receipt == "unconfirmed" else ""))


def mark_venue_deposit_arrived(reference: str, status: str = "completed") -> None:
    now = int(time.time())

    def fn(rows):
        for row in rows:
            if row.get("reference") == str(reference) and row.get("status") in COUNTED_STATUSES:
                row["status"] = status
                row["completed_at"] = now
    _L.update_rows(_L.venue_deposit_path(), fn)


def inflight_rows_for(direction: str | None = None, venue: str | None = None) -> list:
    """Counted in-transit rows for a bridge direction or a venue (used to refuse duplicate sends)."""
    if venue is not None:
        return [r for r in _L.read_rows(_L.venue_deposit_path())
                if r.get("venue") == venue and r.get("status") in COUNTED_STATUSES]
    return [r for r in _load_ledger(BRIDGE_LEDGER_PATH)
            if r.get("direction") == direction and r.get("status") in COUNTED_STATUSES]


def _bridge_status(row: dict) -> str:
    """Return "filled" | "refunded" | "pending" from the provider's status API."""
    provider = row.get("provider")
    if provider == "across":
        data = request_json("GET", f"{across_api_url()}/deposit/status", params={
            "originChainId": row.get("origin_chain_id"),
            "depositTxHash": row.get("tx_hash") or row.get("reference"),
        }, timeout=10)
        st = str(data.get("status", "")).lower()
        return "filled" if st == "filled" else "refunded" if st in ("refunded", "expired") else "pending"
    if provider == "relay":
        data = request_json("GET", f"{relay_api_url()}/intents/status/v2",
                            params={"requestId": row.get("reference")}, timeout=10)
        st = str(data.get("status", "")).lower()
        return "filled" if st == "success" else "refunded" if st in ("refund", "failure") else "pending"
    # No status API and no balance snapshot. Robinhood mode: never assumed landed (ETA is only an
    # alert; the row stays counted until a balance delta or an operator resolves it).
    if is_robinhood_funding():
        return "pending"
    return "filled" if int(time.time()) >= int(row.get("eta_at") or 0) else "pending"


def _receipt_of(row: dict, chain_key: str) -> str | None:
    """"success" | "failed" | None for a row's tx hash (None = unknown / no receipt yet)."""
    tx_hash = row.get("tx_hash")
    if not tx_hash:
        return None
    try:
        from .arb_funder import _rpc, rpc_for_chain
        rec = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_for_chain(int(row.get(chain_key))))
    except Exception as e:
        log(f"⚠️ Receipt check failed for {tx_hash}: {e}")
        return None
    if not rec:
        return None
    return "success" if int(str(rec.get("status", "0x0")), 16) == 1 else "failed"


def _age_alerts(row: dict, label: str) -> bool:
    """Overdue / stale alerts. Returns True when the row was moved to "stale" (still counted)."""
    now = int(time.time())
    if row.get("status") == "pending" and now > int(row.get("created_at") or 0) + inflight_stale_secs():
        row["status"] = "stale"
        row["stale_at"] = now
        log(f"🚨 STALE {label} {row.get('amount_usdc')} ref={row.get('reference')} — still counted, manual review needed")
        return True
    if now > int(row.get("eta_at") or 0):
        log(f"⏰ {label} ref={row.get('reference')} overdue (ETA passed) — still counted")
    return False


def reconcile_bridge_transfers() -> None:
    """Clear in-transit rows that have provably landed; never drop value silently.

    * receipt-unconfirmed rows: re-check the receipt (a reverted tx is marked "reverted");
    * Across / Relay: provider status API (filled / refunded);
    * Polymarket deposit rows with a balance snapshot: Polymarket balance delta;
    * legacy rows with no API and no snapshot: ETA (unchanged);
    * anything older than ARB_INFLIGHT_STALE_SECS becomes "stale" (still counted, alerted).
    Venue deposits (Base USDC → Kalshi) are reconciled the same way against the Kalshi balance.
    """
    venue_bal: dict = {}

    def platform_balance(venue: str):
        if venue not in venue_bal:
            try:
                from .arb_funder import _get_platform_balance
                venue_bal[venue] = _get_platform_balance(venue, raise_on_error=True)
            except Exception as e:
                log(f"⚠️ {venue} balance unreadable — deposits kept in transit: {e}")
                venue_bal[venue] = None
        return venue_bal[venue]

    consumed: dict = {}
    for row in sorted(_load_ledger(BRIDGE_LEDGER_PATH), key=lambda r: int(r.get("created_at") or 0)):
        if row.get("status") not in COUNTED_STATUSES:
            continue
        ref = row.get("reference")
        if row.get("receipt") == "unconfirmed":
            rs = _receipt_of(row, "origin_chain_id")
            if rs == "failed":
                mark_bridge_arrived(ref, status="reverted")
                continue
            if rs == "success":
                _set_row_fields(BRIDGE_LEDGER_PATH, ref, receipt="success")
        try:
            if row.get("provider") in ("across", "relay"):
                st = _bridge_status(row)
            elif row.get("dest_balance_before") is not None:
                bal = platform_balance("polymarket")
                st = "pending"
                if bal is not None:
                    amt = float(row.get("amount_usdc") or 0)
                    ok, _ = _arrived(bal, float(row["dest_balance_before"]), amt, 0.0,
                                     consumed.get("polymarket", 0.0))
                    if ok:
                        consumed["polymarket"] = consumed.get("polymarket", 0.0) + amt
                        st = "filled"
            else:
                st = _bridge_status(row)
        except Exception as e:
            log(f"⚠️ Bridge status check failed for {ref}: {e} (kept in-transit)")
            st = "pending"
        if st == "filled":
            mark_bridge_arrived(ref)
        elif st == "refunded":
            mark_bridge_arrived(ref, status="refunded")
        else:
            _alert_and_maybe_stale(BRIDGE_LEDGER_PATH, ref, "bridge")

    consumed = {}
    for row in sorted(_L.read_rows(_L.venue_deposit_path()), key=lambda r: int(r.get("created_at") or 0)):
        if row.get("status") not in COUNTED_STATUSES:
            continue
        ref = row.get("reference")
        if row.get("receipt") == "unconfirmed":
            rs = _receipt_of(row, "chain_id")
            if rs == "failed":
                mark_venue_deposit_arrived(ref, status="reverted")
                chain, token = BASE_CHAIN_ID, usdc_base_address()
                _L.record_cashflow(chain, token, float(row.get("amount_usdc") or 0), "deposit_reverted", ref)
                continue
            if rs == "success":
                _set_row_fields(_L.venue_deposit_path(), ref, receipt="success")
        venue = row.get("venue")
        bal = platform_balance(venue) if row.get("dest_balance_before") is not None else None
        if bal is not None:
            amt = float(row.get("amount_usdc") or 0)
            ok, _ = _arrived(bal, float(row["dest_balance_before"]), amt, 0.0, consumed.get(venue, 0.0))
            if ok:
                consumed[venue] = consumed.get(venue, 0.0) + amt
                mark_venue_deposit_arrived(ref)
                continue
        _alert_and_maybe_stale(_L.venue_deposit_path(), ref, f"{venue} deposit")


def _set_row_fields(path: str, reference: str, **fields) -> None:
    def fn(rows):
        for r in rows:
            if r.get("reference") == reference and r.get("status") in COUNTED_STATUSES:
                r.update(fields)
    _L.update_rows(path, fn)


def _alert_and_maybe_stale(path: str, reference: str, label: str) -> None:
    def fn(rows):
        for r in rows:
            if r.get("reference") == reference and r.get("status") in COUNTED_STATUSES:
                _age_alerts(r, label)
    _L.update_rows(path, fn)


def get_inflight_bridge_usdc() -> float:
    """All value in transit between servicer balances and venues: bridge rows + direct venue
    deposits, pending or stale (stale is alerted, never dropped). Also counts write-ahead intents
    whose value tx was broadcast but whose in-transit row was not written yet (crash window)."""
    return round(sum(get_inflight_breakdown().values()), 6)


def get_inflight_breakdown() -> dict:
    out = {"bridges": 0.0, "venue_deposits": 0.0, "unledgered_intents": 0.0}
    for row in _load_ledger(BRIDGE_LEDGER_PATH):
        if row.get("status") in COUNTED_STATUSES:
            out["bridges"] += float(row.get("amount_usdc") or 0.0)
    for row in _L.read_rows(_L.venue_deposit_path()):
        if row.get("status") in COUNTED_STATUSES:
            out["venue_deposits"] += float(row.get("amount_usdc") or 0.0)
    for row in _L.read_rows(_L.intents_path()):
        if row.get("status") in _L.INTENT_MONEY_LEFT and not row.get("ledgered"):
            out["unledgered_intents"] += float(row.get("amount_usdc") or 0.0)
    return {k: round(v, 6) for k, v in out.items()}


def sweep_base_usdc_to_robinhood(servicer_wallet: str, private_key: str) -> float:
    """Robinhood mode: bridge servicer USDC on Base (Kalshi withdrawals, stranded bridge output)
    back to USDG on 4663. Skipped while a 4663→Base bridge is in flight (that USDC is about to be
    forwarded to Kalshi) or while a previous sweep's outcome is unknown. Returns the USDC amount bridged."""
    if not is_robinhood_funding() or not servicer_wallet or not private_key:
        return 0.0
    if inflight_rows_for(direction="rh_to_base"):
        log("ℹ️ 4663→Base bridge in flight — Base USDC sweep skipped")
        return 0.0
    if _L.unresolved_intents(kinds=("base_to_rh", "rh_to_base")):
        log("🚨 Unresolved bridge intent — Base USDC sweep skipped until reconciliation resolves it")
        return 0.0
    from .arb_funder import _get_usdc_balance, _bridge_base_usdc_to_usdg, _NonceTracker, get_servicer_cash_usdc
    base_usdc = _get_usdc_balance(servicer_wallet)
    # Settle delta-tracked Kalshi recalls that already landed on Base before moving that USDC on.
    try:
        mark_withdrawals_arrived(get_servicer_cash_usdc(servicer_wallet), base_usdc)
    except Exception as e:
        log(f"⚠️ Recall arrival check before sweep failed: {e}")
    if base_usdc < rh_sweep_min_usdc():
        return 0.0
    amount = round(base_usdc, 6)
    # The bridge op writes the base_to_rh in-transit row itself (after a status-1 receipt).
    _bridge_base_usdc_to_usdg(private_key, servicer_wallet, amount, _NonceTracker(servicer_wallet))
    # Legacy (snapshot-less) Kalshi recalls that landed on Base are now "bridge in-transit".
    _complete_base_withdrawals_upto(amount)
    return amount


def _poly_clob_headers(method: str = "GET") -> dict:
    api_key = os.environ.get("POLY_API_KEY", "")
    if not api_key:
        raise ValueError("POLY_API_KEY not set")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _poly_get_balance() -> float:
    poly_api_key        = os.environ.get("POLY_API_KEY", "")
    poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
    poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
    private_key         = os.environ.get("POLY_PRIVATE_KEY", "")
    poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None

    if not poly_api_key:
        raise ValueError("POLY_API_KEY not set")

    # clob_compat prefers py-clob-client-v2: the V1 SDK stops working after the CLOB V2 cutover.
    from .clob_compat import make_client, ApiCreds, BalanceAllowanceParams, AssetType

    creds = ApiCreds(
        api_key=poly_api_key.strip(),
        api_secret=poly_api_secret.strip(),
        api_passphrase=poly_api_passphrase.strip(),
    )

    client = make_client(
        POLY_CLOB_URL,
        key=private_key,
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=poly_proxy_address,
    )

    try:
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
    except Exception as upd_err:
        log(f"⚠️ [POLY] update_balance_allowance failed (non-fatal): {upd_err}")

    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
    )
    log(f"🔍 [POLY] sig_type=2 response: {result}")

    raw = result.get("balance", "0")
    bal = float(raw) / 1_000_000

    try:
        client_eoa = make_client(
            POLY_CLOB_URL,
            key=private_key,
            chain_id=137,
            creds=creds,
            signature_type=0,
        )
        result_eoa = client_eoa.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
        )
        log(f"🔍 [POLY] sig_type=0 response: {result_eoa}")
        raw_eoa = result_eoa.get("balance", "0")
        bal_eoa = float(raw_eoa) / 1_000_000
        if bal == 0 and bal_eoa > 0:
            log(f"ℹ️ [POLY] Funds found in EOA account — using EOA balance {bal_eoa:.6f} USDC")
            return bal_eoa
    except Exception as eoa_err:
        log(f"⚠️ [POLY] EOA balance check failed: {eoa_err}")

    return bal

def _rpc_call(rpc_url: str, method: str, params: list) -> str:
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]


def _relay_bridge_polygon_to_base(
    amount_usdc: float,
    private_key: str,
    dest_address: str,
) -> tuple[bool, str]:
    """USDC.e on Polygon → USDC on Base via Relay (EXACT_INPUT).

    Same fail-closed checks as the Robinhood bridge path (arb_allowlist): allowlisted (chain, token),
    approvals exact (<= input) to an allowlisted spender, deposit `to` allowlisted, no native value,
    recipient/destination must match when the quote states them, fee <= ARB_BRIDGE_MAX_FEE_BPS.
    Every quoted tx is sent in nonce order and its receipt checked (the old code returned after the
    first item, i.e. after the approval). Quote is fetched with timeout + bounded retries.
    """
    from . import arb_allowlist as AL
    try:
        from eth_account import Account

        account = Account.from_key(private_key)
        amount_raw = int(round(amount_usdc * 1_000_000))
        usdc_polygon = usdc_polygon_address()
        polygon_chain_id = 137
        polygon_rpc = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

        log(f"🌉 Getting Relay quote: {amount_usdc:.4f} USDC Polygon → Base → {dest_address}")
        quote = request_json("POST", f"{relay_api_url()}/quote", json_body={
            "user": account.address,
            "originChainId": polygon_chain_id,
            "destinationChainId": BASE_CHAIN_ID,
            "originCurrency": usdc_polygon,
            "destinationCurrency": usdc_base_address(),
            "amount": str(amount_raw),
            "recipient": dest_address,
            "tradeType": "EXACT_INPUT",
        }, timeout=20)

        from .arb_funder import _relay_route_fields, _to_int
        txs = []
        for step in quote.get("steps", []):
            for item in step.get("items", []):
                data = item.get("data") or {}
                if data.get("to"):
                    txs.append(data)
        if not txs:
            return False, f"Relay quote returned no executable steps: {list(quote.keys())}"
        cur_out = (quote.get("details") or {}).get("currencyOut") or {}
        out_raw = _to_int(cur_out.get("minimumAmount") or cur_out.get("amount"))
        max_fee = bridge_max_fee_bps()
        if out_raw <= 0 or out_raw * 10_000 < amount_raw * (10_000 - max_fee):
            return False, f"Relay quote fee unverifiable or above {max_fee} bps (in={amount_raw} out={out_raw})"
        AL.validate_bridge_txs(txs, polygon_chain_id, usdc_polygon, amount_raw)
        AL.validate_route(_relay_route_fields(quote), recipient=dest_address,
                          origin_chain=polygon_chain_id, origin_token=usdc_polygon,
                          dest_chain=BASE_CHAIN_ID, dest_token=usdc_base_address())

        nonce = int(_rpc_call(polygon_rpc, "eth_getTransactionCount", [account.address, "pending"]), 16)
        gas_price = int(int(_rpc_call(polygon_rpc, "eth_gasPrice", []), 16) * 1.3)
        tx_hash = None
        for i, tx_data in enumerate(txs):
            tx = {
                "to": tx_data["to"],
                "data": tx_data.get("data", "0x"),
                "value": 0,
                "gas": _to_int(tx_data.get("gas"), 200_000),
                "gasPrice": gas_price,
                "nonce": nonce + i,
                "chainId": polygon_chain_id,
            }
            signed = account.sign_transaction(tx)
            raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
            tx_hash = _rpc_call(polygon_rpc, "eth_sendRawTransaction", ["0x" + raw.hex()])
            from .arb_funder import _wait_for_receipt
            st = _wait_for_receipt(tx_hash, polygon_rpc)
            if st == "failed":
                return False, f"Relay tx {i} reverted: {tx_hash}"
            log(f"✅ Relay bridge tx {i + 1}/{len(txs)} submitted: {tx_hash} (receipt={st})")
        return True, tx_hash

    except Exception as e:
        return False, str(e)


def get_pending_withdrawals_total_usdc() -> float:
    """
    Sum USDC for all pending withdrawal intents still considered in-flight.
    """
    rows = _load_ledger()
    if not rows:
        return 0.0

    now = int(time.time())
    total = 0.0

    for row in rows:
        if row.get("status") == "stale":
            total += float(row.get("amount_usdc") or 0.0)   # stale = still owed, never dropped
            continue
        if row.get("status") != "pending":
            continue

        try:
            amount = float(row.get("amount_usdc") or 0.0)
        except Exception:
            amount = 0.0

        eta_at = int(row.get("eta_at") or 0)

        # Count pending recalls as in-flight while ETA is still in the future.
        # If ETA is missing, count them conservatively.
        if eta_at <= 0 or eta_at >= now:
            total += amount

    return round(total, 6)


def withdraw_from_polymarket(
    amount_usdc: float,
    min_amount: float = 1.0,
) -> tuple[bool, str, float]:
    import subprocess
    import re

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    poly_proxy = os.environ.get("POLY_PROXY_ADDRESS", "")

    if not private_key:
        return False, "POLY_PRIVATE_KEY not set", 0.0
    if not poly_proxy:
        return False, "POLY_PROXY_ADDRESS not set", 0.0

    try:
        balance = _poly_get_balance()
        log(f"Polymarket available: {balance:.4f} USDC")

        # The TS Safe/bridge script has a hard MIN_WITHDRAWAL_USDC = 5.
        # Redeem shortfall can be slightly below 5 because PPS < 1, e.g. 4.98252.
        # In that case request exactly 5.00 so the recall is allowed.
        requested = float(amount_usdc)
        if requested < 5.0 and requested >= 4.90:
            requested = 5.0

        withdraw_amount = min(requested, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Poly balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        if withdraw_amount < 5.0:
            msg = f"Poly withdrawal {withdraw_amount:.6f} below bridge minimum 5.00"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        cmd = [
            "npx",
            "tsx",
            "trading_bot/safe_proxy_withdraw_parb.ts",
            "withdraw",
            f"{withdraw_amount:.6f}",
        ]

        script_env = {**os.environ}
        if is_robinhood_funding():
            # Route the Relay leg to USDG on Robinhood Chain, landing in the servicer wallet
            # (safe_proxy_withdraw_parb.ts reads WITHDRAW_DEST_CHAIN_ID / WITHDRAW_DEST_CURRENCY).
            servicer = os.environ.get("ARB_SERVICER_WALLET", "")
            if not servicer:
                return False, "ARB_SERVICER_WALLET not set (robinhood withdrawal destination)", 0.0
            script_env.update({
                "WITHDRAW_DEST_CHAIN_ID": str(robinhood_chain_id()),
                "WITHDRAW_DEST_CURRENCY": usdg_address(),
                "TREASURY_ADDRESS": servicer,
            })
            log(f"📤 Full Polymarket recall: {withdraw_amount:.4f} USDC.e {poly_proxy} -> EOA -> USDG (4663) {servicer}")
        else:
            log(f"📤 Full Polymarket recall: {withdraw_amount:.4f} USDC.e {poly_proxy} -> EOA -> Base vault")

        result = subprocess.run(
            cmd,
            cwd="/root/PolyNotifyBot",
            capture_output=True,
            text=True,
            timeout=180,
            env=script_env,
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        log(f"Direct Safe withdraw output:\n{output[:3000]}")

        if result.returncode != 0:
            return False, f"direct_safe_withdraw failed: {output[:300]}", 0.0

        tx_match = (
            re.search(r"Step 2 \(EOA → Base\):\s*(0x[a-fA-F0-9]+)", output)
            or re.search(r"TX:\s*(0x[a-fA-F0-9]+)", output)
            or re.search(r"Bridge tx sent:\s*(0x[a-fA-F0-9]+)", output)
            or re.search(r"TX_HASH:\s*(0x[a-fA-F0-9]+)", output)
        )

        bridge_ok = (
            "✅ BRIDGE COMPLETE" in output
            or "✅ FULL WITHDRAWAL COMPLETE" in output
            or "Bridge complete!" in output
        )

        tx_hash = tx_match.group(1) if tx_match else "unknown"

        if not bridge_ok:
            return False, f"bridge script did not report success: {output[:500]}", 0.0

        log(f"✅ Full Polymarket recall complete: tx={tx_hash} amount={withdraw_amount:.4f}")
        return True, tx_hash, withdraw_amount

    except Exception as e:
        log(f"❌ withdraw_from_polymarket error: {e}")
        return False, str(e), 0.0


def _kalshi_get_balance() -> float:
    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        raise ValueError("Kalshi credentials not configured")

    url = f"{KALSHI_BASE_URL}/portfolio/balance"
    headers = get_kalshi_headers("GET", url)
    if not headers:
        raise ValueError("Kalshi RSA signing failed")

    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return float(data.get("balance", 0)) / 100.0


def withdraw_from_kalshi(
    amount_usdc: float,
    dest_address: str,
    min_amount: float = 1.0,
) -> tuple[bool, str, float]:
    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available

    if not kalshi_auth_available():
        return False, "Kalshi credentials not configured", 0.0
    if not dest_address:
        return False, "dest_address not set", 0.0

    try:
        balance = _kalshi_get_balance()
        log(f"Kalshi available: {balance:.4f} USDC")

        withdraw_amount = min(amount_usdc, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Kalshi balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        log(f"📤 Requesting Kalshi crypto withdrawal: {withdraw_amount:.4f} USDC → {dest_address}")

        url = f"{KALSHI_BASE_URL}/portfolio/balance/withdrawals"
        headers = get_kalshi_headers("POST", url)
        if not headers:
            return False, "Kalshi RSA signing failed for withdrawal", 0.0

        amount_cents = int(withdraw_amount * 100)
        payload = {
            "amount": amount_cents,
            "method": "crypto",
            "address": dest_address,
        }

        try:
            # Not retried: a withdrawal POST is not idempotent.
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
        except Exception as post_err:
            log(f"❌ Kalshi withdrawal POST outcome unknown: {post_err}")
            return False, f"ambiguous: Kalshi withdrawal POST outcome unknown: {post_err}", 0.0

        if resp.status_code not in (200, 201):
            return False, f"Kalshi /withdrawals HTTP {resp.status_code}: {resp.text[:200]}", 0.0

        data = resp.json()
        withdrawal_id = data.get("id") or data.get("withdrawal_id") or str(data)
        log(f"✅ Kalshi withdrawal initiated: id={withdrawal_id} amount={withdraw_amount:.4f} USDC → {dest_address}")
        return True, withdrawal_id, withdraw_amount

    except Exception as e:
        log(f"❌ withdraw_from_kalshi error: {e}")
        return False, str(e), 0.0


def _opinion_headers() -> dict:
    api_key = os.environ.get("OPINION_API_KEY", "")
    if not api_key:
        raise ValueError("OPINION_API_KEY not set")
    return {"X-API-KEY": api_key, "Content-Type": "application/json"}


def _opinion_get_balance() -> float:
    for path in ("/account/balance", "/balance", "/account"):
        try:
            resp = requests.get(
                f"{OPINION_BASE_URL}{path}",
                headers=_opinion_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                for field in ("balance", "usdc", "usdcBalance", "availableBalance", "available"):
                    if field in data:
                        return float(data[field])
        except Exception:
            continue
    return 0.0


def _bridge_bsc_to_base(
    amount_usdc: float,
    private_key: str,
    dest_address: str,
) -> tuple[bool, str]:
    try:
        from eth_account import Account

        account = Account.from_key(private_key)
        amount_raw = int(amount_usdc * 1_000_000)

        log(f"🌉 Getting LI.FI quote: {amount_usdc:.4f} USDC BSC → Base → {dest_address}")

        quote_resp = requests.get(
            f"{LIFI_API}/quote",
            params={
                "fromChain": BSC_CHAIN_ID,
                "toChain": BASE_CHAIN_ID,
                "fromToken": USDC_BSC,
                "toToken": USDC_BASE,
                "fromAmount": str(amount_raw),
                "fromAddress": account.address,
                "toAddress": dest_address,
                "slippage": "0.005",
            },
            timeout=20,
        )
        quote_resp.raise_for_status()
        quote = quote_resp.json()

        tx_req = quote.get("transactionRequest")
        if not tx_req:
            return False, f"LI.FI quote missing transactionRequest: {list(quote.keys())}"

        lifi_router = tx_req.get("to", "")
        if not lifi_router:
            return False, "LI.FI transactionRequest missing 'to'"

        allowance_data = (
            "0xdd62ed3e"
            + "000000000000000000000000" + account.address.lower().replace("0x", "")
            + "000000000000000000000000" + lifi_router.lower().replace("0x", "")
        )
        raw_allowance = _rpc_call(BSC_RPC, "eth_call", [{"to": USDC_BSC, "data": allowance_data}, "latest"])
        current_allowance = int(raw_allowance, 16) / 1e6

        nonce_raw = _rpc_call(BSC_RPC, "eth_getTransactionCount", [account.address, "pending"])
        nonce = int(nonce_raw, 16)

        gas_price_raw = _rpc_call(BSC_RPC, "eth_gasPrice", [])
        gas_price = int(int(gas_price_raw, 16) * 1.2)

        if current_allowance < amount_usdc:
            log(f"🔐 Approving LI.FI router on BSC for {amount_usdc:.4f} USDC")
            amount_hex = amount_raw.to_bytes(32, "big").hex()
            padded_router = "000000000000000000000000" + lifi_router.lower().replace("0x", "")
            approve_data = "0x095ea7b3" + padded_router + amount_hex

            approve_tx = {
                "to": USDC_BSC,
                "data": approve_data,
                "gas": 80_000,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": BSC_CHAIN_ID,
                "value": 0,
            }
            signed_approve = account.sign_transaction(approve_tx)
            _rpc_call(BSC_RPC, "eth_sendRawTransaction", ["0x" + signed_approve.raw_transaction.hex()])
            nonce += 1
            log("✅ BSC USDC approval sent")

        tx_data = tx_req.get("data", "0x")
        tx_value = int(tx_req.get("value", "0x0"), 16) if tx_req.get("value") else 0
        tx_gas = int(tx_req.get("gasLimit", "0x30d40"), 16) if tx_req.get("gasLimit") else 250_000
        tx_gas = int(tx_gas * 1.2)

        bridge_tx = {
            "to": lifi_router,
            "data": tx_data,
            "value": tx_value,
            "gas": tx_gas,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": BSC_CHAIN_ID,
        }
        signed_bridge = account.sign_transaction(bridge_tx)
        tx_hash = _rpc_call(BSC_RPC, "eth_sendRawTransaction", ["0x" + signed_bridge.raw_transaction.hex()])
        log(f"✅ LI.FI BSC→Base bridge tx: {tx_hash}")
        return True, tx_hash

    except Exception as e:
        return False, str(e)


def withdraw_from_opinion(
    amount_usdc: float,
    dest_address: str,
    min_amount: float = 1.0,
) -> tuple[bool, str, float]:
    opinion_api_key = os.environ.get("OPINION_API_KEY", "")
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")

    if not opinion_api_key:
        return False, "OPINION_API_KEY not set", 0.0
    if not private_key:
        return False, "POLY_PRIVATE_KEY not set", 0.0
    if not dest_address:
        return False, "dest_address not set", 0.0

    try:
        from eth_account import Account
        servicer_bsc_address = Account.from_key(private_key).address

        try:
            bnb_raw = _rpc_call(BSC_RPC, "eth_getBalance", [servicer_bsc_address, "latest"])
            bnb_balance = int(bnb_raw, 16) / 1e18
            if bnb_balance < 0.002:
                log(
                    f"⚠️ BNB_BALANCE_WARNING: servicer has {bnb_balance:.6f} BNB on BSC — "
                    f"need at least 0.002 BNB for bridge gas. Opinion withdrawal may fail."
                )
        except Exception as e:
            log(f"⚠️ Could not check BNB balance: {e}")

        balance = _opinion_get_balance()
        log(f"Opinion available: {balance:.4f} USDC")

        withdraw_amount = min(amount_usdc, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Opinion balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        log(f"📤 Requesting Opinion withdrawal: {withdraw_amount:.4f} USDC → BSC {servicer_bsc_address}")

        withdraw_result = None
        for path in ("/account/withdraw", "/withdraw", "/account/withdrawals"):
            try:
                resp = requests.post(
                    f"{OPINION_BASE_URL}{path}",
                    headers=_opinion_headers(),
                    json={
                        "amount": withdraw_amount,
                        "address": servicer_bsc_address,
                        "chain": "BSC",
                        "token": "USDC",
                    },
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    withdraw_result = resp.json()
                    log(f"✅ Opinion withdrawal accepted via {path}: {withdraw_result}")
                    break
                elif resp.status_code == 404:
                    continue
                else:
                    log(f"⚠️ Opinion {path} HTTP {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                log(f"⚠️ Opinion {path} error: {e}")

        if withdraw_result is None:
            return False, "Opinion withdrawal API returned no success response", 0.0

        withdrawal_id = (
            withdraw_result.get("id")
            or withdraw_result.get("withdrawal_id")
            or withdraw_result.get("txHash")
            or "pending"
        )

        log(f"⏳ Waiting 5 min for Opinion withdrawal to settle on BSC (id={withdrawal_id})…")
        time.sleep(300)

        success, tx_or_err = _bridge_bsc_to_base(
            amount_usdc=withdraw_amount,
            private_key=private_key,
            dest_address=dest_address,
        )

        if success:
            log(f"✅ Opinion→Base bridge submitted: {tx_or_err} ({withdraw_amount:.4f} USDC)")
            return True, tx_or_err, withdraw_amount
        else:
            log(f"⚠️ Bridge failed after Opinion withdrawal: {tx_or_err}")
            return False, f"Bridge failed: {tx_or_err}", 0.0

    except Exception as e:
        log(f"❌ withdraw_from_opinion error: {e}")
        return False, str(e), 0.0


def _try(fn, label: str):
    try:
        return fn()
    except Exception as e:
        log(f"⚠️ {label} unreadable: {e}")
        return None


def _rh_recall_polymarket(target: float, servicer_wallet: str) -> float:
    """Robinhood-mode Polymarket recall with a write-ahead ledger row.

    The row is written BEFORE the Safe/Relay script runs (so a crash mid-script still leaves the
    recall counted) with a snapshot of servicer USDG on 4663; it is settled by USDG balance delta
    (immediately if the script's fill is already visible). The amount recorded is what actually
    moved: if USDG rose by more than requested, the larger measured amount is recorded. Returns the
    amount initiated.

    Route (arb_venue_routes, B9): Polymarket collateral is pUSD, so only the Polymarket bridge route
    (ARB_POLY_WITHDRAW_ROUTE=polymarket_bridge: pUSD -> USDG on 4663) is used; the legacy USDC.e script
    is refused here. Base mode does not use this function.
    """
    from .arb_funder import get_servicer_cash_usdc, _get_usdc_balance
    from . import arb_venue_routes as routes
    if routes.poly_recall_route() != routes.POLY_BRIDGE:
        log("⚠️ Polymarket recall skipped: set ARB_POLY_WITHDRAW_ROUTE=polymarket_bridge (pUSD collateral; "
            "the USDC.e relay script is not a valid robinhood route)")
        return 0.0
    usdg_before = _try(lambda: get_servicer_cash_usdc(servicer_wallet), "servicer USDG")
    if usdg_before is None:
        log("⚠️ Polymarket recall skipped: cannot snapshot servicer USDG (retry next tick)")
        return 0.0
    base_now = _try(lambda: _get_usdc_balance(servicer_wallet), "servicer Base USDC")
    _try(lambda: mark_withdrawals_arrived(usdg_before, base_now), "earlier recall arrivals")
    poly_before = _try(_poly_get_balance, "Polymarket balance")

    key = _L.new_key("recall_polymarket")
    record_withdrawal_intent("polymarket", target, key, POLY_ESTIMATED_SETTLEMENT_SECS,
                             dest="rh_usdg", dest_balance_before=usdg_before, idem_key=key)
    ok, ref, amount, bridge_addr = routes.withdraw_polymarket_bridge(target)
    if bridge_addr:
        update_withdrawal(key, poly_bridge_address=bridge_addr)
    if not ok and str(ref).startswith("ambiguous"):
        update_withdrawal(key, recall_error=str(ref)[:300], receipt="unconfirmed")
        log(f"🚨 Polymarket bridge recall outcome unknown — kept in transit (ref={key}, bridge={bridge_addr}); "
            f"check GET bridge.polymarket.com/status/<bridge address> before any retry")
        return 0.0
    if ok:
        after = _try(lambda: get_servicer_cash_usdc(servicer_wallet), "servicer USDG after recall")
        sent = float(amount)
        if after is not None and (after - usdg_before) > sent * 1.001 + 0.01:
            log(f"⚠️ Recall moved {after - usdg_before:.4f} USDG, more than the {sent:.4f} requested — recording actual")
            sent = after - usdg_before
        update_withdrawal(key, reference=str(ref), amount_usdc=round(sent, 6), requested_usdc=round(target, 6))
        if after is not None:
            _try(lambda: mark_withdrawals_arrived(after, base_now), "recall arrival")
        log(f"✅ Polymarket withdrawal initiated: {sent:.4f} USDC (tx={ref})")
        return round(sent, 6)

    poly_after = _try(_poly_get_balance, "Polymarket balance after failed recall")
    if poly_before is not None and poly_after is not None \
            and poly_before - poly_after >= target * 0.99 - 0.01:
        # Money left Polymarket but the script did not confirm the bridge: keep it counted.
        update_withdrawal(key, recall_error=str(ref)[:300], receipt="unconfirmed")
        log(f"🚨 Polymarket recall reported failure but {poly_before - poly_after:.4f} left Polymarket — "
            f"kept in transit (ref={key}), manual review needed")
        return 0.0
    mark_withdrawal_failed(key)
    log(f"⚠️ Polymarket withdrawal failed (nothing left Polymarket): {ref}")
    return 0.0


def _rh_recall_kalshi(target: float, servicer_wallet: str) -> float:
    """Robinhood-mode Kalshi recall: write-ahead row with a Base USDC snapshot, keyed on the
    Kalshi withdrawal id once known, settled by Base USDC balance delta."""
    from .arb_funder import get_servicer_cash_usdc, _get_usdc_balance
    from . import arb_venue_routes as routes
    if not routes.kalshi_api_withdraw_allowed():
        # Kalshi documents no API to create a crypto withdrawal (GET /portfolio/withdrawals only).
        log(f"⚠️ Kalshi recall of {target:.4f} USDC needs an operator: withdraw USDC on Base to {servicer_wallet} "
            f"in the Kalshi app (no documented withdrawal API; ARB_KALSHI_API_WITHDRAW_VERIFIED is not set)")
        return 0.0
    base_before = _try(lambda: _get_usdc_balance(servicer_wallet), "servicer Base USDC")
    if base_before is None:
        log("⚠️ Kalshi recall skipped: cannot snapshot servicer Base USDC (retry next tick)")
        return 0.0
    usdg_now = _try(lambda: get_servicer_cash_usdc(servicer_wallet), "servicer USDG")
    if usdg_now is not None:
        _try(lambda: mark_withdrawals_arrived(usdg_now, base_before), "earlier recall arrivals")
    kalshi_before = _try(_kalshi_get_balance, "Kalshi balance")

    key = _L.new_key("recall_kalshi")
    record_withdrawal_intent("kalshi", target, key, KALSHI_ESTIMATED_SETTLEMENT_SECS,
                             dest="base_usdc", dest_balance_before=base_before, idem_key=key)
    ok, ref, amount = withdraw_from_kalshi(target, servicer_wallet)
    if ok:
        update_withdrawal(key, reference=str(ref), kalshi_withdrawal_id=str(ref), amount_usdc=round(float(amount), 6))
        log(f"✅ Kalshi withdrawal initiated: {amount:.4f} USDC (id={ref})")
        return round(float(amount), 6)
    if str(ref).startswith("ambiguous"):
        kalshi_after = _try(_kalshi_get_balance, "Kalshi balance after ambiguous withdrawal")
        if kalshi_before is None or kalshi_after is None or kalshi_before - kalshi_after >= target * 0.99 - 0.01:
            update_withdrawal(key, recall_error=str(ref)[:300], receipt="unconfirmed")
            log(f"🚨 Kalshi withdrawal outcome unknown — kept in transit (ref={key}), manual review needed")
            return 0.0
    mark_withdrawal_failed(key)
    log(f"⚠️ Kalshi withdrawal failed: {ref}")
    return 0.0


def withdraw_from_platforms(
    shortfall_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    opinion_cash: float,
    servicer_wallet: str,
) -> float:
    if shortfall_usdc <= 0:
        return 0.0

    remaining = shortfall_usdc
    total_initiated = 0.0

    log(
        f"🔄 Platform withdrawal orchestration: shortfall={shortfall_usdc:.4f} USDC "
        f"poly_avail={poly_cash:.4f} kalshi_avail={kalshi_cash:.4f} opinion_avail={opinion_cash:.4f}"
    )

    rh_mode = is_robinhood_funding()
    if (rh_mode or not opinion_enabled()) and opinion_cash:
        # MILORD V1: Opinion is fully disabled in robinhood mode regardless of ARB_OPINION_ENABLED.
        log(f"ℹ️ Opinion disabled — ignoring opinion_cash={opinion_cash:.4f}")
        opinion_cash = 0.0

    if remaining > 0 and poly_cash > 1.0:
        target = min(poly_cash * 0.9, remaining)
        log(f"📤 Polymarket withdrawal target: {target:.4f} USDC")
        if rh_mode:
            amount = _rh_recall_polymarket(target, servicer_wallet)
            total_initiated += amount
            remaining -= amount
        else:
            ok, ref, amount = withdraw_from_polymarket(target)
            if ok:
                record_withdrawal_intent("polymarket", amount, ref, 600)
                log(f"✅ Polymarket withdrawal initiated: {amount:.4f} USDC (tx={ref})")
                total_initiated += amount
                remaining -= amount
            else:
                mark_withdrawal_failed(ref)
                log(f"⚠️ Polymarket withdrawal failed: {ref}")

    if remaining > 0 and kalshi_cash > 1.0:
        target = min(kalshi_cash * 0.9, remaining)
        log(f"📤 Kalshi withdrawal target: {target:.4f} USDC")
        if rh_mode:
            # Kalshi still pays USDC on Base to the servicer EOA; run_tend_tick's
            # sweep_base_usdc_to_robinhood() then bridges it to USDG on 4663.
            amount = _rh_recall_kalshi(target, servicer_wallet)
            total_initiated += amount
            remaining -= amount
        else:
            ok, ref, amount = withdraw_from_kalshi(target, servicer_wallet)
            if ok:
                record_withdrawal_intent("kalshi", amount, ref, KALSHI_ESTIMATED_SETTLEMENT_SECS)
                log(f"✅ Kalshi withdrawal initiated: {amount:.4f} USDC (id={ref})")
                total_initiated += amount
                remaining -= amount
            else:
                mark_withdrawal_failed(ref)
                log(f"⚠️ Kalshi withdrawal failed: {ref}")

    if remaining > 0 and opinion_cash > 1.0:
        target = min(opinion_cash * 0.9, remaining)
        log(f"📤 Opinion withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_opinion(target, servicer_wallet)
        if ok:
            record_withdrawal_intent("opinion", amount, ref, OPINION_ESTIMATED_SETTLEMENT_SECS)
            log(f"✅ Opinion withdrawal initiated: {amount:.4f} USDC (bridge={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            mark_withdrawal_failed(ref)
            log(f"⚠️ Opinion withdrawal failed: {ref}")

    if remaining > 0:
        log(
            f"⚠️ Platform withdrawals initiated {total_initiated:.4f} USDC but "
            f"{remaining:.4f} USDC shortfall remains — position unwind may be needed"
        )
    else:
        log(f"✅ Platform withdrawals cover full shortfall ({total_initiated:.4f} USDC initiated)")

    return round(total_initiated, 6)
