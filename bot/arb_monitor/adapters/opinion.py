"""Opinion.trade adapter - fetches markets and orderbook data."""

import time
import requests
from typing import Optional
from ..config import OPINION_API_KEY, OPINION_BASE_URL


def log(msg: str):
    print(f"💭 [Arb/Opinion] {msg}")


def _headers() -> dict:
    h = {"accept": "application/json"}
    if OPINION_API_KEY:
        h["Authorization"] = f"Bearer {OPINION_API_KEY}"
    return h


def fetch_active_markets(limit: int = 200, offset: int = 0) -> list[dict]:
    """Fetch active markets from Opinion API."""
    try:
        url = f"{OPINION_BASE_URL}/market/active"
        params = {"limit": limit, "offset": offset}
        log(f"Fetching markets: limit={limit} offset={offset}")
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)
        if resp.status_code != 200:
            log(f"Markets API returned {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        binary = [m for m in markets if _is_binary(m)]
        log(f"Fetched {len(markets)} markets, {len(binary)} binary")
        return binary
    except Exception as e:
        log(f"Error fetching markets: {e}")
        return []


def fetch_all_active_markets(max_pages: int = 5) -> list[dict]:
    """Paginate through active markets."""
    all_markets = []
    for page in range(max_pages):
        batch = fetch_active_markets(limit=200, offset=page * 200)
        all_markets.extend(batch)
        if len(batch) < 200:
            break
    return all_markets


def fetch_orderbook(token_id: str) -> Optional[dict]:
    """Fetch orderbook from Opinion for a given token."""
    try:
        url = f"{OPINION_BASE_URL}/token/orderbook"
        resp = requests.get(url, headers=_headers(), params={"token_id": token_id}, timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        log(f"Orderbook error for {token_id}: {e}")
        return None


def get_best_prices(token_id: str) -> dict:
    """Get best bid/ask from orderbook."""
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


def normalize_market(market: dict) -> dict:
    """Normalize an Opinion market into a standard format."""
    tokens = market.get("tokens", [])
    yes_token = None
    no_token = None

    if isinstance(tokens, list):
        for t in tokens:
            if isinstance(t, dict):
                outcome = str(t.get("outcome", t.get("name", ""))).upper()
                tid = t.get("token_id", t.get("id", ""))
                if outcome in ("YES", "Y"):
                    yes_token = tid
                elif outcome in ("NO", "N"):
                    no_token = tid
            elif isinstance(t, str):
                if not yes_token:
                    yes_token = t
                elif not no_token:
                    no_token = t

    if not yes_token:
        yes_token = market.get("yes_token_id", market.get("yesTokenId", ""))
    if not no_token:
        no_token = market.get("no_token_id", market.get("noTokenId", ""))

    expiry_ts = 0
    end_date = market.get("endDate", market.get("end_date", market.get("expirationDate", "")))
    if end_date:
        try:
            from datetime import datetime
            if isinstance(end_date, (int, float)):
                expiry_ts = int(end_date)
            elif "T" in str(end_date):
                dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                expiry_ts = int(dt.timestamp())
        except Exception:
            pass

    return {
        "venue": "opinion",
        "id": str(market.get("id", market.get("market_id", ""))),
        "question": market.get("title", market.get("question", market.get("name", ""))),
        "slug": market.get("slug", ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "expiry_ts": expiry_ts,
        "active": True,
        "volume": float(market.get("volume", 0) or 0),
    }


def _is_binary(market: dict) -> bool:
    """Check if a market is binary (YES/NO)."""
    outcomes = market.get("outcomes", market.get("options", []))
    if isinstance(outcomes, list) and len(outcomes) == 2:
        return True
    tokens = market.get("tokens", [])
    if isinstance(tokens, list) and len(tokens) == 2:
        return True
    if market.get("yes_token_id") or market.get("yesTokenId"):
        return True
    return False
