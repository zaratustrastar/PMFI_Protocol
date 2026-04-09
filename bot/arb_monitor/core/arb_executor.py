"""Arb Executor - places both legs of an arbitrage trade with live price verification.

Security principles:
- Live orderbook re-check at execution (never stale/cached prices)
- Leg atomicity guard: verify Bopinion_clob.py  OTH orderbooks before placing EITHER order
- Auto-unwind: if leg 2 (Kalshi) fails, immediately cancel/sell leg 1 (Polymarket)
- Slippage guard: reject if live ask is more than 50 bps worse than Oddpool quote
- MIN_EDGE_PCT (default 2.5%) ensures fees are covered before any trade is placed
"""

import time
import os
import math
from typing import Optional

# ── Route Polymarket CLOB through residential proxy (bypasses geoblock) ──────
# py_clob_client calls requests.request() directly (no Session, no proxies arg).
# Setting env vars (HTTPS_PROXY) is NOT reliable inside systemd-managed processes.
# Instead we monkey-patch py_clob_client.http_helpers.helpers.request directly so
# every call made by ClobClient.post_order / get / post automatically goes through
# the proxy — no env-var dependency whatsoever.
#
# Kalshi has dedicated direct-request calls in this file that use _KALSHI_SESSION
# (trust_env=False) so they are never routed through the proxy.
import re as _re
import requests as _requests_mod

_poly_clob_proxy = (
    os.environ.get("POLY_CLOB_PROXY_URL")
    or os.environ.get("PROXY_URL", "")
)
_proxy_display = (
    _re.sub(r"//[^@]+@", "//<redacted>@", _poly_clob_proxy)
    if _poly_clob_proxy else ""
)

if _poly_clob_proxy:
    # Monkey-patch py_clob_client's internal request function to inject proxies.
    # post() and get() in helpers.py resolve "request" via the module namespace,
    # so replacing helpers.request here redirects ALL py_clob_client HTTP calls.
    import py_clob_client.http_helpers.helpers as _pch_helpers
    from py_clob_client.exceptions import PolyApiException as _PolyApiException

    _pch_proxies = {"https": _poly_clob_proxy, "http": _poly_clob_proxy}
    _pch_orig_request = _pch_helpers.request  # keep reference for debugging

    def _pch_proxied_request(endpoint, method, headers=None, data=None):
        try:
            headers = _pch_helpers.overloadHeaders(method, headers)
            resp = _requests_mod.request(
                method=method,
                url=endpoint,
                headers=headers,
                json=data if data else None,
                proxies=_pch_proxies,
            )
            if resp.status_code != 200:
                raise _PolyApiException(resp)
            try:
                return resp.json()
            except _requests_mod.exceptions.JSONDecodeError:
                return resp.text
        except _requests_mod.exceptions.RequestException:
            raise _PolyApiException(error_msg="Request exception!")

    _pch_helpers.request = _pch_proxied_request
    print(f"⚡ [Arb/Executor] 🌐 Poly CLOB proxy ACTIVE (monkey-patched): {_proxy_display}", flush=True)
else:
    print("⚡ [Arb/Executor] ⚠️ No Poly CLOB proxy configured (PROXY_URL / POLY_CLOB_PROXY_URL)", flush=True)

# Kalshi HTTP calls in this file use a dedicated session with trust_env=False
# to ensure they always go direct and are never affected by any proxy settings.
_KALSHI_SESSION = _requests_mod.Session()
_KALSHI_SESSION.trust_env = False

from ..adapters.polymarket import (
    get_best_prices as poly_get_best_prices,
    fetch_orderbook as poly_fetch_orderbook,
    extract_asks as poly_extract_asks,
)
from ..adapters.kalshi import (
    get_best_prices as kalshi_get_best_prices,
    fetch_orderbook_depth as kalshi_fetch_orderbook_depth,
    resolve_market_ticker as kalshi_resolve_market_ticker,
    extract_asks as kalshi_extract_asks,
)
from ..adapters.opinion import (
    fetch_orderbook as opinion_fetch_orderbook,
    extract_asks as opinion_extract_asks,
)
from ..adapters.oddpool import ArbOpportunity
from ..config import (
    ARB_MIN_EDGE_PCT,
    ARB_MAX_PAIR_USDC,
    ARB_VWAP_SAFETY_BUFFER_PCT,
    ARB_MIN_CONTRACTS,
    ARB_POLY_FEE_PCT,
    ARB_KALSHI_FEE_PCT,
    ARB_OPINION_FEE_PCT,
)


def log(msg: str):
    print(f"⚡ [Arb/Executor] {msg}")


def _get_db_conn():
    """Get a database connection for logging executions."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(database_url)
    except Exception as e:
        log(f"⚠️ DB connection error: {e}")
        return None


def log_execution_to_db(
    pair_id: str,
    leg: int,
    venue: str,
    side: str,
    price: float,
    size: float,
    success: bool,
    error: str = "",
    order_id: str = "",
):
    """Log a per-leg execution event to arb_executions table."""
    conn = _get_db_conn()
    if not conn:
        log(f"⚠️ Cannot log execution (no DB): leg={leg} venue={venue} side={side} price={price} success={success}")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO arb_executions
                (pair_id, leg, venue, side, price, size, success, error, order_id, executed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (pair_id, leg, venue, side, price, size, success, error, order_id))
        conn.commit()
        cur.close()
    except Exception as e:
        log(f"⚠️ DB log error: {e}")
    finally:
        conn.close()


def _place_poly_order(
    token_id: str,
    side: str,
    price: float,
    contract_count: int,
) -> tuple[bool, str, str]:
    """Place a Polymarket CLOB order (FOK — Fill or Kill).

    Returns (success, order_id, error_message).
    Accepts contract_count directly — avoids float/int drift from re-deriving
    via size_usdc/price inside this function.
    """
    log(f"📤 [POLY] Placing {side} order: token={token_id[:16]}... price={price} contracts={contract_count}")

    poly_private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not poly_private_key:
        err = "POLY_PRIVATE_KEY not set — cannot place real Polymarket order"
        log(f"❌ [POLY] {err}")
        return False, "", err

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import (
            ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
        )
    except ImportError:
        err = "py_clob_client not installed — cannot place Polymarket orders"
        log(f"❌ [POLY] {err}")
        return False, "", err

    try:
        clob_url            = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id            = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None

        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds = ApiCreds(
                api_key=poly_api_key,
                api_secret=poly_api_secret,
                api_passphrase=poly_api_passphrase,
            )
            client = ClobClient(
                clob_url, key=poly_private_key, chain_id=chain_id, creds=creds,
                signature_type=2, funder=poly_proxy_address,
            )
            log(f"🔑 [POLY] Using L2-authenticated ClobClient (sig_type=2/GNOSIS_SAFE, funder={poly_proxy_address})")
        else:
            client = ClobClient(
                clob_url, key=poly_private_key, chain_id=chain_id,
                signature_type=2, funder=poly_proxy_address,
            )
            log(f"🔑 [POLY] Using L1-only ClobClient (sig_type=2/GNOSIS_SAFE, funder={poly_proxy_address})")

        tick_size = client.get_tick_size(token_id)
        neg_risk  = client.get_neg_risk(token_id)

        # Polymarket enforces 2-decimal maker price. Ceiling-round so the limit
        # sits at or above all asks — FOK sweeps levels ≤ px at their actual prices.
        # e.g. 0.374 → 0.38 (fills at 0.373, 0.374 etc; limit doesn't raise cost).
        px       = math.ceil(float(price) * 100) / 100
        size_val = int(contract_count)

        log(
            f"📤 [POLY] Submitting order: side={side} contracts={contract_count} "
            f"price={px} size={size_val} tick_size={tick_size} neg_risk={neg_risk}"
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=px,
            size=size_val,
            side=side,
        )
        signed_order = client.create_order(
            order_args,
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
        )
        log("🧾 [POLY] Signed order built — posting FOK")

        # Retry the POST on transient network errors (status_code=None / "Request exception").
        # The signed_order is already built — safe to reuse across retries.
        # Non-network errors (order rejections with an actual HTTP status) are not retried.
        _max_attempts = 3
        _retry_delay  = 2.0  # seconds between retries
        for _attempt in range(1, _max_attempts + 1):
            try:
                resp = client.post_order(signed_order, OrderType.FOK)
                log(f"📡 [POLY] post_order response (attempt {_attempt}): {resp}")
                break  # success — exit retry loop
            except Exception as _post_exc:
                _post_err = str(_post_exc)
                if "Request exception" in _post_err and _attempt < _max_attempts:
                    log(
                        f"⚠️ [POLY] Network error on attempt {_attempt}/{_max_attempts} "
                        f"— retrying in {_retry_delay}s: {_post_err}"
                    )
                    time.sleep(_retry_delay)
                    continue
                # Non-retriable error or final attempt exhausted
                log(f"❌ [POLY] Order error (attempt {_attempt}/{_max_attempts}): {_post_err}")
                return False, "", _post_err
        else:
            # Should not reach here (break exits the loop on success), but guard anyway
            return False, "", "poly_post_order: all retries exhausted"

        order_id = resp.get("orderID", "") or resp.get("orderId", "")
        if resp.get("status") in ("matched", "filled", "live"):
            log(f"✅ [POLY] Order accepted: orderId={order_id} status={resp.get('status')}")
            return True, order_id, ""
        else:
            err = f"Order status={resp.get('status')} errorMsg={resp.get('errorMsg', '')}"
            log(f"❌ [POLY] {err}")
            return False, "", err
    except Exception as e:
        err = str(e)
        log(f"❌ [POLY] Order error: {err}")
        return False, "", err


def _place_kalshi_order(
    ticker: str,
    side: str,
    price: float,
    size_usdc: float,
    contract_count: Optional[int] = None,
) -> tuple[bool, str, str]:
    """Place a Kalshi order.

    Args:
        contract_count: Exact integer contract count to place. When provided, this is
            used directly (avoids float/int precision drift from re-deriving via size_usdc/price).
            When None, falls back to int(size_usdc / price).

    Returns (success, order_id, error_message).
    Fails hard if KALSHI_API_KEY is missing.
    Never simulates success: a missing credential is an error, not a fake fill.
    """
    log(f"📤 [KALSHI] Placing {side} order: ticker={ticker} price={price} size_usdc={size_usdc}")

    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        err = "Kalshi credentials not configured — set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
        log(f"❌ [KALSHI] {err}")
        return False, "", err

    try:
        from ..config import KALSHI_BASE_URL
        url = f"{KALSHI_BASE_URL}/portfolio/orders"
        headers = get_kalshi_headers("POST", url)
        if not headers:
            err = "Kalshi RSA signing failed — check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
            log(f"❌ [KALSHI] {err}")
            return False, "", err
        # Use the explicit contract_count when provided to avoid float/int precision drift
        if contract_count is not None:
            contracts = int(contract_count)
        else:
            contracts = int(size_usdc / price) if price > 0 else 0
        payload = {
            "ticker": ticker,
            "client_order_id": f"arb_{int(time.time())}",
            "type": "limit",
            "action": "buy",
            "side": side.lower(),
            "count": contracts,
            "yes_price": int(price * 100) if side == "YES" else None,
            "no_price": int(price * 100) if side == "NO" else None,
            "expiration_ts": int(time.time()) + 30,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        # Use _KALSHI_SESSION (trust_env=False) to bypass proxy — Kalshi must be direct
        resp = _KALSHI_SESSION.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            data = resp.json()
            order_id = data.get("order", {}).get("order_id", "")
            if not order_id:
                err = "no order_id in Kalshi placement response"
                log(f"❌ [KALSHI] {err}")
                return False, "", err
            log(f"🔍 [KALSHI] Order accepted (id={order_id}), polling for fill confirmation...")
            # HTTP 200 only means the order was queued — it may sit in the book
            # unfilled until its 30 s TTL expires.  Poll to confirm actual fill.
            poll_url = f"{KALSHI_BASE_URL}/portfolio/orders/{order_id}"
            for poll_attempt in range(5):
                time.sleep(3)
                try:
                    poll_headers = get_kalshi_headers("GET", poll_url)
                    if not poll_headers:
                        log(f"⚠️ [KALSHI] Auth failed during fill poll #{poll_attempt + 1}")
                        continue
                    pr = _KALSHI_SESSION.get(poll_url, headers=poll_headers, timeout=8)
                    if pr.status_code != 200:
                        log(f"⚠️ [KALSHI] Fill poll HTTP {pr.status_code} on attempt #{poll_attempt + 1}")
                        continue
                    pdata = pr.json()
                    order_obj   = pdata.get("order") or pdata
                    status      = order_obj.get("status", "")
                    qty_matched = int(order_obj.get("quantity_matched") or 0)
                    log(
                        f"🔄 [KALSHI] Fill poll #{poll_attempt + 1}: "
                        f"status={status!r} qty_matched={qty_matched} target={contracts}"
                    )
                    if status in ("executed", "filled", "matched") or (
                        qty_matched >= contracts > 0
                    ):
                        log(f"✅ [KALSHI] Order confirmed filled: orderId={order_id}")
                        return True, order_id, ""
                    if status in ("cancelled", "canceled", "expired"):
                        log(f"⚠️ [KALSHI] Order {status!r} before fill — triggering unwind")
                        return False, order_id, f"kalshi_order_{status}: orderId={order_id}"
                except Exception as pe:
                    log(f"⚠️ [KALSHI] Fill poll error #{poll_attempt + 1}: {pe}")
            # Poll window exhausted (15 s).  Cancel whatever remains and fail so
            # the caller can unwind the already-placed Polymarket leg.
            log(f"⚠️ [KALSHI] Order {order_id!r} did not fill in 15 s — cancelling and failing")
            try:
                del_headers = get_kalshi_headers("DELETE", poll_url)
                if del_headers:
                    _KALSHI_SESSION.delete(poll_url, headers=del_headers, timeout=8)
                    log(f"🗑️ [KALSHI] Cancellation sent for unfilled order {order_id!r}")
            except Exception as ce:
                log(f"⚠️ [KALSHI] Cancel attempt failed: {ce}")
            return False, order_id, f"kalshi_order_unfilled: orderId={order_id} timeout after 15s"
        else:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            log(f"❌ [KALSHI] {err}")
            return False, "", err
    except Exception as e:
        err = str(e)
        log(f"❌ [KALSHI] Order error: {err}")
        return False, "", err


def _unwind_poly_leg(
    order_id: str,
    token_id: str,
    filled_size_usdc: float,
    filled_price: float,
) -> bool:
    """Auto-unwind leg 1 after leg 2 failure.

    Strategy:
    1. First try to cancel the open order (if still pending).
    2. If order is already filled (FOK returns filled immediately), place an
       offsetting SELL at market (best bid) to close the position and recover USDC.
    Returns True if unwind succeeded, False if manual intervention is required.
    """
    log(f"🔄 [POLY] Auto-unwind for order={order_id} token={token_id[:16]}...")

    poly_private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not poly_private_key:
        log("⚠️ POLY_PRIVATE_KEY not set — cannot unwind. Manual intervention needed.")
        return False

    def _make_poly_client_for_unwind():
        """Build a fully-authenticated ClobClient for unwind operations."""
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        clob_url            = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id            = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds = ApiCreds(
                api_key=poly_api_key,
                api_secret=poly_api_secret,
                api_passphrase=poly_api_passphrase,
            )
            log(f"🔑 [POLY/UNWIND] Using L2-authenticated ClobClient (funder={poly_proxy_address})")
            return ClobClient(
                clob_url, key=poly_private_key, chain_id=chain_id,
                creds=creds, signature_type=2, funder=poly_proxy_address,
            )
        log("⚠️ [POLY/UNWIND] L2 API credentials missing — cancel/sell may fail auth")
        return ClobClient(
            clob_url, key=poly_private_key, chain_id=chain_id,
            signature_type=2, funder=poly_proxy_address,
        )

    cancel_ok = False
    try:
        client = _make_poly_client_for_unwind()
        resp = client.cancel(order_id=order_id)
        log(f"✅ [POLY] Cancel response: {resp}")
        # cancel() returns {"canceled": [...], "not_canceled": {...}}
        # An order that already matched appears in not_canceled — do NOT treat that as success.
        canceled_ids = resp.get("canceled") or [] if isinstance(resp, dict) else []
        not_canceled = resp.get("not_canceled") or {} if isinstance(resp, dict) else {}
        if order_id in canceled_ids:
            cancel_ok = True
            log(f"✅ [POLY] Order {order_id} successfully canceled")
        elif order_id in not_canceled:
            reason = not_canceled[order_id]
            log(f"⚠️ [POLY] Order {order_id} NOT canceled: {reason!r} — will attempt offset sell")
        else:
            # Unexpected response shape — assume cancel worked if no error
            cancel_ok = True
            log(f"⚠️ [POLY] Unexpected cancel response shape; assuming canceled: {resp}")
    except Exception as e:
        log(f"⚠️ [POLY] Cancel failed (order may already be filled): {e}")

    if cancel_ok:
        return True

    # Cancel failed → order was likely already filled (FOK); place offsetting SELL
    if not token_id or filled_size_usdc <= 0 or filled_price <= 0:
        log("❌ [POLY] Cannot offset: missing token_id or fill data. Manual intervention needed.")
        return False

    log(f"📤 [POLY] Placing offset SELL to unwind filled position: token={token_id[:16]}... size_usdc={filled_size_usdc}")
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType

        client = _make_poly_client_for_unwind()

        from ..adapters.polymarket import get_best_prices as poly_prices_fn
        prices = poly_prices_fn(token_id)
        sell_price = prices.get("best_bid")
        if sell_price is None or sell_price <= 0:
            sell_price = max(filled_price - 0.05, 0.01)
            log(f"⚠️ [POLY] No live bid; using fallback sell price={sell_price}")

        shares = filled_size_usdc / filled_price if filled_price > 0 else 0
        size_val = int(round(shares))
        sell_px  = round(float(sell_price), 2)
        from py_clob_client.clob_types import PartialCreateOrderOptions
        tick_size = client.get_tick_size(token_id)
        neg_risk  = client.get_neg_risk(token_id)
        order_args = OrderArgs(
            token_id=token_id,
            price=sell_px,
            size=size_val,
            side="SELL",
        )
        signed_order = client.create_order(
            order_args,
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
        )
        resp = client.post_order(signed_order, OrderType.FOK)
        if resp.get("status") in ("matched", "filled", "live"):
            log(f"✅ [POLY] Offset SELL filled. Leg 1 unwound. Recovered ~{shares * sell_price:.2f} USDC")
            return True
        else:
            log(f"❌ [POLY] Offset SELL did not fill: {resp}. Manual intervention needed.")
            return False
    except ImportError:
        log("⚠️ py_clob_client not installed — cannot place offset sell")
        return False
    except Exception as e:
        log(f"❌ [POLY] Offset sell error: {e}. Manual intervention needed.")
        return False


class ExecutionResult:
    def __init__(self, success: bool, pair_id: str, error: str = "", unwound: bool = False):
        self.success = success
        self.pair_id = pair_id
        self.error = error
        self.unwound = unwound
        self.leg1_order_id = ""
        self.leg2_order_id = ""
        self.live_poly_ask = None
        self.live_kalshi_ask = None
        self.live_edge = None
        # Which side was bought on Kalshi ("YES" or "NO") — set by execute_arb
        self.kalshi_side: str = "YES"
        # Actual fill quantities — populated on success
        self.filled_shares: float = 0.0
        self.filled_poly_price: float = 0.0
        self.filled_kalshi_price: float = 0.0
        self.total_cost_usdc: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "pair_id": self.pair_id,
            "error": self.error,
            "unwound": self.unwound,
            "leg1_order_id": self.leg1_order_id,
            "leg2_order_id": self.leg2_order_id,
            "live_poly_ask": self.live_poly_ask,
            "live_kalshi_ask": self.live_kalshi_ask,
            "live_edge": self.live_edge,
            "filled_shares": self.filled_shares,
            "filled_poly_price": self.filled_poly_price,
            "filled_kalshi_price": self.filled_kalshi_price,
            "total_cost_usdc": self.total_cost_usdc,
        }


def _place_opinion_order(
    market_id: str,
    side: str,
    price: float,
    size_usdc: float,
    contract_count: int,
    outcome_hint: str = "",
    label_hint: str = "",
) -> tuple[bool, str, str]:
    """Place a limit buy order on Opinion Labs via the CLOB client.

    Delegates to opinion_clob.place_order which handles:
      - Categorical parent → child market resolution (via resolve_tradable_market)
      - OPINION_PRIVATE_KEY signing
      - OPINION_PORTFOLIO_ADDRESS headers
      - Proper CLOB auth

    Args:
        market_id:    Opinion market ID (may be categorical parent, e.g. "340").
        outcome_hint: Oddpool outcome_key for categorical child selection.
        label_hint:   Oddpool label field for exact child title matching.

    Returns (ok, order_id, error_msg). Never raises.
    """
    try:
        from ..adapters.opinion_clob import place_order as opinion_place_order
        return opinion_place_order(
            market_id=market_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            contract_count=contract_count,
            outcome_hint=outcome_hint,
            label_hint=label_hint,
        )
    except Exception as e:
        err = str(e)
        log(f"❌ [OPINION] _place_opinion_order exception: {err}")
        return False, "", err


def _cancel_leg2_order(
    venue2: str,
    order_id: str,
    kalshi_ticker: str = "",
    opinion_market_id: str = "",
) -> bool:
    """Best-effort cancel of a leg 2 (Kalshi or Opinion) order that was placed but
    needs to be unwound because leg 1 (Polymarket) failed simultaneously.

    Returns True if the cancel request succeeded (HTTP 200/204), False otherwise.
    Failure means the order may have already filled — flag for manual reconciliation.
    """
    if not order_id:
        log(f"⚠️ [LEG2 CANCEL] No order_id provided for {venue2} cancel — cannot cancel")
        return False

    if venue2 == "kalshi":
        try:
            from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
            if not kalshi_auth_available():
                log("⚠️ [LEG2 CANCEL] Kalshi credentials not configured — cannot cancel")
                return False
            from ..config import KALSHI_BASE_URL
            url = f"{KALSHI_BASE_URL}/portfolio/orders/{order_id}"
            headers = get_kalshi_headers("DELETE", url)
            if not headers:
                log("⚠️ [LEG2 CANCEL] Kalshi RSA signing failed")
                return False
            # Use _KALSHI_SESSION (trust_env=False) — bypass proxy for Kalshi
            resp = _KALSHI_SESSION.delete(url, headers=headers, timeout=10)
            ok = resp.status_code in (200, 204)
            log(
                f"{'✅' if ok else '❌'} [LEG2 CANCEL] Kalshi cancel orderId={order_id!r} "
                f"HTTP {resp.status_code}"
            )
            return ok
        except Exception as e:
            log(f"❌ [LEG2 CANCEL] Kalshi cancel exception: {e}")
            return False

    elif venue2 == "opinion":
        try:
            from ..adapters.opinion_clob import get_client as opinion_get_client
            client = opinion_get_client()
            if client is None:
                log("⚠️ [LEG2 CANCEL] Opinion CLOB client not available — cannot cancel")
                return False
            result = client.cancel_order(order_id)
            ok = result is not None
            log(f"{'✅' if ok else '❌'} [LEG2 CANCEL] Opinion cancel orderId={order_id!r} result={result}")
            return ok
        except Exception as e:
            log(f"❌ [LEG2 CANCEL] Opinion cancel exception: {e}")
            return False

    else:
        log(f"⚠️ [LEG2 CANCEL] Unknown venue2={venue2!r} — cannot cancel")
        return False


# ── VWAP helper ──────────────────────────────────────────────────────────────

def compute_fill_vwap_for_contracts(
    asks: list[tuple[float, float]],
    target_contracts: int,
) -> tuple[Optional[float], int, bool]:
    """Walk the ask ladder and compute VWAP for filling target_contracts.

    The natural execution unit for cross-venue arb is contracts (shares), not
    USDC, because both legs must cover exactly the same number of contracts.
    Walking to a contract count rather than a spend target guarantees matched
    share counts between venues regardless of price asymmetry.

    Args:
        asks:             Sorted list of (price, size) tuples, ascending by price.
                          Each size entry is the number of contracts at that level.
        target_contracts: How many contracts we want to fill.

    Returns:
        (vwap, filled_contracts, has_full_depth)
            vwap             — weighted average price paid per contract (None if book empty)
            filled_contracts — contracts actually filled (≤ target_contracts)
            has_full_depth   — True when the book supplied the full target_contracts
    """
    if not asks or target_contracts <= 0:
        return None, 0, False

    total_cost = 0.0
    filled = 0
    remaining = target_contracts

    for price, size in asks:
        if remaining <= 0:
            break
        take = min(int(size), remaining)
        if take <= 0:
            continue
        total_cost += take * price
        filled += take
        remaining -= take

    if filled == 0:
        return None, 0, False

    vwap = total_cost / filled
    return vwap, filled, filled >= target_contracts


def compute_marginal_ask(
    asks: list[tuple[float, float]],
    n_contracts: int,
) -> Optional[float]:
    """Return the worst (highest) ask price needed to fill exactly n_contracts.

    This is the limit price that guarantees a full fill of n_contracts — any
    order placed at this price will consume every level up to and including the
    marginal level.  Placing at VWAP instead would leave the deepest levels
    unfilled since VWAP < marginal_ask when depth > 1 level.

    Args:
        asks:        Sorted (price, size) list, ascending by price.
        n_contracts: Number of contracts we want to fill.

    Returns:
        The marginal ask price, or None if the book cannot supply n_contracts
        (in which case it returns the worst available price for partial fills).
    """
    if not asks or n_contracts <= 0:
        return None

    remaining = n_contracts
    last_price: Optional[float] = None

    for price, size in asks:
        take = min(int(size), remaining)
        if take <= 0:
            continue
        last_price = price
        remaining -= take
        if remaining <= 0:
            break

    return last_price  # None only if asks was empty


# ── Opinion market_id → resolved tradable market cache ───────────────────────
# Caches (child_market_id, yes_token_id, no_token_id) keyed by parent market_id.
# For binary markets: child_market_id == market_id.
# For categorical parents: child_market_id is the specific tradable child.
# Keyed by (market_id, outcome_hint) so different outcomes of the same categorical
# parent (e.g. market 340 + "value_above_120k" vs "value_above_150k") are stored
# independently and never return each other's child token IDs.
_OPINION_TOKEN_CACHE: dict[tuple[str, str], tuple[tuple[str, str, str], float]] = {}
_OPINION_TOKEN_CACHE_TTL = 1800  # 30 minutes


def _opinion_resolve_tokens(
    market_id: str,
    outcome_hint: str = "",
    label_hint: str = "",
) -> Optional[tuple[str, str, str]]:
    """Return (child_market_id, yes_token_id, no_token_id) for an Opinion market.

    Handles both binary markets (child_market_id == market_id) and categorical
    parents (child_market_id is the specific tradable child resolved via outcome_hint).
    Results are cached for _OPINION_TOKEN_CACHE_TTL seconds per (market_id, outcome_hint)
    pair — different outcomes of the same categorical parent are independent.

    Args:
        market_id:    Opinion market ID (may be categorical parent like "340").
        outcome_hint: Oddpool outcome_key (e.g. "value_above_120k") to select
                      the correct child from a categorical parent.
        label_hint:   Oddpool label field (e.g. "↑ 120,000") for exact title matching.
    """
    from ..adapters.opinion import resolve_tradable_market
    now = time.time()
    _cache_key = (market_id, outcome_hint)
    cached = _OPINION_TOKEN_CACHE.get(_cache_key)
    if cached is not None:
        resolved, cached_at = cached
        if now - cached_at < _OPINION_TOKEN_CACHE_TTL:
            return resolved
    resolved = resolve_tradable_market(market_id, outcome_hint=outcome_hint, label_hint=label_hint)
    if resolved:
        _OPINION_TOKEN_CACHE[_cache_key] = (resolved, now)
    return resolved


def _opinion_get_best_ask(
    market_id: str,
    side: str = "YES",
    outcome_hint: str = "",
    label_hint: str = "",
) -> Optional[float]:
    """Fetch the best ask for the given side of an Opinion Labs market.

    Resolves market_id to a tradable child (binary or categorical) via
    resolve_tradable_market, then fetches the correct side's orderbook.

    Args:
        market_id:    Opinion Labs market ID (numeric string, e.g. "403" or "340").
        side:         "YES" or "NO" — which side we are buying.
        outcome_hint: Oddpool outcome_key used to pick categorical child markets.

    Returns the best ask price (0.0–1.0) or None if unavailable.
    """
    from ..adapters.opinion import fetch_orderbook
    if not market_id:
        return None
    try:
        resolved = _opinion_resolve_tokens(market_id, outcome_hint=outcome_hint, label_hint=label_hint)
        if not resolved:
            log(f"⚠️ [OPINION] could not resolve tradable market for marketId={market_id!r}")
            return None
        child_market_id, yes_token_id, no_token_id = resolved
        token_id = yes_token_id if side == "YES" else no_token_id
        log(f"🔍 [OPINION] fetching {side} orderbook: parent={market_id!r} child={child_market_id!r} token={token_id[:16]}...")
        book = fetch_orderbook(token_id)
        if not book:
            log(f"⚠️ [OPINION] empty orderbook for {side} token={token_id[:16]}...")
            return None
        asks = book.get("asks") or []
        if not asks:
            log(f"⚠️ [OPINION] no asks in {side} orderbook for token={token_id[:16]}...")
            return None
        # Use min() to guarantee lowest ask regardless of API sort order
        best = min(asks, key=lambda x: float(x.get("price") or x.get("yes_price") or 999))
        price = best.get("price") or best.get("yes_price")
        if price is None:
            return None
        result = float(price) / 100.0 if float(price) > 1 else float(price)
        log(f"✅ [OPINION] live {side} ask={result:.4f} for marketId={market_id!r} child={child_market_id!r}")
        return result
    except Exception as e:
        log(f"⚠️ [OPINION] fetch best ask error for {market_id!r} side={side}: {e}")
        return None


def execute_arb(
    opportunity: ArbOpportunity,
    size_usdc: float,
    min_edge_pct: Optional[float] = None,
) -> ExecutionResult:
    """Execute both legs of an arbitrage opportunity with live price verification.

    Routes based on opportunity.venue2:
    - "kalshi"  → Polymarket YES + Kalshi complementary side (default)
    - "opinion" → Polymarket YES + Opinion Labs complementary side

    Steps:
    1. Re-fetch live asks on both Polymarket CLOB and leg-2 venue
    2. Recompute live_edge = 1 - live_poly_ask - live_leg2_ask
    3. Abort if live_edge < MIN_EDGE_PCT (accounts for fees)
    4. Verify slippage: reject if live ask is >50 bps worse than Oddpool quote
    5. Place leg 1 (Polymarket YES)
    6. Place leg 2 (Kalshi or Opinion Labs)
    7. If leg 2 fails: auto-unwind leg 1 immediately
    8. Log both legs to arb_executions DB table
    """
    if min_edge_pct is None:
        min_edge_pct = ARB_MIN_EDGE_PCT

    pair_id = opportunity.pair_id
    venue2 = getattr(opportunity, "venue2", "kalshi")
    result = ExecutionResult(success=False, pair_id=pair_id)
    log(f"🔍 Starting execution for pair {pair_id} (venue2={venue2})")

    poly_yes_token = opportunity.poly_yes_token
    poly_no_token = getattr(opportunity, "poly_no_token", "") or ""
    kalshi_side = getattr(opportunity, "kalshi_side", "NO")
    kalshi_event_ticker = opportunity.kalshi_ticker  # may be event-level (e.g. "KXBTC-25FEB21")
    opinion_market_id = getattr(opportunity, "opinion_market_id", "")
    opinion_side = kalshi_side  # reuse kalshi_side for opinion side
    outcome_key = getattr(opportunity, "outcome_key", "yes")

    # Determine which Polymarket token to price.
    # When kalshi_side=="YES" the executor is buying YES on venue2 and NO on Polymarket.
    # → Must fetch the NO token's ask (not the YES token's ask).
    # When kalshi_side=="NO" the executor is buying YES on Polymarket.
    # → Use the YES token's ask as normal.
    buying_poly_no = (kalshi_side == "YES")
    if buying_poly_no:
        if poly_no_token:
            poly_token_for_price = poly_no_token
            log(
                f"↔️ kalshi_side=YES → buying NO on Poly. "
                f"Using NO token {poly_no_token[:16]}... for price check"
            )
        else:
            # NO token not resolved yet — cannot price the correct leg; abort.
            result.error = (
                "poly_no_token_missing: buying NO on Polymarket but NO token ID not resolved. "
                "Will retry on next Oddpool cycle after token cache is populated."
            )
            log(f"❌ {result.error}")
            return result
    else:
        poly_token_for_price = poly_yes_token
        log(
            f"↔️ kalshi_side=NO → buying YES on Poly. "
            f"Using YES token {poly_yes_token[:16]}... for price check"
        )

    # Resolve event-level Kalshi ticker → market-level ticker (e.g. "KXBTC-25FEB21-T100500").
    # Oddpool supplies event tickers; Kalshi's price/orderbook APIs need market tickers.
    # Falls back to the original event ticker on failure (will hit Oddpool fallback path).
    if venue2 != "opinion" and kalshi_event_ticker:
        resolved_market_ticker = kalshi_resolve_market_ticker(
            kalshi_event_ticker,
            outcome_key,
            label_hint=getattr(opportunity, "kalshi_title", "") or "",
        )
        if resolved_market_ticker:
            kalshi_ticker = resolved_market_ticker
            log(f"🎯 Kalshi event→market: {kalshi_event_ticker!r} → {kalshi_ticker!r}")
        else:
            kalshi_ticker = kalshi_event_ticker
            log(f"⚠️ Kalshi event→market resolution failed for {kalshi_event_ticker!r} — will use event ticker (likely 404)")
    else:
        kalshi_ticker = kalshi_event_ticker

    # ── Early Opinion token resolution ────────────────────────────────────────
    # Must happen BEFORE the parallel book fetch so the child token ID is known.
    # Uses the 30-min module-level cache — a warm hit costs microseconds.
    leg2_venue_label = venue2  # "kalshi" or "opinion"
    _opinion_label: str = getattr(opportunity, "kalshi_title", "") or ""
    _opinion_child_book_token: str = ""  # set below for Opinion; token ID to fetch OB from

    if venue2 == "opinion" and opinion_market_id:
        _pre_resolved = _opinion_resolve_tokens(
            opinion_market_id, outcome_hint=outcome_key, label_hint=_opinion_label
        )
        if not _pre_resolved:
            result.error = (
                f"opinion_token_unresolvable: parent={opinion_market_id!r} "
                f"outcome={outcome_key!r} — no tradable child market found; "
                "skipping to avoid certain leg-2 failure"
            )
            log(f"❌ {result.error}")
            return result
        _pre_child_id, _pre_yes_token, _pre_no_token = _pre_resolved
        _opinion_child_book_token = (
            _pre_yes_token if opinion_side == "YES" else _pre_no_token
        )
        log(
            f"✅ [OPINION] Token resolved: parent={opinion_market_id!r} "
            f"→ child={_pre_child_id!r} token={_opinion_child_book_token[:16]}... side={opinion_side}"
        )

    # ── Parallel orderbook fetch: both legs simultaneously ────────────────────
    log(
        f"📊 Fetching live orderbooks in parallel: "
        f"poly={poly_token_for_price[:16]}... (side={'NO' if buying_poly_no else 'YES'}) | "
        f"{'kalshi=' + kalshi_ticker if venue2 != 'opinion' else 'opinion=' + opinion_market_id}"
    )

    import concurrent.futures as _cf_fetch

    def _fetch_poly_book() -> Optional[dict]:
        return poly_fetch_orderbook(poly_token_for_price)

    def _fetch_venue2_book() -> Optional[dict]:
        if venue2 == "opinion":
            if not _opinion_child_book_token:
                return None
            return opinion_fetch_orderbook(_opinion_child_book_token)
        else:
            return kalshi_fetch_orderbook_depth(kalshi_ticker)

    poly_book: Optional[dict] = None
    venue2_book: Optional[dict] = None

    with _cf_fetch.ThreadPoolExecutor(max_workers=2) as _pool:
        _poly_fut  = _pool.submit(_fetch_poly_book)
        _v2_fut    = _pool.submit(_fetch_venue2_book)
        try:
            poly_book = _poly_fut.result(timeout=15)
        except Exception as _pe:
            log(f"⚠️ Poly orderbook fetch error: {_pe}")
        try:
            venue2_book = _v2_fut.result(timeout=15)
        except Exception as _v2e:
            log(f"⚠️ {leg2_venue_label} orderbook fetch error: {_v2e}")

    # ── Normalize raw books to sorted (price, size) ask tuples ───────────────
    _poly_asks: list[tuple[float, float]] = (
        poly_extract_asks(poly_book) if poly_book else []
    )

    if venue2 == "opinion":
        _v2_asks: list[tuple[float, float]] = (
            opinion_extract_asks(venue2_book) if venue2_book else []
        )
    else:
        _v2_asks = (
            kalshi_extract_asks(venue2_book, side=kalshi_side) if venue2_book else []
        )

    log(
        f"📖 Books loaded: poly={len(_poly_asks)} levels | "
        f"{leg2_venue_label}={len(_v2_asks)} levels"
    )

    # ── Derive best ask from book (fallback to REST/Oddpool) ─────────────────
    # Used only for initial target_contracts estimate — VWAP replaces this for gating.
    live_poly_ask: Optional[float] = (
        _poly_asks[0][0] if _poly_asks else None
    )
    if live_poly_ask is None:
        _pp = poly_get_best_prices(poly_token_for_price)
        live_poly_ask = _pp.get("best_ask")
        log(f"⚠️ Poly book empty — falling back to REST best_ask={live_poly_ask}")

    if live_poly_ask is None:
        result.error = "poly_orderbook_missing: could not fetch live Polymarket ask"
        log(f"❌ {result.error}")
        return result

    live_kalshi_ask: Optional[float] = (
        _v2_asks[0][0] if _v2_asks else None
    )
    _stale_limit = int(os.environ.get("ARB_STALE_QUOTE_SECONDS", "300"))
    _opp_age = time.time() - getattr(opportunity, "fetched_at", 0)

    if live_kalshi_ask is None:
        if venue2 == "opinion":
            # Try dedicated best-ask fetch (hits cache → orderbook internally)
            live_kalshi_ask = _opinion_get_best_ask(
                opinion_market_id, side=opinion_side,
                outcome_hint=outcome_key, label_hint=_opinion_label,
            )
            if live_kalshi_ask is not None:
                log(f"⚠️ [OPINION] Book empty — fallback to _opinion_get_best_ask: {live_kalshi_ask:.4f}")
        else:
            _kp = kalshi_get_best_prices(kalshi_ticker)
            if kalshi_side == "YES":
                live_kalshi_ask = _kp.get("yes_best_ask")
            else:
                live_kalshi_ask = _kp.get("no_best_ask")
            if live_kalshi_ask is not None:
                log(f"⚠️ Kalshi book empty — fallback to REST best_ask={live_kalshi_ask}")

    if live_kalshi_ask is None:
        # Final fallback: Oddpool quote (with staleness guard)
        if _opp_age > _stale_limit:
            result.error = (
                f"{leg2_venue_label}_orderbook_missing: all live fetches failed and "
                f"Oddpool quote is stale ({_opp_age:.0f}s > {_stale_limit}s limit)"
            )
            log(f"❌ {result.error}")
            return result
        live_kalshi_ask = opportunity.kalshi_yes_ask
        log(
            f"⚠️ [{leg2_venue_label.upper()}] All live fetches failed — using stale Oddpool quote "
            f"{live_kalshi_ask:.4f} (age={_opp_age:.0f}s) [FALLBACK]"
        )

    result.live_poly_ask = live_poly_ask
    result.live_kalshi_ask = live_kalshi_ask
    opp_net_edge = getattr(opportunity, "net_edge_pct", 0.0)
    log(
        f"📊 Best asks: poly={live_poly_ask:.4f} {leg2_venue_label}={live_kalshi_ask:.4f} | "
        f"Oddpool net_edge={opp_net_edge:.4f}% (reference only)"
    )

    # ── Initial target_contracts from best-ask estimate ───────────────────────
    # Both legs must fill EXACTLY the same number of contracts — the natural
    # execution unit is contracts, not USDC.  USDC cost is downstream of this.
    total_budget = min(size_usdc, ARB_MAX_PAIR_USDC)
    combined_best_ask = live_poly_ask + live_kalshi_ask
    if combined_best_ask <= 0:
        result.error = "cannot_compute_contracts: combined best-ask is zero"
        log(f"❌ {result.error}")
        return result

    target_contracts = int(total_budget / combined_best_ask)
    if target_contracts < 1:
        result.error = (
            f"trade_too_small: budget={total_budget:.2f} / "
            f"combined_best_ask={combined_best_ask:.4f} < 1 contract"
        )
        log(f"❌ {result.error}")
        return result

    log(
        f"📊 VWAP gate: target_contracts={target_contracts} "
        f"budget=${total_budget:.2f} combined_best_ask={combined_best_ask:.4f}"
    )

    # ── VWAP computation for target_contracts ─────────────────────────────────
    # Walk each book to exactly target_contracts — matched contract counts guaranteed.
    poly_vwap, poly_filled, poly_depth_ok = compute_fill_vwap_for_contracts(
        _poly_asks, target_contracts
    )
    v2_vwap,   v2_filled,   v2_depth_ok   = compute_fill_vwap_for_contracts(
        _v2_asks, target_contracts
    )

    log(
        f"📊 VWAP result: "
        f"poly_vwap={poly_vwap} ({poly_filled}/{target_contracts} contracts, depth_ok={poly_depth_ok}) | "
        f"{leg2_venue_label}_vwap={v2_vwap} ({v2_filled}/{target_contracts} contracts, depth_ok={v2_depth_ok})"
    )

    # FAIL CLOSED when books yield no fillable contracts.
    # Oddpool quotes are only used above for target_contracts estimation — they must
    # NOT substitute for live orderbook walking in the profitability gate.
    if poly_vwap is None:
        result.error = (
            f"poly_orderbook_missing_for_vwap: book has 0 fillable contracts "
            f"at target={target_contracts}. Cannot verify profitability without live depth. "
            f"poly_book_levels={len(_poly_asks)}"
        )
        log(f"❌ {result.error}")
        return result

    if v2_vwap is None:
        result.error = (
            f"{leg2_venue_label}_orderbook_missing_for_vwap: book has 0 fillable contracts "
            f"at target={target_contracts}. Cannot verify profitability without live depth. "
            f"{leg2_venue_label}_book_levels={len(_v2_asks)}"
        )
        log(f"❌ {result.error}")
        return result

    matched_contracts = min(poly_filled, v2_filled)
    log(
        f"📊 Matched contracts: {matched_contracts} "
        f"(poly_filled={poly_filled}/{target_contracts} {leg2_venue_label}_filled={v2_filled}/{target_contracts})"
    )

    # Early depth check before re-computing VWAP at matched size
    if matched_contracts < ARB_MIN_CONTRACTS:
        result.error = (
            f"depth_insufficient: matched_contracts={matched_contracts} < "
            f"ARB_MIN_CONTRACTS={ARB_MIN_CONTRACTS}. "
            f"poly_filled={poly_filled} {leg2_venue_label}_filled={v2_filled} "
            f"at target={target_contracts}. Market too thin at this size."
        )
        log(f"❌ {result.error}")
        return result

    # ── Re-compute VWAP at exactly matched_contracts ───────────────────────────
    # When one leg partially fills (e.g. poly fills 100 but v2 only 60), the
    # initial VWAP for the deeper leg was computed at 100 contracts — not what
    # we will actually execute.  Recomputing at exactly matched_contracts gives
    # the correct average execution price for the actual trade size on both legs.
    matched_poly_vwap, _, _ = compute_fill_vwap_for_contracts(_poly_asks, matched_contracts)
    matched_v2_vwap,   _, _ = compute_fill_vwap_for_contracts(_v2_asks,   matched_contracts)

    # Both should be non-None since matched_contracts ≤ poly_filled and ≤ v2_filled
    if matched_poly_vwap is None or matched_v2_vwap is None:
        result.error = (
            f"vwap_recompute_failed: unexpected None at matched_contracts={matched_contracts} "
            f"(poly={matched_poly_vwap} {leg2_venue_label}={matched_v2_vwap})"
        )
        log(f"❌ {result.error}")
        return result

    log(
        f"📊 VWAP @ matched size ({matched_contracts} contracts): "
        f"poly={matched_poly_vwap:.4f} {leg2_venue_label}={matched_v2_vwap:.4f} "
        f"(initial @ target: poly={poly_vwap:.4f} {leg2_venue_label}={v2_vwap:.4f})"
    )

    # ── VWAP profitability gate ────────────────────────────────────────────────
    # Use matched-size VWAPs — these reflect actual execution prices for both legs.
    fee_pct = ARB_POLY_FEE_PCT + (
        ARB_KALSHI_FEE_PCT if venue2 == "kalshi" else ARB_OPINION_FEE_PCT
    )
    gross_edge_pct = (1.0 - matched_poly_vwap - matched_v2_vwap) * 100.0
    net_edge_pct   = gross_edge_pct - fee_pct
    required_edge  = min_edge_pct * 100.0 + ARB_VWAP_SAFETY_BUFFER_PCT

    log(
        f"📐 VWAP profitability @ {matched_contracts} contracts: "
        f"poly_vwap={matched_poly_vwap:.4f} {leg2_venue_label}_vwap={matched_v2_vwap:.4f} "
        f"gross_edge={gross_edge_pct:.4f}% fee={fee_pct:.4f}% "
        f"net_edge={net_edge_pct:.4f}% required={required_edge:.4f}%"
    )

    if net_edge_pct < required_edge:
        result.error = (
            f"vwap_edge_insufficient: net_edge={net_edge_pct:.4f}% < "
            f"required={required_edge:.4f}% "
            f"(poly_vwap={matched_poly_vwap:.4f} {leg2_venue_label}_vwap={matched_v2_vwap:.4f} "
            f"matched={matched_contracts} contracts). "
            f"Market depth erases arb edge at this size."
        )
        log(f"❌ {result.error}")
        return result

    log(
        f"✅ VWAP gate passed: net_edge={net_edge_pct:.4f}% ≥ required={required_edge:.4f}% — "
        f"{matched_contracts} contracts @ poly_vwap={matched_poly_vwap:.4f} / {leg2_venue_label}_vwap={matched_v2_vwap:.4f}"
    )

    # ── Compute marginal ask (execution limit price) ───────────────────────────
    # VWAP is the average fill price — NOT the execution limit price.
    # Placing orders at VWAP would leave levels above VWAP unfilled (partial fill).
    # The marginal ask is the worst level we'd consume to fill matched_contracts —
    # using this as the limit price guarantees the full fill.
    # Example: book [(0.14, 50), (0.15, 30)], target=80:
    #   VWAP = 0.1437 → placing at 0.1437 misses 0.15 level → only 50 fill
    #   marginal = 0.15  → placing at 0.15 fills all 80 contracts ✓
    poly_marginal_ask = compute_marginal_ask(_poly_asks, matched_contracts)
    v2_marginal_ask   = compute_marginal_ask(_v2_asks,   matched_contracts)

    if poly_marginal_ask is None or v2_marginal_ask is None:
        result.error = (
            f"marginal_ask_unavailable: poly={poly_marginal_ask} "
            f"{leg2_venue_label}={v2_marginal_ask} at matched={matched_contracts}"
        )
        log(f"❌ {result.error}")
        return result

    log(
        f"📊 Execution prices (marginal ask @ {matched_contracts} contracts): "
        f"poly={poly_marginal_ask:.4f} {leg2_venue_label}={v2_marginal_ask:.4f} "
        f"(vs vwap: poly={matched_poly_vwap:.4f} {leg2_venue_label}={matched_v2_vwap:.4f})"
    )

    # Execution prices: marginal ask (for order placement and USDC sizing)
    # Gate prices: matched-size VWAP (reported in result for diagnostics only)
    live_poly_ask   = poly_marginal_ask
    live_kalshi_ask = v2_marginal_ask
    result.live_poly_ask   = live_poly_ask
    result.live_kalshi_ask = live_kalshi_ask
    # live_edge uses VWAP (more conservative / realistic for the full fill)
    result.live_edge = 1.0 - matched_poly_vwap - matched_v2_vwap

    # Re-cap contract_count to budget at marginal ask prices (worst-case USDC cost)
    budget_at_marginal = int(total_budget / (live_poly_ask + live_kalshi_ask))
    contract_count = min(matched_contracts, budget_at_marginal)

    log(
        f"📐 Contract sizing: matched={matched_contracts} "
        f"budget_at_marginal={budget_at_marginal} → final={contract_count}"
    )

    if contract_count < 1:
        result.error = (
            f"trade_too_small_at_marginal: budget={total_budget:.2f} / "
            f"marginal_combined={live_poly_ask + live_kalshi_ask:.4f} < 1 contract"
        )
        log(f"❌ {result.error}")
        return result

    # Enforce minimum contract threshold on final executable count
    # (budget cap after marginal-ask repricing can reduce count below the depth minimum)
    if contract_count < ARB_MIN_CONTRACTS:
        result.error = (
            f"final_count_below_minimum: contract_count={contract_count} < "
            f"ARB_MIN_CONTRACTS={ARB_MIN_CONTRACTS} after budget cap "
            f"(matched={matched_contracts} budget_at_marginal={budget_at_marginal})"
        )
        log(f"❌ {result.error}")
        return result

    # Derive exact USDC cost per leg from the final contract count at marginal ask
    leg1_usdc = contract_count * live_poly_ask
    leg2_usdc = contract_count * live_kalshi_ask

    # ── Balance-fit cap: scale down if servicer can't cover the funding gaps ──
    # Finds the largest N ≤ contract_count where the EFFECTIVE funding requirement
    # (raw gap raised to venue minimum deposit when positive) fits in svc_deployable.
    # Uses VENUE_MIN_DEPOSIT from arb_funder so this mirrors fund_both_legs_for_trade()
    # exactly — preventing the case where a tiny positive gap looks affordable here
    # but triggers a $3 min-deposit in the funder and fails again.
    #
    # Effective gap per leg:
    #   raw_gap = max(0, N*ask - platform_balance)
    #   eff_gap = max(raw_gap, venue_min) if raw_gap > 0 else 0
    # Accept N when: eff_poly_gap + eff_v2_gap <= svc_deployable
    try:
        from .arb_funder import (
            get_platform_spot_balances,
            get_servicer_deployable_usdc,
            VENUE_MIN_DEPOSIT,
        )
        _poly_bal, _v2_bal = get_platform_spot_balances(venue2)
        _svc_dep = get_servicer_deployable_usdc()
        _poly_min = VENUE_MIN_DEPOSIT.get("polymarket", 1.0)
        _v2_min   = VENUE_MIN_DEPOSIT.get(venue2, 1.0)
        log(
            f"💰 Balance-fit check: poly_on_platform={_poly_bal:.4f} "
            f"{venue2}_on_platform={_v2_bal:.4f} "
            f"servicer_deployable={_svc_dep:.4f} "
            f"(poly_min={_poly_min} {venue2}_min={_v2_min})"
        )
        _original_count = contract_count
        _found = False
        for _n in range(contract_count, 0, -1):
            _raw_poly = max(0.0, _n * live_poly_ask - _poly_bal)
            _raw_v2   = max(0.0, _n * live_kalshi_ask - _v2_bal)
            # Effective gap mirrors funder: any positive raw gap is raised to venue minimum
            _eff_poly = max(_raw_poly, _poly_min) if _raw_poly > 0 else 0.0
            _eff_v2   = max(_raw_v2,   _v2_min)   if _raw_v2   > 0 else 0.0
            if _eff_poly + _eff_v2 <= _svc_dep:
                if _n < _original_count:
                    log(
                        f"⬇️ Balance-fit: scaled {_original_count}→{_n} contracts "
                        f"(eff_poly_gap={_eff_poly:.4f} eff_{venue2}_gap={_eff_v2:.4f} "
                        f"fits svc_deployable={_svc_dep:.4f})"
                    )
                contract_count = _n
                _found = True
                break
        if not _found:
            result.error = (
                f"insufficient_capital: no contract count (1..{_original_count}) fits "
                f"poly_bal={_poly_bal:.4f} + {venue2}_bal={_v2_bal:.4f} + "
                f"servicer_deployable={_svc_dep:.4f} "
                f"(poly_min={_poly_min} {venue2}_min={_v2_min})"
            )
            log(f"❌ {result.error}")
            return result
        # Re-derive leg costs from the (possibly scaled-down) contract_count
        leg1_usdc = contract_count * live_poly_ask
        leg2_usdc = contract_count * live_kalshi_ask

        # ── Poly minimum marketable order size guard ───────────────────────
        # Polymarket rejects FOK orders below $1.00 total value regardless of
        # contract count. After balance-fit scaling the Poly leg can fall below
        # this threshold (e.g. 2 contracts × $0.094 = $0.188). Catching this
        # here as trade_too_small (a pre-funding error) prevents the Kalshi leg
        # from firing and then needing an auto-cancel, and avoids pair suppression.
        _poly_order_min = VENUE_MIN_DEPOSIT.get("polymarket", 1.0)
        if leg1_usdc < _poly_order_min:
            result.error = (
                f"trade_too_small: poly_leg={leg1_usdc:.4f} USDC < "
                f"poly_min_order={_poly_order_min:.2f} after balance-fit scaling "
                f"({contract_count} contracts × {live_poly_ask:.4f})"
            )
            log(f"❌ {result.error}")
            return result

    except Exception as _bfe:
        log(f"⚠️ Balance-fit check failed (non-fatal, proceeding with original size): {_bfe}")

    log(
        f"📐 Trade sizing: contract_count={contract_count} (integer, depth-capped, balance-fit) "
        f"leg1_usdc={leg1_usdc:.4f} leg2_usdc={leg2_usdc:.4f} "
        f"total_cost={leg1_usdc + leg2_usdc:.4f}"
    )

    # ── Simultaneous funding: deposit BOTH legs in one nonce sequence ────────
    # Sends poly_deposit_tx and venue2_deposit_tx without waiting between them,
    # then polls BOTH platform balances until they reach their targets. This
    # replaces the old sequential per-leg top-up pattern that caused multiple
    # small deposits and split the safety-buffer check across two separate calls.
    try:
        from .arb_funder import fund_both_legs_for_trade
        log(
            f"💰 Funding both legs simultaneously: "
            f"poly={leg1_usdc:.4f} USDC | {venue2}={leg2_usdc:.4f} USDC"
        )
        _fund_timeout = int(os.environ.get("ARB_FUND_WAIT_SECS", "180"))
        _poll_secs    = int(os.environ.get("ARB_FUND_POLL_SECS", "10"))
        funded, fund_err = fund_both_legs_for_trade(
            poly_usdc=leg1_usdc,
            venue2=venue2,
            venue2_usdc=leg2_usdc,
            wait_timeout=_fund_timeout,
            poll_interval=_poll_secs,
        )
        if not funded:
            # Preserve the "funding_insufficient" prefix when the funder's pre-TX
            # gate refused (no capital moved). Only wrap with "funding_failed:" for
            # actual deposit-attempt failures (where capital may be in-flight).
            if fund_err.startswith("funding_insufficient"):
                result.error = fund_err
            else:
                result.error = f"funding_failed: {fund_err}"
            log(f"❌ {result.error}")
            return result
        log(f"✅ Both legs funded — proceeding to order placement")
    except Exception as _fe:
        log(f"⚠️ fund_both_legs_for_trade raised ({_fe}) — attempting trade with existing platform balance")

    # ── Fire BOTH legs SIMULTANEOUSLY via ThreadPoolExecutor ─────────────────
    # Per the reference pipeline: submit both orders at the same time so that
    # execution is atomic — the arb window cannot close between leg 1 and leg 2.
    # Both futures are awaited with a 30-second timeout.
    import concurrent.futures as _cf_exec

    poly_order_side_label = "NO_BUY" if buying_poly_no else "YES_BUY"
    leg2_side_label = opinion_side if venue2 == "opinion" else kalshi_side

    log(
        f"📤 Firing both legs SIMULTANEOUSLY | "
        f"LEG1: Polymarket {('NO' if buying_poly_no else 'YES')} {contract_count}×"
        f"@{live_poly_ask} token={poly_token_for_price[:16]}... | "
        f"LEG2: {venue2} {leg2_side_label} {contract_count}×@{live_kalshi_ask}"
    )

    def _run_leg1() -> tuple:
        return _place_poly_order(
            token_id=poly_token_for_price,
            side="BUY",
            price=live_poly_ask,
            contract_count=contract_count,
        )

    def _run_leg2() -> tuple:
        if venue2 == "opinion":
            return _place_opinion_order(
                market_id=opinion_market_id,
                side=opinion_side,
                price=live_kalshi_ask,
                size_usdc=leg2_usdc,
                contract_count=contract_count,
                outcome_hint=outcome_key,
                label_hint=getattr(opportunity, "kalshi_title", "") or "",
            )
        return _place_kalshi_order(
            ticker=kalshi_ticker,
            side=kalshi_side,
            price=live_kalshi_ask,
            size_usdc=leg2_usdc,
            contract_count=contract_count,
        )

    leg1_ok: bool = False;  leg1_order_id: str = "";  leg1_err: str = ""
    leg2_ok: bool = False;  leg2_order_id: str = "";  leg2_err: str = ""

    with _cf_exec.ThreadPoolExecutor(max_workers=2) as _exec_pool:
        fut1 = _exec_pool.submit(_run_leg1)
        fut2 = _exec_pool.submit(_run_leg2)
        try:
            leg1_ok, leg1_order_id, leg1_err = fut1.result(timeout=30)
        except Exception as _e1:
            leg1_ok, leg1_order_id, leg1_err = False, "", str(_e1)
            log(f"❌ Leg 1 future raised: {_e1}")
        try:
            leg2_ok, leg2_order_id, leg2_err = fut2.result(timeout=30)
        except Exception as _e2:
            leg2_ok, leg2_order_id, leg2_err = False, "", str(_e2)
            log(f"❌ Leg 2 future raised: {_e2}")

    log(
        f"{'✅' if leg1_ok else '❌'} Leg1={leg1_ok} orderId={leg1_order_id!r} err={leg1_err!r} | "
        f"{'✅' if leg2_ok else '❌'} Leg2={leg2_ok} orderId={leg2_order_id!r} err={leg2_err!r}"
    )

    result.leg1_order_id = leg1_order_id
    result.leg2_order_id = leg2_order_id
    result.kalshi_side = leg2_side_label

    log_execution_to_db(
        pair_id=pair_id, leg=1, venue="polymarket", side=poly_order_side_label,
        price=live_poly_ask, size=float(contract_count),
        success=leg1_ok, error=leg1_err, order_id=leg1_order_id,
    )
    log_execution_to_db(
        pair_id=pair_id, leg=2, venue=venue2, side=f"{leg2_side_label}_BUY",
        price=live_kalshi_ask, size=float(contract_count),
        success=leg2_ok, error=leg2_err, order_id=leg2_order_id,
    )

    # ── Handle failures ──────────────────────────────────────────────────
    if not leg1_ok and not leg2_ok:
        result.error = f"both_legs_failed: leg1={leg1_err} | leg2={leg2_err}"
        log(f"❌ Both legs failed — no capital moved: {result.error}")
        return result

    if not leg1_ok:
        # Leg 2 went through but Leg 1 (Polymarket) failed.
        # Immediately cancel leg 2 to avoid holding a one-sided hedge.
        log(f"❌ Leg 1 failed: {leg1_err} — initiating AUTO-CANCEL of leg 2 ({venue2} orderId={leg2_order_id!r})")
        cancel2_ok = _cancel_leg2_order(
            venue2=venue2,
            order_id=leg2_order_id,
            kalshi_ticker=kalshi_ticker,
            opinion_market_id=opinion_market_id,
        )
        result.unwound = cancel2_ok
        result.error = (
            f"leg1_failed: {leg1_err}. "
            f"Leg 2 ({venue2}) auto-cancel "
            f"{'succeeded' if cancel2_ok else 'FAILED — manual intervention needed'} "
            f"(orderId={leg2_order_id!r})."
        )
        log(f"{'✅' if cancel2_ok else '⚠️'} Leg 2 cancel: {result.error}")
        return result

    if not leg2_ok:
        # Leg 1 (Polymarket) went through but Leg 2 failed — unwind Polymarket position.
        log(f"❌ Leg 2 failed: {leg2_err} — initiating AUTO-UNWIND of leg 1")
        unwind_ok = _unwind_poly_leg(
            order_id=leg1_order_id,
            token_id=poly_token_for_price,
            filled_size_usdc=leg1_usdc,
            filled_price=live_poly_ask,
        )
        result.unwound = unwind_ok
        result.error = (
            f"leg2_failed: {leg2_err}. "
            f"Leg 1 auto-unwind {'succeeded' if unwind_ok else 'FAILED — manual intervention needed'}."
        )
        log(f"{'✅' if unwind_ok else '❌'} Auto-unwind: {result.error}")
        return result

    log(f"✅ Both legs placed! pair_id={pair_id} contracts={contract_count}")

    # Post-placement validation: re-check live prices immediately after both legs are placed.
    # If the actual fill caused the locked spread to deteriorate below the minimum edge threshold,
    # trigger immediate unwind of both legs to prevent holding a loss-making position.
    log("🔍 Post-placement validation: re-checking live prices after both fills...")
    try:
        # Use the same token we priced pre-trade (poly_token_for_price respects buying_poly_no).
        post_poly = poly_get_best_prices(poly_token_for_price)
        post_poly_ask = post_poly.get("best_ask", live_poly_ask)
        if venue2 == "opinion":
            # Pass outcome_key so categorical parents resolve the same child as pre-flight
            post_leg2_ask = _opinion_get_best_ask(opinion_market_id, outcome_hint=outcome_key) or live_kalshi_ask
        else:
            post_kalshi = kalshi_get_best_prices(kalshi_ticker)
            # Select the side we actually bought — same logic as the pre-trade price fetch.
            post_leg2_ask = post_kalshi.get(
                "yes_best_ask" if kalshi_side == "YES" else "no_best_ask",
                live_kalshi_ask,
            )
        post_edge = 1.0 - post_poly_ask - post_leg2_ask
        log(
            f"📐 Post-fill edge check: poly_ask={post_poly_ask:.4f} "
            f"{leg2_venue_label}_ask={post_leg2_ask:.4f} edge={post_edge:.4f}"
        )
        post_edge_threshold = min_edge_pct * 0.5
        if post_edge < post_edge_threshold:
            log(
                f"❌ Post-fill edge={post_edge:.4f} < threshold={post_edge_threshold:.4f} — "
                f"spread deteriorated after fills; unwinding BOTH legs"
            )
            unwind_ok = _unwind_poly_leg(
                order_id=leg1_order_id,
                token_id=poly_yes_token,
                filled_size_usdc=leg1_usdc,
                filled_price=live_poly_ask,
            )
            if venue2 != "opinion":
                _kalshi_unwind_best_effort(kalshi_ticker, float(contract_count), live_kalshi_ask, kalshi_side)
            result.unwound = unwind_ok
            result.error = (
                f"post_fill_edge_too_thin: post_edge={post_edge:.4f} < {post_edge_threshold:.4f}. "
                f"Both legs unwound (poly={'ok' if unwind_ok else 'FAILED'})."
            )
            log(f"⚠️ Post-fill unwind: {result.error}")
            return result
    except Exception as e:
        log(f"⚠️ Post-placement price validation error (non-fatal): {e}")

    log(f"✅ Both legs placed and validated! pair_id={pair_id} contracts={contract_count}")

    # Store the actual fill details so the caller can persist the correct position
    result.filled_shares = float(contract_count)
    result.filled_poly_price = live_poly_ask
    result.filled_kalshi_price = live_kalshi_ask
    result.total_cost_usdc = leg1_usdc + leg2_usdc
    result.success = True
    return result


def _kalshi_unwind_best_effort(ticker: str, shares: float, fill_price: float, side: str = "YES") -> bool:
    """Best-effort Kalshi position unwind: sell back the side we originally bought.

    Args:
        ticker: Kalshi market ticker
        shares: Number of contracts to unwind
        fill_price: Price we paid (used as reference for limit price)
        side: "YES" or "NO" — must match the side we originally bought

    Returns True if successful, False otherwise. Never raises.
    """
    side_lower = side.lower() if side in ("YES", "NO") else "yes"
    log(f"🔄 [KALSHI] Best-effort unwind: ticker={ticker} shares={shares:.4f} side={side}")

    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        log("❌ [KALSHI] Kalshi credentials not configured — cannot unwind Kalshi position")
        return False

    try:
        from ..config import KALSHI_BASE_URL

        # Place sell limit 5 cents below fill price (in cents) to ensure fill
        sell_price_cents = max(int(fill_price * 100) - 5, 1)
        url = f"{KALSHI_BASE_URL}/portfolio/orders"
        headers = get_kalshi_headers("POST", url)
        if not headers:
            log("❌ [KALSHI] RSA signing failed — cannot unwind Kalshi position")
            return False
        contracts = max(1, int(shares))
        payload = {
            "ticker": ticker,
            "client_order_id": f"unwind_{int(time.time())}",
            "type": "limit",
            "action": "sell",
            "side": side_lower,
            "count": contracts,
            "expiration_ts": int(time.time()) + 30,
        }
        # Set the price field matching the side being sold
        if side_lower == "yes":
            payload["yes_price"] = sell_price_cents
        else:
            payload["no_price"] = sell_price_cents

        # Use _KALSHI_SESSION (trust_env=False) — bypass proxy for Kalshi
        resp = _KALSHI_SESSION.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            log(f"✅ [KALSHI] Unwind order placed (side={side})")
            return True
        else:
            log(f"❌ [KALSHI] Unwind failed: HTTP {resp.status_code} — manual intervention needed")
            return False
    except Exception as e:
        log(f"❌ [KALSHI] Unwind error: {e} — manual intervention needed")
        return False
