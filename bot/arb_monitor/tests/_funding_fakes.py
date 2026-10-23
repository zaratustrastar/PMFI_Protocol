"""Offline fakes for the funding-safety tests (extends _fakes.FakeNet; does not modify it).

ReceiptNet adds eth_getTransactionReceipt (scriptable success / revert / missing), separate
"latest" vs "pending" nonces, and a revert hook. Nothing touches the network; signing uses the
throwaway key from _fakes.
"""

import os
import unittest
from unittest import mock

import _fakes as F
from arb_monitor.core import arb_funder, arb_withdrawals


ALLOWLIST = f"4663:{F.SPOKE},4663:{F.RELAY_DEPOSITORY},8453:{F.SPOKE},8453:{F.RELAY_DEPOSITORY}"


class Crash(BaseException):
    """Simulated process death (bypasses `except Exception`)."""


class ReceiptNet(F.FakeNet):
    def __init__(self, base_rpc):
        super().__init__(base_rpc)
        self.receipts = {}            # tx hash -> 1 | 0 | None (None = no receipt yet)
        self.default_receipt = 1
        self.revert_if = None         # callable(tx) -> bool: the tx's receipt will be status 0
        self.latest_nonce = {}        # (rpc, wallet_lower) -> int; defaults to the pending nonce
        self.hash_of_index = []

    def _rpc(self, rpc, method, params):
        if method == "eth_getTransactionReceipt":
            self.rpc_calls.append((rpc, method, params))
            st = self.receipts.get(params[0], self.default_receipt)
            return None if st is None else {"status": hex(st), "transactionHash": params[0]}
        if method == "eth_getTransactionCount":
            self.rpc_calls.append((rpc, method, params))
            key = (rpc, params[0].lower())
            pending = self.nonces.get(key, 0)
            if params[1] == "latest":
                return hex(self.latest_nonce.get(key, pending))
            return hex(pending)
        result = super()._rpc(rpc, method, params)
        if method == "eth_sendRawTransaction":
            tx = self.sent[-1]
            if self.revert_if and self.revert_if(tx):
                self.receipts[result] = 0
            self.hash_of_index.append(result)
        return result


class FundingCase(unittest.TestCase):
    """Robinhood env + allowlist + venue deposit declarations + temp ledgers + ReceiptNet."""

    def env(self):
        return F.rh_env(
            ARB_BRIDGE_ALLOWLIST=ALLOWLIST,
            POLY_RH_DEPOSIT_CHAIN_ID="4663", POLY_RH_DEPOSIT_TOKEN=F.USDG,
            KALSHI_BASE_DEPOSIT_CHAIN_ID="8453", KALSHI_BASE_DEPOSIT_TOKEN=F.USDC_BASE,
            KALSHI_BASE_DEPOSIT_ADDR=F.KALSHI_ADDR,
            ARB_TX_RECEIPT_TIMEOUT_SECS="3", ARB_TX_RECEIPT_POLL_SECS="1",
        )

    def setUp(self):
        self.net = ReceiptNet(arb_funder.BASE_RPC)
        for p in self.net.patches() + [
            mock.patch.dict(os.environ, self.env(), clear=True),
            mock.patch("time.sleep"),
            mock.patch.object(arb_funder, "ARB_DEPOSIT_WAIT_SECS", 0),
            mock.patch.object(arb_funder, "POLY_BASE_DEPOSIT_ADDR", F.POLY_BASE_ADDR),
            mock.patch.object(arb_funder, "KALSHI_BASE_DEPOSIT_ADDR", F.KALSHI_ADDR),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.ledger_dir = F.temp_ledgers(self)
        self.net.set_eth(F.RH_RPC, F.SERVICER, 1.0)
        self.net.set_eth(arb_funder.BASE_RPC, F.SERVICER, 1.0)
        self.platform = {"polymarket": [0.0], "kalshi": [0.0], "opinion": [0.0]}

    # platform balances (scripted, last value repeats)
    def script_platform(self, venue, *values):
        self.platform[venue] = list(values)

    def _platform_balance(self, venue, raise_on_error=False):
        vals = self.platform[venue]
        return vals.pop(0) if len(vals) > 1 else vals[0]

    def patch_platform(self):
        p = mock.patch.object(arb_funder, "_get_platform_balance", side_effect=self._platform_balance)
        p.start()
        self.addCleanup(p.stop)

    def across_quote(self, approve_spender=F.SPOKE, approve_amount=None, swap_to=F.SPOKE,
                     value="0", fee_raw=50_000, extra=None, input_token=F.USDG):
        def quote(params):
            amt = int(params["amount"])
            q = {
                "approvalTxns": [{"to": input_token, "data": F.approve_data(
                    approve_spender, approve_amount if approve_amount is not None else amt + fee_raw)}],
                "swapTx": {"to": swap_to, "data": "0xdeadbeef", "value": value},
                "maxInputAmount": str(amt + fee_raw),
                "minOutputAmount": str(amt - fee_raw),
                "expectedOutputAmount": str(amt),
            }
            q.update(extra or {})
            return q
        self.net.get_routes["/swap/approval"] = quote

    def relay_refuses(self):
        def refuse(body):
            raise AssertionError("relay must not be reached in this test")
        self.net.post_routes["/quote"] = lambda body: {"steps": []}   # no executable steps → rejected

    def bridge_rows(self):
        return arb_withdrawals._load_ledger(arb_withdrawals.BRIDGE_LEDGER_PATH)

    def withdrawal_rows(self):
        return arb_withdrawals._load_ledger()
