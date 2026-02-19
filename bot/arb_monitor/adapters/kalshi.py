"""Kalshi adapter - fetches markets and orderbook data via Kalshi Trade API v2."""

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


def fetch_all_active_markets(max_pages: int = None) -> list[dict]:
    if max_pages is None:
        max_pages = ARB_MAX_PAGES_KALSHI
    simple_markets: list[dict] = []
    all_binary: list[dict] = []
    skipped_multi = 0
    cursor = ""

    for page in range(max_pages):
        batch, next_cursor = fetch_markets_page(limit=200, cursor=cursor, status="open")
        log(f"Page {page + 1}: fetched {len(batch)} markets")

        for m in batch:
            all_binary.append(m)
            if _is_simple_binary(m):
                simple_markets.append(m)
            else:
                skipped_multi += 1

        if not next_cursor or len(batch) == 0:
            break
        cursor = next_cursor

    if simple_markets:
        log(f"Total Kalshi markets discovered: {len(simple_markets)} simple binary (skipped {skipped_multi} parlays)")
        return simple_markets

    if all_binary:
        log(f"⚠️ No simple binary markets found, falling back to all {len(all_binary)} markets (including parlays)")
        return all_binary

    log("Total Kalshi markets discovered: 0")
    return []


def _is_simple_binary(market: dict) -> bool:
    cap_strike = market.get("cap_strike")
    floor_strike = market.get("floor_strike")
    if cap_strike is not None and floor_strike is not None:
        return False

    title = market.get("title", "")
    if ",yes " in title or ",no " in title:
        return False

    event_ticker = market.get("event_ticker", "")
    if "MULTIGAME" in event_ticker.upper():
        return False

    return True


def _parse_timestamp(ts_str: str) -> int:
    if not ts_str:
        return 0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


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

    return NormalizedMarket(
        venue="kalshi",
        marketId=ticker,
        title=title,
        expiryTs=expiry_ts,
        yesTokenId=ticker,
        noTokenId=ticker,
        meta={
            "event_ticker": market.get("event_ticker", ""),
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
    from ..core.filters import filter_by_expiry, classify_sport
    raw = fetch_all_active_markets()
    normalized = [normalize_market(m) for m in raw]
    filtered = filter_by_expiry(normalized)
    for nm in filtered:
        nm.sport = classify_sport(nm.title)
    return filtered
