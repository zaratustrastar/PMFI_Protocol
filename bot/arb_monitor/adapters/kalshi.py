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

    resp = http_client.get(url, venue="kalshi", params=params, headers=_headers())
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

    resp = http_client.get(url, venue="kalshi", params=params, headers=_headers())
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


def get_best_prices(ticker: str, debug: bool = False) -> dict:
    """Fetch best prices for a Kalshi market ticker.

    Uses _to_dollars() to normalize price units (cents vs dollars).
    Does NOT use open_interest as bid/ask size — sizes are set to None
    since the single-market endpoint doesn't provide top-of-book sizes.

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
    resp = http_client.get(url, venue="kalshi", headers=_headers(), timeout=10)
    if resp is None or resp.status_code != 200:
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

        return result
    except Exception as e:
        log(f"Price fetch error for {ticker}: {e}")
        return empty


def get_kalshi_markets() -> list[NormalizedMarket]:
    from ..core.filters import filter_by_expiry
    raw = fetch_all_active_markets()
    normalized = [normalize_market(m) for m in raw]
    filtered = filter_by_expiry(normalized)
    return filtered
