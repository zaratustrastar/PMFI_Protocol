"""Prediction.com adapter, feed selection and venue fees. Offline: every venue call is faked."""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import _fakes  # noqa: F401  (puts bot/ on sys.path)
from arb_monitor.adapters import oddpool, opportunity_feed as FEED, prediction_com as P
from arb_monitor.core import venue_fees as F

NOW = 1_800_000_000.0
YES_TOK, NO_TOK = "111" * 20, "222" * 20
COND = "0x" + "ab" * 32


def gamma(**over):
    m = {
        "id": "666659", "slug": "will-x-happen", "conditionId": COND, "closed": False, "active": True,
        "acceptingOrders": True, "enableOrderBook": True, "negRisk": False,
        "outcomes": json.dumps(["No", "Yes"]),  # deliberately NO first: order comes from outcomes
        "clobTokenIds": json.dumps([NO_TOK, YES_TOK]),
        "endDate": "2027-02-01T00:00:00Z", "question": "Will X happen?",
    }
    m.update(over)
    return m


def kalshi(**over):
    k = {"ticker": "KXTEST-27JAN31-X", "status": "active", "close_time": "2027-01-31T23:00:00Z",
         "yes_ask_dollars": "0.5600", "no_ask_dollars": "0.4500", "title": "X happens"}
    k.update(over)
    return k


def entry(poly_side="yes", poly_price=0.40, kal_price=0.55, wager=200.0, **over):
    e = {
        "group_id": 85210, "group_title": "Will X happen?", "event_date": "2027-01-31",
        "roi_pct": 4.9, "total_cost": None, "max_wager_usd": wager,
        "detected_at": "2027-01-15T07:59:00Z",
        "legs": [
            {"side": poly_side, "platform": "polymarket", "market_id": "666659",
             "source_url": "https://polymarket.com/market/will-x-happen?r=predictionhunt",
             "price": poly_price, "liquidity_usd": 900, "fee_usd": 0.012},
            {"side": "no" if poly_side == "yes" else "yes", "platform": "kalshi",
             "market_id": "KXTEST-27JAN31-X", "source_url": "https://kalshi.com/markets/KXTEST",
             "price": kal_price, "liquidity_usd": 700, "fee_usd": 0.02},
        ],
    }
    e.update(over)
    return e


class _Patched(unittest.TestCase):
    """Fakes Gamma, Kalshi, the Polymarket book and fee data; fixed clock."""

    def setUp(self):
        P._CACHE.clear()
        F._FEE_CACHE.clear()
        self.g, self.k = gamma(), kalshi()
        self.asks = {YES_TOK: 0.40, NO_TOK: 0.61}
        self.fee = (0.04, 1.0, "clob")
        env = {"ARB_STALE_QUOTE_SECONDS": "120", "ARB_FUNDING_CHAIN": "robinhood"}
        for p in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(P, "gamma_market", lambda mid: self.g),
            mock.patch.object(P, "kalshi_market", lambda t: self.k),
            mock.patch.object(P, "poly_best_ask", lambda tok: self.asks.get(tok)),
            mock.patch.object(F, "poly_fee_info", lambda cid, session=None: self.fee),
            mock.patch.object(P.time, "time", lambda: NOW),
        ):
            p.start()
            self.addCleanup(p.stop)
        # endDate/close_time relative to NOW: ~ 18 days out
        self.g["endDate"] = "2027-02-01T00:00:00Z"
        self.now = datetime_ts("2027-01-15T08:00:00Z")

    def norm(self, e, age=10):
        return P.normalize(e, self.now - age, now=self.now)


def datetime_ts(s):
    return P._ts(s)


class Normalize(_Patched):
    def test_poly_yes_leg_buys_kalshi_no_with_resolved_tokens(self):
        opp, why = self.norm(entry())
        self.assertEqual(why, "ok")
        self.assertIsInstance(opp, oddpool.ArbOpportunity)
        self.assertEqual((opp.poly_yes_token, opp.poly_no_token), (YES_TOK, NO_TOK), "order from outcomes, not index")
        self.assertEqual(opp.kalshi_side, "NO")
        self.assertEqual(opp.kalshi_ticker, "KXTEST-27JAN31-X")
        self.assertFalse(opp.is_display_only)
        self.assertEqual(opp.venue2, "kalshi")
        self.assertEqual(opp.fetched_at, self.now - 10, "signal time, not fetch time")

    def test_poly_no_leg_buys_kalshi_yes_and_live_prices_win_when_worse(self):
        self.k["yes_ask_dollars"] = "0.3300"
        opp, why = self.norm(entry(poly_side="no", poly_price=0.60, kal_price=0.32))
        self.assertEqual(why, "ok")
        self.assertEqual(opp.kalshi_side, "YES")
        self.assertEqual(opp.poly_yes_ask, 0.61, "live Polymarket ask 0.61 > quoted 0.60")
        self.assertEqual(opp.kalshi_yes_ask, 0.33, "live Kalshi ask 0.33 > quoted 0.32")

    def test_net_edge_is_computed_at_size_with_both_fees_not_provider_roi(self):
        opp, _ = self.norm(entry(wager=200.0))
        n = int(200 / 0.95)
        poly_fee = 0.04 * 0.4 * 0.6 * n
        kal_fee = -(-(0.07 * n * 0.55 * 0.45 * 100) // 1) / 100  # ceil to the cent
        expect = (1 - 0.40 - 0.55 - (poly_fee + kal_fee) / n) * 100  # quoted 0.55 is worse than live 0.45
        self.assertAlmostEqual(opp.raw["_engine"]["net_cents"], round(expect, 4), places=3)
        self.assertAlmostEqual(opp.net_edge_pct, expect - P.ARB_RISK_BUFFER_PCT, places=3)
        self.assertEqual(opp.raw["_engine"]["provider_roi_pct"], 4.9)
        self.assertEqual(opp.fillable_size_usdc, 200.0)

    def test_size_capped_by_max_pair(self):
        with mock.patch.object(P, "ARB_MAX_PAIR_USDC", 50.0):
            opp, _ = self.norm(entry(wager=5000))
        self.assertEqual(opp.fillable_size_usdc, 50.0)

    def test_cents_prices_are_normalised(self):
        opp, why = self.norm(entry(poly_price=40, kal_price=45))
        self.assertEqual(why, "ok")
        self.assertEqual(opp.poly_yes_ask, 0.40)

    def test_refusals(self):
        cases = [
            (entry(poly_price=None), "null_price"),
            (entry(kal_price=0), "null_price"),
            (entry(legs=[]), "unsupported_platforms"),
        ]
        for e, why in cases:
            self.assertEqual(self.norm(e), (None, why))
        self.assertEqual(self.norm(entry(), age=500), (None, "stale_signal"))
        bad_side = entry()
        bad_side["legs"][1]["side"] = "yes"
        self.assertEqual(self.norm(bad_side), (None, "sides_not_complementary"))
        e = entry()
        e["legs"][0]["source_url"] = "https://polymarket.com/event/some-other-market"
        self.assertEqual(self.norm(e), (None, "poly_slug_mismatch"))

    def test_market_state_refusals(self):
        for over, why in (
            ({"closed": True}, "poly_market_not_live"),
            ({"acceptingOrders": False}, "poly_market_not_live"),
            ({"outcomes": json.dumps(["Trump", "Harris"])}, "poly_tokens_unresolved"),
            ({"endDate": "2027-06-01T00:00:00Z"}, "expiry_mismatch"),
        ):
            self.g = gamma(**over)
            self.assertEqual(self.norm(entry())[1], why, over)
        self.g = gamma()
        self.k = kalshi(status="closed")
        self.assertEqual(self.norm(entry())[1], "kalshi_market_not_active")
        self.k = kalshi(no_ask_dollars=None, no_ask=None)
        self.assertEqual(self.norm(entry())[1], "kalshi_null_price")

    def test_wrong_token_or_unprofitable_is_dropped(self):
        self.asks[YES_TOK] = 0.75
        self.assertEqual(self.norm(entry())[1], "poly_price_mismatch")
        self.asks[YES_TOK] = 0.40
        self.k["no_ask_dollars"] = "0.5900"
        self.assertEqual(self.norm(entry())[1], "unprofitable_after_fees")

    def test_too_small_or_expiring(self):
        self.assertEqual(self.norm(entry(wager=5))[1], "below_min_size")
        self.g = gamma(endDate="2027-01-15T20:00:00Z")
        self.k = kalshi(close_time="2027-01-15T20:00:00Z")
        self.assertEqual(self.norm(entry())[1], "expires_within_1d")


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Provider(unittest.TestCase):
    def test_request_shape_and_rate_limit_headers(self):
        calls = []

        class S:
            def get(self, url, params=None, headers=None, timeout=None):
                calls.append((url, params, headers))
                return _Resp(200, {"opportunities": []}, {"X-RateLimit-Remaining-Month": "9000"})

        with mock.patch.dict(os.environ, {"PREDICTION_COM_API_KEY": "k"}):
            P.fetch_arb_raw(S())
        url, params, headers = calls[0]
        self.assertEqual(url, "https://prediction.com/api/v2/arb")
        self.assertEqual(params["platforms"], "polymarket,kalshi")
        self.assertEqual(headers["X-API-Key"], "k")
        self.assertEqual(P.RATE_LIMITS["X-RateLimit-Remaining-Month"], "9000")

    def test_429_returns_nothing_and_no_key_skips(self):
        class S:
            def get(self, *a, **k):
                return _Resp(429, {}, {"Retry-After": "3"})

        with mock.patch.dict(os.environ, {"PREDICTION_COM_API_KEY": "k"}):
            self.assertEqual(P.fetch_arb_raw(S()), {})
        with mock.patch.dict(os.environ, {"PREDICTION_COM_API_KEY": ""}):
            self.assertEqual(P.fetch_arb_raw(S()), {})

    def test_signal_time_is_as_of_minus_delay(self):
        seen = []
        body = {"as_of": "2027-01-15T08:00:00Z", "delay_seconds": 15, "opportunities": [entry()]}
        with mock.patch.object(P, "fetch_arb_raw", lambda s=None: body), \
             mock.patch.object(P, "normalize", lambda e, ts: (seen.append(ts), (None, "x"))[1]):
            P.fetch_opportunities()
        self.assertEqual(seen, [P._ts("2027-01-15T08:00:00Z") - 15])


def _opp(tok, net, kside="NO"):
    return oddpool.ArbOpportunity(
        pair_id=tok, poly_yes_token=tok if kside == "NO" else "y", poly_no_token=tok if kside == "YES" else "n",
        kalshi_ticker="K", poly_yes_ask=0.4, kalshi_yes_ask=0.5, gross_edge_pct=net, expiry_ts=0,
        days_to_expiry=10, pnl_velocity=0, net_edge_pct=net, fillable_size_usdc=100, score=net,
        kalshi_side=kside, is_display_only=False,
    )


class Feed(unittest.TestCase):
    def test_default_is_oddpool_and_bad_value_refused(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_OPPORTUNITY_FEED", None)
            self.assertEqual(FEED.feed_mode(), "oddpool")
        with mock.patch.dict(os.environ, {"ARB_OPPORTUNITY_FEED": "nope"}):
            self.assertRaises(ValueError, FEED.feed_mode)

    def test_compare_trades_from_primary_and_logs_overlap(self):
        a = [_opp("t1", 1.0), _opp("t2", 2.0)]
        b = [_opp("t2", 1.5), _opp("t3", 3.0, "YES")]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cmp.jsonl")
            env = {"ARB_OPPORTUNITY_FEED": "compare", "ARB_FEED_COMPARE_LOG": path, "ARB_FEED_COMPARE_PRIMARY": "prediction_com"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(FEED, "_fetch", lambda n: a if n == "oddpool" else b):
                got = FEED.fetch_opportunities()
            self.assertIs(got, b)
            with open(path) as fh:
                row = json.loads(fh.read().strip())
        self.assertEqual((row["both"], row["only_oddpool"], row["only_prediction_com"]), (1, 1, 1))
        self.assertEqual(row["pairs"][0]["oddpool_net"], 2.0)

    def test_compare_survives_one_provider_failing(self):
        def boom(n):
            if n == "prediction_com":
                raise RuntimeError("down")
            return [_opp("t1", 1.0)]

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ARB_OPPORTUNITY_FEED": "compare", "ARB_FEED_COMPARE_LOG": os.path.join(d, "c")}), \
             mock.patch.object(FEED, "_fetch", boom):
            self.assertEqual(len(FEED.fetch_opportunities()), 1)


class Fees(unittest.TestCase):
    def test_kalshi_fee_matches_executor_pad(self):
        from arb_monitor.core.arb_executor import _kalshi_taker_fee_pad
        for p, n in ((0.45, 100), (0.5, 1), (0.03, 7), (0.97, 250)):
            self.assertAlmostEqual(F.kalshi_taker_fee(p, n) / n, _kalshi_taker_fee_pad(p, n), places=9)
        self.assertEqual(F.kalshi_taker_fee(0.5, 100), 1.75)

    def test_polymarket_fee_formula(self):
        self.assertAlmostEqual(F.poly_taker_fee_per_contract(0.5, 0.07, 1), 0.0175)
        self.assertAlmostEqual(F.poly_taker_fee(0.4, 100, 0.04, 1), 0.96)
        self.assertEqual(F.poly_taker_fee_per_contract(0.4, 0.0, 1), 0.0)

    def test_fee_info_from_clob_and_fallback(self):
        F._FEE_CACHE.clear()

        class S:
            def __init__(self, status, body):
                self.r = _Resp(status, body)

            def get(self, url, timeout=None):
                return self.r

        self.assertEqual(F.poly_fee_info("0xc1", S(200, {"t": [{}], "fd": {"r": 0.05, "e": 1}})), (0.05, 1.0, "clob"))
        self.assertEqual(F.poly_fee_info("0xc2", S(200, {"t": [{}]})), (0.07, 1.0, "fallback"))
        self.assertEqual(F.poly_fee_info("0xc3", S(500, {})), (0.07, 1.0, "fallback"))
        self.assertEqual(F.poly_fee_info(None), (0.07, 1.0, "fallback"))

    def test_executor_poly_pad_only_when_enabled(self):
        from arb_monitor.core import arb_executor as X
        with mock.patch.object(F, "poly_fee_info", lambda c: (0.05, 1.0, "clob")):
            with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}):
                os.environ.pop("ARB_POLY_EXACT_FEES", None)
                self.assertAlmostEqual(X._poly_fee_pad(COND, 0.4), 0.012)
            with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "base"}):
                os.environ.pop("ARB_POLY_EXACT_FEES", None)
                self.assertEqual(X._poly_fee_pad(COND, 0.4), 0.0, "base mode unchanged by default")


class ClobProxySdk(unittest.TestCase):
    def test_v2_sdk_httpx_client_is_replaced(self):
        from arb_monitor.core import clob_proxy as C
        made = []

        class FakeHttpx(types.ModuleType):
            class Client:
                def __init__(self, **kw):
                    made.append(kw)

        helpers = types.ModuleType("py_clob_client_v2.http_helpers.helpers")
        helpers._http_client = object()
        mods = {
            "httpx": FakeHttpx("httpx"),
            "py_clob_client_v2": types.ModuleType("py_clob_client_v2"),
            "py_clob_client_v2.http_helpers": types.ModuleType("py_clob_client_v2.http_helpers"),
            "py_clob_client_v2.http_helpers.helpers": helpers,
        }
        mods["py_clob_client_v2.http_helpers"].helpers = helpers
        with mock.patch.dict(sys.modules, mods):
            self.assertTrue(C.patch_sdk_httpx("http://user:secret@proxy.test:8080"))
        self.assertEqual(made, [{"http2": True, "proxy": "http://user:secret@proxy.test:8080"}])
        self.assertIsInstance(helpers._http_client, FakeHttpx.Client)
        self.assertEqual(C.SDK_ROUTE, "proxy")
        self.assertNotIn("secret", C._redact("http://user:secret@proxy.test:8080"))


if __name__ == "__main__":
    unittest.main()
