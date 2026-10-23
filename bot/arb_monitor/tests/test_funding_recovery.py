"""Write-ahead intents + crash + startup reconciliation: no duplicate bridge for the same key."""

import unittest
from unittest import mock

import _fakes as F
import _funding_fakes as FF
from arb_monitor.core import arb_funder, arb_withdrawals as W, arb_ledger as L


class CrashRecoveryTests(FF.FundingCase):
    def setUp(self):
        super().setUp()
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        self.across_quote()

    def bridge(self, key):
        return arb_funder._bridge_usdg_to_base_usdc(
            F.TEST_KEY, F.SERVICER, 20.0, arb_funder._NonceTracker(F.SERVICER, F.RH_RPC), idempotency_key=key)

    def crash_after_deposit_broadcast(self, key):
        real_wait = arb_funder._wait_for_receipt
        seen = {"n": 0}

        def wait(tx_hash, rpc_url, *a, **kw):
            seen["n"] += 1
            if seen["n"] == 2:            # the deposit (2nd tx) is on the wire — process dies
                raise FF.Crash()
            return real_wait(tx_hash, rpc_url, *a, **kw)
        with mock.patch.object(arb_funder, "_wait_for_receipt", side_effect=wait):
            with self.assertRaises(FF.Crash):
                self.bridge(key)

    def test_crash_after_broadcast_then_reconcile_and_no_duplicate(self):
        self.crash_after_deposit_broadcast("K1")
        self.assertEqual(len(self.net.sent), 2)                       # approve + deposit
        intent = L.get_intent("K1")
        self.assertEqual(intent["status"], "broadcast")
        self.assertEqual(self.bridge_rows(), [])                      # crashed before ledgering…
        self.assertAlmostEqual(W.get_inflight_bridge_usdc(), 20.05)   # …but still counted (intent)

        # restart: same key is refused before any quote / tx
        with self.assertRaises(L.DuplicateOperation):
            self.bridge("K1")
        self.assertEqual(len(self.net.sent), 2)

        counts = arb_funder.reconcile_funding_intents()
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(L.get_intent("K1")["status"], "confirmed")
        rows = self.bridge_rows()
        self.assertEqual([(r["direction"], r["status"], r["idem_key"]) for r in rows],
                         [("rh_to_base", "pending", "K1")])
        self.assertAlmostEqual(W.get_inflight_bridge_usdc(), 20.05)   # counted exactly once

        # reconciliation is idempotent
        arb_funder.reconcile_funding_intents()
        self.assertEqual(len(self.bridge_rows()), 1)
        # same key still refused; a NEW key for Kalshi funding is refused by the in-transit guard
        with self.assertRaises(L.DuplicateOperation):
            self.bridge("K1")
        self.patch_platform()
        self.script_platform("polymarket", 50.0)
        self.script_platform("kalshi", 0.0)
        ok, err = arb_funder.fund_both_legs_for_trade(10.0, "kalshi", 20.0, wait_timeout=1, poll_interval=0)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("funding_insufficient"), err)
        self.assertIn("in_transit_pending", err)
        self.assertEqual(len(self.net.sent), 2)                       # no duplicate bridge

    def test_crash_before_broadcast_is_abandoned(self):
        def boom(*a, **kw):
            raise FF.Crash()
        real_rpc = arb_funder._rpc

        def rpc(method, params, rpc_url=arb_funder.BASE_RPC):
            if method == "eth_sendRawTransaction" and len(self.net.sent) == 1:
                raise FF.Crash()          # dies after signing the deposit, before it hits the wire
            return real_rpc(method, params, rpc_url)
        with mock.patch.object(arb_funder, "_rpc", side_effect=rpc):
            with self.assertRaises(FF.Crash):
                self.bridge("K2")
        self.assertEqual(L.get_intent("K2")["status"], "broadcast")
        # nonce of the deposit never consumed on chain (latest == pending == 1: only the approval)
        self.net.nonces[(F.RH_RPC, F.SERVICER.lower())] = 1
        self.net.default_receipt = None
        counts = arb_funder.reconcile_funding_intents()
        self.assertEqual(counts["abandoned"], 1)
        self.assertEqual(L.get_intent("K2")["status"], "abandoned")
        self.assertEqual(self.bridge_rows(), [])
        self.assertEqual(W.get_inflight_bridge_usdc(), 0.0)
        # an abandoned key may be retried
        self.net.default_receipt = 1
        self.bridge("K2")
        self.assertEqual(len(self.bridge_rows()), 1)

    def test_tx_in_mempool_stays_unresolved_and_blocks(self):
        def rpc_crash(method, params, rpc_url=arb_funder.BASE_RPC):
            if method == "eth_sendRawTransaction" and len(self.net.sent) == 1:
                raise FF.Crash()
            return real(method, params, rpc_url)
        real = arb_funder._rpc
        with mock.patch.object(arb_funder, "_rpc", side_effect=rpc_crash):
            with self.assertRaises(FF.Crash):
                self.bridge("K3")
        key = (F.RH_RPC, F.SERVICER.lower())
        self.net.nonces[key] = 2               # pending includes our deposit
        self.net.latest_nonce[key] = 1         # but it is not mined
        self.net.default_receipt = None
        counts = arb_funder.reconcile_funding_intents()
        self.assertEqual(counts["still_pending"], 1)
        self.assertTrue(L.unresolved_intents(kinds=("rh_to_base",)))
        self.assertEqual(arb_withdrawals_sweep(self), 0.0)     # sweep refuses while unresolved

    def test_reverted_after_restart_is_not_counted(self):
        self.crash_after_deposit_broadcast("K4")
        deposit_hash = self.net.hash_of_index[1]
        self.net.receipts[deposit_hash] = 0
        counts = arb_funder.reconcile_funding_intents()
        self.assertEqual(counts["reverted"], 1)
        self.assertEqual(self.bridge_rows(), [])
        self.assertEqual(W.get_inflight_bridge_usdc(), 0.0)

    def test_startup_checks_run_reconciliation_and_report_config(self):
        self.crash_after_deposit_broadcast("K5")
        self.assertEqual(arb_funder.run_startup_checks(), [])
        self.assertEqual(L.get_intent("K5")["status"], "confirmed")


def arb_withdrawals_sweep(tc):
    tc.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 50)
    return W.sweep_base_usdc_to_robinhood(F.SERVICER, F.TEST_KEY)


if __name__ == "__main__":
    unittest.main()
