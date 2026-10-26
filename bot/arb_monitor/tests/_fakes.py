"""Shared offline fakes for the arb_monitor funding tests.

Every JSON-RPC and HTTP call goes through FakeNet (patched over requests.post / requests.get),
so nothing touches the network. Transactions are signed locally with a throwaway key created
per test run and decoded back from the raw bytes; no real keys, no real funds.
"""

import os
import sys
import tempfile
from unittest import mock

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

import rlp  # noqa: E402  (dependency of eth_account)
from eth_account import Account  # noqa: E402

RH_RPC = "http://robinhood.rpc.test"
RH_CHAIN = 4663
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POLY_RH_ADDR = "0x1111111111111111111111111111111111111111"
POLY_BASE_ADDR = "0x2222222222222222222222222222222222222222"
KALSHI_ADDR = "0x3333333333333333333333333333333333333333"
SPOKE = "0x4444444444444444444444444444444444444444"
RELAY_DEPOSITORY = "0x5555555555555555555555555555555555555555"
VAULT = "0x6666666666666666666666666666666666666666"
ACROSS_URL = "http://across.test/api"
RELAY_URL = "http://relay.test"

TEST_ACCOUNT = Account.create()          # throwaway, generated per run
TEST_KEY = TEST_ACCOUNT.key.hex()
SERVICER = TEST_ACCOUNT.address


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def pad_addr(a: str) -> str:
    return "000000000000000000000000" + a.lower().replace("0x", "")


def pad_amt(raw: int) -> str:
    return hex(raw)[2:].zfill(64)


class FakeNet:
    """Fake RPC nodes (keyed by URL) plus scripted HTTP API responses."""

    def __init__(self, base_rpc: str):
        self.base_rpc = base_rpc
        self.chain_of = {base_rpc: 8453, RH_RPC: RH_CHAIN}
        self.token_bal = {}       # (rpc, token_lower, wallet_lower) -> raw int
        self.eth_bal = {}         # (rpc, wallet_lower) -> wei
        self.nonces = {}
        self.sent = []            # decoded txs, in broadcast order
        self.rpc_calls = []       # (rpc, method, params)
        self.http_calls = []      # (verb, url, params/json)
        self.get_routes = {}      # url suffix -> callable(params) -> payload | raises
        self.post_routes = {}
        self.on_send = None       # hook(tx) after a tx is broadcast

    # -- state helpers ------------------------------------------------------
    def set_token(self, rpc, token, wallet, amount_usdc):
        self.token_bal[(rpc, token.lower(), wallet.lower())] = int(round(amount_usdc * 1_000_000))

    def token(self, rpc, token, wallet):
        return self.token_bal.get((rpc, token.lower(), wallet.lower()), 0) / 1_000_000

    def set_eth(self, rpc, wallet, eth):
        self.eth_bal[(rpc, wallet.lower())] = int(eth * 1e18)

    # -- transport ----------------------------------------------------------
    def post(self, url, json=None, timeout=None, **kw):
        if json and json.get("jsonrpc"):
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": self._rpc(url, json["method"], json["params"])})
        self.http_calls.append(("POST", url, json))
        for suffix, fn in self.post_routes.items():
            if url.endswith(suffix):
                return FakeResponse(fn(json))
        raise AssertionError(f"unexpected HTTP POST {url}")

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.http_calls.append(("GET", url, params))
        for suffix, fn in self.get_routes.items():
            if url.endswith(suffix):
                return FakeResponse(fn(params))
        raise AssertionError(f"unexpected HTTP GET {url}")

    def _rpc(self, rpc, method, params):
        if rpc not in self.chain_of:
            raise AssertionError(f"RPC to unknown node {rpc!r}")
        self.rpc_calls.append((rpc, method, params))
        if method == "eth_call":
            to, data = params[0]["to"].lower(), params[0]["data"]
            if data.startswith("0x70a08231"):
                wallet = "0x" + data[-40:]
                return hex(self.token_bal.get((rpc, to, wallet), 0))
            return "0x0"   # allowance(), lastTendTimestamp(), tendCooldown() ...
        if method == "eth_getBalance":
            return hex(self.eth_bal.get((rpc, params[0].lower()), 0))
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get((rpc, params[0].lower()), 0))
        if method == "eth_gasPrice":
            return hex(1_000_000)
        if method == "eth_estimateGas":
            return hex(60_000)
        if method == "eth_sendRawTransaction":
            tx = self._decode(params[0])
            tx["rpc"] = rpc
            if tx["chain_id"] != self.chain_of[rpc]:
                raise AssertionError(f"tx chainId {tx['chain_id']} sent to {rpc}")
            self.sent.append(tx)
            if self.on_send:
                self.on_send(tx)
            return "0x" + f"{len(self.sent):064x}"
        raise AssertionError(f"unexpected RPC {method}")

    @staticmethod
    def _decode(raw_hex: str) -> dict:
        nonce, gas_price, gas, to, value, data, v, r, s = rlp.decode(bytes.fromhex(raw_hex[2:]))
        v = int.from_bytes(v, "big")
        return {
            "nonce": int.from_bytes(nonce, "big"),
            "to": "0x" + to.hex(),
            "value": int.from_bytes(value, "big"),
            "data": "0x" + data.hex(),
            "chain_id": (v - 35) // 2,
        }

    def patches(self):
        return [mock.patch("requests.post", side_effect=self.post),
                mock.patch("requests.get", side_effect=self.get)]


def erc20_transfer_data(to: str, amount_raw: int) -> str:
    return "0xa9059cbb" + pad_addr(to) + pad_amt(amount_raw)


def approve_data(spender: str, amount_raw: int) -> str:
    return "0x095ea7b3" + pad_addr(spender) + pad_amt(amount_raw)


def temp_ledgers(testcase):
    """Point both ledgers at a temp dir for the duration of a test."""
    from arb_monitor.core import arb_withdrawals
    import shutil
    d = tempfile.mkdtemp(prefix="arb_ledger_test_")
    testcase.addCleanup(shutil.rmtree, d, True)
    for attr, name in (("WITHDRAWAL_LEDGER_PATH", "w.json"), ("BRIDGE_LEDGER_PATH", "b.json")):
        p = mock.patch.object(arb_withdrawals, attr, os.path.join(d, name))
        p.start()
        testcase.addCleanup(p.stop)
    return d


def base_env(**extra):
    env = {
        "POLY_PRIVATE_KEY": TEST_KEY,
        "ARB_SERVICER_WALLET": SERVICER,
        "POLY_RH_DEPOSIT_ADDR": POLY_RH_ADDR,
        "ACROSS_API_URL": ACROSS_URL,
        "RELAY_API_URL": RELAY_URL,
    }
    env.update(extra)
    return env


def rh_env(**extra):
    extra.setdefault("ARB_EXECUTION_MODE", "live")  # these tests exercise the live money paths
    return base_env(ARB_FUNDING_CHAIN="robinhood", ROBINHOOD_RPC_URL=RH_RPC, **extra)
