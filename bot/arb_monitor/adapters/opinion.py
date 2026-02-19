"""Opinion.trade adapter - fetches markets and orderbook data using the OpenAPI."""

import time
import threading
from typing import Optional
from ..config import OPINION_API_KEY, OPINION_BASE_URL, ARB_MAX_PAGES_OPINION, ARB_DETAIL_CACHE_TTL
from .. import http_client
from ..models import NormalizedMarket


if not OPINION_API_KEY:
    print("⚠️ [Arb/Opinion] OPINION_API_KEY not set — Opinion.trade API calls will fail with 401")


def log(msg: str):
    print(f"💭 [Arb/Opinion] {msg}")


def _headers() -> dict:
    h = {"accept": "application/json"}
    if OPINION_API_KEY:
        h["apikey"] = OPINION_API_KEY
    return h


_detail_cache: dict[str, tuple[float, dict]] = {}
_detail_cache_lock = threading.Lock()


def _get_cached_detail(market_id: str) -> Optional[dict]:
    with _detail_cache_lock:
        entry = _detail_cache.get(market_id)
        if entry and (time.time() - entry[0]) < ARB_DETAIL_CACHE_TTL:
            return entry[1]
    return None


def _set_cached_detail(market_id: str, detail: dict):
    with _detail_cache_lock:
        _detail_cache[market_id] = (time.time(), detail)


def fetch_market_detail(market_id: str) -> Optional[dict]:
    cached = _get_cached_detail(market_id)
    if cached:
        return cached
    url = f"{OPINION_BASE_URL}/market/{market_id}"
    resp = http_client.get(url, venue="opinion", headers=_headers(), timeout=10, max_retries=2)
    if resp is None or resp.status_code != 200:
        log(f"Detail fetch failed for market {market_id}")
        return None
    try:
        data = resp.json()
        detail = data if isinstance(data, dict) else {}
        _set_cached_detail(market_id, detail)
        return detail
    except Exception as e:
        log(f"Detail parse error for {market_id}: {e}")
        return None


def _fetch_page(page: int, limit: int, sort_by: str) -> list[dict]:
    url = f"{OPINION_BASE_URL}/market"
    params = {
        "status": "activated",
        "marketType": "0",
        "limit": limit,
        "page": page,
        "sortBy": sort_by,
    }
    resp = http_client.get(url, venue="opinion", params=params, headers=_headers())
    if resp is None or resp.status_code != 200:
        log(f"Page fetch failed: page={page} sort={sort_by} status={resp.status_code if resp is not None else 'None'}")
        return []
    try:
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", data.get("markets", data.get("items", [])))
        if not isinstance(items, list):
            items = []
        return items
    except Exception as e:
        log(f"Page parse error: {e}")
        return []


def fetch_all_active_markets(max_pages: int = None) -> list[dict]:
    if max_pages is None:
        max_pages = ARB_MAX_PAGES_OPINION
    seen_ids: set[str] = set()
    all_markets: list[dict] = []
    limit = 20

    pages_per_pass = max_pages // 2 if max_pages >= 2 else max_pages

    for sort_by in ["ending soon", "volume7d desc"]:
        log(f"Pass: sortBy='{sort_by}' (up to {pages_per_pass} pages)")
        empty_streak = 0
        for page in range(1, pages_per_pass + 1):
            batch = _fetch_page(page, limit, sort_by)
            if not batch:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            new_count = 0
            for m in batch:
                mid = str(m.get("marketId", m.get("id", "")))
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    if _is_binary(m):
                        all_markets.append(m)
                        new_count += 1
            log(f"  page {page}: {len(batch)} items, {new_count} new binary")
            if len(batch) < limit:
                break

    log(f"Total Opinion markets discovered: {len(all_markets)}")
    return all_markets


def _extract_token_ids(market: dict) -> tuple[str, str]:
    yes_token = market.get("yesTokenId", market.get("yes_token_id", ""))
    no_token = market.get("noTokenId", market.get("no_token_id", ""))

    if yes_token and no_token:
        return str(yes_token), str(no_token)

    tokens = market.get("tokens", market.get("outcomes", []))
    if isinstance(tokens, list):
        for t in tokens:
            if isinstance(t, dict):
                outcome = str(t.get("outcome", t.get("name", t.get("title", "")))).upper()
                tid = str(t.get("token_id", t.get("tokenId", t.get("id", ""))))
                if outcome in ("YES", "Y") and tid:
                    yes_token = tid
                elif outcome in ("NO", "N") and tid:
                    no_token = tid

    if yes_token and no_token:
        return str(yes_token), str(no_token)

    mid = str(market.get("marketId", market.get("id", "")))
    if mid:
        detail = fetch_market_detail(mid)
        if detail:
            yes_token = detail.get("yesTokenId", detail.get("yes_token_id", ""))
            no_token = detail.get("noTokenId", detail.get("no_token_id", ""))
            if not yes_token or not no_token:
                dtokens = detail.get("tokens", detail.get("outcomes", []))
                if isinstance(dtokens, list):
                    for t in dtokens:
                        if isinstance(t, dict):
                            outcome = str(t.get("outcome", t.get("name", t.get("title", "")))).upper()
                            tid = str(t.get("token_id", t.get("tokenId", t.get("id", ""))))
                            if outcome in ("YES", "Y") and tid:
                                yes_token = tid
                            elif outcome in ("NO", "N") and tid:
                                no_token = tid

    return str(yes_token or ""), str(no_token or "")


def normalize_market(market: dict) -> NormalizedMarket:
    yes_token, no_token = _extract_token_ids(market)

    expiry_ts = 0
    for field_name in ("cutoffAt", "cutoff_at", "endDate", "end_date", "expirationDate"):
        val = market.get(field_name)
        if val:
            try:
                if isinstance(val, (int, float)):
                    expiry_ts = int(val)
                    if expiry_ts < 1e10:
                        expiry_ts = int(expiry_ts * 1000)
                elif isinstance(val, str):
                    if val.isdigit():
                        expiry_ts = int(val)
                    else:
                        from datetime import datetime
                        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        expiry_ts = int(dt.timestamp())
                if expiry_ts > 0:
                    break
            except Exception:
                continue

    title = market.get("marketTitle", market.get("title", market.get("question", market.get("name", ""))))

    return NormalizedMarket(
        venue="opinion",
        marketId=str(market.get("marketId", market.get("id", ""))),
        title=title,
        expiryTs=expiry_ts,
        yesTokenId=yes_token,
        noTokenId=no_token,
        meta={
            "createdAt": market.get("createdAt", ""),
            "slug": market.get("slug", ""),
            "volume": market.get("volume", 0),
        },
    )


def fetch_orderbook(token_id: str) -> Optional[dict]:
    url = f"{OPINION_BASE_URL}/token/orderbook"
    resp = http_client.get(url, venue="opinion", params={"token_id": token_id}, headers=_headers(), timeout=10)
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


def get_opinion_markets() -> list[NormalizedMarket]:
    from ..core.filters import filter_by_expiry, classify_sport
    raw = fetch_all_active_markets()
    normalized = [normalize_market(m) for m in raw]
    filtered = filter_by_expiry(normalized)
    for nm in filtered:
        nm.sport = classify_sport(nm.title)
    return filtered


def _is_binary(market: dict) -> bool:
    market_type = market.get("marketType", market.get("market_type"))
    if market_type is not None:
        try:
            if int(market_type) == 0:
                return True
        except (ValueError, TypeError):
            pass

    outcomes = market.get("outcomes", market.get("options", []))
    if isinstance(outcomes, list) and len(outcomes) == 2:
        return True
    tokens = market.get("tokens", [])
    if isinstance(tokens, list) and len(tokens) == 2:
        return True
    if market.get("yesTokenId") or market.get("yes_token_id"):
        return True
    return False
