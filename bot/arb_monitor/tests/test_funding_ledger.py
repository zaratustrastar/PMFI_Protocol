"""Ledger tests: delta-based arrival, no double count, stale-not-dropped, file locking."""

import os
import subprocess
import threading
import time
import unittest
from unittest import mock

import _fakes as F
import _funding_fakes as FF
from arb_monitor.core import arb_funder, arb_withdrawals as W, arb_ledger as L


class DeltaArrivalTests(FF.FundingCase):
    def test_recall_not_completed_when_balance_already_above_amount(self):
        # Servicer already holds 500 USDG; an 80 USDG recall must NOT be marked arrived from that.
        W.record_withdrawal_intent("polymarket", 80.0, "r1", 60, dest="rh_usdg", dest_balance_before=500.0)
        self.assertEqual(W.mark_withdrawals_arrived(500.0, 0.0), 0.0)
        self.assertEqual(self.withdrawal_rows()[0]["status"], "pending")
        self.assertEqual(W.mark_withdrawals_arrived(580.0, 0.0), 80.0)
        self.assertEqual(self.withdrawal_rows()[0]["status"], "completed")

    def test_fee_tolerance(self):
        W.record_withdrawal_intent("polymarket", 100.0, "r1", 60, dest="rh_usdg", dest_balance_before=0.0)
        self.assertEqual(W.mark_withdrawals_arrived(99.0, None), 0.0)          # 1% short > 50 bps
        self.assertEqual(W.mark_withdrawals_arrived(99.6, None), 100.0)        # within 50 bps

    def test_two_recalls_do_not_share_one_delta(self):
        W.record_withdrawal_intent("polymarket", 50.0, "r1", 60, dest="rh_usdg", dest_balance_before=0.0)
        W.record_withdrawal_intent("polymarket", 50.0, "r2", 60, dest="rh_usdg", dest_balance_before=0.0)
        self.assertEqual(W.mark_withdrawals_arrived(50.0, None), 50.0)         # only one landed
        self.assertEqual([r["status"] for r in self.withdrawal_rows()], ["completed", "pending"])
        self.assertEqual(W.get_pending_withdrawal_usdc(), 50.0)
        # a later pass with the same balance must not settle the second one either
        self.assertEqual(W.mark_withdrawals_arrived(50.0, None), 0.0)
        self.assertEqual(W.mark_withdrawals_arrived(100.0, None), 50.0)

    def test_arrival_detected_despite_our_own_spending(self):
        W.record_withdrawal_intent("polymarket", 40.0, "r1", 60, dest="rh_usdg", dest_balance_before=100.0)
        time.sleep(0)  # (patched) — journal entries are strictly after the snapshot below
        L.record_cashflow(4663, F.USDG, -40.0, "rh_to_polymarket", "k1")       # we spent 40 meanwhile
        self.assertEqual(W.mark_withdrawals_arrived(100.0, None), 40.0)        # 100 − 100 + 40 ≥ 40

    def test_known_inflow_is_not_mistaken_for_recall(self):
        W.record_withdrawal_intent("polymarket", 40.0, "r1", 60, dest="rh_usdg", dest_balance_before=100.0)
        L.record_cashflow(4663, F.USDG, 40.0, "tend", "0xtend")                # vault tend() paid 40
        self.assertEqual(W.mark_withdrawals_arrived(140.0, None), 0.0)
        self.assertEqual(W.mark_withdrawals_arrived(180.0, None), 40.0)

    def test_kalshi_recall_settles_against_base_balance(self):
        W.record_withdrawal_intent("kalshi", 25.0, "kw", 3600, dest="base_usdc", dest_balance_before=10.0)
        self.assertEqual(W.mark_withdrawals_arrived(999.0, 20.0), 0.0)         # USDG irrelevant
        self.assertEqual(W.mark_withdrawals_arrived(999.0, 35.0), 25.0)

    def test_polymarket_recall_write_ahead_and_no_double_count(self):
        """RH recall: row exists BEFORE the script runs; once the fill is visible it settles in the
        same call, so (servicer USDG + pending recalls) never counts the money twice."""
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 100)
        seen = {}

        def run(*a, **kw):
            seen["rows_during_script"] = self.withdrawal_rows()
            self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 120)              # fill lands
            return mock.Mock(returncode=0, stdout='{"txHash": "0x' + "ab" * 32 + '"}\n', stderr="")
        with mock.patch.object(W, "_poly_get_balance", return_value=100.0), \
             mock.patch.dict(os.environ, {"POLY_PROXY_ADDRESS": F.POLY_BASE_ADDR, "ARB_POLY_WITHDRAW_ROUTE": "polymarket_bridge"}), \
             mock.patch.object(subprocess, "run", side_effect=run):
            got = W.withdraw_from_platforms(20.0, 100.0, 0.0, 0.0, F.SERVICER)
        self.assertEqual(got, 20.0)
        during = seen["rows_during_script"]
        self.assertEqual([(r["platform"], r["status"], r["dest_balance_before"]) for r in during],
                         [("polymarket", "pending", 100.0)])
        row = self.withdrawal_rows()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(W.get_pending_withdrawal_usdc(), 0.0)                 # 120 USDG, 0 pending

    def test_polymarket_recall_records_amount_actually_moved(self):
        self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 0)

        def run(*a, **kw):   # TS script bridged the whole EOA balance (35) instead of 20
            self.net.set_token(F.RH_RPC, F.USDG, F.SERVICER, 35)
            return mock.Mock(returncode=0, stdout='{"txHash": "0x' + "cd" * 32 + '"}\n', stderr="")
        with mock.patch.object(W, "_poly_get_balance", return_value=100.0), \
             mock.patch.dict(os.environ, {"POLY_PROXY_ADDRESS": F.POLY_BASE_ADDR, "ARB_POLY_WITHDRAW_ROUTE": "polymarket_bridge"}), \
             mock.patch.object(subprocess, "run", side_effect=run):
            got = W.withdraw_from_platforms(20.0, 100.0, 0.0, 0.0, F.SERVICER)
        self.assertAlmostEqual(got, 35.0)
        self.assertAlmostEqual(self.withdrawal_rows()[0]["amount_usdc"], 35.0)

    def test_failed_recall_kept_when_money_left_polymarket(self):
        bal = iter([100.0, 60.0])   # before, after (the bridge script reads no balance)

        with mock.patch.object(W, "_poly_get_balance", side_effect=lambda: next(bal)), \
             mock.patch.dict(os.environ, {"POLY_PROXY_ADDRESS": F.POLY_BASE_ADDR, "ARB_POLY_WITHDRAW_ROUTE": "polymarket_bridge"}), \
             mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=1, stdout='{"error": "bridge step failed"}', stderr="")):
            got = W.withdraw_from_platforms(40.0, 100.0, 0.0, 0.0, F.SERVICER)
        self.assertEqual(got, 0.0)
        row = self.withdrawal_rows()[0]
        self.assertEqual((row["status"], row.get("receipt")), ("pending", "unconfirmed"))
        self.assertEqual(W.get_pending_withdrawal_usdc(), 40.0)

    def test_polymarket_deposit_cleared_by_balance_delta_not_eta(self):
        W.record_bridge_transfer("rh_to_polymarket", "polymarket_bridge", "0xpd", 30.0, 4663,
                                 eta_seconds=0, dest_balance_before=10.0)
        self.patch_platform()
        self.script_platform("polymarket", 10.0)
        W.reconcile_bridge_transfers()
        self.assertEqual(self.bridge_rows()[0]["status"], "pending")           # ETA passed, not credited
        self.script_platform("polymarket", 40.0)
        W.reconcile_bridge_transfers()
        self.assertEqual(self.bridge_rows()[0]["status"], "completed")

    def test_kalshi_deposit_in_transit_until_kalshi_balance_moves(self):
        W.record_venue_deposit("kalshi", "0xkd", 20.0, 8453, dest_balance_before=5.0, receipt="success")
        self.assertEqual(W.get_inflight_bridge_usdc(), 20.0)
        self.patch_platform()
        self.script_platform("kalshi", 5.0)
        W.reconcile_bridge_transfers()
        self.assertEqual(W.get_inflight_bridge_usdc(), 20.0)
        self.script_platform("kalshi", 25.0)
        W.reconcile_bridge_transfers()
        self.assertEqual(W.get_inflight_bridge_usdc(), 0.0)


class StaleNotDroppedTests(FF.FundingCase):
    def _age(self, path, seconds):
        rows = L.read_rows(path)
        for r in rows:
            r["created_at"] -= seconds
            r["eta_at"] -= seconds
        L.write_rows(path, rows)

    def test_stale_recall_still_counted_in_robinhood_mode(self):
        W.record_withdrawal_intent("kalshi", 30.0, "kw", 3600, dest="base_usdc", dest_balance_before=0.0)
        self._age(W.WITHDRAWAL_LEDGER_PATH, 2 * 86400)
        self.assertEqual(W.get_pending_withdrawal_usdc(), 30.0)
        self.assertEqual(self.withdrawal_rows()[0]["status"], "stale")
        self.assertEqual(W.get_pending_withdrawal_usdc_by_dest()["base_usdc"], 30.0)
        self.assertEqual(W.get_pending_withdrawals_split()["base_usdc_tracked"], 30.0)
        # and a stale row can still settle when the money finally shows up
        self.assertEqual(W.mark_withdrawals_arrived(0.0, 30.0), 30.0)
        self.assertEqual(W.get_pending_withdrawal_usdc(), 0.0)

    def test_stale_bridge_still_counted(self):
        W.record_bridge_transfer("rh_to_base", "across", "0xold", 12.0, 4663)
        self._age(W.BRIDGE_LEDGER_PATH, 2 * 86400)
        self.net.get_routes["/deposit/status"] = lambda p: {"status": "pending"}
        W.reconcile_bridge_transfers()
        self.assertEqual(self.bridge_rows()[0]["status"], "stale")
        self.assertEqual(W.get_inflight_bridge_usdc(), 12.0)

    def test_base_mode_keeps_legacy_24h_expiry(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "base"}):
            W.record_withdrawal_intent("kalshi", 30.0, "k1", 3600)
            self._age(W.WITHDRAWAL_LEDGER_PATH, 2 * 86400)
            self.assertEqual(W.get_pending_withdrawal_usdc(), 0.0)
            self.assertEqual(self.withdrawal_rows()[0]["status"], "failed")


class FileLockTests(FF.FundingCase):
    def test_concurrent_appends_lose_nothing(self):
        path = os.path.join(self.ledger_dir, "race.json")
        n, threads = 150, 4
        orig_read = L._read_unlocked

        def slow_read(p, strict):          # widen the load→save window to provoke lost updates
            rows = orig_read(p, strict)
            time.sleep(0.0005)
            return rows
        errors = []

        def worker(tid):
            try:
                for i in range(n):
                    L.append_row(path, {"t": tid, "i": i})
            except Exception as e:  # pragma: no cover
                errors.append(e)
        with mock.patch("time.sleep", new=_real_sleep), mock.patch.object(L, "_read_unlocked", side_effect=slow_read):
            ts = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(errors, [])
        rows = L.read_rows(path)
        self.assertEqual(len(rows), n * threads)
        self.assertEqual(len({(r["t"], r["i"]) for r in rows}), n * threads)

    def test_concurrent_writers_on_withdrawal_ledger(self):
        def writer(tag):
            for i in range(60):
                W.record_withdrawal_intent("kalshi", 1.0, f"{tag}-{i}", 60)

        def marker():
            for _ in range(60):
                W.get_pending_withdrawal_usdc()   # load-modify-save from the "other thread"
        with mock.patch("time.sleep", new=_real_sleep):
            ts = [threading.Thread(target=writer, args=("a",)), threading.Thread(target=writer, args=("b",)),
                  threading.Thread(target=marker)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(self.withdrawal_rows()), 120)

    def test_corrupt_ledger_is_not_overwritten(self):
        path = os.path.join(self.ledger_dir, "bad.json")
        with open(path, "w") as f:
            f.write("{not json")
        with self.assertRaises(L.LedgerError):
            L.append_row(path, {"x": 1})
        with open(path) as f:
            self.assertEqual(f.read(), "{not json")

    def test_snapshot_is_a_copy(self):
        W.record_bridge_transfer("rh_to_base", "across", "0xa", 5.0, 4663)
        snap = W.get_ledger_snapshot()
        snap["bridges"][0]["status"] = "completed"
        self.assertEqual(self.bridge_rows()[0]["status"], "pending")


_real_sleep = time.sleep

if __name__ == "__main__":
    unittest.main()
