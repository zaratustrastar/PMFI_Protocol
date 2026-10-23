"""Offline fakes for the reporter / NAV tests (no network, no DB, no real keys).

RobinhoodWorld wires one fake "economy" for robinhood mode:
  * chain 4663 (vault RPC = _fakes.RH_RPC): V3 vault getters, USDG balances, eth_blockNumber,
    eth_chainId, report() simulation, receipts — every eth_call's block tag is recorded;
  * Base: servicer USDC (via _fakes.FakeNet);
  * venues: Polymarket / Kalshi cash in raw units (arb_nav strict readers patched);
  * positions: an in-memory list (arb_nav._load_value_positions patched);
  * ledgers: the REAL arb_withdrawals ledger functions on temp files.
Any component can be made to fail with world.fail.add(<name>).
"""

import os
import sqlite3
from unittest import mock

import _fakes as F
from eth_hash.auto import keccak

from arb_monitor.core import arb_funder, arb_nav, arb_reporter, arb_withdrawals

SIGNER = F.Account.create()            # throwaway report signer
U = 1_000_000                          # 1 USDG / USDC in raw units


def sel(sig: str) -> str:
    return keccak(sig.encode())[:4].hex()


def word(v: int) -> str:
    return "0x" + f"{v:064x}"


class FakeChain(F.FakeNet):
    """FakeNet + V3 vault getters, block number, chain id, receipts and failure injection."""

    def __init__(self, base_rpc):
        super().__init__(base_rpc)
        self.block = 1_000
        self.vault = {
            "officialPPS()": 10 ** 6, "lastReportNonce()": 1, "lastReportTimestamp()": 1,
            "reportCooldown()": 3600, "totalPendingRedeemShares()": 0,
            "totalPendingDepositAssets()": 0, "totalClaimableRedeemAssets()": 0,
            "maxPpsMoveBps()": 500, "lastReportedBackingAssets()": 0, "totalSupply()": 0,
            "paused()": 0, "shutdown()": 0,
            "usdc()": int(F.USDG, 16), "reportSigner()": int(SIGNER.address, 16),
        }
        self.fail = set()
        self.call_blocks = []          # (rpc, to, selector, block tag) for every eth_call
        self.report_revert = None      # str -> report() eth_call simulation reverts
        self.receipt_status = 1        # 1 | 0 | None (never mined)
        self._sel = {sel(k): k for k in list(self.vault) + ["idleBalance()"]}

    def vault_balance_raw(self):
        return self.token_bal.get((F.RH_RPC, F.USDG.lower(), F.VAULT.lower()), 0)

    def set_raw(self, rpc, token, wallet, raw):
        self.token_bal[(rpc, token.lower(), wallet.lower())] = int(raw)

    def raw(self, rpc, token, wallet):
        return self.token_bal.get((rpc, token.lower(), wallet.lower()), 0)

    def _rpc(self, rpc, method, params):
        if method == "eth_blockNumber":
            if "blockNumber" in self.fail:
                raise RuntimeError("RPC down (eth_blockNumber)")
            return hex(self.block)
        if method == "eth_chainId":
            return hex(self.chain_of[rpc])
        if method == "eth_getTransactionReceipt":
            if self.receipt_status is None:
                return None
            return {"status": hex(self.receipt_status)}
        if method == "eth_call":
            to, data = params[0]["to"].lower(), params[0]["data"]
            blk = params[1] if len(params) > 1 else None
            self.call_blocks.append((rpc, to, data[2:10], blk))
            if rpc == F.RH_RPC and to == F.VAULT.lower():
                s = data[2:10]
                if s == sel("report((uint256,uint256,uint256,uint256),bytes,uint256,uint256)"):
                    if self.report_revert:
                        raise RuntimeError(f"execution reverted: {self.report_revert}")
                    return "0x"
                name = self._sel.get(s)
                if name is None:
                    raise AssertionError(f"unexpected vault call {data[:10]}")
                if f"getter:{name}" in self.fail:
                    return "0x"            # e.g. getter missing on this contract
                if name == "idleBalance()":
                    reserved = self.vault["totalPendingDepositAssets()"] + self.vault["totalClaimableRedeemAssets()"]
                    return word(max(0, self.vault_balance_raw() - reserved))
                return word(self.vault[name])
            if data.startswith("0x70a08231"):
                wallet = "0x" + data[-40:]
                if f"balance:{rpc}:{wallet}" in self.fail:
                    raise RuntimeError(f"RPC down (balanceOf {wallet})")
        return super()._rpc(rpc, method, params)


class RobinhoodWorld:
    """Robinhood-mode reporter fixture. Call from a TestCase.setUp."""

    def __init__(self, tc, extra_env=None):
        self.tc = tc
        self.net = FakeChain(arb_funder.BASE_RPC)
        self.venue = {"polymarket": 0, "kalshi": 0}
        self.positions = []
        self.bridge_status = {}        # reference -> "pending" | "filled" | "refunded"
        self.fail = self.net.fail
        env = F.rh_env(
            ARB_VAULT_ASSET=F.USDG, ARB_VAULT_RPC_URL=F.RH_RPC, ARB_VAULT_CHAIN_ID="4663",
            ARB_VAULT_DOMAIN_SALT="PMFIArbVaultV3.milord.v1", ARB_VAULT_V2_ADDRESS=F.VAULT,
            ARB_NAV_SIGNER_PRIVATE_KEY=SIGNER.key.hex(), ROBINHOOD_CHAIN_ID="4663",
        )
        env.update(extra_env or {})
        saved = {k: getattr(arb_reporter, k) for k in ("DOMAIN_SALT_TEXT", "BASE_CHAIN_ID", "BASE_RPC", "USDC_BASE")}
        tc.addCleanup(lambda: [setattr(arb_reporter, k, v) for k, v in saved.items()])
        for p in self.net.patches() + [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("time.sleep"),
            mock.patch.object(arb_nav, "_get_poly_cash_raw", side_effect=self._poly),
            mock.patch.object(arb_nav, "_get_kalshi_cash_raw", side_effect=self._kalshi),
            mock.patch.object(arb_nav, "_load_value_positions", side_effect=self._positions),
            # venue balances as the funding side's reconciliation reads them
            mock.patch.object(arb_funder, "_get_platform_balance", side_effect=self._platform_balance),
            mock.patch.object(arb_reporter, "_last_local_report_ts", 0.0),
            mock.patch.object(arb_reporter, "_pending_report_until", 0.0),
            mock.patch.object(arb_reporter, "_last_recall_ts", {}),
        ]:
            p.start()
            tc.addCleanup(p.stop)
        F.temp_ledgers(tc)
        self.net.get_routes["/deposit/status"] = \
            lambda params: {"status": self.bridge_status.get(params["depositTxHash"], "pending")}
        self.net.get_routes["/intents/status/v2"] = \
            lambda params: {"status": {"filled": "success"}.get(self.bridge_status.get(params["requestId"]), "pending")}
        arb_reporter.load_vault_config()

    # -- component fakes ------------------------------------------------------
    def _poly(self, require_configured=False):
        if "poly" in self.fail:
            raise arb_nav.NavReadError("poly_cash", "CLOB API 503")
        return self.venue["polymarket"]

    def _kalshi(self, require_configured=False):
        if "kalshi" in self.fail:
            raise arb_nav.NavReadError("kalshi_cash", "HTTP 500")
        return self.venue["kalshi"]

    def _platform_balance(self, venue, raise_on_error=False):
        return {"polymarket": self.venue["polymarket"], "kalshi": self.venue["kalshi"]}.get(venue, 0) / U

    def _positions(self, strict=True):
        if "positions" in self.fail:
            raise arb_nav.NavReadError("positions", "DB connection refused")
        return list(self.positions)

    # -- balances (raw units) -------------------------------------------------
    def vault(self, raw=None):
        if raw is not None:
            self.net.set_raw(F.RH_RPC, F.USDG, F.VAULT, raw)
        return self.net.raw(F.RH_RPC, F.USDG, F.VAULT)

    def usdg(self, raw=None):
        if raw is not None:
            self.net.set_raw(F.RH_RPC, F.USDG, F.SERVICER, raw)
        return self.net.raw(F.RH_RPC, F.USDG, F.SERVICER)

    def base(self, raw=None):
        if raw is not None:
            self.net.set_raw(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER, raw)
        return self.net.raw(arb_funder.BASE_RPC, F.USDC_BASE, F.SERVICER)

    def add_position(self, pair_id, cost_raw, status="open", venue="", expiry_ts=0):
        self.positions.append(arb_nav.ArbPosition(
            pair_id=pair_id, poly_yes_token="t", kalshi_ticker="K", shares=cost_raw / U,
            cost_basis_usdc=cost_raw / U, expiry_ts=expiry_ts, status=status, resolved_venue=venue))

    def age_withdrawals(self, seconds):
        rows = arb_withdrawals._load_ledger()
        for r in rows:
            r["created_at"] -= seconds
            r["eta_at"] -= seconds
        arb_withdrawals._save_ledger(rows)

    # -- reporter -------------------------------------------------------------
    def assets(self):
        return arb_reporter.compute_reported_assets(F.VAULT)

    def R(self):
        """reportedAssets in raw units; fails the test if any component failed."""
        a = self.assets()
        self.tc.assertTrue(a["ok"], a["failures"])
        return a["reported_assets_raw"]

    def backing(self, reported_raw):
        """What PMFIArbVaultV3.report() turns reportedAssets into (before fees)."""
        d = self.net.vault["totalPendingDepositAssets()"] + self.net.vault["totalClaimableRedeemAssets()"]
        return max(0, reported_raw - d)


# ── DB-less SQL: SQLite behind a psycopg2-shaped connection ─────────────────────
class _Cur:
    def __init__(self, c):
        self._c = c

    def execute(self, sql, params=()):
        sql = sql.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")
        self._c.execute(sql, params)

    def fetchall(self):
        return self._c.fetchall()

    def fetchone(self):
        return self._c.fetchone()

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        pass


class SqliteConn:
    """Shared in-memory DB; close() is a no-op so successive get_db_conn() calls see one DB."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""
            CREATE TABLE arb_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL UNIQUE, poly_yes_token TEXT NOT NULL,
                poly_no_token TEXT DEFAULT '', kalshi_ticker TEXT NOT NULL,
                kalshi_side TEXT NOT NULL DEFAULT 'YES', shares REAL NOT NULL DEFAULT 0,
                cost_basis_usdc REAL NOT NULL DEFAULT 0, expiry_ts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open', settled_pnl_usdc REAL DEFAULT 0,
                opened_at TEXT DEFAULT CURRENT_TIMESTAMP, settled_at TEXT,
                poly_title TEXT DEFAULT '', kalshi_title TEXT DEFAULT '', notes TEXT DEFAULT '',
                poly_entry_price REAL DEFAULT 0, kalshi_entry_price REAL DEFAULT 0,
                poly_side TEXT DEFAULT 'YES', resolved_venue TEXT DEFAULT '',
                resolved_at TEXT, payout_usdc REAL)
        """)

    def cursor(self):
        return _Cur(self.db.cursor())

    def commit(self):
        self.db.commit()

    def close(self):
        pass

    def rows(self):
        return self.db.execute(
            "SELECT pair_id, status, shares, cost_basis_usdc, settled_pnl_usdc FROM arb_positions ORDER BY id"
        ).fetchall()
