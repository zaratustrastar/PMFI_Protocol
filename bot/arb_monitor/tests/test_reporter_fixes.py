"""Targeted tests for the reporter audit fixes (C1, C2, C3, C4, H3, H4, H6, M3, M5, liquidity)
plus base-mode (live Base V2) compatibility. Offline; see _reporter_fakes.py."""

import os
import unittest
from unittest import mock

import rlp
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct

import _fakes as F
import _reporter_fakes as RF
from _reporter_fakes import U

from arb_monitor.core import (arb_funder, arb_liquidity, arb_nav, arb_positions_db as DB,
                              arb_reporter as R, arb_settlement, arb_withdrawals as W)


class _RH(unittest.TestCase):
    def setUp(self):
        self.w = RF.RobinhoodWorld(self)
        self.w.vault(1_000 * U)
        self.w.usdg(300 * U)

    def build(self):
        return R.build_report_payload(F.VAULT, RF.SIGNER.key.hex())


class V3StateAndPinnedBlockTests(_RH):
    def test_v3_getters_read_individually(self):
        self.w.net.vault["paused()"] = 1
        self.w.net.vault["totalPendingRedeemShares()"] = 7 * 10 ** 18
        st = R._read_vault_state(F.VAULT)
        self.assertTrue(st["ok"])
        self.assertTrue(st["paused"])
        self.assertEqual(st["pending_redeem_shares"], 7 * 10 ** 18)
        self.assertEqual(st["vault_balance_raw"], 1_000 * U)
        self.assertEqual(st["idle_balance_raw"], 1_000 * U)
        self.assertEqual(st["report_signer"].lower(), RF.SIGNER.address.lower())
        get_vault_state = RF.sel("getVaultState()")
        self.assertFalse(any(s == get_vault_state for _, _, s, _ in self.w.net.call_blocks))

    def test_paused_vault_not_reported_unless_shutdown(self):
        self.w.net.vault["paused()"] = 1
        self.assertIsNone(self.build())
        self.assertIn("paused", R.LAST_ABORT_REASON)
        self.w.net.vault["shutdown()"] = 1          # V3 allows report() in shutdown
        self.assertIsNotNone(self.build(), R.LAST_ABORT_REASON)

    def test_wrong_signer_key_refused(self):
        self.assertIsNone(R.build_report_payload(F.VAULT, F.TEST_KEY))
        self.assertIn("reportSigner", R.LAST_ABORT_REASON)

    def test_asset_mismatch_fails_closed(self):
        self.w.net.vault["usdc()"] = int(F.USDC_BASE, 16)
        self.assertFalse(R._read_vault_state(F.VAULT)["ok"])

    def test_all_4663_reads_use_one_pinned_block(self):
        self.w.net.call_blocks.clear()
        p = self.build()
        self.assertIsNotNone(p, R.LAST_ABORT_REASON)
        rh = [(to, blk) for rpc, to, _, blk in self.w.net.call_blocks if rpc == F.RH_RPC]
        self.assertGreater(len(rh), 15)
        self.assertEqual({blk for _, blk in rh}, {hex(self.w.net.block)})
        usdg_reads = [blk for rpc, to, s, blk in self.w.net.call_blocks
                      if rpc == F.RH_RPC and to == F.USDG.lower() and s == "70a08231"]
        self.assertEqual(len(usdg_reads), 2)          # vault balance + servicer USDG, same block
        self.assertEqual(p["block"], hex(self.w.net.block))

    def test_chain_id_mismatch_aborts(self):
        self.w.net.chain_of[F.RH_RPC] = 1
        self.assertIsNone(self.build())
        self.assertIn("chain", R.LAST_ABORT_REASON)


class ExactUnitsTests(_RH):
    def test_249_raw_units_signed_and_encoded_exactly(self):
        self.w.vault(249)
        self.w.usdg(0)
        p = self.build()
        self.assertIsNotNone(p, R.LAST_ABORT_REASON)
        self.assertEqual(p["reported_assets_raw"], 249)
        data = R._abi_encode_report_call(p, 50, 50)
        (tup, sig, _, _) = abi_decode(["(uint256,uint256,uint256,uint256)", "bytes", "uint256", "uint256"], data[4:])
        self.assertEqual(tup[0], 249)
        struct_hash = R._abi_encode_report_struct(249, p["timestamp"], p["deadline"], p["nonce"],
                                                  F.VAULT, 4663, R._keccak256_text("PMFIArbVaultV3.milord.v1"))
        signer = Account.recover_message(encode_defunct(primitive=struct_hash), signature=sig)
        self.assertEqual(signer, RF.SIGNER.address)

    def test_float_path_no_longer_truncates(self):
        self.assertEqual(int(0.000249 * 1e6), 248)               # the old bug
        self.assertEqual(arb_nav.usdc_to_raw(0.000249), 249)
        self.assertEqual(arb_nav.usdc_to_raw(123.45), 123_450_000)


class SubmitReportTests(_RH):
    def _payload(self):
        p = self.build()
        self.assertIsNotNone(p, R.LAST_ABORT_REASON)
        return p

    def test_simulation_revert_sends_nothing_and_sets_no_cooldown(self):
        p = self._payload()
        self.w.net.report_revert = "Report cooldown"
        self.assertIsNone(R.submit_report(p, RF.SIGNER.key.hex()))
        self.assertEqual(self.w.net.sent, [])
        self.assertEqual(R._last_local_report_ts, 0.0)

    def test_reverted_receipt_sets_no_cooldown(self):
        p = self._payload()
        self.w.net.receipt_status = 0
        self.assertIsNone(R.submit_report(p, RF.SIGNER.key.hex()))
        self.assertEqual(len(self.w.net.sent), 1)
        self.assertEqual(R._last_local_report_ts, 0.0)

    def test_success_sets_cooldown_and_gas_is_configurable(self):
        p = self._payload()
        with mock.patch.dict(os.environ, {"ARB_REPORT_GAS_LIMIT": "650000"}):
            self.assertIsNotNone(R.submit_report(p, RF.SIGNER.key.hex()))
        self.assertGreater(R._last_local_report_ts, 0)
        raw = next(params[0] for _, m, params in self.w.net.rpc_calls if m == "eth_sendRawTransaction")
        self.assertEqual(int.from_bytes(rlp.decode(bytes.fromhex(raw[2:]))[2], "big"), 650_000)
        self.assertEqual(self.w.net.sent[0]["chain_id"], 4663)

    def test_unmined_receipt_sets_short_retry_guard_only(self):
        p = self._payload()
        self.w.net.receipt_status = None
        with mock.patch.dict(os.environ, {"ARB_REPORT_RECEIPT_TIMEOUT_SECS": "0"}):
            self.assertIsNotNone(R.submit_report(p, RF.SIGNER.key.hex()))
        self.assertEqual(R._last_local_report_ts, 0.0)
        self.assertGreater(R._pending_report_until, 0)


class VaultConfigTests(unittest.TestCase):
    def setUp(self):
        saved = {k: getattr(R, k) for k in ("DOMAIN_SALT_TEXT", "BASE_CHAIN_ID", "BASE_RPC", "USDC_BASE")}
        self.addCleanup(lambda: [setattr(R, k, v) for k, v in saved.items()])

    def _env(self, **kw):
        env = {"ARB_FUNDING_CHAIN": "robinhood", "ARB_VAULT_ASSET": F.USDG, "ARB_VAULT_RPC_URL": F.RH_RPC,
               "ARB_VAULT_CHAIN_ID": "4663", "ARB_VAULT_DOMAIN_SALT": "PMFIArbVaultV3.milord.v1"}
        env.update(kw)
        return mock.patch.dict(os.environ, {k: v for k, v in env.items() if v is not None}, clear=True)

    def test_missing_settings_refuse_to_start(self):
        for k in ("ARB_VAULT_ASSET", "ARB_VAULT_RPC_URL", "ARB_VAULT_CHAIN_ID", "ARB_VAULT_DOMAIN_SALT"):
            with self._env(**{k: None}), self.assertRaises(R.VaultConfigError):
                R.load_vault_config()
        with self._env(ARB_VAULT_ASSET=None, ARB_VAULT_V2_ADDRESS=F.VAULT), self.assertRaises(R.VaultConfigError):
            R.run_reporter_tick()

    def test_inconsistent_settings_refused(self):
        with self._env(ARB_VAULT_CHAIN_ID="8453"), self.assertRaises(R.VaultConfigError):
            R.load_vault_config()
        with self._env(ARB_VAULT_ASSET=F.USDC_BASE), self.assertRaises(R.VaultConfigError):
            R.load_vault_config()
        with self._env(ROBINHOOD_CHAIN_ID="46630"), self.assertRaises(R.VaultConfigError):
            R.load_vault_config()

    def test_valid_settings_and_base_defaults(self):
        with self._env():
            cfg = R.load_vault_config()
        self.assertEqual((cfg["chain_id"], cfg["rpc"], cfg["asset"]), (4663, F.RH_RPC, F.USDG))
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = R.load_vault_config()
        self.assertEqual((cfg["chain_id"], cfg["salt"]), (8453, "PMFIArbVaultV2.v1"))


class WaterfallRecallTests(_RH):
    def setUp(self):
        super().setUp()
        self.w.vault(0)
        self.w.usdg(0)
        self.w.venue["polymarket"] = 500 * U
        self.w.net.vault["totalPendingRedeemShares()"] = 100 * 10 ** 18   # 100 USDG owed
        p = mock.patch.object(R, "withdraw_from_platforms", return_value=0.0)
        self.wfp = p.start()
        self.addCleanup(p.stop)

    def tick(self):
        R.run_withdrawal_waterfall(F.VAULT, F.TEST_KEY)

    def test_pending_recall_covering_shortfall_is_not_reissued(self):
        W.record_withdrawal_intent("polymarket", 100.0, "0xinflight", 1800, dest="rh_usdg",
                                   dest_balance_before=0.0)
        self.tick()
        self.wfp.assert_not_called()

    def test_partial_in_flight_is_subtracted(self):
        W.record_withdrawal_intent("kalshi", 40.0, "kw", 3600, dest="base_usdc", dest_balance_before=0.0)
        self.tick()
        self.assertAlmostEqual(self.wfp.call_args.kwargs["shortfall_usdc"], 60.0)
        self.assertEqual(self.wfp.call_args.kwargs["kalshi_cash"], 0.0)   # kalshi recall pending

    def test_per_venue_cooldown_prevents_reissue_every_tick(self):
        self.tick()
        self.assertEqual(self.wfp.call_count, 1)
        self.assertAlmostEqual(self.wfp.call_args.kwargs["shortfall_usdc"], 100.0)
        self.tick()                                   # recall failed to ledger anything
        self.assertEqual(self.wfp.call_count, 1)
        with mock.patch.dict(os.environ, {"ARB_RECALL_COOLDOWN_SECS": "0"}):
            self.tick()
        self.assertEqual(self.wfp.call_count, 2)

    def test_unreadable_ledger_issues_no_recall(self):
        with open(W.WITHDRAWAL_LEDGER_PATH, "w") as f:
            f.write("garbage")
        self.tick()
        self.wfp.assert_not_called()


class LiquidityTests(_RH):
    def test_ok_state_allows_trading(self):
        st = arb_liquidity.compute_liquidity_state(F.VAULT, F.SERVICER)
        self.assertTrue(st.ok and st.trading_allowed)

    def test_component_failure_blocks_trading(self):
        self.w.fail.add("poly")
        st = arb_liquidity.compute_liquidity_state(F.VAULT, F.SERVICER)
        self.assertTrue(st.ok)                       # legacy gates in the loop still evaluated
        self.assertFalse(st.trading_allowed)
        self.assertIn("platform_cash", st.blocked_reason)
        self.assertEqual(st.deployable_capital, 0.0)

    def test_vault_state_failure_blocks_trading(self):
        self.w.fail.add("getter:totalSupply()")
        st = arb_liquidity.compute_liquidity_state(F.VAULT, F.SERVICER)
        self.assertFalse(st.ok)
        self.assertFalse(st.trading_allowed)
        self.assertIn("vault state", st.blocked_reason)

    def test_settled_pnl_not_counted_as_coverable_cash(self):
        self.w.vault(0)
        self.w.net.vault["totalPendingRedeemShares()"] = 100 * 10 ** 18
        self.w.usdg(10 * U)
        with mock.patch.object(arb_nav, "_get_settled_pnl", return_value=1_000.0):
            st = arb_liquidity.compute_liquidity_state(F.VAULT, F.SERVICER)
        self.assertEqual(st.settled_proceeds, 0.0)
        self.assertAlmostEqual(st.coverable_cash, 9.0)
        self.assertTrue(st.unwind_needed)


class PositionsDbTests(unittest.TestCase):
    def setUp(self):
        self.db = RF.SqliteConn()
        p = mock.patch.object(DB, "get_db_conn", return_value=self.db)
        p.start()
        self.addCleanup(p.stop)

    def up(self, pid, shares, cost):
        self.assertTrue(DB.upsert_position(pid, "tok", "KX", shares, cost, 0))

    def test_new_trade_on_settled_pair_is_a_new_open_position(self):
        self.up("p1", 10, 9.7)
        self.assertTrue(DB.settle_position("p1", 0.3))
        self.up("p1", 5, 4.8)
        rows = self.db.rows()
        self.assertEqual(rows[0][1], "settled")
        self.assertTrue(rows[0][0].startswith("p1#closed-"))
        self.assertEqual(rows[1][:4], ("p1", "open", 5.0, 4.8))

    def test_open_pair_still_accumulates(self):
        self.up("p2", 10, 9.7)
        self.up("p2", 5, 4.8)
        self.assertEqual(self.db.rows(), [("p2", "open", 15.0, 14.5, 0)])

    def test_resolved_then_redeemed_lifecycle(self):
        self.up("p3", 10, 9.7)
        self.assertTrue(DB.mark_position_resolved("p3", "polymarket", 10.0))
        self.assertEqual(DB.get_positions_by_status(("open", "pending_redeem"))[0]["status"], "pending_redeem")
        self.assertFalse(DB.mark_position_redeemed("nope", 10.0))
        self.assertTrue(DB.mark_position_redeemed("p3", 10.0))
        row = self.db.rows()[0]
        self.assertEqual(row[1], "redeemed")
        self.assertAlmostEqual(row[4], 0.3)
        with self.assertRaises(ValueError):
            DB.mark_position_resolved("p3", "opinion", 1.0)


class SettlementTests(unittest.TestCase):
    POS = {"pair_id": "p", "poly_yes_token": "t", "kalshi_ticker": "K", "shares": 10, "cost_basis_usdc": 9.7}

    def run_check(self, trigger, rh):
        env = {"ARB_FUNDING_CHAIN": "robinhood"} if rh else {}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(arb_settlement, "get_open_positions", return_value=[dict(self.POS)]), \
             mock.patch.object(arb_settlement, "_detect", return_value=(trigger, 10.0, 1.0)), \
             mock.patch.object(arb_settlement, "settle_position", return_value=True) as settle, \
             mock.patch.object(DB, "mark_position_resolved", return_value=True) as resolve:
            n = arb_settlement.check_and_settle_positions()
        return n, settle, resolve

    def test_robinhood_resolution_goes_to_pending_redeem_not_settled(self):
        n, settle, resolve = self.run_check("KALSHI_SIDE_WON", rh=True)
        self.assertEqual(n, 1)
        settle.assert_not_called()
        resolve.assert_called_once_with("p", "kalshi", 10.0)

    def test_robinhood_early_exit_drops_nothing(self):
        n, settle, resolve = self.run_check("EARLY_EXIT", rh=True)
        self.assertEqual(n, 0)
        settle.assert_not_called()
        resolve.assert_not_called()

    def test_base_mode_settlement_unchanged(self):
        n, settle, resolve = self.run_check("EARLY_EXIT", rh=False)
        self.assertEqual(n, 1)
        settle.assert_called_once()
        resolve.assert_not_called()

    def test_redemption_disabled_without_explicit_route_configuration(self):
        from arb_monitor.core import arb_ctf_redemption as ctf
        with mock.patch.object(ctf, "enabled", return_value=False), mock.patch.object(ctf, "tick") as tick:
            self.assertIsNone(arb_settlement.request_venue_redemption(dict(self.POS), "polymarket"))
            tick.assert_not_called()


class BaseModeCompatTests(unittest.TestCase):
    """Live Base V2 path: same formula, exact units, fail-closed on read errors."""

    def setUp(self):
        self.net = F.FakeNet(arb_funder.BASE_RPC)
        self.venue = {"polymarket": 12_345_678, "kalshi": 5_000_000}
        self.fail = set()
        state = abi_encode(["uint256"] * 10 + ["bool", "bool"],
                           [10 ** 6, 0, 0, 0, 0, 0, 0, 0, 1, 3, False, False])
        self.state = {"raw": "0x" + state.hex()}

        def poly(require_configured=False):
            if "poly" in self.fail:
                raise arb_nav.NavReadError("poly_cash", "down")
            return self.venue["polymarket"]
        for p in self.net.patches() + [
            mock.patch.dict(os.environ, F.base_env(), clear=True),
            mock.patch.object(R, "BASE_RPC", arb_funder.BASE_RPC),
            mock.patch.object(R, "USDC_BASE", F.USDC_BASE),
            mock.patch.object(R, "BASE_CHAIN_ID", 8453),
            mock.patch.object(R, "_call_view", side_effect=lambda to, s: self.state["raw"]),
            mock.patch.object(R, "_call_view_uint", return_value=3600),
            mock.patch.object(arb_nav, "_get_poly_cash_raw", side_effect=poly),
            mock.patch.object(arb_nav, "_get_kalshi_cash_raw", return_value=self.venue["kalshi"]),
            mock.patch.object(arb_nav, "_load_value_positions",
                              return_value=[arb_nav.ArbPosition("p", "t", "K", 10, 9.7, 0, "open")]),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.VAULT, 1000.000001)
        self.net.set_token(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, 100)

    def test_formula_and_exact_units(self):
        a = R.compute_reported_assets(F.VAULT)
        self.assertTrue(a["ok"], a["failures"])
        self.assertEqual(a["reported_assets_raw"], 1_000_000_001 + 100 * U + 12_345_678 + 5 * U + 9_700_000)
        self.assertEqual(a["poly_cash"], 12.345678)
        p = R.build_report_payload(F.VAULT, F.TEST_KEY)
        self.assertIsNotNone(p, R.LAST_ABORT_REASON)
        self.assertEqual(p["chain_id"], 8453)
        self.assertEqual(p["domain_salt"], R.DOMAIN_SALT_TEXT)

    def test_read_error_aborts_instead_of_zero(self):
        self.fail.add("poly")
        self.assertIsNone(R.build_report_payload(F.VAULT, F.TEST_KEY))
        self.assertIn("poly_cash", R.LAST_ABORT_REASON)

    def test_paused_v2_vault_not_reported(self):
        state = abi_encode(["uint256"] * 10 + ["bool", "bool"],
                           [10 ** 6, 0, 0, 0, 0, 0, 0, 0, 1, 3, True, False])
        self.state["raw"] = "0x" + state.hex()
        st = R._read_vault_state(F.VAULT)
        self.assertTrue(st["paused"])
        self.assertIsNone(R.build_report_payload(F.VAULT, F.TEST_KEY))
        self.assertIn("paused", R.LAST_ABORT_REASON)

    def test_lenient_display_nav_still_zeroes_on_error(self):
        self.fail.add("poly")
        self.assertEqual(arb_nav._get_servicer_balances()[0], 0.0)


if __name__ == "__main__":
    unittest.main()
