"""Polymarket adapter - fetches markets and orderbook data via Gamma API."""

import json
import time
from typing import Optional
from ..config import POLY_GAMMA_URL, POLY_CLOB_URL, ARB_MAX_PAGES_POLY
from .. import http_client
from ..models import NormalizedMarket


def log(msg: str):
    print(f"📊 [Arb/Polymarket] {msg}")


def fetch_active_markets(limit: int = 100, offset: int = 0) -> list[dict]:
    url = f"{POLY_GAMMA_URL}/markets"
    params = {
        "limit": limit,
        "offset": offset,
        "active": "true",
        "closed": "false",
    }
    log(f"Fetching markets: limit={limit} offset={offset}")
    resp = http_client.get(url, venue="polymarket", params=params)
    if resp is None or resp.status_code != 200:
        log(f"Markets API returned {resp.status_code if resp is not None else 'None'}")
        return []
    try:
        markets = resp.json()
        if not isinstance(markets, list):
            markets = markets.get("data", markets.get("markets", []))
        binary = [m for m in markets if _is_binary(m)]
        log(f"Fetched {len(markets)} markets, {len(binary)} binary")
        return binary
    except Exception as e:
        log(f"Parse error: {e}")
        return []


def fetch_events_ending_soon(limit: int = 50) -> list[dict]:
    url = f"{POLY_GAMMA_URL}/events"
    params = {
        "limit": limit,
        "active": "true",
        "closed": "false",
        "order": "end_date_min",
        "ascending": "true",
    }
    log(f"Fetching events ending soon: limit={limit}")
    resp = http_client.get(url, venue="polymarket", params=params)
    if resp is None or resp.status_code != 200:
        log(f"Events API returned {resp.status_code if resp is not None else 'None'}")
        return []
    try:
        events = resp.json()
        if not isinstance(events, list):
            events = events.get("data", events.get("events", []))
        markets = []
        for event in events:
            event_markets = event.get("markets", [])
            for m in event_markets:
                if _is_binary(m):
                    if event.get("tags"):
                        m["_event_tags"] = event.get("tags")
                    markets.append(m)
        log(f"Events: {len(events)} events → {len(markets)} binary markets")
        return markets
    except Exception as e:
        log(f"Events parse error: {e}")
        return []


def fetch_all_active_markets(max_pages: int = None) -> list[dict]:
    if max_pages is None:
        max_pages = ARB_MAX_PAGES_POLY
    seen_ids: set[str] = set()
    all_markets: list[dict] = []

    event_markets = fetch_events_ending_soon(limit=50)
    for m in event_markets:
        mid = _market_id(m)
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            all_markets.append(m)

    log(f"After events pass: {len(all_markets)} unique markets")

    for page in range(max_pages):
        batch = fetch_active_markets(limit=100, offset=page * 100)
        new_count = 0
        for m in batch:
            mid = _market_id(m)
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                all_markets.append(m)
                new_count += 1
        log(f"Markets page {page}: {len(batch)} fetched, {new_count} new")
        if len(batch) < 100:
            break

    log(f"Total Polymarket markets discovered: {len(all_markets)}")
    return all_markets


def _market_id(m: dict) -> str:
    return m.get("condition_id", m.get("id", m.get("conditionId", "")))


def fetch_orderbook(token_id: str) -> Optional[dict]:
    url = f"{POLY_CLOB_URL}/book"
    resp = http_client.get(url, venue="polymarket", params={"token_id": token_id}, timeout=10)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception as e:
        log(f"Orderbook error for {token_id}: {e}")
        return None


def get_best_prices(token_id: str) -> dict:
    book = fetch_orderbook(token_id)
    if not book:
        return {"best_bid": None, "best_ask": None, "bid_size": 0, "ask_size": 0}

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    bid_size = float(bids[0].get("size", 0)) if bids else 0
    ask_size = float(asks[0].get("size", 0)) if asks else 0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }


def normalize_market(market: dict) -> NormalizedMarket:
    tokens = market.get("tokens", [])
    yes_token = ""
    no_token = ""

    if isinstance(tokens, list) and len(tokens) >= 2:
        if isinstance(tokens[0], dict):
            for t in tokens:
                outcome = t.get("outcome", "").upper()
                if outcome == "YES":
                    yes_token = t.get("token_id", "")
                elif outcome == "NO":
                    no_token = t.get("token_id", "")
        else:
            yes_token = str(tokens[0]) if tokens else ""
            no_token = str(tokens[1]) if len(tokens) > 1 else ""

    clob_ids = market.get("clobTokenIds", "")
    if not yes_token and isinstance(clob_ids, str) and clob_ids:
        parts = [p.strip() for p in clob_ids.split(",") if p.strip()]
        yes_token = parts[0] if len(parts) > 0 else ""
        no_token = parts[1] if len(parts) > 1 else ""

    end_date = market.get("endDate", market.get("end_date_iso", ""))
    expiry_ts = 0
    if end_date:
        try:
            if isinstance(end_date, (int, float)):
                expiry_ts = int(end_date)
            elif "T" in str(end_date):
                from datetime import datetime
                dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                expiry_ts = int(dt.timestamp())
            else:
                expiry_ts = int(float(end_date))
        except Exception:
            pass

    question = market.get("question", market.get("title", ""))

    event_tags = market.get("_event_tags")
    tags = []
    if event_tags:
        if isinstance(event_tags, list):
            tags = event_tags
        elif isinstance(event_tags, str):
            tags = [t.strip() for t in event_tags.split(",")]

    return NormalizedMarket(
        venue="polymarket",
        marketId=_market_id(market),
        title=question,
        expiryTs=expiry_ts,
        yesTokenId=yes_token,
        noTokenId=no_token,
        meta={
            "slug": market.get("slug", ""),
            "volume": float(market.get("volume", 0) or 0),
            "tags": tags,
        },
    )


def get_polymarket_markets() -> list[NormalizedMarket]:
    from ..core.filters import filter_by_expiry, classify_sport
    raw = fetch_all_active_markets()
    normalized = [normalize_market(m) for m in raw]
    filtered = filter_by_expiry(normalized)
    for nm in filtered:
        sport = classify_sport(nm.title)
        if not sport and nm.meta.get("tags"):
            for tag in nm.meta["tags"]:
                tl = tag.lower()
                if any(kw in tl for kw in ("sports", "basketball", "football", "soccer", "esports")):
                    sport = _tag_to_sport(tl)
                    break
        nm.sport = sport
    return filtered


def _tag_to_sport(tag: str) -> Optional[str]:
    if any(k in tag for k in ("basketball", "nba")):
        return "nba"
    if any(k in tag for k in ("esport",)):
        return "esports"
    if any(k in tag for k in ("football", "nfl")):
        return "nfl"
    if "soccer" in tag:
        return "soccer"
    if any(k in tag for k in ("mma", "ufc", "boxing")):
        return "mma"
    if "sport" in tag:
        return "sports"
    return None


def _is_binary(market: dict) -> bool:
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            pass
    if isinstance(outcomes, list):
        labels = [str(o).upper() for o in outcomes]
        if labels == ["YES", "NO"] or labels == ["NO", "YES"]:
            return True
    tokens = market.get("tokens", [])
    if isinstance(tokens, list) and len(tokens) == 2:
        return True
    clob = market.get("clobTokenIds", "")
    if isinstance(clob, str) and clob:
        return len(clob.split(",")) == 2
    return False
