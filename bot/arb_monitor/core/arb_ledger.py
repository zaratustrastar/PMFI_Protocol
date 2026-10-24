"""arb_ledger.py — locked JSON ledgers, write-ahead funding intents, servicer cash-flow journal.

Every load-modify-save of a funding ledger goes through update_rows(), which holds an
exclusive fcntl.flock on "<ledger>.lock" for the whole read-modify-write. flock locks
belong to an open file description, so two threads of one process (execution loop +
reporter) exclude each other just like two processes do. Readers use read_rows(), which
takes a shared lock and returns a private copy (safe for NAV).

Write-ahead intents (M2): before a value-moving tx is signed, begin_intent() records the
operation under a client idempotency key; the precomputed tx hash and nonce are stored
BEFORE broadcast. Startup reconciliation (arb_funder.reconcile_funding_intents) resolves
every unresolved intent by receipt or nonce, and begin_intent() refuses a key that is still
live, so a crash/restart cannot re-send the same bridge or deposit.

Cash-flow journal: servicer outflows/inflows we caused or observed (deposits, bridge
inputs/outputs, tend, recalls). Delta-based arrival detection uses it to separate a recall
landing from our own spending.
"""

import contextlib
import copy
import fcntl
import json
import os
import threading
import time
import uuid
from typing import Any, Callable

_tls = threading.local()

INTENT_UNRESOLVED = ("intent", "broadcast")          # value tx not known to be mined / not broadcast
INTENT_MONEY_LEFT = ("broadcast", "confirmed", "unconfirmed")
INTENT_RETRYABLE = ("failed", "abandoned", "reverted")  # a key in these states may be reused


class LedgerError(RuntimeError):
    pass


class DuplicateOperation(RuntimeError):
    pass


def log(msg: str):
    print(f"📒 [ArbLedger] {msg}")


# ---------------------------------------------------------------------------
# Paths (derived at call time so tests that move the withdrawal/bridge ledgers move these too)
# ---------------------------------------------------------------------------

def _ledger_dir() -> str:
    from . import arb_withdrawals
    return os.path.dirname(arb_withdrawals.BRIDGE_LEDGER_PATH)


def intents_path() -> str:
    return os.environ.get("ARB_INTENT_LEDGER_PATH", "") or os.path.join(_ledger_dir(), "arb_funding_intents.json")


def cashflow_path() -> str:
    return os.environ.get("ARB_CASHFLOW_JOURNAL_PATH", "") or os.path.join(_ledger_dir(), "arb_cashflow_journal.json")


def venue_deposit_path() -> str:
    return os.environ.get("ARB_VENUE_DEPOSIT_LEDGER_PATH", "") or os.path.join(_ledger_dir(), "arb_venue_deposit_ledger.json")


# ---------------------------------------------------------------------------
# Locked file primitives
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(path: str, exclusive: bool):
    held = getattr(_tls, "held", None)
    if held is None:
        held = _tls.held = set()
    if path in held:
        raise LedgerError(f"nested ledger lock on {path} (would deadlock)")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        held.add(path)
        yield
    finally:
        held.discard(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_unlocked(path: str, strict: bool) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        if strict:
            raise LedgerError(f"ledger {path} unreadable: {e}") from e
        log(f"⚠️ Could not load ledger {path}: {e}")
        return []
    if not isinstance(data, list):
        if strict:
            raise LedgerError(f"ledger {path} is not a list")
        return []
    return data


def _write_unlocked(path: str, rows: list) -> None:
    from .shadow_guard import assert_not_shadow
    assert_not_shadow(f"ledger write {os.path.basename(path)}")
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_rows(path: str, strict: bool = False) -> list[dict[str, Any]]:
    """Read-only snapshot (shared lock). Returns a private deep copy."""
    with _locked(path, exclusive=False):
        return copy.deepcopy(_read_unlocked(path, strict))


def write_rows(path: str, rows: list) -> None:
    with _locked(path, exclusive=True):
        _write_unlocked(path, rows)


def update_rows(path: str, fn: Callable[[list], Any]) -> Any:
    """Exclusive load → fn(rows) (mutate in place) → save if changed. Returns fn's result.

    A corrupt ledger raises instead of being overwritten with an empty list.
    """
    with _locked(path, exclusive=True):
        rows = _read_unlocked(path, strict=True)
        before = json.dumps(rows, sort_keys=True)
        result = fn(rows)
        if json.dumps(rows, sort_keys=True) != before:
            _write_unlocked(path, rows)
        return result


def append_row(path: str, row: dict) -> None:
    update_rows(path, lambda rows: rows.append(row))


# ---------------------------------------------------------------------------
# Write-ahead intents
# ---------------------------------------------------------------------------

def new_key(kind: str) -> str:
    return f"{kind}:{int(time.time())}:{uuid.uuid4().hex[:12]}"


def _live(row: dict) -> bool:
    return row.get("status") not in INTENT_RETRYABLE


def assert_key_available(key: str) -> None:
    for row in read_rows(intents_path()):
        if row.get("key") == key and _live(row):
            raise DuplicateOperation(f"idempotency key {key} already used (status={row.get('status')})")


def begin_intent(key: str, kind: str, chain_id: int, token: str, amount_usdc: float,
                 sender: str, to: str, meta: dict | None = None) -> dict:
    """Record an operation BEFORE any tx is signed. Refuses a key that is still live."""
    now = int(time.time())
    row = {
        "key": key, "kind": kind, "chain_id": int(chain_id), "token": str(token).lower(),
        "amount_usdc": round(float(amount_usdc), 6), "sender": str(sender).lower(), "to": str(to).lower(),
        "status": "intent", "tx_hashes": [], "final_nonce": None, "ledgered": False,
        "meta": meta or {}, "created_at": now, "updated_at": now, "error": None,
    }

    def fn(rows):
        for r in rows:
            if r.get("key") == key and _live(r):
                raise DuplicateOperation(f"idempotency key {key} already used (status={r.get('status')})")
        rows.append(row)

    update_rows(intents_path(), fn)
    return dict(row)


def update_intent(key: str, add_tx: str | None = None, **fields) -> dict | None:
    def fn(rows):
        for r in reversed(rows):           # latest row for this key
            if r.get("key") == key:
                if add_tx and add_tx.lower() not in [h.lower() for h in r.get("tx_hashes", [])]:
                    r.setdefault("tx_hashes", []).append(add_tx)
                r.update(fields)
                r["updated_at"] = int(time.time())
                return dict(r)
        return None
    return update_rows(intents_path(), fn)


def get_intent(key: str) -> dict | None:
    for r in reversed(read_rows(intents_path())):
        if r.get("key") == key:
            return r
    return None


def list_intents(statuses: tuple | None = None, kinds: tuple | None = None) -> list[dict]:
    return [r for r in read_rows(intents_path())
            if (statuses is None or r.get("status") in statuses)
            and (kinds is None or r.get("kind") in kinds)]


def unresolved_intents(kinds: tuple | None = None) -> list[dict]:
    """Intents whose outcome is unknown, or whose money left but no in-transit row exists yet."""
    out = []
    for r in list_intents(kinds=kinds):
        st = r.get("status")
        if st in INTENT_UNRESOLVED or (st in ("confirmed", "unconfirmed") and not r.get("ledgered")):
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Servicer cash-flow journal
# ---------------------------------------------------------------------------

def record_cashflow(chain_id: int, token: str, delta_usdc: float, kind: str, ref: str) -> None:
    """delta < 0: money left the servicer (our tx); delta > 0: known inflow (tend, recall, bridge fill)."""
    append_row(cashflow_path(), {
        "ts": time.time(), "chain_id": int(chain_id), "token": str(token).lower(),
        "delta_usdc": round(float(delta_usdc), 6), "kind": kind, "ref": str(ref),
    })


def flows_since(chain_id: int, token: str, since_ts: float, rows: list | None = None) -> tuple[float, float]:
    """(outflows, inflows) as positive numbers for (chain, token) at or after since_ts."""
    rows = read_rows(cashflow_path()) if rows is None else rows
    out = inn = 0.0
    for r in rows:
        if int(r.get("chain_id", -1)) != int(chain_id) or r.get("token") != str(token).lower():
            continue
        if float(r.get("ts", 0)) < float(since_ts):
            continue
        d = float(r.get("delta_usdc", 0.0))
        if d < 0:
            out += -d
        else:
            inn += d
    return out, inn
