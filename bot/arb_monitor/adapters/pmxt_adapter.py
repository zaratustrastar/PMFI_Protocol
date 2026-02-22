"""Unified PMXT adapter — uses pmxt SDK for both Polymarket and Kalshi
discovery and orderbook data through a single stack.

Discovery: query-driven via fetch_markets(query=..., sort='volume')
Orderbook: Polymarket via fetch_order_book (no auth), Kalshi via listing prices (auth needed for orderbook)
Seed pairs: loaded from seedPairs.json for guaranteed coverage of known overlapping events.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Optional
from ..config import (
    PMXT_DISCOVERY_QUERIES, PMXT_QUERY_LIMIT,
    PMXT_KALSHI_RATE_DELAY, PMXT_POLY_RATE_DELAY,
    SEED_PAIRS_PATH, ARB_EXPIRY_WINDOW_DAYS,
    MIN_PRICE_THRESHOLD,
)
from ..models import NormalizedMarket, extract_team_key
from ..core.filters import classify_sport


_poly_client = None
_kalshi_client = None
_client_lock = threading.Lock()
_last_poly_stats: dict = {}
_last_kalshi_stats: dict = {}


def log(msg: str):
    print(f"📡 [Arb/PMXT] {msg}")


def _ensure_clients():
    global _poly_client, _kalshi_client
    with _client_lock:
        if _poly_client is None:
            import pmxt
            _poly_client = pmxt.Polymarket()
            log("Polymarket client initialized")
        if _kalshi_client is None:
            import pmxt
            _kalshi_client = pmxt.Kalshi()
            log("Kalshi client initialized")


def _clean_pmxt_title(title: str) -> str:
    if " - " in title:
        parts = title.split(" - ", 1)
        market_q = parts[1].strip()
        if len(market_q) > 15:
            return market_q
    if " | " in title:
        parts = title.split(" | ", 1)
        market_q = parts[0].strip()
        if len(market_q) > 15:
            return market_q
    if ": " in title:
        parts = title.split(": ", 1)
        if len(parts[0]) < 30 and len(parts[1]) > 15:
            return parts[1].strip()
    return title


def _parse_resolution_date(rd) -> int:
    if not rd:
        return 0
    try:
        if isinstance(rd, datetime):
            return int(rd.timestamp())
        if isinstance(rd, (int, float)):
            ts = int(rd)
            if ts > 1e12:
                ts = ts // 1000
            return ts
        s = str(rd)
        if "T" in s or " " in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        return int(float(s))
    except Exception:
        return 0


def _pmxt_market_to_normalized(m, venue: str) -> Optional[NormalizedMarket]:
    if not m.yes or not m.no:
        return None
    if not m.yes.outcome_id or not m.no.outcome_id:
        return None

    yes_price = m.yes.price or 0
    no_price = m.no.price or 0
    if yes_price <= MIN_PRICE_THRESHOLD and no_price <= MIN_PRICE_THRESHOLD:
        return None

    raw_title = m.title or m.question or ""
    if not raw_title:
        return None
    title = _clean_pmxt_title(raw_title)

    expiry_ts = _parse_resolution_date(m.resolution_date)

    now = int(time.time())
    max_expiry = now + ARB_EXPIRY_WINDOW_DAYS * 86400
    if expiry_ts > 0 and expiry_ts < now:
        return None
    if expiry_ts > 0 and expiry_ts > max_expiry:
        return None

    market_id = str(m.market_id) if m.market_id else ""

    team_key = extract_team_key(title)
    sport = classify_sport(title)

    return NormalizedMarket(
        venue=venue,
        marketId=market_id,
        title=title,
        expiryTs=expiry_ts,
        yesTokenId=str(m.yes.outcome_id),
        noTokenId=str(m.no.outcome_id),
        sport=sport,
        team_key=team_key,
        meta={
            "volume": m.volume or 0,
            "volume_24h": getattr(m, "volume_24h", 0) or 0,
            "liquidity": getattr(m, "liquidity", 0) or 0,
            "yes_price": yes_price,
            "no_price": no_price,
            "url": getattr(m, "url", ""),
            "category": getattr(m, "category", ""),
        },
    )


def _fetch_venue_markets(client, venue: str, queries: list[str], limit: int, rate_delay: float) -> tuple[list, list]:
    raw_markets = {}
    errors = []

    for i, query in enumerate(queries):
        q = query.strip()
        if not q:
            continue
        try:
            if i > 0 and rate_delay > 0:
                time.sleep(rate_delay)
            results = client.fetch_markets(query=q, limit=limit)
            for m in results:
                mid = str(m.market_id) if m.market_id else ""
                if mid and mid not in raw_markets:
                    raw_markets[mid] = m
            log(f"  {venue} query '{q}': {len(results)} results ({len(raw_markets)} unique total)")
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg:
                log(f"  {venue} query '{q}': rate limited, backing off...")
                time.sleep(rate_delay * 3)
                try:
                    results = client.fetch_markets(query=q, limit=limit)
                    for m in results:
                        mid = str(m.market_id) if m.market_id else ""
                        if mid and mid not in raw_markets:
                            raw_markets[mid] = m
                    log(f"  {venue} query '{q}' (retry): {len(results)} results")
                except Exception as e2:
                    errors.append(f"{q}: {e2}")
                    log(f"  {venue} query '{q}' retry failed: {e2}")
            else:
                errors.append(f"{q}: {e}")
                log(f"  {venue} query '{q}' error: {e}")

    return list(raw_markets.values()), errors


def fetch_polymarket_markets() -> tuple[list[NormalizedMarket], dict]:
    global _last_poly_stats
    _ensure_clients()

    stats = {
        "fetchedRaw": 0,
        "normalized": 0,
        "excludedNoTokens": 0,
        "excludedExpiry": 0,
        "excludedPrice": 0,
        "apiErrors": [],
    }

    log(f"Fetching Polymarket via PMXT ({len(PMXT_DISCOVERY_QUERIES)} queries, limit={PMXT_QUERY_LIMIT})...")
    raw_markets, errors = _fetch_venue_markets(
        _poly_client, "polymarket", PMXT_DISCOVERY_QUERIES, PMXT_QUERY_LIMIT, PMXT_POLY_RATE_DELAY
    )
    stats["fetchedRaw"] = len(raw_markets)
    stats["apiErrors"] = errors

    normalized = []
    seen_ids = set()
    for m in raw_markets:
        nm = _pmxt_market_to_normalized(m, "polymarket")
        if nm and nm.marketId not in seen_ids:
            seen_ids.add(nm.marketId)
            normalized.append(nm)
        elif nm is None:
            if not m.yes or not m.no:
                stats["excludedNoTokens"] += 1
            else:
                stats["excludedExpiry"] += 1

    stats["normalized"] = len(normalized)
    _last_poly_stats = stats
    log(f"Polymarket: {stats['fetchedRaw']} raw → {stats['normalized']} normalized")
    return normalized, stats


def fetch_kalshi_markets() -> tuple[list[NormalizedMarket], dict]:
    global _last_kalshi_stats
    _ensure_clients()

    stats = {
        "fetchedRaw": 0,
        "normalized": 0,
        "excludedNoTokens": 0,
        "excludedExpiry": 0,
        "excludedPrice": 0,
        "apiErrors": [],
    }

    log(f"Fetching Kalshi via PMXT ({len(PMXT_DISCOVERY_QUERIES)} queries, limit={PMXT_QUERY_LIMIT})...")
    raw_markets, errors = _fetch_venue_markets(
        _kalshi_client, "kalshi", PMXT_DISCOVERY_QUERIES, PMXT_QUERY_LIMIT, PMXT_KALSHI_RATE_DELAY
    )
    stats["fetchedRaw"] = len(raw_markets)
    stats["apiErrors"] = errors

    normalized = []
    seen_ids = set()
    for m in raw_markets:
        nm = _pmxt_market_to_normalized(m, "kalshi")
        if nm and nm.marketId not in seen_ids:
            seen_ids.add(nm.marketId)
            normalized.append(nm)
        elif nm is None:
            if not m.yes or not m.no:
                stats["excludedNoTokens"] += 1
            else:
                stats["excludedExpiry"] += 1

    stats["normalized"] = len(normalized)
    _last_kalshi_stats = stats
    log(f"Kalshi: {stats['fetchedRaw']} raw → {stats['normalized']} normalized")
    return normalized, stats


def get_poly_discovery_stats() -> dict:
    return dict(_last_poly_stats)


def get_kalshi_discovery_stats() -> dict:
    return dict(_last_kalshi_stats)


def fetch_poly_orderbook(outcome_id: str) -> dict:
    _ensure_clients()
    try:
        book = _poly_client.fetch_order_book(outcome_id)
        best_ask = book.asks[0].price if book.asks else None
        best_bid = book.bids[0].price if book.bids else None
        ask_size = book.asks[0].size if book.asks else 0
        bid_size = book.bids[0].size if book.bids else 0
        return {
            "best_ask": best_ask,
            "best_bid": best_bid,
            "ask_size": ask_size,
            "bid_size": bid_size,
        }
    except Exception as e:
        log(f"Poly orderbook error for {outcome_id[:20]}...: {e}")
        return {"best_ask": None, "best_bid": None, "ask_size": 0, "bid_size": 0}


def fetch_kalshi_orderbook(outcome_id: str) -> dict:
    _ensure_clients()
    try:
        book = _kalshi_client.fetch_order_book(outcome_id)
        best_ask = book.asks[0].price if book.asks else None
        best_bid = book.bids[0].price if book.bids else None
        ask_size = book.asks[0].size if book.asks else 0
        bid_size = book.bids[0].size if book.bids else 0
        return {
            "best_ask": best_ask,
            "best_bid": best_bid,
            "ask_size": ask_size,
            "bid_size": bid_size,
            "source": "orderbook",
        }
    except Exception as e:
        log(f"Kalshi orderbook error for {outcome_id[:20]}...: {e}")
        return {"best_ask": None, "best_bid": None, "ask_size": 0, "bid_size": 0, "source": "error"}


def get_kalshi_prices_from_listing(market: NormalizedMarket) -> dict:
    yes_price = market.meta.get("yes_price", 0)
    no_price = market.meta.get("no_price", 0)
    return {
        "yes_best_ask": yes_price if yes_price else None,
        "yes_best_bid": None,
        "no_best_ask": no_price if no_price else None,
        "no_best_bid": None,
        "yes_ask_size": 0,
        "yes_bid_size": 0,
        "no_ask_size": 0,
        "no_bid_size": 0,
        "source": "listing_price",
    }


def load_seed_pairs() -> list[dict]:
    path = SEED_PAIRS_PATH
    if not os.path.exists(path):
        log(f"No seedPairs.json found at {path}")
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        pairs = data.get("pairs", [])
        log(f"Loaded {len(pairs)} seed pairs from {path}")
        return pairs
    except Exception as e:
        log(f"Error loading seedPairs.json: {e}")
        return []


def fetch_seed_pair_markets(seed_pairs: list[dict]) -> tuple[list[NormalizedMarket], list[NormalizedMarket]]:
    if not seed_pairs:
        return [], []

    _ensure_clients()
    poly_markets = []
    kalshi_markets = []
    seen_poly = set()
    seen_kalshi = set()

    for sp in seed_pairs:
        poly_q = sp.get("poly_query", "")
        kalshi_q = sp.get("kalshi_query", "")
        note = sp.get("note", "")

        if poly_q:
            try:
                time.sleep(PMXT_POLY_RATE_DELAY)
                results = _poly_client.fetch_markets(query=poly_q, limit=20)
                for m in results:
                    nm = _pmxt_market_to_normalized(m, "polymarket")
                    if nm and nm.marketId not in seen_poly:
                        seen_poly.add(nm.marketId)
                        nm.meta["seed_pair"] = True
                        nm.meta["seed_note"] = note
                        poly_markets.append(nm)
                log(f"  Seed '{poly_q}' → {len(results)} poly results")
            except Exception as e:
                log(f"  Seed poly query '{poly_q}' error: {e}")

        if kalshi_q:
            try:
                time.sleep(PMXT_KALSHI_RATE_DELAY)
                results = _kalshi_client.fetch_markets(query=kalshi_q, limit=20)
                for m in results:
                    nm = _pmxt_market_to_normalized(m, "kalshi")
                    if nm and nm.marketId not in seen_kalshi:
                        seen_kalshi.add(nm.marketId)
                        nm.meta["seed_pair"] = True
                        nm.meta["seed_note"] = note
                        kalshi_markets.append(nm)
                log(f"  Seed '{kalshi_q}' → {len(results)} kalshi results")
            except Exception as e:
                log(f"  Seed kalshi query '{kalshi_q}' error: {e}")

    log(f"Seed pairs yielded {len(poly_markets)} poly + {len(kalshi_markets)} kalshi markets")
    return poly_markets, kalshi_markets


def shutdown():
    try:
        import pmxt
        pmxt.stop_server()
        log("PMXT server stopped")
    except Exception as e:
        log(f"Error stopping PMXT server: {e}")
