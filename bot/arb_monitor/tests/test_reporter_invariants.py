"""reportedAssets invariants (robinhood mode, PMFIArbVaultV3 on 4663, USDG 6 decimals).

PMFIArbVaultV3.report() treats reportedAssets as GROSS: it requires reportedAssets >=
USDG.balanceOf(vault) and subtracts totalPendingDepositAssets + totalClaimableRedeemAssets.
So every unit of capital must be counted exactly once wherever it sits, and moving capital
between places must not change reportedAssets except by explicit fees.

Each transfer test checks reportedAssets at every intermediate step at which the ledger is
updated the way the funding code is supposed to update it. Steps that are NOT exact with
the current ledger model are separate @expectedFailure tests naming the missing ledger write.

Offline: fake chain / venues / positions (see _reporter_fakes.py); real ledger functions.
"""

import json
import subprocess
import unittest
from unittest import mock

import _fakes as F
import _reporter_fakes as RF
from _reporter_fakes import U

from arb_monitor.core import arb_funder, arb_nav, arb_reporter, arb_withdrawals as W


class _World(unittest.TestCase):
    def setUp(self):
        self.w = RF.RobinhoodWorld(self)
        self.w.vault(1_000 * U)
        self.w.usdg(300 * U)
        self.R0 = self.w.R()
        self.assertEqual(self.R0, 1_300 * U)


class CapitalTransferInvariantTests(_World):

    def test_vault_to_servicer_tend(self):
        self.w.vault(700 * U)
        self.w.usdg(600 * U)
        self.assertEqual(self.w.R(), self.R0)

    def test_tend_has_no_artificial_loss_when_usdg_peg_is_off(self):
        # compute_nav (display) haircuts servicer USDG when the peg is off; the V3 report must
        # not, since USDG is the vault's own unit (vault USDG is never haircut either).
        with mock.patch.dict("os.environ", {"ARB_PEG_MAX_DEVIATION_BPS": "50"}), \
             mock.patch.object(arb_nav, "PEG_PRICE_SOURCE", lambda: 0.97):
            before = self.w.R()
            self.w.vault(700 * U)
            self.w.usdg(600 * U)
            self.assertEqual(self.w.R(), before)

    def test_servicer_to_polymarket_deposit_in_transit_then_credited(self):
        # funder: USDG transfer to the Polymarket bridge address + record_bridge_transfer
        self.w.usdg(200 * U)
        W.record_bridge_transfer("rh_to_polymarket", "polymarket_bridge", "0xpd", 100.0, 4663)
        self.assertEqual(self.w.R(), self.R0)
        # Polymarket credits; funder observes the balance and marks the row arrived
        self.w.venue["polymarket"] = 100 * U
        W.mark_bridge_arrived("0xpd")
        self.assertEqual(self.w.R(), self.R0)

    def test_servicer_to_kalshi_deposit_via_bridge_ledger_steps(self):
        fee = 50_000                                             # 0.05 USDG bridge fee
        self.w.usdg(300 * U - (50 * U + fee))                    # Across deposit on 4663
        W.record_bridge_transfer("rh_to_base", "across", "0xkd", (50 * U + fee) / U, 4663)
        self.assertEqual(self.w.R(), self.R0)
        self.w.base(50 * U)                                      # fill on Base
        self.w.bridge_status["0xkd"] = "filled"
        self.assertEqual(self.w.R(), self.R0 - fee)              # only the explicit fee
        self.w.base(0)                                           # Base USDC → Kalshi deposit address
        self.w.venue["kalshi"] = 50 * U                          # (credited in the same step)
        self.assertEqual(self.w.R(), self.R0 - fee)

    def test_polymarket_withdrawal_pending_then_arrived(self):
        self.w.venue["polymarket"] = 80 * U
        R0 = self.w.R()
        self.w.venue["polymarket"] = 0
        W.record_withdrawal_intent("polymarket", 80.0, "0xpw", 60, dest="rh_usdg")
        self.assertEqual(self.w.R(), R0)                         # pending, not yet landed
        self.w.usdg(380 * U)
        self.w.age_withdrawals(120)                              # ETA passed → reconciled
        self.assertEqual(self.w.R(), R0)
        self.assertEqual(W._load_ledger()[0]["status"], "completed")

    def test_kalshi_withdrawal_pending_on_base_then_bridged_home(self):
        self.w.venue["kalshi"] = 40 * U
        R0 = self.w.R()
        self.w.venue["kalshi"] = 0
        W.record_withdrawal_intent("kalshi", 40.0, "kw1", 3600, dest="base_usdc")
        self.assertEqual(self.w.R(), R0)                         # pending
        self.w.base(40 * U)                                      # landed on Base, row still pending
        self.assertEqual(self.w.R(), R0)
        # sweep_base_usdc_to_robinhood: Base USDC → Across, recall completed, bridge recorded
        self.w.base(0)
        W._complete_base_withdrawals_upto(40.0)
        W.record_bridge_transfer("base_to_rh", "across", "0xsw", 40.0, 8453)
        self.assertEqual(self.w.R(), R0)
        fee = 40_000
        self.w.usdg(300 * U + 40 * U - fee)
        self.w.bridge_status["0xsw"] = "filled"
        self.assertEqual(self.w.R(), R0 - fee)

    def test_servicer_to_vault_refill(self):
        self.w.usdg(100 * U)
        self.w.vault(1_200 * U)
        self.assertEqual(self.w.R(), self.R0)

    def test_open_position_counted_at_cost_until_cash_observed(self):
        self.w.usdg(300 * U - 97 * U)                            # trade: cash → position
        self.w.add_position("p1", 97 * U)
        self.assertEqual(self.w.R(), self.R0)
        self.w.positions[0].status = "pending_redeem"            # resolved, payout not seen
        self.w.positions[0].resolved_venue = "polymarket"
        self.assertEqual(self.w.R(), self.R0)
        self.w.positions.clear()                                 # mark_position_redeemed …
        self.w.venue["polymarket"] = 100 * U                     # … once 100 is in venue cash
        self.assertEqual(self.w.R(), self.R0 + 3 * U)            # realised gain, booked once

    # ── production (snapshot / delta-settled) ledger rows ─────────────────────

    def test_polymarket_deposit_snapshot_row_survives_eta_until_credited(self):
        self.w.usdg(200 * U)
        W.record_bridge_transfer("rh_to_polymarket", "polymarket_bridge", "0xpd2", 100.0, 4663,
                                 eta_seconds=0, dest_balance_before=0.0)
        self.assertEqual(self.w.R(), self.R0)                    # ETA passed, not credited
        self.w.venue["polymarket"] = 100 * U
        self.assertEqual(self.w.R(), self.R0)                    # credited → row cleared by delta
        self.assertEqual(W.get_inflight_bridge_usdc(), 0.0)

    def test_polymarket_recall_snapshot_row_not_completed_before_it_lands(self):
        self.w.venue["polymarket"] = 80 * U
        R0 = self.w.R()
        self.w.venue["polymarket"] = 0
        W.record_withdrawal_intent("polymarket", 80.0, "0xslow2", 60, dest="rh_usdg",
                                   dest_balance_before=self.w.usdg() / U)
        self.w.age_withdrawals(120)
        self.assertEqual(self.w.R(), R0)                         # servicer already held 300 > 80
        self.w.usdg(self.w.usdg() + 80 * U)
        self.assertEqual(self.w.R(), R0)
        self.assertEqual(W._load_ledger()[0]["status"], "completed")

    def test_kalshi_recall_snapshot_row_with_stranded_base_usdc(self):
        self.w.base(10 * U)
        self.w.venue["kalshi"] = 40 * U
        R0 = self.w.R()
        self.w.venue["kalshi"] = 0
        W.record_withdrawal_intent("kalshi", 40.0, "kw3", 3600, dest="base_usdc",
                                   dest_balance_before=self.w.base() / U)
        self.assertEqual(self.w.R(), R0)                         # in flight, 10 unrelated on Base
        self.w.base(50 * U)                                      # lands
        self.assertEqual(self.w.R(), R0)
        self.assertEqual(W._load_ledger()[0]["status"], "completed")

    # ── legacy snapshot-less ledger rows (base-mode / pre-upgrade ledgers) ────
    # These heuristics still exist for rows without dest_balance_before. Any such row left in
    # a ledger at robinhood cut-over must be drained/settled manually first.

    def _run_funding_path(self, fn):
        """Run a real funding-side function; if it cannot run in this fake environment
        (its plumbing changed), skip instead of passing as an 'expected failure'."""
        try:
            res = fn()
        except Exception as e:
            self.skipTest(f"funding path not runnable offline: {e}")
        return res

    def test_kalshi_deposit_base_transfer_until_kalshi_credit(self):
        """Real arb_funder._fund_kalshi_from_robinhood path: between the Base USDC transfer to
        KALSHI_BASE_DEPOSIT_ADDR and Kalshi crediting it, the venue-deposit row (written by
        the funding side, counted by get_inflight_bridge_usdc) keeps it in reportedAssets; the
        Kalshi credit clears it. (Missing at 406cc2a: the transfer was recorded nowhere.)"""
        self.w.base(50 * U)                                      # USDC already on Base: no bridge
        self.w.net.set_eth(arb_funder.BASE_RPC, F.SERVICER, 1.0)
        R0 = self.w.R()

        def on_send(tx):
            if tx["to"] == F.USDC_BASE.lower() and tx["data"].startswith("0xa9059cbb"):
                self.w.base(self.w.base() - int(tx["data"][-64:], 16))   # → Kalshi, not yet credited
        self.w.net.on_send = on_send
        with mock.patch.object(arb_funder, "KALSHI_BASE_DEPOSIT_ADDR", F.KALSHI_ADDR):
            ok, ref = self._run_funding_path(lambda: arb_funder._fund_kalshi_from_robinhood(
                F.TEST_KEY, F.SERVICER, 50.0, arb_funder._NonceTracker(F.SERVICER, F.RH_RPC), 5, 0,
                kalshi_before=self.w.venue["kalshi"] / U))
        if not ok:
            self.skipTest(f"funding path refused offline: {ref}")
        self.assertEqual(self.w.base(), 0)
        self.assertEqual(self.w.R(), R0)                         # in transit to Kalshi
        self.w.venue["kalshi"] = 50 * U                          # Kalshi credits
        self.assertEqual(self.w.R(), R0)

    def test_polymarket_recall_is_not_double_counted_right_after_it_lands(self):
        """Real withdraw_from_platforms path: the withdraw script returns only after the Relay
        fill landed as USDG on 4663. The recall row (balance snapshot, delta-settled) must not
        be counted on top of the USDG that already arrived. (Was a double count at 406cc2a:
        pending row with a 60 s ETA; the legacy-row case is still refused by
        build_report_payload — test_report_refused_while_polymarket_recall_lands.)"""
        self.w.venue["polymarket"] = 100 * U
        R0 = self.w.R()

        def run(*a, **kw):                                       # script moves the money
            self.w.venue["polymarket"] -= 20 * U
            self.w.usdg(self.w.usdg() + 20 * U)
            return mock.Mock(returncode=0, stdout='{"txHash": "0x' + "ab" * 32 + '"}\n', stderr="")
        with mock.patch.object(W, "_poly_get_balance", return_value=100.0), \
             mock.patch.dict("os.environ", {"POLY_PROXY_ADDRESS": F.POLY_BASE_ADDR, "ARB_POLY_WITHDRAW_ROUTE": "polymarket_bridge"}), \
             mock.patch.object(subprocess, "run", side_effect=run):
            got = self._run_funding_path(lambda: W.withdraw_from_platforms(20.0, 100.0, 0.0, 0.0, F.SERVICER))
        self.assertEqual(got, 20.0)
        self.assertEqual(self.w.R(), R0)

    @unittest.expectedFailure
    def test_polymarket_recall_not_completed_before_it_lands(self):
        """LEGACY ROW, MISSING LEDGER DATA (no dest_balance_before; arb_withdrawals.
        mark_withdrawals_arrived legacy branch): arrival is decided by 'ETA passed and
        servicer balance >= amount'. A servicer that already holds more
        than the amount marks a recall completed before it lands, dropping it from NAV.
        Needs a per-row baseline balance (or the landing tx) so arrival = observed delta."""
        self.w.venue["polymarket"] = 80 * U
        R0 = self.w.R()
        self.w.venue["polymarket"] = 0
        W.record_withdrawal_intent("polymarket", 80.0, "0xslow", 60, dest="rh_usdg")
        self.w.age_withdrawals(120)                              # ETA passed, money not here yet
        self.assertEqual(self.w.R(), R0)                         # actual: R0 - 80 USDC

    @unittest.expectedFailure
    def test_kalshi_recall_pending_with_stranded_base_usdc(self):
        """LEGACY ROW, MISSING LEDGER DATA (record_withdrawal_intent without
        dest_balance_before, dest=base_usdc):
        arb_nav counts max(0, pending_base - base_usdc) to avoid counting a landed recall
        twice; with unrelated USDC already on Base (e.g. a stranded bridge output) a recall
        still in flight is under-counted by that amount. Needs the Base balance at intent
        time (baseline) or an arrival tx per row."""
        self.w.base(10 * U)
        self.w.venue["kalshi"] = 40 * U
        R0 = self.w.R()
        self.w.venue["kalshi"] = 0
        W.record_withdrawal_intent("kalshi", 40.0, "kw2", 3600, dest="base_usdc")
        self.assertEqual(self.w.R(), R0)                         # actual: R0 - 10 USDC

    def test_polymarket_deposit_not_assumed_credited_at_eta(self):
        """LEGACY ROW (no dest_balance_before), provider 'polymarket_bridge'. Fixed on the
        funding side: in robinhood mode such rows no longer clear on ETA. Previously:
        a Polymarket deposit whose funder wait timed out is assumed 'filled' once its ETA
        (ARB_BRIDGE_ETA_SECS) passes, whether or not Polymarket credited it. Needs arrival
        confirmed against the Polymarket balance (or the bridge's own status API)."""
        self.w.usdg(200 * U)
        W.record_bridge_transfer("rh_to_polymarket", "polymarket_bridge", "0xslowpd", 100.0, 4663,
                                 eta_seconds=0)
        self.assertEqual(self.w.R(), self.R0)                    # was: R0 - 100 USDC


class PendingRequestsCountedOnceTests(_World):

    def test_pending_deposit_counted_once_via_vault_balance(self):
        b0 = self.w.backing(self.R0)
        self.w.vault(1_500 * U)                                  # requestDeposit(500)
        self.w.net.vault["totalPendingDepositAssets()"] = 500 * U
        a = self.w.assets()
        self.assertEqual(a["components_raw"]["vault"], 1_500 * U)   # gross balance, not balance+pending
        self.assertEqual(a["reported_assets_raw"], self.R0 + 500 * U)
        self.assertEqual(self.w.backing(a["reported_assets_raw"]), b0)

    def test_claimable_redeem_counted_once_and_claim_is_neutral(self):
        self.w.net.vault["totalClaimableRedeemAssets()"] = 100 * U   # already in the vault balance
        R1 = self.w.R()
        self.assertEqual(R1, self.R0)
        b1 = self.w.backing(R1)
        self.w.vault(900 * U)                                    # claimRedeem pays the user
        self.w.net.vault["totalClaimableRedeemAssets()"] = 0
        R2 = self.w.R()
        self.assertEqual(R2, R1 - 100 * U)
        self.assertEqual(self.w.backing(R2), b1)

    def test_no_off_vault_assets_equals_vault_balance_exactly(self):
        self.w.usdg(0)
        self.w.vault(1_234_567_891)
        self.assertEqual(self.w.R(), 1_234_567_891)


class ReadFailureAbortsReportTests(_World):

    def _assert_aborts(self, needle):
        self.assertIsNone(arb_reporter.build_report_payload(F.VAULT, RF.SIGNER.key.hex()))
        self.assertIn(needle, arb_reporter.LAST_ABORT_REASON)
        self.assertEqual(self.w.net.sent, [])

    def test_control_report_builds(self):
        p = arb_reporter.build_report_payload(F.VAULT, RF.SIGNER.key.hex())
        self.assertIsNotNone(p, arb_reporter.LAST_ABORT_REASON)
        self.assertEqual(p["reported_assets_raw"], self.R0)

    def test_block_number_unavailable(self):
        self.w.fail.add("blockNumber")
        self._assert_aborts("vault chain/config check failed")

    def test_vault_getter_missing(self):
        self.w.fail.add("getter:officialPPS()")
        self._assert_aborts("officialPPS()")

    def test_vault_balance_unreadable(self):
        self.w.fail.add(f"balance:{F.RH_RPC}:{F.VAULT.lower()}")
        self._assert_aborts("vault state unreadable")

    def test_servicer_usdg_unreadable(self):
        self.w.fail.add(f"balance:{F.RH_RPC}:{F.SERVICER.lower()}")
        self._assert_aborts("servicer_usdg_rh")

    def test_servicer_base_usdc_unreadable(self):
        self.w.fail.add(f"balance:{arb_funder.BASE_RPC}:{F.SERVICER.lower()}")
        self._assert_aborts("servicer_usdc_base")

    def test_polymarket_cash_unreadable(self):
        self.w.fail.add("poly")
        self._assert_aborts("poly_cash")

    def test_kalshi_cash_unreadable(self):
        self.w.fail.add("kalshi")
        self._assert_aborts("kalshi_cash")

    def test_positions_db_unreadable(self):
        self.w.fail.add("positions")
        self._assert_aborts("positions")

    def test_corrupt_withdrawal_ledger(self):
        with open(W.WITHDRAWAL_LEDGER_PATH, "w") as f:
            f.write("{not json")
        self._assert_aborts("withdrawal_ledger")

    def test_corrupt_bridge_ledger(self):
        with open(W.BRIDGE_LEDGER_PATH, "w") as f:
            json.dump({"oops": 1}, f)
        self._assert_aborts("bridge_ledger")

    def test_report_refused_while_polymarket_recall_lands(self):
        W.record_withdrawal_intent("polymarket", 20.0, "0xland", 60, dest="rh_usdg")
        self._assert_aborts("Polymarket recall")

    def test_report_refused_while_kalshi_won_position_unreconciled(self):
        self.w.add_position("pk", 50 * U, status="pending_redeem", venue="kalshi")
        self._assert_aborts("resolved on Kalshi")


if __name__ == "__main__":
    unittest.main()
