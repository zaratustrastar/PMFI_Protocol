"""Kalshi adapter - fetches markets and orderbook data via Kalshi Trade API v2.

Discovery strategy:
  1. Fetch events from /events endpoint (gives us category metadata)
  2. For each event, fetch nested markets
  3. Filter out parlays using definitive metadata: mve_collection_ticker / mve_selected_legs
  4. NO fallback — prefer fewer clean markets over polluted parlay data

Price normalization:
  Kalshi API returns prices that may be in cents (0-100) or dollars (0.0-1.0)
  depending on endpoint/version. _to_dollars() detects and normalizes to [0.0, 1.0].
"""

import time
from datetime import datetime
from typing import Optional
from ..config import KALSHI_BASE_URL, ARB_MAX_PAGES_KALSHI
from .. import http_client
from ..models import NormalizedMarket, extract_team_key


def log(msg: str):
    print(f"🎯 [Arb/Kalshi] {msg}")


def _to_dollars(x) -> Optional[float]:
    """Normalize a Kalshi price to dollar units [0.0, 1.0].

    Heuristic:
      - If x > 1.0 => treat as cents and divide by 100
      - If 0 <= x <= 1.0 => already in dollars
      - Clamp minor rounding overshoots (e.g. 1.006) to 1.0
      - Return None for invalid/negative values
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (ValueError, TypeError):
        return None
    d = v / 100.0 if v > 1.0 else v
    if d < 0:
        return None
    if d > 1.0 and d < 1.2:
        d = min(d, 1.0)
    elif d > 1.2:
        return None
    return round(d, 6)


def _headers() -> dict:
    return {"accept": "application/json"}


EXCLUSION_STATS = {
    "mve_parlay": 0,
    "title_parlay_keyword": 0,
    "cap_floor_range": 0,
    "total_fetched": 0,
    "passed": 0,
}


def reset_exclusion_stats():
    for k in EXCLUSION_STATS:
        EXCLUSION_STATS[k] = 0


def get_exclusion_stats() -> dict:
    return dict(EXCLUSION_STATS)


def fetch_events_page(limit: int = 200, cursor: str = "", status: str = "open") -> tuple[list[dict], str]:
    url = f"{KALSHI_BASE_URL}/events"
    params = {
        "limit": limit,
        "status": status,
        "with_nested_markets": "true",
    }
    if cursor:
        params["cursor"] = cursor

    resp = http_client.get(url, venue="kalshi", params=params, headers=_headers(), bypass_proxy=True)
    if resp is None:
        log(f"❌ Events API returned None (likely proxy/network/Cloudflare error)")
        return [], ""
    if resp.status_code != 200:
        log(f"❌ Events API returned HTTP {resp.status_code}, body: {resp.text[:200] if resp.text else '(empty)'}")
        return [], ""
    try:
        data = resp.json()
        events = data.get("events", [])
        next_cursor = data.get("cursor", "")
        return events, next_cursor
    except Exception as e:
        log(f"❌ Events parse error: {e}, body: {resp.text[:200] if resp.text else '(empty)'}")
        return [], ""


def fetch_markets_page(limit: int = 200, cursor: str = "", status: str = "open") -> tuple[list[dict], str]:
    url = f"{KALSHI_BASE_URL}/markets"
    params = {
        "limit": limit,
        "status": status,
    }
    if cursor:
        params["cursor"] = cursor

    resp = http_client.get(url, venue="kalshi", params=params, headers=_headers(), bypass_proxy=True)
    if resp is None:
        log(f"❌ Markets API returned None (likely proxy/network/Cloudflare error)")
        return [], ""
    if resp.status_code != 200:
        log(f"❌ Markets API returned HTTP {resp.status_code}, body: {resp.text[:200] if resp.text else '(empty)'}")
        return [], ""
    try:
        data = resp.json()
        markets = data.get("markets", [])
        next_cursor = data.get("cursor", "")
        return markets, next_cursor
    except Exception as e:
        log(f"❌ Markets parse error: {e}, body: {resp.text[:200] if resp.text else '(empty)'}")
        return [], ""


def _is_parlay(market: dict) -> str | None:
    """Returns exclusion reason string if market is a parlay/multi-leg, else None."""
    if market.get("mve_collection_ticker"):
        return "mve_parlay"
    legs = market.get("mve_selected_legs")
    if legs and isinstance(legs, list) and len(legs) > 0:
        return "mve_parlay"

    title = (market.get("title") or "").lower()
    if "parlay" in title:
        return "title_parlay_keyword"

    cap_strike = market.get("cap_strike")
    floor_strike = market.get("floor_strike")
    if cap_strike is not None and floor_strike is not None:
        return "cap_floor_range"

    return None


def fetch_all_active_markets(max_pages: Optional[int] = None) -> list[dict]:
    """Fetch markets via events endpoint to get category metadata, then filter parlays.

    Returns list of raw market dicts, each enriched with '_event_category' and '_event_title'.
    """
    if max_pages is None:
        max_pages = ARB_MAX_PAGES_KALSHI

    reset_exclusion_stats()
    accepted: list[dict] = []
    seen_tickers: set[str] = set()
    cursor = ""

    for page in range(max_pages):
        events, next_cursor = fetch_events_page(limit=200, cursor=cursor, status="open")
        log(f"Events page {page + 1}: {len(events)} events")

        for event in events:
            event_category = event.get("category", "")
            event_title = event.get("title", "")
            event_ticker = event.get("event_ticker", "")
            markets = event.get("markets") or []

            for m in markets:
                ticker = m.get("ticker", "")
                if ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                EXCLUSION_STATS["total_fetched"] += 1

                reason = _is_parlay(m)
                if reason:
                    EXCLUSION_STATS[reason] = EXCLUSION_STATS.get(reason, 0) + 1
                    continue

                m["_event_category"] = event_category
                m["_event_title"] = event_title
                m["_event_ticker_parent"] = event_ticker
                EXCLUSION_STATS["passed"] += 1
                accepted.append(m)

        if not next_cursor or len(events) == 0:
            break
        cursor = next_cursor

    log(f"Total Kalshi markets discovered: {EXCLUSION_STATS['passed']} accepted, "
        f"{EXCLUSION_STATS['total_fetched'] - EXCLUSION_STATS['passed']} excluded "
        f"(mve_parlay={EXCLUSION_STATS['mve_parlay']}, "
        f"title_parlay={EXCLUSION_STATS['title_parlay_keyword']}, "
        f"cap_floor={EXCLUSION_STATS['cap_floor_range']})")

    return accepted


def _parse_timestamp(ts_str: str) -> int:
    if not ts_str:
        return 0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


KALSHI_CATEGORY_TO_SPORT = {
    "Sports": "sports",
    "Esports": "esports",
}


def _classify_kalshi_category(event_category: str, title: str) -> Optional[str]:
    """Use Kalshi's event category if available, fallback to keyword classification."""
    if event_category:
        mapped = KALSHI_CATEGORY_TO_SPORT.get(event_category)
        if mapped:
            return mapped

    from ..core.filters import classify_sport
    return classify_sport(title)


def normalize_market(market: dict) -> NormalizedMarket:
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    if subtitle and subtitle not in title:
        title = f"{title} - {subtitle}"

    close_time = market.get("close_time", market.get("expiration_time", ""))
    expiry_ts = _parse_timestamp(close_time)

    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    no_bid = market.get("no_bid")
    no_ask = market.get("no_ask")

    event_category = market.get("_event_category", "")
    sport = _classify_kalshi_category(event_category, title)

    team_key = extract_team_key(title)

    return NormalizedMarket(
        venue="kalshi",
        marketId=ticker,
        title=title,
        expiryTs=expiry_ts,
        yesTokenId=ticker,
        noTokenId=ticker,
        sport=sport,
        team_key=team_key,
        meta={
            "event_ticker": market.get("event_ticker", market.get("_event_ticker_parent", "")),
            "event_category": event_category,
            "volume": market.get("volume", 0),
            "volume_24h": market.get("volume_24h", 0),
            "open_interest": market.get("open_interest", 0),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "status": market.get("status", ""),
        },
    )


# ── Event → Market ticker resolver cache ─────────────────────────────────────
# Keyed by (event_ticker, outcome_key) — e.g. ("KXBTC-25FEB21", "yes").
# Including outcome_key prevents cross-side contamination: for multi-market events
# the resolver picks different markets for "yes" vs "no" outcomes. Caching only
# by event_ticker would let the first outcome's selection poison later queries.
# Value: (market_ticker: str, cached_at: float). TTL: 10 minutes.
_MARKET_TICKER_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_MARKET_TICKER_CACHE_TTL = 600  # seconds


def resolve_market_ticker(event_ticker: str, outcome_key: str = "yes") -> Optional[str]:
    """Resolve a Kalshi event ticker to a specific market-level ticker.

    Oddpool provides event-level tickers (e.g. "KXBTC-25FEB21") but Kalshi's
    price/orderbook endpoints require market-level tickers (e.g. "KXBTC-25FEB21-T100500").
    This function queries GET /markets?event_ticker=... and returns the best matching
    market ticker, with a 10-minute cache to avoid per-execution API overhead.

    Args:
        event_ticker: Kalshi event ticker from Oddpool (e.g. "KXBTC-25FEB21")
        outcome_key:  "yes" or "no" — used as a tie-breaker when multiple markets exist

    Returns:
        A market-level ticker string (e.g. "KXBTC-25FEB21-T100500"), or None on failure.
        If the event_ticker already IS a market-level ticker (contains more than one hyphen
        segment past the date component), it is returned as-is.
    """
    if not event_ticker:
        return None

    # Fast path: if the caller already has a market-level ticker it may pass directly.
    # Market tickers have the form EVENT_TICKER + "-" + STRIKE (e.g. "-T100500").
    # We detect this by checking whether the last segment looks like a strike suffix.
    parts = event_ticker.split("-")
    # Example event: "KXBTC-25FEB21"  → 2 parts after splitting on "-"
    # Example market: "KXBTC-25FEB21-T100500" → 3 parts
    # More complex events: "KXETHD-25FEB21" → still 2 parts (two-component prefix)
    # Simple heuristic: if the last segment starts with T or B and has digits, it's a market.
    if len(parts) >= 3 and parts[-1] and parts[-1][0] in "TBtb" and any(c.isdigit() for c in parts[-1]):
        log(f"✅ resolve_market_ticker: {event_ticker!r} looks like market ticker — using as-is")
        return event_ticker

    # Cache lookup — keyed by (event_ticker, outcome_key) to prevent cross-side contamination
    cache_key = (event_ticker, (outcome_key or "yes").lower())
    now = time.time()
    cached = _MARKET_TICKER_CACHE.get(cache_key)
    if cached is not None:
        ticker_val, cached_at = cached
        if now - cached_at < _MARKET_TICKER_CACHE_TTL:
            log(f"✅ resolve_market_ticker: cache hit {event_ticker!r}[{outcome_key}] → {ticker_val!r}")
            return ticker_val
        else:
            log(f"♻️ resolve_market_ticker: cache expired for {event_ticker!r}[{outcome_key}], re-fetching")

    url = f"{KALSHI_BASE_URL}/markets"
    params = {"event_ticker": event_ticker, "status": "open", "limit": 20}
    log(f"🔍 resolve_market_ticker: fetching markets for event {event_ticker!r}")
    resp = http_client.get(url, venue="kalshi", headers=_headers(), params=params, timeout=10, bypass_proxy=True)
    if resp is None or resp.status_code != 200:
        log(f"⚠️ resolve_market_ticker: HTTP {resp.status_code if resp else 'None'} for {event_ticker!r}")
        return None

    try:
        data = resp.json()
        markets = data.get("markets", [])
    except Exception as e:
        log(f"⚠️ resolve_market_ticker: parse error for {event_ticker!r}: {e}")
        return None

    if not markets:
        log(f"⚠️ resolve_market_ticker: no open markets found for event {event_ticker!r}")
        return None

    # Single market — the common case for binary prediction events
    if len(markets) == 1:
        ticker = markets[0].get("ticker", "")
        if ticker:
            log(f"✅ resolve_market_ticker: {event_ticker!r} → {ticker!r} (only market)")
            _MARKET_TICKER_CACHE[cache_key] = (ticker, now)
            return ticker

    # Multiple markets under the event — try to pick the one aligned with outcome_key.
    # Kalshi markets have a "subtitle" or "title" that describes the specific outcome.
    # outcome_key is "yes"/"no"; on multi-market events we look for keywords.
    # Fallback: return the first open market with a live price.
    outcome_lower = (outcome_key or "yes").lower()
    best_ticker = None
    fallback_ticker = None
    for m in markets:
        t = m.get("ticker", "")
        if not t:
            continue
        status = m.get("status", "")
        if status and status.lower() not in ("open", "active"):
            continue
        if fallback_ticker is None:
            fallback_ticker = t
        subtitle = ((m.get("subtitle") or m.get("title") or "")).lower()
        if outcome_lower in subtitle:
            best_ticker = t
            break

    chosen = best_ticker or fallback_ticker
    if chosen:
        log(
            f"✅ resolve_market_ticker: {event_ticker!r} → {chosen!r} "
            f"(from {len(markets)} markets, outcome_key={outcome_key!r}, "
            f"{'matched subtitle' if best_ticker else 'fallback to first'})"
        )
        _MARKET_TICKER_CACHE[cache_key] = (chosen, now)
        return chosen

    log(f"⚠️ resolve_market_ticker: could not pick market for {event_ticker!r} from {len(markets)} markets")
    return None


def get_best_prices(ticker: str, debug: bool = False) -> dict:
    """Fetch best prices for a Kalshi market ticker.

    Uses _to_dollars() to normalize price units (cents vs dollars).
    Does NOT use open_interest as bid/ask size — sizes are set to None
    since the single-market endpoint doesn't provide top-of-book sizes.

    Auth: RSA credentials are used when configured (same as fetch_orderbook_depth).
    Kalshi's API sits behind Cloudflare which blocks datacenter IPs; RSA auth
    headers are sent so Kalshi's WAF can whitelist authenticated requests even
    from non-residential IPs.  If Cloudflare still blocks (returns HTML), a
    clear log message is emitted so the operator knows to set PROXY_URL.

    Args:
        ticker: Kalshi market ticker (e.g. "KXBTC-25FEB21-T100500")
        debug: If True, include raw (unconverted) values for diagnostics
    """
    empty = {
        "yes_best_bid": None, "yes_best_ask": None,
        "no_best_bid": None, "no_best_ask": None,
        "yes_bid_size": None, "yes_ask_size": None,
        "no_bid_size": None, "no_ask_size": None,
        "best_bid": None, "best_ask": None,
        "bid_size": None, "ask_size": None,
    }

    url = f"{KALSHI_BASE_URL}/markets/{ticker}"

    # Use RSA auth when credentials are available — mirrors fetch_orderbook_depth.
    # Kalshi's Cloudflare WAF may whitelist authenticated API traffic even from
    # datacenter IPs; without auth the request is more likely to be blocked.
    try:
        from ..core.kalshi_auth import get_kalshi_headers, kalshi_auth_available
        if kalshi_auth_available():
            auth_hdrs = get_kalshi_headers("GET", url)
            req_headers = {**_headers(), **(auth_hdrs or {})}
        else:
            req_headers = _headers()
            log(f"⚠️ Kalshi RSA credentials not configured — price call is unauthenticated (may be Cloudflare-blocked)")
    except Exception:
        req_headers = _headers()

    resp = http_client.get(url, venue="kalshi", headers=req_headers, timeout=10, bypass_proxy=True)
    if resp is None:
        # http_client.get() returns None when all retries are exhausted
        # (network error, connection refused, or Cloudflare completely blocked the TCP).
        import os as _os
        if not _os.environ.get("PROXY_URL") and not _os.environ.get("HTTP_PROXY"):
            log(
                f"❌ [cloudflare_block_or_network] Kalshi price fetch for {ticker!r} returned None "
                f"— all retries failed. Likely Cloudflare blocking this datacenter IP (no proxy configured). "
                f"Set PROXY_URL=socks5://user:pass@host:port in .env and restart to route through a residential IP."
            )
        else:
            log(
                f"❌ [network_error] Kalshi price fetch for {ticker!r} returned None "
                f"— proxy is configured, check proxy health / connectivity."
            )
        return empty

    # Inspect the response to classify the failure type precisely.
    if resp.status_code != 200:
        body_preview = resp.text[:400] if resp.text else ""
        is_html = body_preview.lstrip().startswith("<!") or "<html" in body_preview.lower()
        if resp.status_code in (403, 503) and is_html:
            import os as _os
            proxy_hint = (
                "Set PROXY_URL=socks5://user:pass@host:port in .env and restart."
                if not _os.environ.get("PROXY_URL") and not _os.environ.get("HTTP_PROXY")
                else "Proxy is configured — it may also be getting blocked; try a residential proxy."
            )
            log(
                f"❌ [cloudflare_block] Kalshi GET /markets/{ticker} returned HTTP {resp.status_code} "
                f"with HTML body (Cloudflare challenge page). {proxy_hint}"
            )
        elif resp.status_code in (401, 403):
            try:
                err_body = resp.json()
            except Exception:
                err_body = body_preview
            log(
                f"❌ [auth_error] Kalshi GET /markets/{ticker} returned HTTP {resp.status_code} "
                f"(JSON body, RSA credentials rejected or API key revoked). body={err_body!r}"
            )
        elif resp.status_code == 404:
            log(f"❌ [ticker_not_found] Kalshi GET /markets/{ticker} returned HTTP 404 — ticker may be expired/invalid")
        else:
            log(f"❌ [http_error] Kalshi GET /markets/{ticker} returned HTTP {resp.status_code}: {body_preview!r}")
        return empty
    try:
        data = resp.json()
        market = data.get("market", data)

        raw_yes_bid = market.get("yes_bid")
        raw_yes_ask = market.get("yes_ask")
        raw_no_bid = market.get("no_bid")
        raw_no_ask = market.get("no_ask")

        yes_best_bid = _to_dollars(raw_yes_bid)
        yes_best_ask = _to_dollars(raw_yes_ask)
        no_best_bid = _to_dollars(raw_no_bid)
        no_best_ask = _to_dollars(raw_no_ask)

        result = {
            "yes_best_bid": yes_best_bid,
            "yes_best_ask": yes_best_ask,
            "no_best_bid": no_best_bid,
            "no_best_ask": no_best_ask,
            "yes_bid_size": None,
            "yes_ask_size": None,
            "no_bid_size": None,
            "no_ask_size": None,
            "best_bid": yes_best_bid,
            "best_ask": yes_best_ask,
            "bid_size": None,
            "ask_size": None,
        }

        if debug:
            result["raw"] = {
                "yes_bid": raw_yes_bid,
                "yes_ask": raw_yes_ask,
                "no_bid": raw_no_bid,
                "no_ask": raw_no_ask,
            }

        log(f"✅ Kalshi prices for {ticker!r}: yes_ask={yes_best_ask} no_ask={no_best_ask}")
        return result
    except Exception as e:
        log(f"Price fetch error for {ticker}: {e}")
        return empty


def fetch_orderbook_depth(ticker: str, depth: int = 25) -> Optional[dict]:
    """Fetch the full Kalshi orderbook for a market ticker.

    Uses GET /markets/{ticker}/orderbook?depth=N.

    Auth: RSA credentials are used when configured (KALSHI_API_KEY_ID +
    KALSHI_PRIVATE_KEY_PATH/PEM); the Kalshi v2 orderbook read endpoint also
    works without auth in practice, so the implementation sends RSA headers
    when available and falls back to unauthenticated `_headers()` otherwise.
    This is consistent with how all other price-checking functions in this
    adapter work (`get_best_prices`, `fetch_all_active_markets`) — only order
    placement and portfolio endpoints strictly require RSA signing.

    Response format:
      { "orderbook": { "yes": [{"price": 52, "delta": 150}, ...],
                       "no":  [{"price": 48, "delta": 200}, ...] } }

    Prices are in CENTS (0-100). delta is quantity of contracts at that level.
    Returns None on any error.
    """
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    # Use RSA auth when credentials are available (required by some API environments);
    # fall back to unauthenticated headers when not configured (public endpoint fallback).
    try:
        from ..core.kalshi_auth import get_kalshi_headers, kalshi_auth_available
        if kalshi_auth_available():
            auth_hdrs = get_kalshi_headers("GET", url)
            headers = {**_headers(), **(auth_hdrs or {})}
        else:
            headers = _headers()
    except Exception:
        headers = _headers()
    resp = http_client.get(url, venue="kalshi", headers=headers, params={"depth": depth}, timeout=10, bypass_proxy=True)
    if resp is None or resp.status_code != 200:
        log(f"⚠️ [Kalshi] orderbook_depth HTTP {resp.status_code if resp else 'None'} for {ticker}")
        return None
    try:
        return resp.json()
    except Exception as e:
        log(f"⚠️ [Kalshi] orderbook_depth parse error for {ticker}: {e}")
        return None


def compute_kalshi_fillable_contracts(
    orderbook: dict,
    side: str,
    max_fill_price: float,
) -> tuple[int, float]:
    """Walk a Kalshi orderbook and return contracts fillable at or below max_fill_price.

    Kalshi's orderbook has "yes" and "no" ladders.  Each entry is {"price": cents, "delta": qty}.
    Ask prices on Kalshi are ordered from lowest (best) to highest.

    Args:
        orderbook:      Raw response from fetch_orderbook_depth() (contains "orderbook" key).
        side:           "YES" or "NO" — which side we are buying.
        max_fill_price: Maximum dollar price (0.0-1.0) we will pay per contract on this leg.

    Returns:
        (contracts_fillable, usdc_cost).  Returns (0, 0.0) on error or no depth.
    """
    try:
        book = orderbook.get("orderbook", orderbook)
        key = "yes" if side.upper() == "YES" else "no"
        levels = book.get(key, [])
    except Exception as e:
        log(f"⚠️ [Kalshi] depth parse error: {e}")
        return 0, 0.0

    contracts = 0
    usdc_cost = 0.0

    # Validate schema: expect list of {"price": <cents>, "delta": <qty>} dicts.
    # Warn loudly if the format looks wrong so production log scanning can catch
    # Kalshi API shape changes before they silently produce zero-depth results.
    if levels:
        sample = levels[0]
        if not isinstance(sample, dict) or "price" not in sample or "delta" not in sample:
            log(
                f"⚠️ [Kalshi/{side}] unexpected orderbook level format — "
                f"expected {{price, delta}} got {type(sample).__name__}: {sample!r}. "
                f"Depth may be underestimated. Update compute_kalshi_fillable_contracts()."
            )

    # Normalise and sort explicitly — API usually returns best-first but we
    # cannot guarantee ordering across API versions or response edge cases.
    parsed_levels: list[tuple[float, int]] = []
    for level in levels:
        try:
            price_cents = float(level.get("price", 0))
            qty = int(level.get("delta", 0))
            parsed_levels.append((price_cents / 100.0, qty))
        except (ValueError, TypeError):
            continue
    parsed_levels.sort(key=lambda x: x[0])  # ascending: cheapest ask first

    for price_dollars, qty in parsed_levels:
        if price_dollars > max_fill_price:
            break  # sorted ascending — no cheaper levels remain

        if qty > 0:
            contracts += qty
            usdc_cost += qty * price_dollars

    log(
        f"📏 [Kalshi/{side}] depth walk: max_fill_price={max_fill_price:.4f} "
        f"→ {contracts} contracts fillable (${usdc_cost:.2f} USDC)"
    )
    return contracts, usdc_cost


def get_kalshi_markets() -> list[NormalizedMarket]:
    from ..core.filters import filter_by_expiry
    raw = fetch_all_active_markets()
    normalized = [normalize_market(m) for m in raw]
    filtered = filter_by_expiry(normalized)
    return filtered
