"""Execution-loop / executor gates: Opinion filtered, funding exception aborts the trade,
liquidity not-ok blocks new trades in robinhood mode (base mode unchanged)."""

import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import _fakes as F  # noqa: F401  (sys.path setup)
from arb_monitor.core import arb_execution_loop as EL
from arb_monitor.core import arb_executor as EX
from arb_monitor.core import arb_funder
from arb_monitor.core.arb_liquidity import LiquidityState


def opp(venue2="kalshi", pair_id="p1"):
    return SimpleNamespace(
        pair_id=pair_id, venue2=venue2, is_display_only=False, opinion_market_id="77", outcome_key="yes",
        poly_yes_token="0xyes", poly_no_token="0xno", kalshi_side="NO", kalshi_ticker="KX-1",
        kalshi_title="t", poly_title="t", kalshi_yes_ask=0.5, fetched_at=time.time(),
        net_edge_pct=9.0, gross_edge_pct=9.0, score=100.0, days_to_expiry=10.0, expiry_ts=0,
        fillable_size_usdc=50.0, annualized_return=1.0, confidence=1.0, label="",
    )


class OpinionFilterTests(unittest.TestCase):
    def test_robinhood_mode_drops_opinion_even_if_flag_on(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood", "ARB_OPINION_ENABLED": "true"}, clear=True):
            kept = EL._filter_opinion([opp("opinion", "a"), opp("kalshi", "b")])
        self.assertEqual([o.pair_id for o in kept], ["b"])

    def test_base_mode_with_opinion_enabled_unchanged(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "base"}, clear=True):
            ops = [opp("opinion", "a"), opp("kalshi", "b")]
            self.assertEqual(EL._filter_opinion(ops), ops)

    def test_cycle_never_prewarms_or_executes_opinion_in_robinhood_mode(self):
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}, clear=True), \
             mock.patch.object(EL, "_STARTUP_ERRORS", []), \
             mock.patch.object(EL, "run_startup_checks", return_value=[]), \
             mock.patch.object(EL, "run_reporter_tick"), mock.patch.object(EL, "run_tend_tick"), \
             mock.patch.object(EL, "_get_liquidity_state", return_value=LiquidityState(ok=True, deployable_capital=100)), \
             mock.patch.object(EL, "get_servicer_deployable_usdc", return_value=100.0), \
             mock.patch.object(EL, "_get_platform_balance", return_value=0.0), \
             mock.patch.object(EL, "fetch_opportunities", return_value=[opp("opinion", "a")]), \
             mock.patch("arb_monitor.core.arb_executor._opinion_resolve_tokens") as prewarm, \
             mock.patch.object(EL, "execute_arb") as ex:
            EL._execution_cycle()
        prewarm.assert_not_called()
        ex.assert_not_called()


class LiquidityGateTests(unittest.TestCase):
    def _cycle(self, env, liq):
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(EL, "_STARTUP_ERRORS", []), \
             mock.patch.object(EL, "run_startup_checks", return_value=[]), \
             mock.patch.object(EL, "run_reporter_tick"), \
             mock.patch.object(EL, "run_tend_tick") as tend, \
             mock.patch.object(EL, "_get_liquidity_state", return_value=liq), \
             mock.patch.object(EL, "get_servicer_deployable_usdc", return_value=100.0), \
             mock.patch.object(EL, "_get_platform_balance", return_value=0.0), \
             mock.patch.object(EL, "fetch_opportunities", return_value=[]) as fetch:
            EL._execution_cycle()
        return fetch.called, tend.called

    RH = {"ARB_FUNDING_CHAIN": "robinhood"}
    BASE = {"ARB_FUNDING_CHAIN": "base"}

    def test_robinhood_blocks_when_not_ok(self):
        fetched, tended = self._cycle(self.RH, LiquidityState(ok=False))
        self.assertFalse(fetched)
        self.assertTrue(tended)          # tend still runs so vault idle keeps flowing

    def test_robinhood_blocks_when_state_unavailable(self):
        self.assertFalse(self._cycle(self.RH, None)[0])

    def test_robinhood_blocks_on_falsy_trading_allowed(self):
        liq = LiquidityState(ok=True)
        liq.trading_allowed = False
        liq.trading_block_reason = "nav_report_stale"
        self.assertFalse(self._cycle(self.RH, liq)[0])
        self.assertFalse(self._cycle(self.RH, {"ok": True, "trading_allowed": False})[0])

    def test_robinhood_allows_ok_state_with_or_without_field(self):
        self.assertTrue(self._cycle(self.RH, LiquidityState(ok=True))[0])
        liq = LiquidityState(ok=True)
        liq.trading_allowed = True
        self.assertTrue(self._cycle(self.RH, liq)[0])

    def test_robinhood_blocks_when_startup_checks_fail(self):
        with mock.patch.dict(os.environ, self.RH, clear=True), \
             mock.patch.object(EL, "_STARTUP_ERRORS", ["ARB_BRIDGE_ALLOWLIST is empty"]), \
             mock.patch.object(EL, "run_startup_checks", return_value=["ARB_BRIDGE_ALLOWLIST is empty"]), \
             mock.patch.object(EL, "run_reporter_tick"), mock.patch.object(EL, "run_tend_tick"), \
             mock.patch.object(EL, "_get_liquidity_state", return_value=LiquidityState(ok=True)), \
             mock.patch.object(EL, "fetch_opportunities", return_value=[]) as fetch:
            EL._execution_cycle()
        fetch.assert_not_called()

    def test_base_mode_unchanged_when_not_ok(self):
        # legacy behaviour: unknown liquidity → trading continues
        self.assertTrue(self._cycle(self.BASE, LiquidityState(ok=False))[0])
        self.assertTrue(self._cycle(self.BASE, None)[0])


class FundingExceptionAbortsTradeTests(unittest.TestCase):
    def test_trade_aborted_when_funder_raises(self):
        book = object()
        o = opp("kalshi")
        with mock.patch.dict(os.environ, {"ARB_FUNDING_CHAIN": "robinhood"}, clear=True), \
             mock.patch.object(EX, "kalshi_resolve_market_ticker", return_value="KX-1-M"), \
             mock.patch.object(EX, "poly_fetch_orderbook", return_value=book), \
             mock.patch.object(EX, "kalshi_fetch_orderbook_depth", return_value=book), \
             mock.patch.object(EX, "poly_extract_asks", return_value=[(0.40, 1000.0)]), \
             mock.patch.object(EX, "kalshi_extract_asks", return_value=[(0.50, 1000.0)]), \
             mock.patch.object(EX, "kalshi_get_best_prices", return_value={"no_best_ask": 0.50, "yes_best_ask": 0.5}), \
             mock.patch.object(arb_funder, "get_platform_spot_balances", return_value=(1000.0, 1000.0)), \
             mock.patch.object(arb_funder, "get_servicer_deployable_usdc", return_value=1000.0), \
             mock.patch.object(arb_funder, "fund_both_legs_for_trade", side_effect=RuntimeError("rpc exploded")), \
             mock.patch.object(EX, "_place_poly_order") as p1, \
             mock.patch.object(EX, "_place_kalshi_order") as p2:
            res = EX.execute_arb(o, size_usdc=20.0)
        self.assertFalse(res.success)
        self.assertTrue(res.error.startswith("funding_failed"), res.error)
        p1.assert_not_called()
        p2.assert_not_called()
        # "funding_failed" is NOT a pre-funding error: the loop stops the cycle and cools the pair
        self.assertFalse(res.error.startswith(("funding_insufficient",)))


if __name__ == "__main__":
    unittest.main()
