"""B9: robinhood-mode venue recall routes (Polymarket pUSD bridge, Kalshi operator withdrawals). Offline."""
import os
import unittest
from unittest import mock

import _fakes as F
from arb_monitor.core import arb_funder, arb_withdrawals as W, arb_venue_routes as R


class _Base(unittest.TestCase):
    extra = {}

    def setUp(self):
        for p in [
            mock.patch.dict(os.environ, {**F.rh_env(), **self.extra}, clear=True),
            mock.patch.object(arb_funder, "get_servicer_cash_usdc", return_value=100.0),
            mock.patch.object(arb_funder, "_get_usdc_balance", return_value=0.0),
            mock.patch.object(W, "_poly_get_balance", return_value=50.0),
            mock.patch.object(W, "_kalshi_get_balance", return_value=50.0),
        ]:
            p.start()
            self.addCleanup(p.stop)
        F.temp_ledgers(self)
        self.legacy = mock.patch.object(W, "withdraw_from_polymarket", side_effect=AssertionError("legacy USDC.e route used")).start()
        self.kalshi_post = mock.patch.object(W, "withdraw_from_kalshi", side_effect=AssertionError("undocumented Kalshi API used")).start()
        self.addCleanup(mock.patch.stopall)

    def rows(self):
        return W._load_ledger(W.WITHDRAWAL_LEDGER_PATH)


class Unconfigured(_Base):
    def test_polymarket_recall_refuses_legacy_usdce_route(self):
        self.assertEqual(W._rh_recall_polymarket(20.0, F.SERVICER), 0.0)
        self.assertEqual(self.rows(), [])  # no intent: nothing was attempted

    def test_kalshi_recall_is_an_operator_task(self):
        self.assertEqual(W._rh_recall_kalshi(20.0, F.SERVICER), 0.0)
        self.assertEqual(self.rows(), [])


class BridgeRoute(_Base):
    extra = {"ARB_POLY_WITHDRAW_ROUTE": "polymarket_bridge"}

    def test_exact_pusd_amount_and_write_ahead_row(self):
        calls = []
        def runner(action, **kw):
            calls.append((action, kw))
            return {"txHash": "0x" + "ab" * 32, "bridgeAddress": "0x" + "22" * 20, "_exit": 0}
        with mock.patch.object(R, "poly_bridge", runner):
            self.assertEqual(W._rh_recall_polymarket(12.345678, F.SERVICER), 12.345678)
        self.assertEqual(calls, [("withdraw", {"amountRaw": "12345678"})])
        row = self.rows()[0]
        self.assertEqual(row["reference"], "0x" + "ab" * 32)
        self.assertEqual(row["poly_bridge_address"], "0x" + "22" * 20)
        self.assertEqual(row["status"], "pending")

    def test_unknown_outcome_stays_in_transit_and_is_not_retried(self):
        with mock.patch.object(R, "poly_bridge", return_value={"unknown": True, "bridgeAddress": "0x" + "33" * 20, "_exit": 3}):
            self.assertEqual(W._rh_recall_polymarket(10.0, F.SERVICER), 0.0)
        row = self.rows()[0]
        self.assertEqual(row["receipt"], "unconfirmed")
        self.assertEqual(row["status"], "pending")  # still counted as in transit by NAV

    def test_runner_crash_is_ambiguous(self):
        with mock.patch.object(R, "poly_bridge", side_effect=TimeoutError("node")):
            self.assertEqual(W._rh_recall_polymarket(10.0, F.SERVICER), 0.0)
        self.assertEqual(self.rows()[0]["receipt"], "unconfirmed")

    def test_refusal_before_submission_marks_failed(self):
        with mock.patch.object(R, "poly_bridge", return_value={"error": "Polymarket bridge withdrawal disabled", "_exit": 1}):
            self.assertEqual(W._rh_recall_polymarket(10.0, F.SERVICER), 0.0)
        self.assertNotEqual(self.rows()[0]["status"], "pending")


class KalshiVerified(_Base):
    extra = {"ARB_KALSHI_API_WITHDRAW_VERIFIED": "true"}

    def test_api_path_only_when_explicitly_verified(self):
        self.kalshi_post.side_effect = None
        self.kalshi_post.return_value = (True, "wd-1", 20.0)
        self.assertEqual(W._rh_recall_kalshi(20.0, F.SERVICER), 20.0)
        self.kalshi_post.assert_called_once()


class Raw(unittest.TestCase):
    def test_to_raw(self):
        self.assertEqual(R.to_raw(5), 5_000_000)
        self.assertEqual(R.to_raw(0.000001), 1)
        with self.assertRaises(ValueError):
            R.to_raw(0)


if __name__ == "__main__":
    unittest.main()
