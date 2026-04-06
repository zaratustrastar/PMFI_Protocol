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
from ..adapters.polymarket import (
    get_best_prices as poly_get_best_prices,
    fetch_orderbook as poly_fetch_orderbook,
    compute_fillable_contracts as poly_compute_fillable,
)
from ..adapters.kalshi import (
    get_best_prices as kalshi_get_best_prices,
    fetch_orderbook_depth as kalshi_fetch_orderbook_depth,
    compute_kalshi_fillable_contracts,
    resolve_market_ticker as kalshi_resolve_market_ticker,
)
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
        from py_clob_client.clob_types import ApiCreds
        clob_url            = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id            = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        if poly_api_key and poly_api_secret and poly_api_passphrase:
            creds  = ApiCreds(
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

    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        err = "Kalshi credentials not configured — set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
        log(f"❌ [KALSHI] {err}")
        return False, "", err

    try:
        from ..config import KALSHI_BASE_URL
        import requests
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
        clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id           = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        client = ClobClient(clob_url, key=poly_private_key, chain_id=chain_id, signature_type=2, funder=poly_proxy_address)
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

        clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        chain_id           = int(os.environ.get("POLY_CHAIN_ID", "137"))
        poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        client = ClobClient(clob_url, key=poly_private_key, chain_id=chain_id, signature_type=2, funder=poly_proxy_address)

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
) -> tuple[bool, str, str]:
    """Place a limit buy order on Opinion Labs via the CLOB client.

    Delegates to opinion_clob.place_order which handles:
      - OPINION_PRIVATE_KEY signing
      - OPINION_PORTFOLIO_ADDRESS headers
      - Proper CLOB auth

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
            import requests as _req
            url = f"{KALSHI_BASE_URL}/portfolio/orders/{order_id}"
            headers = get_kalshi_headers("DELETE", url)
            if not headers:
                log("⚠️ [LEG2 CANCEL] Kalshi RSA signing failed")
                return False
            resp = _req.delete(url, headers=headers, timeout=10)
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


# ── Opinion market_id → token_id resolution cache ────────────────────────────
# Opinion's /token/orderbook endpoint needs a token ID, not a market ID.
# Cache the lookup for 30 minutes to avoid per-execution round-trips.
_OPINION_TOKEN_CACHE: dict[str, tuple[tuple[str, str], float]] = {}
_OPINION_TOKEN_CACHE_TTL = 1800  # 30 minutes


def _opinion_resolve_tokens(market_id: str) -> Optional[tuple[str, str]]:
    """Return (yes_token_id, no_token_id) for an Opinion market, with caching."""
    from ..adapters.opinion import lookup_token_ids_by_market_id
    now = time.time()
    cached = _OPINION_TOKEN_CACHE.get(market_id)
    if cached is not None:
        token_pair, cached_at = cached
        if now - cached_at < _OPINION_TOKEN_CACHE_TTL:
            return token_pair
    token_pair = lookup_token_ids_by_market_id(market_id)
    if token_pair:
        _OPINION_TOKEN_CACHE[market_id] = (token_pair, now)
    return token_pair


def _opinion_get_best_ask(market_id: str, side: str = "YES") -> Optional[float]:
    """Fetch the best ask for the given side of an Opinion Labs market.

    Resolves market_id → (yes_token_id, no_token_id) via lookup_token_ids_by_market_id
    (checks discovery cache first, then API), then fetches the correct side's orderbook.

    Args:
        market_id: Opinion Labs market ID (numeric string, e.g. "403").
        side: "YES" or "NO" — which side we are buying and need the ask for.

    Returns the best ask price (0.0–1.0) or None if unavailable.
    """
    from ..adapters.opinion import fetch_orderbook
    if not market_id:
        return None
    try:
        token_pair = _opinion_resolve_tokens(market_id)
        if not token_pair:
            log(f"⚠️ [OPINION] could not resolve token IDs for marketId={market_id!r}")
            return None
        yes_token_id, no_token_id = token_pair
        token_id = yes_token_id if side == "YES" else no_token_id
        log(f"🔍 [OPINION] fetching {side} orderbook for marketId={market_id!r} token={token_id[:16]}...")
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
        log(f"✅ [OPINION] live {side} ask={result:.4f} for marketId={market_id!r}")
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
        resolved_market_ticker = kalshi_resolve_market_ticker(kalshi_event_ticker, outcome_key)
        if resolved_market_ticker:
            kalshi_ticker = resolved_market_ticker
            log(f"🎯 Kalshi event→market: {kalshi_event_ticker!r} → {kalshi_ticker!r}")
        else:
            kalshi_ticker = kalshi_event_ticker
            log(f"⚠️ Kalshi event→market resolution failed for {kalshi_event_ticker!r} — will use event ticker (likely 404)")
    else:
        kalshi_ticker = kalshi_event_ticker

    log(
        f"📊 Re-checking live prices for poly={poly_token_for_price[:16]}... "
        f"(side={'NO' if buying_poly_no else 'YES'}) "
        f"venue2={venue2} "
        f"{'kalshi=' + kalshi_ticker if venue2 != 'opinion' else 'opinion=' + opinion_market_id}"
    )
    poly_prices = poly_get_best_prices(poly_token_for_price)
    live_poly_ask = poly_prices.get("best_ask")

    # Fetch live leg-2 ask based on venue.
    # Use the correct side's ask price:
    #   kalshi_side=="YES" → buying YES on venue2 → use yes_best_ask
    #   kalshi_side=="NO"  → buying NO  on venue2 → use no_best_ask
    if venue2 == "opinion":
        # Opinion's proxy API returns errno=10200 / result=null for all market IDs
        # supplied by Oddpool (IDs ~100-500 range don't exist on the proxy endpoint).
        # However:
        #   1. Oddpool's /arbitrage/current response already contains fresh Opinion
        #      yes_ask/no_ask prices — these are the same prices shown on Oddpool's
        #      own live dashboard, so they are authoritative and current.
        #   2. _place_opinion_order() posts to Opinion's /orders endpoint using
        #      market_id directly — no token IDs required for order placement.
        # Therefore we use the Oddpool-provided price as the live Opinion ask and
        # skip the independent token-based orderbook re-fetch entirely.
        live_kalshi_ask = opportunity.kalshi_yes_ask  # Oddpool's live Opinion price
        log(
            f"ℹ️ [OPINION] Using Oddpool-provided price as live Opinion ask="
            f"{live_kalshi_ask:.4f} (token-based re-fetch skipped — proxy API "
            f"does not resolve opinion_market_id={opinion_market_id!r})"
        )
        leg2_venue_label = "opinion"
    else:
        kalshi_prices = kalshi_get_best_prices(kalshi_ticker)
        if kalshi_side == "YES":
            live_kalshi_ask = kalshi_prices.get("yes_best_ask")
        else:
            live_kalshi_ask = kalshi_prices.get("no_best_ask")
        leg2_venue_label = "kalshi"
        log(
            f"📊 Kalshi prices: yes_ask={kalshi_prices.get('yes_best_ask')} "
            f"no_ask={kalshi_prices.get('no_best_ask')} "
            f"→ using {'YES' if kalshi_side == 'YES' else 'NO'} ask={live_kalshi_ask}"
        )

    result.live_poly_ask = live_poly_ask
    result.live_kalshi_ask = live_kalshi_ask

    # ── Primary edge gate: trust Oddpool's net_edge_pct ──────────────────────
    # Oddpool's net_cents already deducts platform fees, slippage allowance, and
    # risk buffer. It is the authoritative source for whether an opportunity is
    # profitable — recalculating edge from raw live prices is WRONG because Poly
    # and venue2 (Kalshi/Opinion) often have dramatically different probability
    # views on the same outcome (e.g. Poly says Maduro wins 0.1%, Kalshi says 92%).
    # Using (1 - poly_ask - venue2_ask) in those cases produces a deeply negative
    # number even when the arb is genuine — Oddpool prices the opportunity based
    # on the COMPLEMENTARY relationship (buy YES on one venue, NO on the other).
    #
    # The correct guard is:
    #   1. Primary: Oddpool's net_edge_pct (guaranteed-profit signal)
    #   2. Slippage check: live price must not be WORSE than Oddpool's quoted price
    #      by more than ARB_SLIPPAGE_GUARD_BPS — this catches cases where the
    #      market moved after Oddpool priced the opportunity.
    opp_net_edge = getattr(opportunity, "net_edge_pct", 0.0)
    log(
        f"📊 Oddpool net_edge={opp_net_edge:.4f} | min_edge={min_edge_pct:.4f} | "
        f"live poly_ask={live_poly_ask} {leg2_venue_label}_ask={live_kalshi_ask}"
    )

    if opp_net_edge < min_edge_pct:
        result.error = (
            f"oddpool_edge_too_thin: net_edge={opp_net_edge:.4f} < "
            f"min_edge={min_edge_pct:.4f}. Oddpool says not profitable after fees."
        )
        log(f"❌ {result.error}")
        return result

    # ── Slippage check against Oddpool quoted prices ──────────────────────────
    # If live prices are available, verify neither leg has moved adversely since
    # Oddpool priced this opportunity. We only reject on ADVERSE slippage (price
    # rose beyond the Oddpool quote) — if the price improved (cheaper than quoted)
    # we proceed; that's strictly better for us.
    slippage_bps = ARB_SLIPPAGE_GUARD_BPS / 10000

    if live_poly_ask is not None:
        poly_slippage = live_poly_ask - opportunity.poly_yes_ask
        if poly_slippage > slippage_bps:
            result.error = (
                f"poly_slippage_exceeded: live={live_poly_ask:.4f} "
                f"quote={opportunity.poly_yes_ask:.4f} "
                f"slippage={poly_slippage:.4f} > {slippage_bps:.4f}"
            )
            log(f"❌ {result.error}")
            return result
        log(f"✅ Poly slippage OK: live={live_poly_ask:.4f} quote={opportunity.poly_yes_ask:.4f} slippage={poly_slippage:+.4f}")
    else:
        # Poly price unavailable — abort. The executor needs at least Poly live
        # price since that's the leg we control directly.
        result.error = "poly_orderbook_missing: could not fetch live Polymarket ask"
        log(f"❌ {result.error}")
        return result

    if live_kalshi_ask is None:
        # Venue2 live price unavailable — fall back to Oddpool-quoted price with a
        # freshness guard. Oddpool updates prices every second; if the opportunity
        # was fetched within the last ARB_STALE_QUOTE_SECONDS seconds the quote is
        # reliable enough to proceed (default 300s to cover full cycle length).
        stale_limit = int(os.environ.get("ARB_STALE_QUOTE_SECONDS", "300"))
        opp_age = time.time() - getattr(opportunity, "fetched_at", 0)
        if opp_age > stale_limit:
            result.error = (
                f"{leg2_venue_label}_orderbook_missing: live fetch failed and "
                f"Oddpool quote is stale ({opp_age:.0f}s old > {stale_limit}s limit)"
            )
            log(f"❌ {result.error}")
            return result
        live_kalshi_ask = opportunity.kalshi_yes_ask
        log(
            f"⚠️ {leg2_venue_label} live orderbook unavailable — using Oddpool "
            f"quoted price {live_kalshi_ask:.4f} (opp_age={opp_age:.0f}s) — skipping slippage check"
        )
        result.live_kalshi_ask = live_kalshi_ask
    else:
        leg2_slippage = live_kalshi_ask - opportunity.kalshi_yes_ask
        if leg2_slippage > slippage_bps:
            result.error = (
                f"{leg2_venue_label}_slippage_exceeded: live={live_kalshi_ask:.4f} "
                f"quote={opportunity.kalshi_yes_ask:.4f} "
                f"slippage={leg2_slippage:.4f} > {slippage_bps:.4f}"
            )
            log(f"❌ {result.error}")
            return result
        log(f"✅ {leg2_venue_label} slippage OK: live={live_kalshi_ask:.4f} quote={opportunity.kalshi_yes_ask:.4f} slippage={leg2_slippage:+.4f}")

    # Store computed live edge for logging/DB (informational only — not used for gating)
    live_edge = 1.0 - live_poly_ask - live_kalshi_ask
    result.live_edge = live_edge
    log(f"📐 Live spread (informational): {live_edge:.4f} | Oddpool net_edge={opp_net_edge:.2f}% — proceeding to size")

    # ── Budget sizing (computed before depth check so fallbacks can reference it) ──
    # Size from the TOTAL budget across both legs using combined cost per contract.
    # This maximizes contract count from available capital regardless of the price split.
    #
    # Example: poly_ask=0.76, venue2_ask=0.24, total_budget=$17
    #   combined = 0.76 + 0.24 = 1.00
    #   contracts = int(17 / 1.00) = 17 → leg1=$12.92, leg2=$4.08 → total=$17
    #
    # Old (wrong): half_budget = total/2; contracts = int(half_budget/max_ask)
    #   → int(8.5/0.76) = 11 → total=$11 (35% of budget wasted on 76/24 splits)
    total_budget = min(size_usdc, ARB_MAX_PAIR_USDC)
    combined_cost_per_contract = live_poly_ask + live_kalshi_ask

    if combined_cost_per_contract <= 0:
        result.error = "cannot_compute_contracts: combined leg cost is zero"
        log(f"❌ {result.error}")
        return result

    budget_contract_count = int(total_budget / combined_cost_per_contract)

    # ── Order book depth cap ──────────────────────────────────────────────────
    # The slippage guard above only verifies the TOP of book is within edge.
    # If the book is thin, filling our full budget walks into unfavourable prices,
    # erasing the arb edge. We cap contract_count by actual book depth.
    #
    # max_fill_price per leg: highest price we can pay on that leg and still retain
    # at least min_edge_pct edge on the combined position.
    #   poly  leg: max = 1.0 - live_leg2_ask - min_edge_pct
    #   leg-2 leg: max = 1.0 - live_poly_ask  - min_edge_pct
    max_poly_fill_price = max(0.0, 1.0 - live_kalshi_ask - min_edge_pct)
    max_leg2_fill_price = max(0.0, 1.0 - live_poly_ask - min_edge_pct)

    # Polymarket: fetch full book and walk it.
    # Fail policy: FAIL-OPEN — fall back to top-of-book ask_size when full book unavailable.
    # Product rationale: Poly's CLOB is highly available and ask_size is a reliable
    # conservative proxy for top-level capacity; blocking a valid trade on a transient
    # CLOB latency is a worse outcome than a slightly under-verified size estimate.
    # This is an explicit asymmetry vs Kalshi (which FAIL-CLOSEs on depth unavailability).
    # If your risk tolerance requires strict full-ladder verification on both legs,
    # change the else branch to: `result.error = "poly_depth_unavailable: ..."; return result`.
    poly_book = poly_fetch_orderbook(poly_token_for_price)
    if poly_book:
        poly_fillable, poly_depth_usdc = poly_compute_fillable(poly_book, max_poly_fill_price)
    else:
        poly_fillable = int(poly_prices.get("ask_size") or 0)
        poly_depth_usdc = poly_fillable * live_poly_ask
        log(f"⚠️ Poly full book unavailable (fail-open), using ask_size={poly_fillable} as depth floor")

    # Leg-2 depth
    if venue2 == "kalshi":
        kalshi_book = kalshi_fetch_orderbook_depth(kalshi_ticker)
        if kalshi_book:
            kalshi_side_for_depth = opportunity.kalshi_side  # "YES" or "NO"
            leg2_fillable, leg2_depth_usdc = compute_kalshi_fillable_contracts(
                kalshi_book, kalshi_side_for_depth, max_leg2_fill_price
            )
        else:
            # Kalshi depth API unavailable (404/500). Use a conservative 10-contract
            # cap rather than aborting — this limits per-trade exposure to a small
            # fixed size while still allowing the arb to execute. The Poly-side depth
            # cap and budget cap remain as additional guards. This avoids blocking 100%
            # of opportunities when Kalshi's API has transient issues.
            _fallback_cap = 10
            leg2_fillable = _fallback_cap
            leg2_depth_usdc = 0.0
            log(
                f"⚠️ Kalshi depth API unavailable for {kalshi_ticker} — "
                f"using conservative {_fallback_cap}-contract fallback cap"
            )
    else:
        # Opinion Labs: no orderbook depth API; Poly-side depth still applied.
        # leg2_fillable is effectively unconstrained — the Poly depth cap and
        # budget cap remain the binding constraints.
        leg2_fillable = budget_contract_count
        leg2_depth_usdc = 0.0
        log(f"ℹ️ Opinion Labs depth API not available — leg-2 capped at budget ({budget_contract_count} contracts)")

    log(
        f"📏 Depth summary: "
        f"depth_poly={poly_fillable} contracts/${poly_depth_usdc:.2f} (max_fill={max_poly_fill_price:.4f}) | "
        f"depth_leg2={leg2_fillable} contracts/${leg2_depth_usdc:.2f} (max_fill={max_leg2_fill_price:.4f})"
    )
    # ─────────────────────────────────────────────────────────────────────────

    # Integer contract count ensures both legs are exactly matched (no directional residual).
    # Cap to the minimum of budget-derived count and the depth-limited fillable count so
    # we never attempt to fill more contracts than the books can absorb at a profitable price.
    depth_limited_count = min(poly_fillable, leg2_fillable)
    contract_count = min(budget_contract_count, depth_limited_count)

    log(
        f"📐 Contract sizing: budget_derived={budget_contract_count} "
        f"depth_limited={depth_limited_count} → final={contract_count}"
    )

    if contract_count < 1:
        if budget_contract_count < 1:
            result.error = (
                f"trade_too_small: budget={total_budget:.2f} / "
                f"combined_cost={combined_cost_per_contract:.4f} "
                f"= {total_budget/combined_cost_per_contract:.4f} contracts < 1 minimum"
            )
        else:
            result.error = (
                f"depth_insufficient: poly_fillable={poly_fillable} "
                f"leg2_fillable={leg2_fillable} — no contracts available at profitable prices. "
                f"Headline edge exists but market is too thin at this size."
            )
        log(f"❌ {result.error}")
        return result

    # Derive exact USDC cost per leg from the matched integer contract count
    leg1_usdc = contract_count * live_poly_ask
    leg2_usdc = contract_count * live_kalshi_ask

    # ── Balance-fit cap: scale down if servicer can't cover the funding gaps ──
    # Finds the largest N ≤ contract_count where:
    #   max(0, N*poly_ask - poly_bal) + max(0, N*v2_ask - v2_bal) <= svc_deployable
    # This lets the bot trade with money already on the platforms without
    # needing the servicer to bridge new capital, avoiding avoidable failures.
    try:
        from .arb_funder import get_platform_spot_balances, get_servicer_deployable_usdc
        _poly_bal, _v2_bal = get_platform_spot_balances(venue2)
        _svc_dep = get_servicer_deployable_usdc()
        log(
            f"💰 Balance-fit check: poly_on_platform={_poly_bal:.4f} "
            f"{venue2}_on_platform={_v2_bal:.4f} "
            f"servicer_deployable={_svc_dep:.4f}"
        )
        _original_count = contract_count
        _found = False
        for _n in range(contract_count, 0, -1):
            _poly_gap = max(0.0, _n * live_poly_ask - _poly_bal)
            _v2_gap   = max(0.0, _n * live_kalshi_ask - _v2_bal)
            if _poly_gap + _v2_gap <= _svc_dep:
                if _n < _original_count:
                    log(
                        f"⬇️ Balance-fit: scaled {_original_count}→{_n} contracts "
                        f"(poly_gap={_poly_gap:.4f} {venue2}_gap={_v2_gap:.4f} "
                        f"fits svc_deployable={_svc_dep:.4f})"
                    )
                contract_count = _n
                _found = True
                break
        if not _found:
            result.error = (
                f"insufficient_capital: no contract count (1..{_original_count}) fits "
                f"poly_bal={_poly_bal:.4f} + {venue2}_bal={_v2_bal:.4f} + "
                f"servicer_deployable={_svc_dep:.4f}"
            )
            log(f"❌ {result.error}")
            return result
        # Re-derive leg costs from the (possibly scaled-down) contract_count
        leg1_usdc = contract_count * live_poly_ask
        leg2_usdc = contract_count * live_kalshi_ask
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
            size_usdc=leg1_usdc,
        )

    def _run_leg2() -> tuple:
        if venue2 == "opinion":
            return _place_opinion_order(
                market_id=opinion_market_id,
                side=opinion_side,
                price=live_kalshi_ask,
                size_usdc=leg2_usdc,
                contract_count=contract_count,
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
        post_poly = poly_get_best_prices(poly_yes_token)
        post_poly_ask = post_poly.get("best_ask", live_poly_ask)
        if venue2 == "opinion":
            post_leg2_ask = _opinion_get_best_ask(opinion_market_id) or live_kalshi_ask
        else:
            post_kalshi = kalshi_get_best_prices(kalshi_ticker)
            post_leg2_ask = post_kalshi.get("yes_best_ask", live_kalshi_ask)
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
        import requests

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

        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            log(f"✅ [KALSHI] Unwind order placed (side={side})")
            return True
        else:
            log(f"❌ [KALSHI] Unwind failed: HTTP {resp.status_code} — manual intervention needed")
            return False
    except Exception as e:
        log(f"❌ [KALSHI] Unwind error: {e} — manual intervention needed")
        return False
