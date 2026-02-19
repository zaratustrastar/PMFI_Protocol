"""Kalshi adapter - fetches markets and orderbook data via Kalshi Trade API v2.

Discovery strategy:
  1. Fetch events from /events endpoint (gives us category metadata)
  2. For each event, fetch nested markets
  3. Filter out parlays using definitive metadata: mve_collection_ticker / mve_selected_legs
  4. NO fallback — prefer fewer clean markets over polluted parlay data
"""

import time
from datetime import datetime
from typing import Optional
from ..config import KALSHI_BASE_URL, ARB_MAX_PAGES_KALSHI
from .. import http_client
from ..models import NormalizedMarket


def log(msg: str):
    print(f"🎯 [Arb/Kalshi] {msg}")


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
    if resp is None or resp.status_code != 200:
        log(f"Events API returned {resp.status_code if resp is not None else 'None'}")
        return [], ""
    try:
        data = resp.json()
        events = data.get("events", [])
        next_cursor = data.get("cursor", "")
        return events, next_cursor
    except Exception as e:
        log(f"Events parse error: {e}")
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
    if resp is None or resp.status_code != 200:
        log(f"Markets API returned {resp.status_code if resp is not None else 'None'}")
        return [], ""
    try:
        data = resp.json()
        markets = data.get("markets", [])
        next_cursor = data.get("cursor", "")
        return markets, next_cursor
    except Exception as e:
        log(f"Parse error: {e}")
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

    return NormalizedMarket(
        venue="kalshi",
        marketId=ticker,
        title=title,
        expiryTs=expiry_ts,
        yesTokenId=ticker,
        noTokenId=ticker,
        sport=sport,
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


def get_best_prices(ticker: str) -> dict:
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    resp = http_client.get(url, venue="kalshi", headers=_headers(), timeout=10)
    if resp is None or resp.status_code != 200:
        return {"best_bid": None, "best_ask": None, "no_best_bid": None, "no_best_ask": None, "bid_size": 0, "ask_size": 0}
    try:
        data = resp.json()
        market = data.get("market", data)

        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        no_bid = market.get("no_bid")
        no_ask = market.get("no_ask")

        return {
            "best_bid": yes_bid / 100.0 if yes_bid is not None else None,
            "best_ask": yes_ask / 100.0 if yes_ask is not None else None,
            "no_best_bid": no_bid / 100.0 if no_bid is not None else None,
            "no_best_ask": no_ask / 100.0 if no_ask is not None else None,
            "bid_size": market.get("open_interest", 0),
            "ask_size": market.get("open_interest", 0),
        }
    except Exception as e:
        log(f"Price fetch error for {ticker}: {e}")
        return {"best_bid": None, "best_ask": None, "no_best_bid": None, "no_best_ask": None, "bid_size": 0, "ask_size": 0}


def get_kalshi_markets() -> list[NormalizedMarket]:
    from ..core.filters import filter_by_expiry
    raw = fetch_all_active_markets()
    normalized = [normalize_market(m) for m in raw]
    filtered = filter_by_expiry(normalized)
    return filtered
