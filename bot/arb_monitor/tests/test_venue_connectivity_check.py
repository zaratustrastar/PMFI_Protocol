"""The VPS connectivity check is read-only and never prints credentials. Offline."""
import os
import re
import unittest
from unittest import mock

import _fakes  # noqa: F401
from arb_monitor.scripts import venue_connectivity_check as V

SRC = open(V.__file__).read()


class ReadOnly(unittest.TestCase):
    def test_only_gets_and_two_rpc_reads(self):
        posts = re.findall(r"\.post\(", SRC)
        self.assertEqual(len(posts), 2, "only eth_chainId and one eth_call")
        for verb in (".delete(", ".put(", "post_order", "create_order", "cancel", "withdraw", "sendRawTransaction"):
            self.assertNotIn(verb, SRC.replace("no order, cancel, withdrawal", ""))


class Secrets(unittest.TestCase):
    def test_redact_and_scrub(self):
        self.assertEqual(V.redact("http://user:pa55@proxy.example:8080"), "http://proxy.example:8080")
        self.assertEqual(V.redact("socks5://u:p@1.2.3.4:1080"), "socks5://1.2.3.4:1080")
        msg = V.scrub("ProxyError('Cannot connect to proxy http://user:pa55@proxy.example:8080')")
        self.assertNotIn("pa55", msg)

    def test_proxy_env_is_redacted(self):
        with mock.patch.dict(os.environ, {"PROXY_URL": "http://u:secret@es.proxy:3128", "LD_PRELOAD": ""}, clear=False):
            env = V.proxy_env()
        self.assertEqual(env["PROXY_URL"], "http://es.proxy:3128")
        self.assertNotIn("secret", str(env))


class Routing(unittest.TestCase):
    def test_kalshi_trading_stays_direct_with_proxy_url(self):
        r = V.engine_routing({"PROXY_URL": "http://es.proxy:3128"})
        self.assertEqual(r["kalshi trading + market data (bypass_proxy / trust_env=False)"], "direct")
        self.assertEqual(r["gamma (http_client session)"], "proxy http://es.proxy:3128")
        self.assertEqual(r["clob books/prices (requests, clob_proxy patch)"], "proxy http://es.proxy:3128")
        self.assertEqual(r["clob orders/balances (py-clob-client-v2 httpx)"], "proxy http://es.proxy:3128")
        self.assertEqual(r["kalshi balance reads (requests default, trust_env)"], "direct")

    def test_sdk_direct_when_disabled(self):
        with mock.patch.dict(os.environ, {"POLY_CLOB_PROXY_SDK": "false"}):
            r = V.engine_routing({"POLY_CLOB_PROXY_URL": "http://pm.proxy:1"})
        self.assertEqual(r["clob orders/balances (py-clob-client-v2 httpx)"], "direct")

    def test_geoblock_judge(self):
        class R:
            status_code = 200
        self.assertEqual(V.geoblock_judge(R(), {"blocked": False, "country": "ES", "region": "MD"}), (True, "blocked=False location=ES/MD"))
        self.assertFalse(V.geoblock_judge(R(), {"blocked": True, "country": "US"})[0])


if __name__ == "__main__":
    unittest.main()
