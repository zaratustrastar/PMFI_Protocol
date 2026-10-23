"""Allowlist / quote-validation / receipt tests for the Robinhood funding path (offline fakes)."""

import os
import unittest
from unittest import mock

import _fakes as F
import _funding_fakes as FF
from arb_monitor.core import arb_funder, arb_withdrawals, arb_allowlist as AL, arb_ledger as L

MAXU = 2 ** 256 - 1


class ValidateBridgeTxsTests(FF.FundingCase):
    def txs(self, spender=F.SPOKE, amount=1_000_000, to=F.SPOKE, value="0", approve_to=F.USDG):
        return [{"to": approve_to, "data": F.approve_data(spender, amount)},
                {"to": to, "data": "0xdeadbeef", "value": value}]

    def test_accepts_exact_approval_to_allowlisted_spender(self):
        AL.validate_bridge_txs(self.txs(), 4663, F.USDG, 1_000_000)

    def test_refuses_wrong_spender(self):
        with self.assertRaisesRegex(AL.AllowlistError, "approve spender"):
            AL.validate_bridge_txs(self.txs(spender="0x" + "9" * 40), 4663, F.USDG, 1_000_000)

    def test_refuses_unlimited_or_excess_approval(self):
        for amt in (MAXU, 1_000_001, 0):
            with self.assertRaisesRegex(AL.AllowlistError, "approve amount"):
                AL.validate_bridge_txs(self.txs(amount=amt), 4663, F.USDG, 1_000_000)

    def test_refuses_wrong_deposit_to(self):
        with self.assertRaisesRegex(AL.AllowlistError, "deposit tx `to`"):
            AL.validate_bridge_txs(self.txs(to="0x" + "8" * 40), 4663, F.USDG, 1_000_000)
        with self.assertRaisesRegex(AL.AllowlistError, "token contract directly"):
            AL.validate_bridge_txs(self.txs(to=F.USDG), 4663, F.USDG, 1_000_000)

    def test_spender_allowlisted_on_another_chain_only_is_refused(self):
        with mock.patch.dict(os.environ, {"ARB_BRIDGE_ALLOWLIST": f"8453:{F.SPOKE}"}):
            with self.assertRaises(AL.AllowlistError):
                AL.validate_bridge_txs(self.txs(), 4663, F.USDG, 1_000_000)

    def test_refuses_wrong_chain_or_token(self):
        with self.assertRaisesRegex(AL.AllowlistError, "not allowlisted"):
            AL.validate_bridge_txs(self.txs(approve_to=F.USDC_BASE), 4663, F.USDC_BASE, 1_000_000)
        with self.assertRaisesRegex(AL.AllowlistError, "not allowlisted"):
            AL.validate_bridge_txs(self.txs(), 1, F.USDG, 1_000_000)

    def test_refuses_nonzero_value_unless_explicitly_allowed(self):
        with self.assertRaisesRegex(AL.AllowlistError, "native value"):
            AL.validate_bridge_txs(self.txs(value="0x1"), 4663, F.USDG, 1_000_000)
        with mock.patch.dict(os.environ, {"ARB_BRIDGE_ALLOW_NATIVE_VALUE": "true"}):
            AL.validate_bridge_txs(self.txs(value="0x1"), 4663, F.USDG, 1_000_000)

    def test_empty_allowlist_refuses_everything(self):
        with mock.patch.dict(os.environ, {"ARB_BRIDGE_ALLOWLIST": ""}):
            with self.assertRaisesRegex(AL.AllowlistError, "empty"):
                AL.validate_bridge_txs(self.txs(), 4663, F.USDG, 1_000_000)

    def test_quoted_recipient_and_destination_must_match(self):
        kw = dict(recipient=F.SERVICER, origin_chain=4663, origin_token=F.USDG,
                  dest_chain=8453, dest_token=F.USDC_BASE)
        AL.validate_route({}, **kw)                                          # absent → skipped
        AL.validate_route({"recipient": F.SERVICER.lower(), "dest_chain": "8453"}, **kw)
        for bad in ({"recipient": "0x" + "7" * 40}, {"dest_chain": 137}, {"dest_token": F.USDG}):
            with self.assertRaises(AL.AllowlistError):
                AL.validate_route(bad, **kw)


class StartupConfigTests(FF.FundingCase):
    def test_valid_config_passes(self):
        self.assertEqual(AL.validate_funding_config(kalshi_deposit_addr=F.KALSHI_ADDR), [])

    def test_missing_declarations_and_bad_addresses_reported(self):
        env = {"POLY_RH_DEPOSIT_CHAIN_ID": "", "POLY_RH_DEPOSIT_TOKEN": "",
               "KALSHI_BASE_DEPOSIT_TOKEN": F.USDG,          # Kalshi is paid in Base USDC, not USDG
               "ARB_BRIDGE_ALLOWLIST": "", "POLY_RH_DEPOSIT_ADDR": F.USDG}
        with mock.patch.dict(os.environ, env):
            errs = " | ".join(AL.validate_funding_config(kalshi_deposit_addr=F.KALSHI_ADDR))
        self.assertIn("POLY_RH_DEPOSIT_CHAIN_ID", errs)
        self.assertIn("kalshi deposit declared token", errs)
        self.assertIn("ARB_BRIDGE_ALLOWLIST is empty", errs)
        self.assertIn("collides", errs)

    def test_base_mode_has_no_startup_requirements(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "base", "ARB_BRIDGE_ALLOWLIST": ""}):
            self.assertEqual(AL.validate_funding_config(), [])
            self.assertEqual(arb_funder.run_startup_checks(), [])

    def test_per_send_venue_check_refuses_wrong_route(self):
        with self.assertRaises(AL.AllowlistError):
            AL.require_venue_deposit("kalshi", 4663, F.USDG, F.KALSHI_ADDR)
        with self.assertRaises(AL.AllowlistError):
            AL.require_venue_deposit("polymarket", 4663, F.USDG, F.SPOKE)      # a bridge contract
        AL.require_venue_deposit("polymarket", 4663, F.USDG, F.POLY_RH_ADDR)


class BridgeQuoteEndToEndTests(FF.FundingCase):
    """A rejected quote must lead to zero transactions (fall back to Relay, which is also refused)."""

    def setUp(self):
        super().setUp()
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.relay_refuses()

    def run_bridge(self):
        with self.assertRaisesRegex(RuntimeError, "all bridge providers failed"):
            arb_funder._bridge_usdg_to_base_usdc(F.TEST_KEY, F.SERVICER, 20.0,
                                                 arb_funder._NonceTracker(F.SERVICER, F.RH_RPC))
        self.assertEqual(self.net.sent, [])
        self.assertEqual(self.bridge_rows(), [])

    def test_unlimited_approve_quote_refused(self):
        self.across_quote(approve_amount=MAXU)
        self.run_bridge()

    def test_wrong_spender_quote_refused(self):
        self.across_quote(approve_spender="0x" + "9" * 40)
        self.run_bridge()

    def test_wrong_deposit_to_quote_refused(self):
        self.across_quote(swap_to="0x" + "8" * 40)
        self.run_bridge()

    def test_nonzero_value_quote_refused(self):
        self.across_quote(value="0x5")
        self.run_bridge()

    def test_wrong_recipient_in_quote_refused(self):
        self.across_quote(extra={"recipient": "0x" + "7" * 40})
        self.run_bridge()

    def test_fee_above_bound_refused(self):
        self.across_quote(fee_raw=500_000)          # 2.5% > 50 bps
        self.run_bridge()

    def test_valid_quote_sends_and_ledgers(self):
        self.across_quote()
        res = arb_funder._bridge_usdg_to_base_usdc(F.TEST_KEY, F.SERVICER, 20.0,
                                                   arb_funder._NonceTracker(F.SERVICER, F.RH_RPC))
        self.assertEqual(res["receipt"], "success")
        self.assertEqual([t["to"] for t in self.net.sent], [F.USDG.lower(), F.SPOKE.lower()])
        row = self.bridge_rows()[0]
        self.assertEqual((row["direction"], row["status"], row["receipt"]), ("rh_to_base", "pending", "success"))
        self.assertEqual(L.get_intent(res["key"])["status"], "confirmed")

    def test_quote_api_retried_on_5xx_then_succeeds(self):
        calls = {"n": 0}
        good = None

        def flaky(params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _HTTP503()
            return good(params)
        self.across_quote()
        good = self.net.get_routes["/swap/approval"]
        self.net.get_routes["/swap/approval"] = flaky
        with mock.patch("requests.get", side_effect=_retrying_get(self.net)):
            arb_funder._bridge_usdg_to_base_usdc(F.TEST_KEY, F.SERVICER, 20.0,
                                                 arb_funder._NonceTracker(F.SERVICER, F.RH_RPC))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(self.net.sent), 2)


class _HTTP503(Exception):
    pass


def _retrying_get(net):
    """Wrap FakeNet.get so a route raising _HTTP503 yields an HTTP 503 response."""
    def get(url, params=None, headers=None, timeout=None, **kw):
        assert timeout is not None, "quote calls must carry a timeout"
        try:
            return net.get(url, params=params, timeout=timeout)
        except _HTTP503:
            return F.FakeResponse({"error": "busy"}, status=503)
    return get


class ReceiptTests(FF.FundingCase):
    def setUp(self):
        super().setUp()
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.patch_platform()

    def test_reverted_polymarket_deposit_not_recorded_in_transit(self):
        self.net.revert_if = lambda tx: tx["to"] == F.USDG.lower()
        self.script_platform("polymarket", 0.0)
        self.script_platform("kalshi", 50.0)
        ok, err = arb_funder.fund_both_legs_for_trade(12.5, "kalshi", 10.0, wait_timeout=1, poll_interval=0)
        self.assertFalse(ok)
        self.assertIn("reverted", err)
        self.assertEqual(self.bridge_rows(), [])
        self.assertEqual(arb_withdrawals.get_inflight_bridge_usdc(), 0.0)
        intents = L.list_intents()
        self.assertEqual([i["status"] for i in intents], ["reverted"])

    def test_receipt_timeout_records_unconfirmed_and_counts_it(self):
        self.net.default_receipt = None          # never mined within the wait
        self.script_platform("polymarket", 0.0)  # and never credited
        self.script_platform("kalshi", 50.0)
        ok, err = arb_funder.fund_both_legs_for_trade(12.5, "kalshi", 10.0, wait_timeout=0.05, poll_interval=0)
        self.assertFalse(ok)
        row = self.bridge_rows()[0]
        self.assertEqual((row["status"], row["receipt"]), ("pending", "unconfirmed"))
        self.assertAlmostEqual(arb_withdrawals.get_inflight_bridge_usdc(), 12.5)

    def test_reverted_kalshi_transfer_not_recorded(self):
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 30)   # no bridge needed
        self.net.revert_if = lambda tx: tx["to"] == F.USDC_BASE.lower()
        self.script_platform("polymarket", 50.0)
        self.script_platform("kalshi", 0.0)
        ok, err = arb_funder.fund_both_legs_for_trade(10.0, "kalshi", 20.0, wait_timeout=1, poll_interval=0)
        self.assertFalse(ok)
        self.assertIn("reverted", err)
        self.assertEqual(L.read_rows(L.venue_deposit_path()), [])


if __name__ == "__main__":
    unittest.main()
