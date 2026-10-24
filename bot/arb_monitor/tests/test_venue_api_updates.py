"""Kalshi order payload against the current API (fixed-point dollars, fill-or-kill, fill_count) and
the Polymarket balance read through the V2-compatible SDK layer. Offline."""
import os
import unittest
from unittest import mock

import _fakes  # noqa: F401
from arb_monitor.core import arb_executor as X
from arb_monitor.core import kalshi_auth


class _R:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, str(body)

    def json(self):
        return self._b


class KalshiPrice(unittest.TestCase):
    def test_float_noise_never_rounds_below_the_ask(self):
        self.assertEqual(int(0.29 * 100), 28, "the old truncation bug")
        self.assertEqual(X._kalshi_price_dollars(0.29), "0.2900")
        self.assertEqual(X._kalshi_price_dollars(0.064), "0.0640")
        self.assertEqual(X._kalshi_price_dollars(0.12341), "0.1235")
        self.assertRaises(ValueError, X._kalshi_price_dollars, 1.0)
        self.assertRaises(ValueError, X._kalshi_price_dollars, 0)

    def test_fill_count_fields(self):
        self.assertEqual(X._kalshi_fill_count({"fill_count_fp": "12.00"}), 12)
        self.assertEqual(X._kalshi_fill_count({"fill_count": 7}), 7)
        self.assertEqual(X._kalshi_fill_count({"quantity_matched": 3}), 3)
        self.assertEqual(X._kalshi_fill_count({}), 0)


class KalshiOrder(unittest.TestCase):
    def setUp(self):
        self.posts = []
        for p in (
            mock.patch.object(kalshi_auth, "kalshi_auth_available", lambda: True),
            mock.patch.object(kalshi_auth, "get_kalshi_headers", lambda m, u: {"KALSHI-ACCESS-KEY": "k"}),
            mock.patch.object(X.time, "sleep", lambda s: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def place(self, reply, side="NO", price=0.29, n=10, env=None):
        sess = mock.Mock()
        sess.post.side_effect = lambda url, json=None, headers=None, timeout=None: (self.posts.append(json), reply)[1]
        sess.get.return_value = _R(200, {"order": {"status": "resting"}})
        with mock.patch.object(X, "_KALSHI_SESSION", sess), mock.patch.dict(os.environ, env or {}):
            return X._place_kalshi_order("KXT-1", side, price, 0, contract_count=n)

    def test_payload_is_fill_or_kill_in_dollars(self):
        ok, oid, err = self.place(_R(201, {"order": {"order_id": "o1", "status": "executed", "fill_count_fp": "10.00"}}))
        self.assertTrue(ok, err)
        body = self.posts[0]
        self.assertEqual(body["time_in_force"], "fill_or_kill")
        self.assertEqual(body["no_price_dollars"], "0.2900")
        self.assertNotIn("no_price", body)
        self.assertNotIn("yes_price", body)
        self.assertNotIn("expiration_ts", body)
        self.assertEqual((body["action"], body["side"], body["count"]), ("buy", "no", 10))

    def test_killed_order_fails_without_polling(self):
        ok, oid, err = self.place(_R(201, {"order": {"order_id": "o2", "status": "canceled", "fill_count": 0}}))
        self.assertFalse(ok)
        self.assertIn("kalshi_order_canceled", err)

    def test_ioc_is_still_available(self):
        self.place(_R(201, {"order": {"order_id": "o3", "status": "executed", "fill_count": 10}}),
                   env={"ARB_KALSHI_TIME_IN_FORCE": "immediate_or_cancel"})
        self.assertEqual(self.posts[0]["time_in_force"], "immediate_or_cancel")


class PolyBalanceUsesCompat(unittest.TestCase):
    def test_balance_goes_through_clob_compat(self):
        from arb_monitor.core import arb_withdrawals as W
        made = []

        class C:
            def __init__(self, sig):
                self.sig = sig

            def update_balance_allowance(self, p):
                pass

            def get_balance_allowance(self, p):
                return {"balance": "2500000" if self.sig == 2 else "0"}

        fake_compat = mock.Mock()
        fake_compat.make_client.side_effect = lambda *a, **k: (made.append(k), C(k["signature_type"]))[1]
        env = {"POLY_API_KEY": "a", "POLY_API_SECRET": "b", "POLY_API_PASSPHRASE": "c", "POLY_PRIVATE_KEY": "0x" + "1" * 64}
        with mock.patch.dict(os.environ, env), mock.patch.dict("sys.modules", {"arb_monitor.core.clob_compat": fake_compat}):
            self.assertEqual(W._poly_get_balance(), 2.5)
        self.assertEqual(made[0]["signature_type"], 2)


if __name__ == "__main__":
    unittest.main()
