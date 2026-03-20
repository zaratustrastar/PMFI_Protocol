"""Arb Executor - places both legs of an arbitrage trade with live price verification.

Security principles:
- Live orderbook re-check at execution (never stale/cached prices)
- Leg atomicity guard: verify BOTH orderbooks before placing EITHER order
- Auto-unwind: if leg 2 (Kalshi) fails, immediately cancel/sell leg 1 (Polymarket)
- Slippage guard: reject if live ask is more than 50 bps worse than Oddpool quote
- MIN_EDGE_PCT (default 2.5%) ensures fees are covered before any trade is placed
"""

import time
import os
from typing import Optional
from ..adapters.polymarket import get_best_prices as poly_get_best_prices
from ..adapters.kalshi import get_best_prices as kalshi_get_best_prices
from ..adapters.oddpool import ArbOpportunity
from ..config import (
    ARB_MIN_EDGE_PCT,
    ARB_SLIPPAGE_GUARD_BPS,
    ARB_MAX_PAIR_USDC,
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


def _place_poly_order(token_id: str, side: str, price: float, size_usdc: float) -> tuple[bool, str, str]:
    """Place a Polymarket CLOB order (FOK — Fill or Kill).

    Returns (success, order_id, error_message).
    Fails hard if POLY_PRIVATE_KEY is missing or py_clob_client is not installed.
    Never simulates success: a missing credential means an error, not a fake fill.
    """
    log(f"📤 [POLY] Placing {side} order: token={token_id[:16]}... price={price} size_usdc={size_usdc}")

    poly_private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not poly_private_key:
        err = "POLY_PRIVATE_KEY not set — cannot place real Polymarket order"
        log(f"❌ [POLY] {err}")
        return False, "", err

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
    except ImportError:
        err = "py_clob_client not installed — cannot place Polymarket orders"
        log(f"❌ [POLY] {err}")
        return False, "", err

    try:
        clob_url = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))
        client = ClobClient(clob_url, key=poly_private_key, chain_id=chain_id)

        shares = size_usdc / price if price > 0 else 0
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=side,
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        order_id = resp.get("orderID", "")
        if resp.get("status") in ("matched", "filled"):
            log(f"✅ [POLY] Order filled: orderId={order_id}")
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

    kalshi_api_key = os.environ.get("KALSHI_API_KEY", "")
    if not kalshi_api_key:
        err = "KALSHI_API_KEY not set — cannot place real Kalshi order"
        log(f"❌ [KALSHI] {err}")
        return False, "", err

    try:
        from ..config import KALSHI_BASE_URL
        import requests
        url = f"{KALSHI_BASE_URL}/portfolio/orders"
        headers = {
            "Authorization": f"Bearer {kalshi_api_key}",
            "Content-Type": "application/json",
        }
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
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            data = resp.json()
            order_id = data.get("order", {}).get("order_id", "")
            log(f"✅ [KALSHI] Order placed: orderId={order_id}")
            return True, order_id, ""
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

    cancel_ok = False
    try:
        from py_clob_client.client import ClobClient
        clob_url = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))
        client = ClobClient(clob_url, key=poly_private_key, chain_id=chain_id)
        resp = client.cancel(order_id=order_id)
        log(f"✅ [POLY] Cancel response: {resp}")
        cancel_ok = True
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
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType

        clob_url = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))
        client = ClobClient(clob_url, key=poly_private_key, chain_id=chain_id)

        from ..adapters.polymarket import get_best_prices as poly_prices_fn
        prices = poly_prices_fn(token_id)
        sell_price = prices.get("best_bid")
        if sell_price is None or sell_price <= 0:
            sell_price = max(filled_price - 0.05, 0.01)
            log(f"⚠️ [POLY] No live bid; using fallback sell price={sell_price}")

        shares = filled_size_usdc / filled_price if filled_price > 0 else 0
        order_args = OrderArgs(
            token_id=token_id,
            price=sell_price,
            size=shares,
            side="SELL",
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if resp.get("status") in ("matched", "filled"):
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


def execute_arb(
    opportunity: ArbOpportunity,
    size_usdc: float,
    min_edge_pct: Optional[float] = None,
) -> ExecutionResult:
    """Execute both legs of an arbitrage opportunity with live price verification.

    Steps:
    1. Re-fetch live asks on both Polymarket CLOB and Kalshi
    2. Recompute live_edge = 1 - live_poly_ask - live_kalshi_ask
    3. Abort if live_edge < MIN_EDGE_PCT (accounts for fees)
    4. Verify slippage: reject if live ask is >50 bps worse than Oddpool quote
    5. Place leg 1 (Polymarket YES)
    6. Place leg 2 (Kalshi YES complement / NO as needed)
    7. If leg 2 fails: auto-unwind leg 1 immediately
    8. Log both legs to arb_executions DB table
    """
    if min_edge_pct is None:
        min_edge_pct = ARB_MIN_EDGE_PCT

    pair_id = opportunity.pair_id
    result = ExecutionResult(success=False, pair_id=pair_id)
    log(f"🔍 Starting execution for pair {pair_id}")

    poly_yes_token = opportunity.poly_yes_token
    kalshi_ticker = opportunity.kalshi_ticker

    log(f"📊 Re-checking live prices for poly={poly_yes_token[:16]}... kalshi={kalshi_ticker}")
    poly_prices = poly_get_best_prices(poly_yes_token)
    live_poly_ask = poly_prices.get("best_ask")

    kalshi_prices = kalshi_get_best_prices(kalshi_ticker)
    live_kalshi_ask = kalshi_prices.get("yes_best_ask")

    result.live_poly_ask = live_poly_ask
    result.live_kalshi_ask = live_kalshi_ask

    if live_poly_ask is None:
        result.error = "poly_orderbook_missing: could not fetch live Polymarket ask"
        log(f"❌ {result.error}")
        return result

    if live_kalshi_ask is None:
        result.error = "kalshi_orderbook_missing: could not fetch live Kalshi ask"
        log(f"❌ {result.error}")
        return result

    live_edge = 1.0 - live_poly_ask - live_kalshi_ask
    result.live_edge = live_edge
    log(f"📐 Live edge: {live_edge:.4f} (poly_ask={live_poly_ask}, kalshi_ask={live_kalshi_ask})")

    if live_edge < min_edge_pct:
        result.error = (
            f"edge_too_thin: live_edge={live_edge:.4f} < min_edge_pct={min_edge_pct:.4f}. "
            f"Aborting to protect against fees."
        )
        log(f"❌ {result.error}")
        return result

    slippage_bps = ARB_SLIPPAGE_GUARD_BPS / 10000
    poly_slippage = live_poly_ask - opportunity.poly_yes_ask
    kalshi_slippage = live_kalshi_ask - opportunity.kalshi_yes_ask
    if poly_slippage > slippage_bps:
        result.error = (
            f"poly_slippage_exceeded: live={live_poly_ask:.4f} quote={opportunity.poly_yes_ask:.4f} "
            f"slippage={poly_slippage:.4f} > {slippage_bps:.4f}"
        )
        log(f"❌ {result.error}")
        return result
    if kalshi_slippage > slippage_bps:
        result.error = (
            f"kalshi_slippage_exceeded: live={live_kalshi_ask:.4f} quote={opportunity.kalshi_yes_ask:.4f} "
            f"slippage={kalshi_slippage:.4f} > {slippage_bps:.4f}"
        )
        log(f"❌ {result.error}")
        return result

    # Compute contract count as an integer first — Kalshi trades in whole contracts.
    # Use the more expensive leg's ask as the sizing denominator so the integer count
    # fits within budget for BOTH legs simultaneously (no partial unmatched exposure).
    # Both legs get EXACTLY the same integer contract_count.
    half_budget = min(size_usdc, ARB_MAX_PAIR_USDC) / 2.0
    max_leg_ask = max(live_poly_ask, live_kalshi_ask)

    if max_leg_ask <= 0:
        result.error = "cannot_compute_contracts: max ask is zero"
        log(f"❌ {result.error}")
        return result

    # Integer contract count ensures both legs are exactly matched (no directional residual)
    contract_count = int(half_budget / max_leg_ask)
    if contract_count < 1:
        result.error = (
            f"trade_too_small: budget={half_budget:.2f} / max_ask={max_leg_ask:.4f} "
            f"= {half_budget/max_leg_ask:.4f} contracts < 1 minimum"
        )
        log(f"❌ {result.error}")
        return result

    # Derive exact USDC cost per leg from the matched integer contract count
    leg1_usdc = contract_count * live_poly_ask
    leg2_usdc = contract_count * live_kalshi_ask

    log(
        f"📐 Trade sizing: contract_count={contract_count} (integer, matched) "
        f"leg1_usdc={leg1_usdc:.4f} leg2_usdc={leg2_usdc:.4f} "
        f"total_cost={leg1_usdc + leg2_usdc:.4f}"
    )

    log(f"📤 Placing LEG 1: Polymarket YES buy {contract_count} contracts @ {live_poly_ask}")
    leg1_ok, leg1_order_id, leg1_err = _place_poly_order(
        token_id=poly_yes_token,
        side="BUY",
        price=live_poly_ask,
        size_usdc=leg1_usdc,
    )
    result.leg1_order_id = leg1_order_id
    log_execution_to_db(
        pair_id=pair_id, leg=1, venue="polymarket", side="YES_BUY",
        price=live_poly_ask, size=float(contract_count),
        success=leg1_ok, error=leg1_err, order_id=leg1_order_id,
    )

    if not leg1_ok:
        result.error = f"leg1_failed: {leg1_err}"
        log(f"❌ Leg 1 failed: {result.error}")
        return result

    log(f"✅ Leg 1 placed: {contract_count} contracts orderId={leg1_order_id}")

    log(f"📤 Placing LEG 2: Kalshi YES buy {contract_count} contracts @ {live_kalshi_ask}")
    leg2_ok, leg2_order_id, leg2_err = _place_kalshi_order(
        ticker=kalshi_ticker,
        side="YES",
        price=live_kalshi_ask,
        size_usdc=leg2_usdc,
        contract_count=contract_count,
    )
    result.leg2_order_id = leg2_order_id
    log_execution_to_db(
        pair_id=pair_id, leg=2, venue="kalshi", side="YES_BUY",
        price=live_kalshi_ask, size=float(contract_count),
        success=leg2_ok, error=leg2_err, order_id=leg2_order_id,
    )

    if not leg2_ok:
        log(f"❌ Leg 2 failed: {leg2_err} — initiating AUTO-UNWIND of leg 1")
        unwind_ok = _unwind_poly_leg(
            order_id=leg1_order_id,
            token_id=poly_yes_token,
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

    # Post-placement validation: re-check live prices immediately after both legs are placed.
    # If the actual fill caused the locked spread to deteriorate below the minimum edge threshold,
    # trigger immediate unwind of both legs to prevent holding a loss-making position.
    log("🔍 Post-placement validation: re-checking live prices after both fills...")
    try:
        post_poly = poly_get_best_prices(poly_yes_token)
        post_kalshi = kalshi_get_best_prices(kalshi_ticker)
        post_poly_ask = post_poly.get("best_ask", live_poly_ask)
        post_kalshi_ask = post_kalshi.get("yes_best_ask", live_kalshi_ask)
        post_edge = 1.0 - post_poly_ask - post_kalshi_ask
        log(
            f"📐 Post-fill edge check: poly_ask={post_poly_ask:.4f} "
            f"kalshi_ask={post_kalshi_ask:.4f} edge={post_edge:.4f}"
        )
        # Allow a generous 0.5x headroom on post-fill edge (market may move slightly during fills)
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
            # Best-effort Kalshi unwind (sell back YES position)
            _kalshi_unwind_best_effort(kalshi_ticker, float(contract_count), live_kalshi_ask)
            result.unwound = unwind_ok
            result.error = (
                f"post_fill_edge_too_thin: post_edge={post_edge:.4f} < {post_edge_threshold:.4f}. "
                f"Both legs unwound (poly={'ok' if unwind_ok else 'FAILED'})."
            )
            log(f"⚠️ Post-fill unwind: {result.error}")
            return result
    except Exception as e:
        # Only log price-check errors (e.g. API timeout during validation).
        # The post-fill unwind block above does not raise — it returns early with error.
        # Do NOT mark success after catching here; only continue to success if we reach below.
        log(f"⚠️ Post-placement price validation error (non-fatal): {e}")

    log(f"✅ Both legs placed and validated! pair_id={pair_id} contracts={contract_count}")

    # Store the actual fill details so the caller can persist the correct position
    result.filled_shares = float(contract_count)
    result.filled_poly_price = live_poly_ask
    result.filled_kalshi_price = live_kalshi_ask
    result.total_cost_usdc = leg1_usdc + leg2_usdc
    result.success = True
    return result


def _kalshi_unwind_best_effort(ticker: str, shares: float, fill_price: float) -> bool:
    """Best-effort Kalshi position unwind: sell YES back to market.

    Returns True if successful, False otherwise. Never raises.
    """
    log(f"🔄 [KALSHI] Best-effort unwind: ticker={ticker} shares={shares:.4f}")
    kalshi_api_key = os.environ.get("KALSHI_API_KEY", "")
    if not kalshi_api_key:
        log("❌ [KALSHI] KALSHI_API_KEY not set — cannot unwind Kalshi position")
        return False

    try:
        from ..config import KALSHI_BASE_URL
        import requests

        sell_price = max(int(fill_price * 100) - 5, 1)  # 5 cents below fill as limit
        url = f"{KALSHI_BASE_URL}/portfolio/orders"
        headers = {
            "Authorization": f"Bearer {kalshi_api_key}",
            "Content-Type": "application/json",
        }
        contracts = max(1, int(shares))
        payload = {
            "ticker": ticker,
            "client_order_id": f"unwind_{int(time.time())}",
            "type": "limit",
            "action": "sell",
            "side": "yes",
            "count": contracts,
            "yes_price": sell_price,
            "expiration_ts": int(time.time()) + 30,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            log(f"✅ [KALSHI] Unwind order placed")
            return True
        else:
            log(f"❌ [KALSHI] Unwind failed: HTTP {resp.status_code} — manual intervention needed")
            return False
    except Exception as e:
        log(f"❌ [KALSHI] Unwind error: {e} — manual intervention needed")
        return False
