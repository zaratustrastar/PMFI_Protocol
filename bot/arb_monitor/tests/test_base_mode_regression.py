"""Regression: with ARB_FUNDING_CHAIN unset the legacy Base V2 path is unchanged."""

import os
import unittest
from unittest import mock

import _fakes as F
from arb_monitor.core import arb_funder, arb_withdrawals, arb_nav


class BaseModeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.net = F.FakeNet(arb_funder.BASE_RPC)
        for p in self.net.patches() + [
            mock.patch.dict(os.environ, F.base_env(ARB_OPINION_ENABLED="true"), clear=True),
            mock.patch("time.sleep"),
            mock.patch.object(arb_funder, "ARB_DEPOSIT_WAIT_SECS", 0),
            mock.patch.object(arb_funder, "POLY_BASE_DEPOSIT_ADDR", F.POLY_BASE_ADDR),
            mock.patch.object(arb_funder, "KALSHI_BASE_DEPOSIT_ADDR", F.KALSHI_ADDR),
        ]:
            p.start()
            self.addCleanup(p.stop)
        F.temp_ledgers(self)
        self.net.set_eth(arb_funder.BASE_RPC, F.SERVICER, 1.0)
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 100)

    def test_fund_both_legs_sends_base_usdc_to_both_venues(self):
        seq = {"polymarket": [0.0, 10.0], "kalshi": [0.0, 5.0]}
        def bal(venue, raise_on_error=False):
            v = seq[venue]
            return v.pop(0) if len(v) > 1 else v[0]
        with mock.patch.object(arb_funder, "_get_platform_balance", side_effect=bal):
            ok, err = arb_funder.fund_both_legs_for_trade(10.0, "kalshi", 5.0, wait_timeout=5, poll_interval=0)
        self.assertTrue(ok, err)
        self.assertEqual([(t["chain_id"], t["to"], t["data"]) for t in self.net.sent], [
            (8453, F.USDC_BASE.lower(), F.erc20_transfer_data(F.POLY_BASE_ADDR, 10_000_000)),
            (8453, F.USDC_BASE.lower(), F.erc20_transfer_data(F.KALSHI_ADDR, 5_000_000)),
        ])
        self.assertEqual([t["nonce"] for t in self.net.sent], [0, 1])
        self.assertTrue(all(rpc == arb_funder.BASE_RPC for rpc, _, _ in self.net.rpc_calls))
        self.assertEqual(self.net.http_calls, [])
        self.assertEqual(arb_withdrawals._load_ledger(arb_withdrawals.BRIDGE_LEDGER_PATH), [])

    def test_tend_and_servicer_cash_on_base(self):
        self.assertEqual(arb_funder.get_servicer_cash_usdc(F.SERVICER), 100.0)
        arb_funder._call_tend_if_ready(F.VAULT, F.TEST_KEY, F.SERVICER)
        self.assertEqual(self.net.sent[0]["chain_id"], 8453)

    def test_nav_base_mode_has_no_in_transit_terms(self):
        # A pending recall exists, but legacy NAV never counted it — still must not.
        arb_withdrawals.record_withdrawal_intent("polymarket", 10.0, "0xp", 600)
        with mock.patch("arb_monitor.core.kalshi_auth.kalshi_auth_available", return_value=False), \
             mock.patch.object(arb_nav, "_get_opinion_cash", return_value=4.0):
            nav = arb_nav.compute_nav()
        self.assertEqual(nav["total_assets_usdc"], 104.0)    # servicer 100 + opinion 4
        self.assertEqual(nav["servicer_on_base"], 100.0)
        self.assertNotIn("servicer_breakdown", nav)

    def test_legacy_ledger_rows_compare_against_servicer_balance(self):
        arb_withdrawals.record_withdrawal_intent("kalshi", 30.0, "k1", 60)
        rows = arb_withdrawals._load_ledger()
        rows[0]["eta_at"] -= 120
        arb_withdrawals._save_ledger(rows)
        self.assertNotIn("dest", rows[0])
        self.assertEqual(arb_withdrawals.mark_withdrawals_arrived(30.0), 30.0)


if __name__ == "__main__":
    unittest.main()
