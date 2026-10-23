"""Integration tests for the ARB_FUNDING_CHAIN=robinhood funding / withdrawal / NAV path.

Offline: RPC and HTTP are faked (see _fakes.py); signing uses a throwaway key.
Run from the repo root:  python3 -m unittest discover -s bot/arb_monitor/tests -v
"""

import os
import time
import unittest
from unittest import mock

import _fakes as F
from arb_monitor.core import arb_funder, arb_withdrawals, arb_nav


class _Base(unittest.TestCase):
    env = None

    def setUp(self):
        self.net = F.FakeNet(arb_funder.BASE_RPC)
        for p in self.net.patches() + [
            # Bridge contracts must be allowlisted (empty ARB_BRIDGE_ALLOWLIST refuses every bridge tx).
            mock.patch.dict(os.environ, {**self.env(), "ARB_BRIDGE_ALLOWLIST":
                                         f"4663:{F.SPOKE},4663:{F.RELAY_DEPOSITORY},8453:{F.SPOKE}"}, clear=True),
            mock.patch("time.sleep"),
            mock.patch.object(arb_funder, "ARB_DEPOSIT_WAIT_SECS", 0),
            mock.patch.object(arb_funder, "POLY_BASE_DEPOSIT_ADDR", F.POLY_BASE_ADDR),
            mock.patch.object(arb_funder, "KALSHI_BASE_DEPOSIT_ADDR", F.KALSHI_ADDR),
        ]:
            p.start()
            self.addCleanup(p.stop)
        F.temp_ledgers(self)
        self.net.set_eth(F.RH_RPC, F.SERVICER, 1.0)
        self.net.set_eth(arb_funder.BASE_RPC, F.SERVICER, 1.0)
        self.platform = {"polymarket": [0.0], "kalshi": [0.0], "opinion": [0.0]}

    def script_platform(self, venue, *values):
        """Successive _get_platform_balance(venue) results (last value repeats)."""
        self.platform[venue] = list(values)

    def _platform_balance(self, venue, raise_on_error=False):
        vals = self.platform[venue]
        return vals.pop(0) if len(vals) > 1 else vals[0]

    def patch_platform(self):
        p = mock.patch.object(arb_funder, "_get_platform_balance", side_effect=self._platform_balance)
        p.start()
        self.addCleanup(p.stop)

    def bridge_rows(self):
        return arb_withdrawals._load_ledger(arb_withdrawals.BRIDGE_LEDGER_PATH)


class RobinhoodFundingTests(_Base):
    env = staticmethod(F.rh_env)

    def test_polymarket_deposit_is_usdg_transfer_on_4663(self):
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.patch_platform()
        self.script_platform("polymarket", 0.0, 0.0, 12.5)   # before, then arrival
        self.script_platform("kalshi", 50.0)                 # already funded

        ok, err = arb_funder.fund_both_legs_for_trade(12.5, "kalshi", 10.0, wait_timeout=5, poll_interval=0)

        self.assertTrue(ok, err)
        self.assertEqual(len(self.net.sent), 1)
        tx = self.net.sent[0]
        self.assertEqual(tx["chain_id"], 4663)
        self.assertEqual(tx["rpc"], F.RH_RPC)
        self.assertEqual(tx["to"], F.USDG.lower())
        self.assertEqual(tx["value"], 0)
        self.assertEqual(tx["data"], F.erc20_transfer_data(F.POLY_RH_ADDR, 12_500_000))
        # servicer cash was read as USDG on 4663, never USDC on Base
        bal_reads = [(rpc, p[0]["to"].lower()) for rpc, m, p in self.net.rpc_calls
                     if m == "eth_call" and p[0]["data"].startswith("0x70a08231")]
        self.assertIn((F.RH_RPC, F.USDG.lower()), bal_reads)
        self.assertNotIn((arb_funder.BASE_RPC, F.USDC_BASE.lower()), bal_reads)
        # in-transit row recorded on send and cleared on arrival
        rows = self.bridge_rows()
        self.assertEqual([(r["direction"], r["status"]) for r in rows], [("rh_to_polymarket", "completed")])

    def _across_routes(self, fee_raw=50_000):
        def quote(params):
            amt = int(params["amount"])
            self.assertEqual(params["tradeType"], "exactOutput")
            self.assertEqual(params["originChainId"], 4663)
            self.assertEqual(params["destinationChainId"], 8453)
            self.assertEqual(params["inputToken"], F.USDG)
            self.assertEqual(params["outputToken"], F.USDC_BASE)
            self.assertEqual(params["recipient"], F.SERVICER)
            return {
                "approvalTxns": [{"to": F.USDG, "data": F.approve_data(F.SPOKE, amt + fee_raw)}],
                "swapTx": {"to": F.SPOKE, "data": "0xdeadbeef", "value": "0"},
                "maxInputAmount": str(amt + fee_raw),
                "expectedOutputAmount": str(amt),
            }
        self.net.get_routes["/swap/approval"] = quote

    def _credit_base_on_bridge(self, to_addr):
        def on_send(tx):
            if tx["to"] == to_addr.lower():
                cur = self.net.token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER)
                self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, cur + 20)
        self.net.on_send = on_send

    def test_kalshi_bridges_usdg_then_transfers_base_usdc(self):
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.patch_platform()
        self.script_platform("polymarket", 50.0)
        self.script_platform("kalshi", 0.0, 0.0, 20.0)
        self._across_routes()
        self._credit_base_on_bridge(F.SPOKE)

        ok, err = arb_funder.fund_both_legs_for_trade(10.0, "kalshi", 20.0, wait_timeout=5, poll_interval=0)

        self.assertTrue(ok, err)
        got = [(t["chain_id"], t["to"], t["data"][:10]) for t in self.net.sent]
        self.assertEqual(got, [
            (4663, F.USDG.lower(), "0x095ea7b3"),       # approve Across spoke
            (4663, F.SPOKE.lower(), "0xdeadbeef"),      # Across deposit
            (8453, F.USDC_BASE.lower(), "0xa9059cbb"),  # existing Base USDC → Kalshi
        ])
        self.assertEqual(self.net.sent[2]["data"], F.erc20_transfer_data(F.KALSHI_ADDR, 20_000_000))
        rows = self.bridge_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["direction"], rows[0]["provider"], rows[0]["status"]),
                         ("rh_to_base", "across", "completed"))
        self.assertAlmostEqual(rows[0]["amount_usdc"], 20.05)

    def test_relay_fallback_when_across_quote_too_expensive(self):
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.patch_platform()
        self.script_platform("polymarket", 50.0)
        self.script_platform("kalshi", 0.0, 0.0, 20.0)
        self._across_routes(fee_raw=1_000_000)   # 5% > 50 bps cap → rejected

        def relay_quote(body):
            self.assertEqual(body["tradeType"], "EXACT_OUTPUT")
            self.assertEqual(body["originChainId"], 4663)
            return {
                "steps": [
                    {"id": "approve", "requestId": "0xreq1", "items": [
                        {"data": {"to": F.USDG, "data": F.approve_data(F.RELAY_DEPOSITORY, 20_040_000), "value": "0"}}]},
                    {"id": "deposit", "requestId": "0xreq1", "items": [
                        {"data": {"to": F.RELAY_DEPOSITORY, "data": "0xfeedface", "value": "0"}}]},
                ],
                "details": {"currencyIn": {"amount": "20040000"}},
            }
        self.net.post_routes["/quote"] = relay_quote
        self._credit_base_on_bridge(F.RELAY_DEPOSITORY)

        ok, err = arb_funder.fund_both_legs_for_trade(10.0, "kalshi", 20.0, wait_timeout=5, poll_interval=0)

        self.assertTrue(ok, err)
        self.assertEqual([t["to"] for t in self.net.sent],
                         [F.USDG.lower(), F.RELAY_DEPOSITORY.lower(), F.USDC_BASE.lower()])
        self.assertEqual(self.bridge_rows()[0]["provider"], "relay")
        self.assertEqual(self.bridge_rows()[0]["reference"], "0xreq1")

    def test_kalshi_bridge_timeout_leaves_in_transit_and_no_kalshi_transfer(self):
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.patch_platform()
        self.script_platform("polymarket", 50.0)
        self.script_platform("kalshi", 0.0)
        self._across_routes()          # bridge never credits Base

        ok, err = arb_funder.fund_both_legs_for_trade(10.0, "kalshi", 20.0, wait_timeout=0.05, poll_interval=0)

        self.assertFalse(ok)
        self.assertIn("in-transit", err)
        self.assertTrue(all(t["chain_id"] == 4663 for t in self.net.sent))
        self.assertEqual(self.bridge_rows()[0]["status"], "pending")
        self.assertAlmostEqual(arb_withdrawals.get_inflight_bridge_usdc(), 20.05)

    def test_tend_targets_v3_vault_on_4663(self):
        arb_funder._call_tend_if_ready(F.VAULT, F.TEST_KEY, F.SERVICER)
        self.assertEqual(len(self.net.sent), 1)
        self.assertEqual(self.net.sent[0]["chain_id"], 4663)
        self.assertEqual(self.net.sent[0]["to"], F.VAULT.lower())
        self.assertTrue(all(rpc == F.RH_RPC for rpc, _, _ in self.net.rpc_calls))


class RobinhoodWithdrawalLedgerTests(_Base):
    env = staticmethod(F.rh_env)

    def _age_rows(self, seconds):
        rows = arb_withdrawals._load_ledger()
        for r in rows:
            r["created_at"] -= seconds
            r["eta_at"] -= seconds
        arb_withdrawals._save_ledger(rows)

    def test_arrival_detected_against_usdg_on_4663(self):
        arb_withdrawals.record_withdrawal_intent("polymarket", 50.0, "0xpoly", 60, dest="rh_usdg")
        self._age_rows(120)   # ETA passed
        # 50 USDC sits on Base but only 30 USDG on 4663 → must NOT count as arrived
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 50)
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 30)
        with mock.patch("arb_monitor.core.arb_reporter._read_vault_state",
                        return_value={"ok": True, "official_pps": 10**6, "idle_balance_usdc": 0.0,
                                      "pending_redeem_shares": 0}), \
             mock.patch.object(arb_nav, "_get_settled_pnl", return_value=0.0), \
             mock.patch.object(arb_nav, "_get_servicer_balances", return_value=(0.0, 0.0, 0.0)):
            from arb_monitor.core.arb_liquidity import compute_liquidity_state
            st = compute_liquidity_state(F.VAULT, F.SERVICER)
            self.assertEqual(st.free_cash, 30.0)
            self.assertEqual(st.pending_withdrawals, 50.0)
            self.assertEqual(arb_withdrawals._load_ledger()[0]["status"], "pending")

            self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 80)   # USDG lands on 4663
            st = compute_liquidity_state(F.VAULT, F.SERVICER)
            self.assertEqual(st.pending_withdrawals, 0.0)
            self.assertEqual(arb_withdrawals._load_ledger()[0]["status"], "completed")

    def test_polymarket_recall_routes_to_usdg_on_4663(self):
        """B9: robinhood recalls use Polymarket's own bridge (pUSD -> USDG on 4663), exact raw amount."""
        import json
        import subprocess
        out = '{"txHash": "0x' + "ab" * 32 + '", "bridgeAddress": "0x' + "22" * 20 + '"}\n'
        with mock.patch.object(arb_withdrawals, "_poly_get_balance", return_value=100.0), \
             mock.patch.dict(os.environ, {"POLY_PROXY_ADDRESS": F.POLY_BASE_ADDR, "ARB_POLY_WITHDRAW_ROUTE": "polymarket_bridge"}), \
             mock.patch.object(subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=out, stderr="")) as run:
            total = arb_withdrawals.withdraw_from_platforms(20.0, 100.0, 0.0, 0.0, F.SERVICER)
        self.assertEqual(total, 20.0)
        self.assertTrue(run.call_args.args[0][-1].endswith("trading_bot/poly_bridge_withdraw.mjs"))
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"action": "withdraw", "amountRaw": "20000000"})
        row = arb_withdrawals._load_ledger()[0]
        self.assertEqual((row["platform"], row["dest"], row["status"]), ("polymarket", "rh_usdg", "pending"))
        self.assertEqual(row["poly_bridge_address"], "0x" + "22" * 20)

    def test_kalshi_recall_on_base_is_swept_back_to_usdg(self):
        arb_withdrawals.record_withdrawal_intent("kalshi", 25.0, "kw-1", 3600, dest="base_usdc")
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 25)

        def quote(params):
            self.assertEqual(params["tradeType"], "exactInput")
            self.assertEqual((params["originChainId"], params["destinationChainId"]), (8453, 4663))
            self.assertEqual((params["inputToken"], params["outputToken"]), (F.USDC_BASE, F.USDG))
            return {"approvalTxns": [{"to": F.USDC_BASE, "data": F.approve_data(F.SPOKE, 25_000_000)}],
                    "swapTx": {"to": F.SPOKE, "data": "0xdeadbeef"},
                    "minOutputAmount": "24990000"}
        self.net.get_routes["/swap/approval"] = quote

        swept = arb_withdrawals.sweep_base_usdc_to_robinhood(F.SERVICER, F.TEST_KEY)

        self.assertEqual(swept, 25.0)
        self.assertEqual([t["chain_id"] for t in self.net.sent], [8453, 8453])
        self.assertEqual(arb_withdrawals._load_ledger()[0]["status"], "completed")   # recall → bridge
        row = self.bridge_rows()[0]
        self.assertEqual((row["direction"], row["status"], row["amount_usdc"]), ("base_to_rh", "pending", 25.0))


class RobinhoodNavTests(_Base):
    env = staticmethod(F.rh_env)

    def setUp(self):
        super().setUp()
        p = mock.patch("arb_monitor.core.kalshi_auth.kalshi_auth_available", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 5)
        arb_withdrawals.record_withdrawal_intent("polymarket", 10.0, "0xp", 3600, dest="rh_usdg")
        arb_withdrawals.record_withdrawal_intent("kalshi", 8.0, "kw", 3600, dest="base_usdc")
        arb_withdrawals.record_bridge_transfer("rh_to_base", "across", "0xdep", 7.0, 4663)
        self.net.get_routes["/deposit/status"] = lambda params: {"status": "pending"}

    def test_nav_includes_in_transit_and_excludes_opinion(self):
        with mock.patch.object(arb_nav, "_get_opinion_cash", return_value=999.0) as op:
            nav = arb_nav.compute_nav()
        op.assert_not_called()
        self.assertEqual(nav["opinion_cash"], 0.0)
        b = nav["servicer_breakdown"]
        self.assertEqual(b["servicer_usdg_rh"], 100.0)
        self.assertEqual(b["servicer_usdc_base"], 5.0)
        self.assertEqual(b["pending_withdrawals"], 13.0)   # 10 + (8 − 5 already visible on Base)
        self.assertEqual(b["bridges_in_transit"], 7.0)
        self.assertEqual(nav["total_assets_usdc"], 125.0)   # 100 + 5 + 13 + 7

    def test_bridge_fill_clears_in_transit(self):
        self.net.get_routes["/deposit/status"] = lambda params: {"status": "filled"}
        nav = arb_nav.compute_nav()
        self.assertEqual(nav["servicer_breakdown"]["bridges_in_transit"], 0.0)
        self.assertEqual(nav["total_assets_usdc"], 118.0)

    def test_peg_haircut_applies_only_beyond_threshold(self):
        with mock.patch.dict(os.environ, {"ARB_PEG_MAX_DEVIATION_BPS": "50"}):
            self.assertEqual(arb_nav.compute_nav(peg_price_source=lambda: 0.998)["total_assets_usdc"], 125.0)
            self.assertEqual(arb_nav.compute_nav(peg_price_source=lambda: 0.98)["total_assets_usdc"], 123.0)

    def test_opinion_counted_when_enabled(self):
        with mock.patch.dict(os.environ, {"ARB_OPINION_ENABLED": "true"}), \
             mock.patch.object(arb_nav, "_get_opinion_cash", return_value=4.0):
            self.assertEqual(arb_nav.compute_nav()["total_assets_usdc"], 129.0)


class OpinionDisabledTests(_Base):
    # base mode keeps Opinion on by default (legacy V2 unchanged); V1 operators disable it explicitly
    env = staticmethod(lambda **kw: F.base_env(ARB_OPINION_ENABLED="false", **kw))

    def test_opinion_default_by_mode(self):
        from arb_monitor import config as cfg
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "base"}, clear=True):
            self.assertTrue(cfg.opinion_enabled())
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}, clear=True):
            self.assertFalse(cfg.opinion_enabled())

    def test_opinion_funding_blocked(self):
        ok, err = arb_funder.fund_both_legs_for_trade(5.0, "opinion", 5.0)
        self.assertFalse(ok)
        self.assertIn("opinion_disabled", err)
        self.assertEqual(self.net.rpc_calls, [])
        self.assertEqual(self.net.sent, [])

    def test_opinion_balance_and_withdrawal_skipped(self):
        with mock.patch("arb_monitor.adapters.opinion_clob.get_balance", return_value=50.0) as gb:
            self.assertEqual(arb_funder._get_platform_balance("opinion"), 0.0)
        gb.assert_not_called()
        with mock.patch.object(arb_withdrawals, "withdraw_from_opinion") as wo:
            self.assertEqual(arb_withdrawals.withdraw_from_platforms(10.0, 0.0, 0.0, 50.0, F.SERVICER), 0.0)
        wo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
