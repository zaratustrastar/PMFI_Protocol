"""B9: the Kalshi withdrawal evidence tool is read-only (GET only) and classifies the evidence. Offline."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import _fakes  # noqa: F401  (puts bot/ on sys.path)
from arb_monitor.scripts import kalshi_withdrawal_evidence as E

BASE = "https://kalshi.test/trade-api/v2"


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _Session:
    """Answers the two documented GETs; any other method fails the test."""

    def __init__(self, withdrawals, pages=1):
        self.calls, self.withdrawals, self.pages = [], withdrawals, pages

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        if url.endswith("/portfolio/balance"):
            return _Resp({"balance": 12345})
        page = int((params or {}).get("cursor") or 0)
        nxt = str(page + 1) if page + 1 < self.pages else None
        return _Resp({"withdrawals": self.withdrawals if page == 0 else [], "cursor": nxt})

    def __getattr__(self, name):  # post, put, delete, request ...
        raise AssertionError(f"evidence tool attempted {name.upper()}")


def _ledger(rows):
    fd, p = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(rows, f)
    return p


ROWS = [
    {"platform": "kalshi", "reference": "w1", "amount_usdc": 10.0, "status": "completed", "dest": "0xabc"},
    {"platform": "polymarket", "reference": "p1", "amount_usdc": 5.0, "status": "completed"},
]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("arb_monitor.core.kalshi_auth.get_kalshi_headers", return_value={"KALSHI-ACCESS-KEY": "k"})
        p.start()
        self.addCleanup(p.stop)
        for name in ("post", "put", "patch", "delete", "request"):
            q = mock.patch(f"requests.{name}", side_effect=AssertionError(f"requests.{name} used"))
            q.start()
            self.addCleanup(q.stop)

    def test_only_get_requests_and_pagination(self):
        s = _Session([{"id": "w1", "type": "crypto", "status": "applied", "amount_cents": 1000}], pages=3)
        api = E.api_history(s, BASE)
        self.assertTrue(all(c[0] == "GET" for c in s.calls))
        self.assertEqual([c[1].rsplit("/", 2)[-2:] for c in s.calls][0], ["portfolio", "balance"])
        self.assertEqual(len(s.calls), 1 + 3)
        self.assertEqual(len(api["withdrawals"]), 1)

    def test_ledger_filters_kalshi_rows(self):
        path = _ledger(ROWS)
        self.assertEqual([r["reference"] for r in E.ledger_rows(path)], ["w1"])
        self.assertEqual(E.ledger_rows(path + ".missing"), [])

    def test_confirmed_when_engine_id_applied_as_crypto(self):
        api = {"balance": {"balance": 1}, "withdrawals": [{"id": "w1", "type": "crypto", "status": "applied", "amount_cents": 1000}]}
        out = E.summarize(E.ledger_rows(_ledger(ROWS)), api)
        self.assertEqual(out["verdict"]["result"], "confirmed")
        self.assertEqual(out["api"]["crypto_applied_cents"], 1000)

    def test_contradicted_when_kalshi_does_not_know_engine_ids(self):
        api = {"balance": {"balance": 1}, "withdrawals": [{"id": "zz", "type": "ach", "status": "applied"}]}
        out = E.summarize(E.ledger_rows(_ledger(ROWS)), api)
        self.assertEqual(out["verdict"]["result"], "contradicted")
        self.assertEqual(out["api"]["ledger_refs_unknown_to_kalshi"], ["w1"])

    def test_unconfirmed_when_matched_but_not_paid_or_ledger_only(self):
        api = {"balance": {"balance": 1}, "withdrawals": [{"id": "w1", "type": "crypto", "status": "failed"}]}
        self.assertEqual(E.summarize(E.ledger_rows(_ledger(ROWS)), api)["verdict"]["result"], "unconfirmed")
        self.assertEqual(E.summarize(E.ledger_rows(_ledger(ROWS)), None)["verdict"]["result"], "unconfirmed")
        self.assertEqual(E.summarize([], {"balance": {}, "withdrawals": []})["verdict"]["result"], "unconfirmed")

    def test_cli_ledger_only_makes_no_network_call(self):
        with mock.patch("requests.get", side_effect=AssertionError("network used")), mock.patch("requests.Session", side_effect=AssertionError("network used")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(E.main(["--ledger", _ledger(ROWS)]), 0)
        self.assertEqual(json.loads(buf.getvalue())["ledger"]["kalshi_rows"], 1)


if __name__ == "__main__":
    unittest.main()
