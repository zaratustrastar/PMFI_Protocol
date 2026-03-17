"""Polymarket adapter - fetches markets and orderbook data via Gamma API."""

import json
import time
from datetime import datetime, timezone
from typing import Optional
from ..config import (
    POLY_GAMMA_URL, POLY_CLOB_URL,
    ARB_MAX_PAGES_POLY, ARB_PAGE_SIZE_POLY, ARB_EXPIRY_WINDOW_DAYS,
)
from .. import http_client
from ..models import NormalizedMarket, extract_team_key


_last_poly_stats: dict = {}


def log(msg: str):
    print(f"📊 [Arb/Polymarket] {msg}")


def get_discovery_stats() -> dict:
    return dict(_last_poly_stats)


def _parse_clob_token_ids(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except (json.JSONDecodeError, TypeError):
            parts = [p.strip().strip('"').strip("'") for p in raw.split(",") if p.strip()]
            return parts
    return []


def _parse_expiry(market: dict) -> int:
    end_date = market.get("endDate", market.get("end_date_iso", ""))
    if not end_date:
        return 0
    try:
        if isinstance(end_date, (int, float)):
            return int(end_date)
        if "T" in str(end_date):
            dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            return int(dt.timestamp())
        return int(float(end_date))
    except Exception:
        return 0


def fetch_all_active_markets() -> tuple[list[dict], dict]:
    global _last_poly_stats
    stats = {
        "fetchedTotal": 0,
        "excludedClosed": 0,
        "excludedArchived": 0,
        "excludedMissingTokens": 0,
        "excludedExpiry": 0,
        "includedFinal": 0,
    }

    now = int(time.time())
    max_expiry = now + ARB_EXPIRY_WINDOW_DAYS * 86400

    seen_ids: set[str] = set()
    accepted: list[dict] = []

    api_errors: list[str] = []

    for page in range(ARB_MAX_PAGES_POLY):
        offset = page * ARB_PAGE_SIZE_POLY
        url = f"{POLY_GAMMA_URL}/markets"
        params = {
            "closed": "false",
            "limit": ARB_PAGE_SIZE_POLY,
            "offset": offset,
        }
        log(f"Fetching page {page + 1}: offset={offset} limit={ARB_PAGE_SIZE_POLY}")
        resp = http_client.get(url, venue="polymarket", params=params)
        if resp is None:
            err = f"Page {page + 1}: API returned None (likely proxy/network/Cloudflare error)"
            log(f"❌ {err}")
            api_errors.append(err)
            break
        if resp.status_code != 200:
            err = f"Page {page + 1}: API returned HTTP {resp.status_code}"
            log(f"❌ {err}")
            api_errors.append(err)
            break

        try:
            markets = resp.json()
            if not isinstance(markets, list):
                markets = markets.get("data", markets.get("markets", []))
        except Exception as e:
            err = f"Page {page + 1}: JSON parse error: {e}"
            log(f"❌ {err}")
            api_errors.append(err)
            break

        if not markets:
            log(f"Page {page + 1}: empty response, stopping")
            break

        page_new = 0
        for m in markets:
            stats["fetchedTotal"] += 1

            mid = m.get("id", m.get("condition_id", m.get("conditionId", "")))
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)

            if m.get("closed") is True or str(m.get("closed", "")).lower() == "true":
                stats["excludedClosed"] += 1
                continue

            if m.get("archived") is True:
                stats["excludedArchived"] += 1
                continue

            clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
            if len(clob_ids) < 2:
                stats["excludedMissingTokens"] += 1
                continue

            expiry_ts = _parse_expiry(m)
            if expiry_ts <= now or expiry_ts > max_expiry:
                stats["excludedExpiry"] += 1
                continue

            m["_parsed_clob_ids"] = clob_ids
            m["_parsed_expiry"] = expiry_ts
            accepted.append(m)
            page_new += 1

        log(f"Page {page + 1}: {len(markets)} fetched, {page_new} new accepted")

        if len(markets) < ARB_PAGE_SIZE_POLY:
            break

    stats["includedFinal"] = len(accepted)
    stats["apiErrors"] = api_errors
    _last_poly_stats = stats
    log(f"Total: {stats['fetchedTotal']} fetched → {stats['includedFinal']} included "
        f"(closed={stats['excludedClosed']}, archived={stats['excludedArchived']}, "
        f"missingTokens={stats['excludedMissingTokens']}, expiry={stats['excludedExpiry']})")
    if api_errors:
        log(f"⚠️ API errors during fetch: {api_errors}")
    return accepted, stats


def normalize_market(market: dict) -> NormalizedMarket | None:
    """Normalize a raw Polymarket market dict into a NormalizedMarket.

    Returns None if the market lacks exactly 2 CLOB token IDs (non-binary market).
    """
    clob_ids = market.get("_parsed_clob_ids", [])
    if not clob_ids:
        raw_clob = market.get("clobTokenIds")
        if isinstance(raw_clob, str):
            try:
                raw_clob = json.loads(raw_clob)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(raw_clob, list):
            clob_ids = [str(t) for t in raw_clob if t]
        else:
            clob_ids = []

    if len(clob_ids) < 2:
        mid = market.get("id", market.get("condition_id", ""))
        question = market.get("question", market.get("title", ""))[:60]
        log(f"⚠️ Skipping market {mid} (need 2 clobTokenIds, got {len(clob_ids)}): {question}")
        return None

    yes_token = ""
    no_token = ""

    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = None

    if isinstance(outcomes, list) and len(clob_ids) >= 2:
        outcome_labels = [str(o).upper() for o in outcomes]
        if len(outcome_labels) >= 2:
            for i, label in enumerate(outcome_labels):
                if label == "YES" and i < len(clob_ids):
                    yes_token = clob_ids[i]
                elif label == "NO" and i < len(clob_ids):
                    no_token = clob_ids[i]
    if not yes_token and len(clob_ids) >= 2:
        yes_token = clob_ids[0]
        no_token = clob_ids[1]

    if not yes_token or not no_token:
        mid = market.get("id", market.get("condition_id", ""))
        question = market.get("question", market.get("title", ""))[:60]
        log(f"⚠️ Skipping market {mid} (could not resolve YES/NO tokens): {question}")
        return None

    expiry_ts = market.get("_parsed_expiry") or _parse_expiry(market)

    question = market.get("question", market.get("title", ""))
    mid = market.get("id", market.get("condition_id", market.get("conditionId", "")))
    description = (market.get("description", "") or "").strip()

    team_key = extract_team_key(question)

    return NormalizedMarket(
        venue="polymarket",
        marketId=mid,
        title=question,
        expiryTs=expiry_ts,
        yesTokenId=yes_token,
        noTokenId=no_token,
        team_key=team_key,
        description=description,
        meta={
            "slug": market.get("slug", ""),
            "volume": float(market.get("volume", 0) or 0),
        },
    )


def get_polymarket_markets() -> list[NormalizedMarket]:
    from ..core.filters import classify_sport
    raw, _stats = fetch_all_active_markets()
    normalized = []
    for m in raw:
        nm = normalize_market(m)
        if nm is not None:
            sport = classify_sport(nm.title)
            nm.sport = sport
            normalized.append(nm)
    return normalized


def lookup_token_ids_by_slug(slug: str) -> Optional[tuple[str, str]]:
    """Fetch YES/NO CLOB token IDs for a Polymarket event by its URL slug.
    Returns (yes_token_id, no_token_id) or None on failure.
    Tries the /events endpoint first (event slug → markets), then /markets with slug filter.
    """
    if not slug:
        return None
    try:
        resp = http_client.get(
            f"{POLY_GAMMA_URL}/events",
            venue="polymarket",
            params={"slug": slug, "limit": 3},
            timeout=10,
        )
        if resp and resp.status_code == 200:
            payload = resp.json()
            events = payload if isinstance(payload, list) else payload.get("events", [payload])
            for event in events:
                for m in event.get("markets", []):
                    clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
                    if len(clob_ids) >= 2:
                        log(f"✅ Token lookup for slug={slug!r}: YES={clob_ids[0][:12]}... NO={clob_ids[1][:12]}...")
                        return (clob_ids[0], clob_ids[1])
        resp2 = http_client.get(
            f"{POLY_GAMMA_URL}/markets",
            venue="polymarket",
            params={"slug": slug, "limit": 3},
            timeout=10,
        )
        if resp2 and resp2.status_code == 200:
            markets = resp2.json()
            if isinstance(markets, dict):
                markets = markets.get("markets", [markets])
            for m in (markets if isinstance(markets, list) else []):
                clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
                if len(clob_ids) >= 2:
                    log(f"✅ Token lookup (markets fallback) slug={slug!r}: YES={clob_ids[0][:12]}...")
                    return (clob_ids[0], clob_ids[1])
    except Exception as e:
        log(f"⚠️ lookup_token_ids_by_slug({slug!r}): {e}")
    log(f"⚠️ lookup_token_ids_by_slug: no tokens found for slug={slug!r}")
    return None


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
    if not token_id:
        log("⚠️ poly_orderbook_missing: no token_id provided")
        return {"best_bid": None, "best_ask": None, "bid_size": 0, "ask_size": 0, "warning": "poly_orderbook_missing"}

    book = fetch_orderbook(token_id)
    if not book:
        log(f"⚠️ poly_orderbook_missing: fetch returned None for token {token_id[:16]}...")
        return {"best_bid": None, "best_ask": None, "bid_size": 0, "ask_size": 0, "warning": "poly_orderbook_missing"}

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
