"""Read-only bridge / transfer status for the MILORD monitor (offchain/services/monitor.mjs BRIDGE_STATUS_URL).

Serves GET /bridges -> {"rows": [{id, kind, amount, status, startedAt}], "taken_at": ...} from a lock-consistent
snapshot of the funding ledgers. It never writes a ledger and holds no key.

    python3 -m arb_monitor.core.arb_bridge_status        (from bot/; env ARB_BRIDGE_STATUS_HOST/PORT)

Bind it to a private interface only; the rows carry amounts and tx hashes, nothing secret.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from .arb_withdrawals import get_ledger_snapshot

# Rows that still represent money moving. "confirmed"/"completed" rows are done; failures whose money never left
# are not in transit. Unknown statuses are reported as in transit so the monitor sees them.
DONE = {"completed", "confirmed", "arrived", "failed", "abandoned", "reverted", "cancelled", "refunded"}


def in_transit_rows(snapshot: dict | None = None) -> list[dict]:
    snap = snapshot if snapshot is not None else get_ledger_snapshot()
    rows = []
    for source in ("withdrawals", "bridges", "venue_deposits", "intents"):
        for i, r in enumerate(snap.get(source) or []):
            status = str(r.get("status") or "unknown")
            if source == "intents":
                if r.get("ledgered") or status in ("abandoned", "reverted", "failed"):
                    continue  # ledgered: its ledger row is reported instead; the others moved no money
            elif status in DONE:
                continue
            rows.append({
                "id": str(r.get("id") or r.get("idem_key") or r.get("tx_hash") or f"{source}-{i}"),
                "kind": f"{source}:{r.get('direction') or r.get('platform') or r.get('dest') or r.get('kind') or ''}",
                "amount": r.get("amount_usdc"),
                "status": status,
                "startedAt": int(float(r.get("created_at") or r.get("ts") or 0)),
                "tx": r.get("tx_hash"),
            })
    return rows


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") != "/bridges":
            self.send_response(404)
            self.end_headers()
            return
        try:
            snap = get_ledger_snapshot()
            body, code = {"rows": in_transit_rows(snap), "taken_at": snap.get("taken_at")}, 200
        except Exception as e:  # a corrupt or unreadable ledger is an error the monitor must see
            body, code = {"error": str(e)}, 503
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main():
    host = os.environ.get("ARB_BRIDGE_STATUS_HOST", "127.0.0.1")
    port = int(os.environ.get("ARB_BRIDGE_STATUS_PORT", "9110"))
    HTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
