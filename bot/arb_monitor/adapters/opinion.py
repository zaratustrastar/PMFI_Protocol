"""Opinion adapter - fetches markets and orderbook data via Opinion Open API.

API reference: https://docs.opinion.trade/developer-guide/opinion-open-api
Base URL: https://proxy.opinion.trade:8443/openapi
Auth: apikey header
Rate limit: 15 req/s — we add small delays between orderbook fetches.
"""

import time
from datetime import datetime, timezone
from typing import Optional
from ..config import (
    OPINION_BASE_URL, OPINION_API_KEY,
    OPINION_MAX_PAGES,
    OPINION_ORDERBOOK_DELAY, ARB_EXPIRY_WINDOW_DAYS,
)
from .. import http_client
from ..models import NormalizedMarket, extract_team_key


_last_opinion_stats: dict = {}

# Module-level cache: marketId (str) → (yes_token_id, no_token_id)
# Populated during fetch_all_active_markets so executor can look up tokens
# without a live API call. Oddpool-supplied market IDs match these keys.
_MARKET_TOKEN_CACHE: dict[str, tuple[str, str]] = {}


def log(msg: str):
    print(f"💬 [Arb/Opinion] {msg}")


def get_discovery_stats() -> dict:
    return dict(_last_opinion_stats)


def _headers() -> dict:
    return {
        "apikey": OPINION_API_KEY,
        "Content-Type": "application/json",
    }


def _parse_expiry(market: dict) -> int:
    for field in ("cutoffAt", "endAt", "endDate", "end_at", "closeTime", "close_time"):
        val = market.get(field)
        if not val:
            continue
        try:
            if isinstance(val, (int, float)):
                ts = int(val)
                if ts > 1e12:
                    ts = ts // 1000
                if ts > 0:
                    return ts
            elif isinstance(val, str) and "T" in val:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return int(dt.timestamp())
            else:
                ts = int(float(val))
                if ts > 0:
                    return ts
        except Exception:
            continue
    return 0


_DIRECTION_MARKET_KEYWORDS = (
    "up or down", "hourly", "1hr", "15m", "30m", "4hr", "daily close",
)


def _is_direction_market(title: str) -> bool:
    """Return True for short-term price-direction markets that can never arb against Polymarket."""
    t = title.lower()
    return any(kw in t for kw in _DIRECTION_MARKET_KEYWORDS)


def fetch_all_active_markets() -> tuple[list[dict], dict]:
    global _last_opinion_stats
    stats = {
        "fetchedTotal": 0,
        "excludedClosed": 0,
        "excludedMissingTokens": 0,
        "excludedExpiry": 0,
        "excludedDirection": 0,
        "includedFinal": 0,
        "apiErrors": [],
    }

    if not OPINION_API_KEY:
        err = "OPINION_API_KEY not set"
        log(f"❌ {err}")
        stats["apiErrors"].append(err)
        _last_opinion_stats = stats
        return [], stats

    now = int(time.time())
    max_expiry = now + ARB_EXPIRY_WINDOW_DAYS * 86400

    accepted: list[dict] = []
    seen_ids: set = set()

    # Opinion API uses ?page=N (1-indexed) not ?offset=N.
    # We use status=activated so the API pre-filters to live markets.
    API_PAGE_SIZE = 20

    for page in range(OPINION_MAX_PAGES):
        url = f"{OPINION_BASE_URL}/market"
        params = {
            "status": "activated",
            "limit": API_PAGE_SIZE,
            "page": page + 1,  # 1-indexed
        }
        log(f"Fetching page {page + 1}: page={page + 1} limit={API_PAGE_SIZE}")

        resp = http_client.get(
            url, venue="opinion",
            params=params,
            headers=_headers(),
            timeout=15,
        )

        if resp is None:
            err = f"Page {page + 1}: API returned None"
            log(f"❌ {err}")
            stats["apiErrors"].append(err)
            break

        if resp.status_code != 200:
            err = f"Page {page + 1}: HTTP {resp.status_code}"
            log(f"❌ {err}")
            stats["apiErrors"].append(err)
            break

        try:
            data = resp.json()
        except Exception as e:
            err = f"Page {page + 1}: JSON parse error: {e}"
            log(f"❌ {err}")
            stats["apiErrors"].append(err)
            break

        err_code = data.get("errno", data.get("code"))
        if err_code is not None and err_code != 0:
            err = f"Page {page + 1}: API error errno={err_code} msg={data.get('errmsg', data.get('msg'))}"
            log(f"❌ {err}")
            stats["apiErrors"].append(err)
            break

        result = data.get("result", {})
        markets = result.get("list", [])

        if not markets:
            log(f"Page {page + 1}: empty list, stopping pagination")
            break

        page_new = 0
        page_dupes = 0
        for m in markets:
            stats["fetchedTotal"] += 1

            mid = m.get("marketId")
            if not mid or mid in seen_ids:
                page_dupes += 1
                continue
            seen_ids.add(mid)

            status_enum = (m.get("statusEnum", "") or "").lower()
            if status_enum not in ("activated", "active", ""):
                stats["excludedClosed"] += 1
                continue

            yes_token = m.get("yesTokenId", "")
            no_token = m.get("noTokenId", "")
            if not yes_token or not no_token:
                stats["excludedMissingTokens"] += 1
                continue

            title = m.get("marketTitle", "") or m.get("title", "")
            if _is_direction_market(title):
                stats["excludedDirection"] += 1
                continue

            expiry_ts = _parse_expiry(m)
            if expiry_ts > 0 and (expiry_ts <= now or expiry_ts > max_expiry):
                stats["excludedExpiry"] += 1
                continue

            m["_parsed_expiry"] = expiry_ts
            accepted.append(m)
            page_new += 1
            # Populate token cache so executor can resolve these IDs without
            # making additional API calls during execution.
            _MARKET_TOKEN_CACHE[str(mid)] = (yes_token, no_token)

        log(f"Page {page + 1}: {len(markets)} fetched, {page_new} new accepted, {page_dupes} dupes")

        # If all markets on this page were duplicates, the API has looped — stop
        if page_dupes == len(markets) and page > 0:
            log(f"Page {page + 1}: all dupes detected — API pagination exhausted, stopping")
            break

        total_available = result.get("total", 0)
        fetched_so_far = (page + 1) * API_PAGE_SIZE
        if total_available and fetched_so_far >= total_available:
            break
        if len(markets) < 10:
            break

    stats["includedFinal"] = len(accepted)
    _last_opinion_stats = stats
    log(
        f"Total: {stats['fetchedTotal']} fetched → {stats['includedFinal']} included "
        f"(closed={stats['excludedClosed']}, missingTokens={stats['excludedMissingTokens']}, "
        f"direction={stats['excludedDirection']}, expiry={stats['excludedExpiry']})"
    )
    if stats["apiErrors"]:
        log(f"⚠️ API errors: {stats['apiErrors']}")
    return accepted, stats


def normalize_market(market: dict) -> Optional[NormalizedMarket]:
    mid = str(market.get("marketId", ""))
    title = market.get("marketTitle", "") or market.get("title", "")
    yes_token = market.get("yesTokenId", "")
    no_token = market.get("noTokenId", "")

    if not mid or not title:
        return None
    if not yes_token or not no_token:
        log(f"⚠️ Skipping market {mid} (missing YES/NO tokens): {title[:60]}")
        return None

    expiry_ts = market.get("_parsed_expiry") or _parse_expiry(market)
    team_key = extract_team_key(title)

    description = (
        market.get("marketQuestion", "")
        or market.get("question", "")
        or market.get("description", "")
        or ""
    ).strip()

    return NormalizedMarket(
        venue="opinion",
        marketId=mid,
        title=title,
        expiryTs=expiry_ts,
        yesTokenId=yes_token,
        noTokenId=no_token,
        team_key=team_key,
        description=description,
        meta={
            "volume": market.get("volume", "0"),
            "volume24h": market.get("volume24h", "0"),
            "marketId": mid,
        },
    )


def get_opinion_markets() -> list[NormalizedMarket]:
    from ..core.filters import classify_sport
    raw, _stats = fetch_all_active_markets()
    normalized = []
    for m in raw:
        nm = normalize_market(m)
        if nm is not None:
            sport = classify_sport(nm.title)
            nm.sport = sport
            normalized.append(nm)
    log(f"Normalized {len(normalized)} markets from {len(raw)} raw")
    return normalized


def lookup_token_ids_by_market_id(market_id: str) -> Optional[tuple[str, str]]:
    """Fetch YES/NO token IDs for an Opinion market by its marketId.
    Returns (yes_token_id, no_token_id) or None on failure.

    Strategy:
    1. Check in-memory discovery cache (populated during fetch_all_active_markets).
       Covers all Oddpool-supplied IDs since discovery runs before execution.
    2. Try the single-market endpoint /market/{market_id}.
    3. Fall back to filtered list /market?marketId={market_id}.

    All API paths include verbose debug logging of raw responses so ID-format
    mismatches between Oddpool-supplied IDs and Opinion's actual IDs are
    immediately visible in logs.
    """
    if not market_id or not OPINION_API_KEY:
        log(f"⚠️ lookup_token_ids_by_market_id: marketId={market_id!r} skipped — "
            f"{'OPINION_API_KEY not set' if not OPINION_API_KEY else 'empty market_id'}")
        return None

    cache_size = len(_MARKET_TOKEN_CACHE)
    log(f"🔍 Token lookup for marketId={market_id!r} | cache_size={cache_size} | "
        f"cache_keys_sample={list(_MARKET_TOKEN_CACHE.keys())[:8]!r}")

    # 1. Check discovery cache first — fastest, no API call needed.
    cached = _MARKET_TOKEN_CACHE.get(str(market_id))
    if cached:
        yes, no = cached
        log(f"✅ Token lookup (cache hit) marketId={market_id!r}: YES={yes[:12]}... NO={no[:12]}...")
        return cached

    log(f"⚠️ marketId={market_id!r} not in cache — falling back to live API (cache may be empty or ID format mismatch)")

    try:
        # Path 2: single-market endpoint
        url1 = f"{OPINION_BASE_URL}/market/{market_id}"
        log(f"📡 Token lookup path 2: GET {url1}")
        resp = http_client.get(
            url1,
            venue="opinion",
            headers=_headers(),
            timeout=10,
        )
        if resp is not None:
            log(f"📡 Token lookup path 2 status={resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # /market/{id} wraps the market object under result.data
                    result = data.get("result", {})
                    if isinstance(result, dict) and "data" in result:
                        result = result["data"]
                    elif not isinstance(result, dict):
                        result = data.get("data", data)
                    log(f"📡 Token lookup path 2 raw keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__!r}")
                    if isinstance(result, dict):
                        yes = result.get("yesTokenId", "")
                        no = result.get("noTokenId", "")
                        log(f"📡 Token lookup path 2: yesTokenId={yes!r} noTokenId={no!r}")
                        if yes and no:
                            _MARKET_TOKEN_CACHE[str(market_id)] = (yes, no)
                            log(f"✅ Token lookup (live /market/{{id}}) marketId={market_id!r}: YES={yes[:12]}... NO={no[:12]}...")
                            return (yes, no)
                except Exception as _je:
                    log(f"⚠️ Token lookup path 2 JSON parse error: {_je} | raw={resp.text[:200]!r}")
            else:
                log(f"⚠️ Token lookup path 2 unexpected status {resp.status_code}: {resp.text[:200]!r}")
        else:
            log(f"⚠️ Token lookup path 2: http_client returned None (connection error)")

        # Path 3: filtered list endpoint
        url3 = f"{OPINION_BASE_URL}/market"
        log(f"📡 Token lookup path 3: GET {url3}?marketId={market_id}&limit=5")
        resp2 = http_client.get(
            url3,
            venue="opinion",
            params={"marketId": market_id, "limit": 5},
            headers=_headers(),
            timeout=10,
        )
        if resp2 is not None:
            log(f"📡 Token lookup path 3 status={resp2.status_code}")
            if resp2.status_code == 200:
                try:
                    data = resp2.json()
                    result = data.get("result", {})
                    log(f"📡 Token lookup path 3 result type={type(result).__name__!r} keys={list(result.keys()) if isinstance(result, dict) else '?'!r}")
                    markets = result.get("list", []) if isinstance(result, dict) else []
                    log(f"📡 Token lookup path 3: {len(markets)} markets returned | "
                        f"marketIds={[m.get('marketId') for m in markets]!r}")
                    for m in markets:
                        mid_str = str(m.get("marketId", ""))
                        yes = m.get("yesTokenId", "")
                        no = m.get("noTokenId", "")
                        log(f"📡   market marketId={mid_str!r} yesTokenId={yes!r} noTokenId={no!r}")
                        if mid_str == str(market_id):
                            if yes and no:
                                _MARKET_TOKEN_CACHE[str(market_id)] = (yes, no)
                                log(f"✅ Token lookup (list fallback) marketId={market_id!r}: YES={yes[:12]}...")
                                return (yes, no)
                except Exception as _je:
                    log(f"⚠️ Token lookup path 3 JSON parse error: {_je} | raw={resp2.text[:200]!r}")
            else:
                log(f"⚠️ Token lookup path 3 unexpected status {resp2.status_code}: {resp2.text[:200]!r}")
        else:
            log(f"⚠️ Token lookup path 3: http_client returned None (connection error)")

    except Exception as e:
        log(f"⚠️ lookup_token_ids_by_market_id({market_id!r}) exception: {e}")

    log(f"❌ lookup_token_ids_by_market_id: no tokens found for marketId={market_id!r} "
        f"(cache_size={len(_MARKET_TOKEN_CACHE)}, checked both /market/{{id}} and /market?marketId={{id}})")
    return None


def fetch_orderbook(token_id: str) -> Optional[dict]:
    if not token_id:
        return None

    url = f"{OPINION_BASE_URL}/token/orderbook"
    resp = http_client.get(
        url, venue="opinion",
        params={"token_id": token_id},
        headers=_headers(),
        timeout=10,
    )

    if resp is None or resp.status_code != 200:
        return None

    try:
        data = resp.json()
        err_code = data.get("errno", data.get("code"))
        if err_code is not None and err_code != 0:
            log(f"⚠️ Orderbook error for {token_id[:20]}: errno={err_code} msg={data.get('errmsg', data.get('msg'))}")
            return None
        return data.get("result", {})
    except Exception as e:
        log(f"Orderbook parse error for {token_id[:20]}: {e}")
        return None


def get_best_prices(yes_token_id: str, no_token_id: str) -> dict:
    empty = {
        "yes_best_bid": None, "yes_best_ask": None,
        "no_best_bid": None, "no_best_ask": None,
        "yes_bid_size": 0, "yes_ask_size": 0,
        "no_bid_size": 0, "no_ask_size": 0,
    }

    if not yes_token_id or not no_token_id:
        log("⚠️ opinion_orderbook_missing: no token IDs provided")
        empty["warning"] = "opinion_orderbook_missing"
        return empty

    yes_book = fetch_orderbook(yes_token_id)

    if OPINION_ORDERBOOK_DELAY > 0:
        time.sleep(OPINION_ORDERBOOK_DELAY)

    no_book = fetch_orderbook(no_token_id)

    result = dict(empty)

    if yes_book:
        yes_asks = yes_book.get("asks", [])
        yes_bids = yes_book.get("bids", [])
        if yes_asks:
            best = min(yes_asks, key=lambda x: float(x["price"]))
            result["yes_best_ask"] = float(best["price"])
            result["yes_ask_size"] = float(best.get("size", 0))
        if yes_bids:
            best = max(yes_bids, key=lambda x: float(x["price"]))
            result["yes_best_bid"] = float(best["price"])
            result["yes_bid_size"] = float(best.get("size", 0))

    if no_book:
        no_asks = no_book.get("asks", [])
        no_bids = no_book.get("bids", [])
        if no_asks:
            best = min(no_asks, key=lambda x: float(x["price"]))
            result["no_best_ask"] = float(best["price"])
            result["no_ask_size"] = float(best.get("size", 0))
        if no_bids:
            best = max(no_bids, key=lambda x: float(x["price"]))
            result["no_best_bid"] = float(best["price"])
            result["no_bid_size"] = float(best.get("size", 0))

    return result
