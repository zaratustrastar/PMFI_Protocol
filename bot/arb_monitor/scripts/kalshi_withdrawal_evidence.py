"""READ-ONLY evidence on whether the PMFI engine's automated Kalshi withdrawal has ever worked.

    python -m bot.arb_monitor.scripts.kalshi_withdrawal_evidence [--ledger PATH] [--api]

It never creates, cancels or changes anything: the only network calls are two documented GETs
(/portfolio/balance and /portfolio/withdrawals), and only with --api.

What it reports (JSON on stdout):
  ledger   Kalshi rows in the engine's withdrawal ledger (ARB_WITHDRAWAL_LEDGER_PATH). Each row's
           `reference` is the id that POST /portfolio/balance/withdrawals returned when the engine
           requested the withdrawal. Base-mode rows are marked complete by ETA, so a "completed"
           row alone does not prove money arrived.
  api      Kalshi's own withdrawal history, by type and status (crypto/ach/wire/...), and which
           ledger references Kalshi knows: a ledger id that appears there as type=crypto with
           status=applied is direct evidence that the API-created withdrawal was paid.
  verdict  confirmed | unconfirmed | contradicted, with the reason.

The POST endpoint the engine uses is NOT in Kalshi's published API reference (which documents only
GET /portfolio/withdrawals), so only matched ids count as confirmation; a 2xx in old logs does not.
"""

import argparse
import json
import os
import sys
from collections import Counter

import requests

DEFAULT_LEDGER = os.path.expanduser("~/PolyNotifyBot/data/arb_withdrawals_ledger.json")


def ledger_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    from ..core.arb_ledger import read_rows

    return [r for r in read_rows(path) if isinstance(r, dict) and r.get("platform") == "kalshi"]


def _get(session, base_url: str, path: str, params=None) -> dict:
    """Signed GET. GET only: this tool must never send anything that moves money."""
    from ..core.kalshi_auth import get_kalshi_headers

    url = f"{base_url.rstrip('/')}{path}"
    headers = get_kalshi_headers("GET", url)
    if not headers:
        raise RuntimeError("Kalshi credentials not configured (KALSHI_API_KEY_ID + private key)")
    resp = session.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def api_history(session, base_url: str, max_pages: int = 50) -> dict:
    balance = _get(session, base_url, "/portfolio/balance")
    items, cursor = [], None
    for _ in range(max_pages):
        page = _get(session, base_url, "/portfolio/withdrawals", {"limit": 500, **({"cursor": cursor} if cursor else {})})
        items += page.get("withdrawals") or []
        cursor = page.get("cursor")
        if not cursor:
            break
    return {"balance": balance, "withdrawals": items}


def summarize(rows: list[dict], api: dict | None) -> dict:
    out: dict = {
        "ledger": {
            "kalshi_rows": len(rows),
            "by_status": dict(Counter(str(r.get("status")) for r in rows)),
            "rows": [{k: r.get(k) for k in ("reference", "amount_usdc", "status", "created_at", "dest")} for r in rows][-20:],
        }
    }
    if api is None:
        out["verdict"] = {
            "result": "unconfirmed",
            "reason": "ledger only; run with --api to match the engine's withdrawal ids against Kalshi's own history",
        }
        return out
    ws = api["withdrawals"]
    by_id = {str(w.get("id")): w for w in ws}
    refs = [str(r.get("reference")) for r in rows]
    matched = [by_id[x] for x in refs if x in by_id]
    paid = [w for w in matched if w.get("type") == "crypto" and w.get("status") == "applied"]
    out["api"] = {
        "balance_cents": api["balance"].get("balance"),
        "withdrawals": len(ws),
        "by_type_status": dict(Counter(f"{w.get('type')}:{w.get('status')}" for w in ws)),
        "crypto_applied_cents": sum(int(w.get("amount_cents") or 0) for w in ws if w.get("type") == "crypto" and w.get("status") == "applied"),
        "ledger_refs_known_to_kalshi": len(matched),
        "ledger_refs_paid_crypto": len(paid),
        "ledger_refs_unknown_to_kalshi": [x for x in refs if x not in by_id][:20],
    }
    if paid:
        verdict = ("confirmed", f"{len(paid)} engine-requested crypto withdrawal(s) appear in Kalshi's history as applied")
    elif rows and not matched:
        verdict = ("contradicted", "the engine recorded Kalshi withdrawal ids that Kalshi's history does not contain")
    elif any(w.get("type") == "crypto" for w in ws):
        verdict = ("unconfirmed", "the account has crypto withdrawals, but none matches an engine-recorded id (app-made or not recorded)")
    else:
        verdict = ("unconfirmed", "no crypto withdrawal has ever been made from this account")
    out["verdict"] = {"result": verdict[0], "reason": verdict[1]}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ledger", default=os.environ.get("ARB_WITHDRAWAL_LEDGER_PATH", DEFAULT_LEDGER))
    p.add_argument("--api", action="store_true", help="also read Kalshi's withdrawal history (two signed GETs)")
    a = p.parse_args(argv)
    rows = ledger_rows(a.ledger)
    api = None
    if a.api:
        from ..config import KALSHI_BASE_URL

        api = api_history(requests.Session(), KALSHI_BASE_URL)
    print(json.dumps({"ledger_path": a.ledger, **summarize(rows, api)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
