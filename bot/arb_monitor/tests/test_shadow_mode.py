"""ARB_EXECUTION_MODE=shadow is read-only: no chain broadcast, no funds movement, no venue order or cancel,
no ledger or positions-DB write, no reporter / tend / intent reconciliation. Offline.

Two layers are tested: the execution loop's separate shadow cycle (nothing state-changing is even
called), and the shadow guard at each choke point (a state-changing path reached by mistake raises).
"""

import inspect
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import requests

import _fakes as F  # noqa: F401  (sys.path setup)
from arb_monitor import http_client, run_pilot as R
from arb_monitor.adapters import opportunity_feed as FEED
from arb_monitor.adapters import prediction_com as P
from arb_monitor.core import (
    arb_ctf_redemption, arb_execution_loop as EL, arb_executor as EX, arb_funder, arb_ledger,
    arb_positions_db, arb_reporter, arb_venue_routes, arb_withdrawals, clob_proxy, shadow_guard as SG,
    venue_eligibility as VE,
)
from arb_monitor.scripts import venue_connectivity_check as V

SHADOW = {"ARB_FUNDING_CHAIN": "robinhood", "ARB_EXECUTION_MODE": "shadow"}
BLOCKED = {"available": False, "blocked": True, "country": "US", "region": "CA",
           "reason": "Polymarket geoblocks this host (US/CA)"}


def opp(pair_id="p1"):
    return SimpleNamespace(
        pair_id=pair_id, venue2="kalshi", is_display_only=False, opinion_market_id="", outcome_key="yes",
        poly_yes_token="0xyes", poly_no_token="0xno", kalshi_side="NO", kalshi_ticker="KX-1",
        kalshi_title="t", poly_title="t", kalshi_yes_ask=0.5, fetched_at=time.time(),
        net_edge_pct=9.0, gross_edge_pct=9.0, score=100.0, days_to_expiry=10.0, expiry_ts=0,
        fillable_size_usdc=50.0, annualized_return=1.0, confidence=1.0, label="",
    )


def forbidden(name):
    return mock.Mock(side_effect=AssertionError(f"{name} called in shadow mode"))


class NetRecorder:
    """Patches every requests entry point: records (method, url), refuses anything but GET."""

    def __init__(self):
        self.calls = []

    def request(self, sess_or_method, *a, **k):
        method, url = (a[0], a[1]) if isinstance(sess_or_method, requests.Session) else (sess_or_method, a[0])
        self.calls.append((method.upper(), url))
        if method.upper() != "GET":
            raise AssertionError(f"{method} {url} in shadow mode")
        raise requests.ConnectionError("offline test")

    def __enter__(self):
        self._p = [
            mock.patch.object(requests.Session, "request", lambda s, *a, **k: self.request(s, *a, **k)),
            mock.patch.object(requests, "post", lambda *a, **k: self.request("POST", *a, **k)),
            mock.patch.object(requests, "put", lambda *a, **k: self.request("PUT", *a, **k)),
            mock.patch.object(requests, "delete", lambda *a, **k: self.request("DELETE", *a, **k)),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()


class Guard(unittest.TestCase):
    def test_mode_defaults(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}, clear=True):
            self.assertEqual(SG.execution_mode(), "shadow")
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "base"}, clear=True):
            self.assertEqual(SG.execution_mode(), "live")
        with mock.patch.dict(os.environ, {"ARB_EXECUTION_MODE": "paper"}, clear=True):
            self.assertRaises(ValueError, SG.execution_mode)

    def test_rpc_reads_allowed_writes_refused(self):
        with mock.patch.dict(os.environ, SHADOW, clear=True):
            SG.assert_rpc_allowed("eth_call")
            SG.assert_rpc_allowed("eth_getBalance")
            for m in SG.WRITE_RPC_METHODS:
                self.assertRaises(SG.ShadowModeViolation, SG.assert_rpc_allowed, m)
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood", "ARB_EXECUTION_MODE": "live"}, clear=True):
            SG.assert_rpc_allowed("eth_sendRawTransaction")


class ShadowCycle(unittest.TestCase):
    """A full shadow cycle on live-looking books: one journal row, nothing else."""

    def test_cycle_journals_and_never_touches_state(self):
        book = object()
        names = {
            EL: ["run_reporter_tick", "run_tend_tick", "run_startup_checks", "_get_liquidity_state",
                 "get_servicer_deployable_usdc", "_get_platform_balance", "upsert_position",
                 "get_open_positions", "get_total_deployed_usdc"],
            arb_funder: ["fund_both_legs_for_trade", "reconcile_funding_intents", "get_platform_spot_balances",
                         "get_servicer_deployable_usdc", "_send_erc20_transfer", "_send_erc20_approve",
                         "_call_tend_if_ready"],
            EX: ["_place_poly_order", "_place_kalshi_order", "_cancel_leg2_order", "_unwind_poly_leg",
                 "_kalshi_unwind_best_effort", "log_execution_to_db"],
            arb_withdrawals: ["withdraw_from_platforms", "sweep_base_usdc_to_robinhood", "reconcile_bridge_transfers"],
            arb_ledger: ["_write_unlocked"],
            arb_positions_db: ["init_arb_tables", "upsert_position", "settle_position"],
        }
        with tempfile.TemporaryDirectory() as d:
            journal = os.path.join(d, "shadow.jsonl")
            patches = [mock.patch.object(mod, n, forbidden(n)) for mod, ns in names.items() for n in ns]
            patches += [
                mock.patch.dict(os.environ, {**SHADOW, "ARB_SHADOW_JOURNAL_PATH": journal,
                                             "ARB_SHADOW_CAPITAL_USDC": "20"}, clear=True),
                mock.patch.object(EL, "fetch_opportunities", return_value=[opp()]),
                mock.patch.object(VE, "polymarket_trading_status", return_value=BLOCKED),
                mock.patch.object(EX, "kalshi_resolve_market_ticker", return_value="KX-1-M"),
                mock.patch.object(EX, "poly_fetch_orderbook", return_value=book),
                mock.patch.object(EX, "kalshi_fetch_orderbook_depth", return_value=book),
                mock.patch.object(EX, "poly_extract_asks", return_value=[(0.40, 1000.0)]),
                mock.patch.object(EX, "kalshi_extract_asks", return_value=[(0.50, 1000.0)]),
                mock.patch.object(EX, "kalshi_get_best_prices", return_value={"no_best_ask": 0.50, "yes_best_ask": 0.5}),
            ]
            for p in patches:
                p.start()
            try:
                with NetRecorder() as net:
                    EL._execution_cycle()
            finally:
                for p in reversed(patches):
                    p.stop()
            with open(journal) as fh:
                rows = [json.loads(x) for x in fh]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["polymarket_trading_available"])
        self.assertIn("US/CA", rows[0]["polymarket_status"])
        self.assertTrue(all(m == "GET" for m, _ in net.calls), net.calls)

    def test_worker_skips_startup_reconciliation(self):
        with mock.patch.dict(os.environ, SHADOW, clear=True), \
             mock.patch.object(EL, "run_startup_checks", forbidden("run_startup_checks")), \
             mock.patch.object(EL, "_loop_running", False):
            EL._execution_loop_worker()

    def test_funder_startup_checks_validate_without_reconciling(self):
        with mock.patch.dict(os.environ, SHADOW, clear=True), \
             mock.patch.object(arb_funder, "reconcile_funding_intents", forbidden("reconcile")), \
             mock.patch.object(arb_funder.AL, "validate_funding_config", return_value=["x"]) as v:
            self.assertEqual(arb_funder.run_startup_checks(), ["x"])
        v.assert_called_once()

    def test_live_mode_refuses_when_polymarket_unavailable(self):
        book = object()
        funder = forbidden("fund_both_legs_for_trade")
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood", "ARB_EXECUTION_MODE": "live"}, clear=True), \
             mock.patch.object(VE, "polymarket_trading_status", return_value=BLOCKED), \
             mock.patch.object(EX, "kalshi_resolve_market_ticker", return_value="KX-1-M"), \
             mock.patch.object(EX, "poly_fetch_orderbook", return_value=book), \
             mock.patch.object(EX, "kalshi_fetch_orderbook_depth", return_value=book), \
             mock.patch.object(EX, "poly_extract_asks", return_value=[(0.40, 1000.0)]), \
             mock.patch.object(EX, "kalshi_extract_asks", return_value=[(0.50, 1000.0)]), \
             mock.patch.object(EX, "kalshi_get_best_prices", return_value={"no_best_ask": 0.50, "yes_best_ask": 0.5}), \
             mock.patch.object(arb_funder, "get_platform_spot_balances", return_value=(1000.0, 1000.0)), \
             mock.patch.object(arb_funder, "get_servicer_deployable_usdc", return_value=1000.0), \
             mock.patch.object(arb_funder, "fund_both_legs_for_trade", funder), \
             mock.patch.object(EX, "_place_poly_order", forbidden("poly order")), \
             mock.patch.object(EX, "_place_kalshi_order", forbidden("kalshi order")):
            res = EX.execute_arb(opp(), size_usdc=20.0)
        self.assertTrue(res.error.startswith("polymarket_trading_unavailable"), res.error)
        self.assertTrue(any(res.error.startswith(e) for e in ("polymarket_trading_unavailable",)))


class ChokePoints(unittest.TestCase):
    """Each state-changing function raises ShadowModeViolation before doing anything."""

    TARGETS = [
        (arb_funder, "fund_both_legs_for_trade"), (arb_funder, "run_tend_tick"),
        (arb_funder, "reconcile_funding_intents"), (arb_funder, "_send_erc20_transfer"),
        (arb_funder, "_send_erc20_approve"), (arb_funder, "_call_tend_if_ready"),
        (arb_reporter, "run_reporter_tick"),
        (arb_withdrawals, "withdraw_from_platforms"), (arb_withdrawals, "withdraw_from_polymarket"),
        (arb_withdrawals, "sweep_base_usdc_to_robinhood"), (arb_withdrawals, "reconcile_bridge_transfers"),
        (EX, "_place_poly_order"), (EX, "_place_kalshi_order"), (EX, "_cancel_leg2_order"),
        (EX, "_unwind_poly_leg"), (EX, "_kalshi_unwind_best_effort"), (EX, "log_execution_to_db"),
        (arb_positions_db, "init_arb_tables"), (arb_positions_db, "upsert_position"),
        (arb_positions_db, "settle_position"), (arb_positions_db, "mark_position_resolved"),
        (arb_ledger, "_write_unlocked"), (arb_ctf_redemption, "transport"),
    ]

    def test_all_refuse_in_shadow(self):
        with mock.patch.dict(os.environ, SHADOW, clear=True), NetRecorder() as net:
            for mod, name in self.TARGETS:
                fn = getattr(mod, name)
                args = ["x" for p in inspect.signature(fn).parameters.values()
                        if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
                with self.subTest(fn=name), self.assertRaises(SG.ShadowModeViolation):
                    fn(*args)
            with self.assertRaises(SG.ShadowModeViolation):
                arb_venue_routes.poly_bridge("deposit")
        self.assertEqual(net.calls, [])

    def test_rpc_broadcast_refused_before_any_request(self):
        with mock.patch.dict(os.environ, SHADOW, clear=True), NetRecorder() as net:
            for call in (lambda: arb_funder._rpc("eth_sendRawTransaction", ["0x"]),
                         lambda: arb_reporter._rpc("eth_sendRawTransaction", ["0x"]),
                         lambda: arb_withdrawals._rpc_call("http://x", "eth_sendRawTransaction", ["0x"])):
                self.assertRaises(SG.ShadowModeViolation, call)
        self.assertEqual(net.calls, [])


class Eligibility(unittest.TestCase):
    def setUp(self):
        VE.clear_cache()

    def _sess(self, status=200, body=None, exc=None):
        class S:
            def get(self, url, timeout=None):
                if exc:
                    raise exc
                return SimpleNamespace(status_code=status, json=lambda: body)
        return S()

    def test_fails_closed(self):
        self.assertTrue(VE.polymarket_trading_status(self._sess(body={"blocked": False, "country": "ES"}))["available"])
        for s in (self._sess(body={"blocked": True, "country": "US", "region": "CA"}), self._sess(status=403, body={}),
                  self._sess(body={"ip": "1.2.3.4"}), self._sess(exc=requests.Timeout("t"))):
            self.assertFalse(VE.polymarket_trading_status(s)["available"])

    def test_direct_session_ignores_proxy_env(self):
        self.assertFalse(VE._direct_session().trust_env)


class PublicDataRoute(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}, clear=True):
            self.assertEqual(http_client.public_data_route(), "direct")
        with mock.patch.dict(os.environ, {"ARB_EXECUTION_MODE": "shadow"}, clear=True):
            self.assertEqual(http_client.public_data_route(), "direct")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(http_client.public_data_route(), "proxy")

    def test_malformed_proxy_is_sanitized(self):
        for url in ("http://user:hunter22@host.example:notaport", "http://user:hunter22@:8080", "htp:/garbage"):
            ok, msg = http_client.describe_proxy(url)
            self.assertFalse(ok, url)
            self.assertNotIn("hunter22", msg)
            self.assertNotIn("user", msg)
        self.assertEqual(http_client.describe_proxy("socks5h://u:p@1.2.3.4:1080"), (True, "socks5h://1.2.3.4:1080"))

    def _install(self, env):
        from requests.adapters import HTTPAdapter
        orig = HTTPAdapter.send
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(clob_proxy, "_PATCHED", False), \
             mock.patch("builtins.print") as pr:
            clob_proxy.install_proxy_patch()
        patched = HTTPAdapter.send is not orig
        HTTPAdapter.send = orig
        return patched, " ".join(str(c) for c in pr.call_args_list)

    def test_clob_proxy_not_applied_for_direct_route_or_malformed_url(self):
        patched, _ = self._install({"ARB_FUNDING_CHAIN": "robinhood", "PROXY_URL": "http://u:pw123456@es.proxy:3128"})
        self.assertFalse(patched)
        patched, out = self._install({"ARB_PUBLIC_DATA_ROUTE": "proxy", "PROXY_URL": "http://u:pw123456@es.proxy:bad"})
        self.assertFalse(patched)
        self.assertNotIn("pw123456", out)
        self.assertIn("malformed proxy URL", out)

    def test_gamma_session_direct_in_pilot(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}, clear=True), \
             mock.patch.object(http_client, "PROXY_URL", "http://u:p@es.proxy:3128"), \
             mock.patch.object(http_client, "_session", None):
            s = http_client._get_session()
        self.assertEqual(s.proxies, {})
        self.assertFalse(s.trust_env)


class ConnectivityCheck(unittest.TestCase):
    def test_malformed_proxy_never_crashes_or_leaks(self):
        bad = "http://user:s3cretpw@proxy.example:port"
        with mock.patch.dict(os.environ, {"PROXY_URL": bad}, clear=True):
            self.assertIn("malformed proxy URL", V.redact(bad))
            self.assertNotIn("s3cretpw", json.dumps(V.proxy_env()))
            self.assertFalse(V.probe_proxy(bad)["ok"])
            self.assertNotIn("s3cretpw", V.scrub(f"ValueError: cannot parse {bad}"))
            self.assertNotIn("s3cretpw", V.scrub("auth failed for user:s3cretpw"))
            r = V.engine_routing(V.proxy_env())
        self.assertTrue(r["gamma (http_client session)"].startswith("direct"))

    def test_access_categories_kept_apart(self):
        geo = {"ok": False, "detail": "blocked=True location=US/CA"}
        acc = V.access_summary(geo, {"ok": True}, {"ok": True}, {"ok": True}, {"ok": True}, {"ok": True, "status": 200})
        self.assertTrue(acc["polymarket"]["public_data"])
        self.assertTrue(acc["polymarket"]["authenticated_balance"])
        self.assertFalse(acc["polymarket"]["trade_execution"]["authorized"])
        self.assertIn("refused", acc["polymarket"]["trade_execution"]["live_execution"])
        self.assertTrue(acc["kalshi"]["trade_execution"]["authorized"])
        self.assertIn("inferred", acc["kalshi"]["trade_execution"]["basis"])

    def test_env_file_overrides_and_drops_inherited(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("DATABASE_URL=postgresql://pilot/db\nPOLY_API_KEY=pilot\n")
        try:
            with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://prod/db", "KALSHI_API_KEY_ID": "prod",
                                              "PATH": os.environ.get("PATH", "")}, clear=True):
                dropped = V.load_env_file(fh.name)
                self.assertEqual(os.environ["DATABASE_URL"], "postgresql://pilot/db")
                self.assertNotIn("KALSHI_API_KEY_ID", os.environ)
                self.assertIn("PATH", os.environ)
            self.assertEqual(dropped, ["KALSHI_API_KEY_ID"])
        finally:
            os.unlink(fh.name)


class PilotIsolation(unittest.TestCase):
    def env(self, d, **kw):
        e = {"ARB_FUNDING_CHAIN": "robinhood", "ARB_VAULT_CHAIN_ID": "4663", "ARB_PILOT_DATA_DIR": d,
             "DATABASE_URL": "postgresql://pilot:x@db.internal:5432/pilot"}
        e.update(kw)
        return e

    def test_same_database_refused_across_spellings(self):
        with tempfile.TemporaryDirectory() as d:
            for prod, pilot in (("postgresql://a:b@localhost/pmfi", "postgres://c:d@127.0.0.1:5432/pmfi"),
                                ("postgresql://u@db.internal:5432/pmfi", "postgresql://v:w@DB.INTERNAL/pmfi")):
                probs = R.prepare_env(self.env(d, DATABASE_URL=pilot), production={"DATABASE_URL": prod})
                self.assertTrue(any("same database" in p for p in probs), (prod, pilot))
            probs = R.prepare_env(self.env(d), inherited={"DATABASE_URL": "postgresql://x@db.internal/pilot"})
            self.assertTrue(any("inherited" in p for p in probs))
            self.assertEqual(R.prepare_env(self.env(d), production={"DATABASE_URL": "postgresql://db.internal/pmfi"}), [])

    def test_shared_credentials_refused(self):
        with tempfile.TemporaryDirectory() as d:
            probs = R.prepare_env(self.env(d, POLY_PRIVATE_KEY="0xAB", ARB_SERVICER_WALLET="0xS"),
                                  production={"POLY_PRIVATE_KEY": "0xab", "ARB_SERVICER_WALLET": "0xT"})
        self.assertEqual([p for p in probs if "shared" in p],
                         ["POLY_PRIVATE_KEY is shared with the production engine (the pilot needs its own)"])

    def test_ledger_paths_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as prod_dir:
            prod_ledger = os.path.join(prod_dir, "intents.json")
            open(prod_ledger, "w").close()
            link = os.path.join(d, "link.json")
            os.symlink(prod_ledger, link)
            probs = R.prepare_env(self.env(d, ARB_INTENT_LEDGER_PATH=link),
                                  production={"ARB_INTENT_LEDGER_PATH": prod_ledger})
            self.assertTrue(any("same file as a production ledger" in p for p in probs))
            probs = R.prepare_env(self.env(d, ARB_BRIDGE_LEDGER_PATH="/opt/polymarket-bot/data/b.json"))
            self.assertTrue(any("ARB_BRIDGE_LEDGER_PATH" in p for p in probs))
        self.assertTrue(any("outside" in p for p in R.prepare_env(self.env("/opt/polymarket-bot/pilot"))))
        self.assertTrue(any("outside" in p for p in R.prepare_env(self.env("/root/PolyNotifyBot/pilot"))))

    def test_isolate_env_drops_inherited_engine_vars(self):
        env = {"DATABASE_URL": "prod", "POLY_PRIVATE_KEY": "prod", "PROXY_URL": "p", "HOME": "/root",
               "ARB_SHADOW_JOURNAL_PATH": "/root/PolyNotifyBot/data/x"}
        dropped = R.isolate_env({"DATABASE_URL": "pilot", "ARB_FUNDING_CHAIN": "robinhood"}, env)
        self.assertEqual(dropped, ["ARB_SHADOW_JOURNAL_PATH", "POLY_PRIVATE_KEY", "PROXY_URL"])
        self.assertEqual(env, {"DATABASE_URL": "pilot", "HOME": "/root", "ARB_FUNDING_CHAIN": "robinhood"})

    def test_env_file_is_required(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            R.main(["--check"])


class _Resp:
    def __init__(self, status, body, headers):
        self.status_code, self._body, self.headers = status, body, headers

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class PredictionComQuota(unittest.TestCase):
    def setUp(self):
        P.QUOTA.update(next_allowed=0.0, why="", body=None, body_at=0.0)

    tearDown = setUp

    def test_delay_spreads_monthly_quota(self):
        now = 1_000_000.0
        month_left = P._seconds_to_month_end(now)
        with mock.patch.dict(os.environ, {"PREDICTION_COM_MIN_POLL_SECONDS": "30", "PREDICTION_COM_QUOTA_RESERVE": "50"}):
            d, why = P.next_request_delay({"X-RateLimit-Remaining-Arb-Month": "150"}, now)
            self.assertAlmostEqual(d, max(30.0, month_left / 100))
            d, _ = P.next_request_delay({"X-RateLimit-Remaining-Month": "40"}, now)
            self.assertEqual(d, month_left)            # below the reserve: wait for the month to reset
            self.assertEqual(P.next_request_delay({"X-RateLimit-Remaining-Month": "10000000"}, now)[0], 30.0)

    def test_paced_calls_reuse_last_body_without_a_request(self):
        calls = []

        class S:
            def get(self, *a, **k):
                calls.append(1)
                return _Resp(200, {"as_of": time.time(), "opportunities": []}, {"X-RateLimit-Remaining-Month": "1000000"})

        with mock.patch.dict(os.environ, {"PREDICTION_COM_API_KEY": "k"}):
            first = P.fetch_arb_raw(S())
            second = P.fetch_arb_raw(S())
        self.assertEqual(len(calls), 1)
        self.assertIs(first, second)

    def test_429_honours_retry_after(self):
        class S:
            def get(self, *a, **k):
                return _Resp(429, {}, {"Retry-After": "120"})

        with mock.patch.dict(os.environ, {"PREDICTION_COM_API_KEY": "k"}):
            self.assertEqual(P.fetch_arb_raw(S()), {})
        self.assertGreater(P.QUOTA["next_allowed"] - time.time(), 115)
        self.assertEqual(P._retry_after({}, 0), 60.0)

    def test_stale_or_undated_body_dropped_before_lookups(self):
        with mock.patch.object(P, "normalize", forbidden("normalize")), \
             mock.patch.dict(os.environ, {"ARB_STALE_QUOTE_SECONDS": "120"}):
            for body in ({"opportunities": [{}]},
                         {"as_of": time.time() - 100, "delay_seconds": 60, "opportunities": [{}]}):
                with mock.patch.object(P, "fetch_arb_raw", lambda s=None, b=body: b):
                    self.assertEqual(P.fetch_opportunities(), [])

    def test_oddpool_used_without_prediction_com_key(self):
        for env in ({"ARB_OPPORTUNITY_FEED": "prediction_com"},
                    {"ARB_OPPORTUNITY_FEED": "compare", "ARB_FEED_COMPARE_PRIMARY": "prediction_com"}):
            fetched = []
            with mock.patch.dict(os.environ, {**env, "PREDICTION_COM_API_KEY": ""}), \
                 mock.patch.object(FEED, "_fetch", lambda n: fetched.append(n) or [n]):
                self.assertEqual(FEED.fetch_opportunities(), ["oddpool"])
            self.assertEqual(fetched, ["oddpool"])


if __name__ == "__main__":
    unittest.main()
