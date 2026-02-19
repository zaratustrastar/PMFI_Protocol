"""Polymarket adapter - fetches markets and orderbook data."""

import time
import requests
from typing import Optional
from ..config import POLY_GAMMA_URL, POLY_CLOB_URL


def log(msg: str):
    print(f"📊 [Arb/Polymarket] {msg}")


def fetch_active_markets(limit: int = 200, offset: int = 0) -> list[dict]:
    """Fetch active binary markets from Polymarket Gamma API."""
    try:
        url = f"{POLY_GAMMA_URL}/markets"
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        log(f"Fetching markets: limit={limit} offset={offset}")
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            log(f"Markets API returned {resp.status_code}")
            return []
        markets = resp.json()
        if not isinstance(markets, list):
            markets = markets.get("data", markets.get("markets", []))
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
    """Fetch orderbook from Polymarket CLOB for a given token."""
    try:
        url = f"{POLY_CLOB_URL}/book"
        resp = requests.get(url, params={"token_id": token_id}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data
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
    """Normalize a Polymarket market into a standard format."""
    tokens = market.get("tokens", market.get("clobTokenIds", []))
    yes_token = None
    no_token = None

    if isinstance(tokens, list) and len(tokens) >= 2:
        if isinstance(tokens[0], dict):
            for t in tokens:
                outcome = t.get("outcome", "").upper()
                if outcome == "YES":
                    yes_token = t.get("token_id")
                elif outcome == "NO":
                    no_token = t.get("token_id")
        else:
            yes_token = tokens[0] if len(tokens) > 0 else None
            no_token = tokens[1] if len(tokens) > 1 else None

    clob_ids = market.get("clobTokenIds", "")
    if not yes_token and isinstance(clob_ids, str) and clob_ids:
        parts = clob_ids.split(",") if "," in clob_ids else [clob_ids]
        yes_token = parts[0].strip() if len(parts) > 0 else None
        no_token = parts[1].strip() if len(parts) > 1 else None

    end_date = market.get("endDate", market.get("end_date_iso", ""))
    expiry_ts = 0
    if end_date:
        try:
            from datetime import datetime
            if "T" in str(end_date):
                dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                expiry_ts = int(dt.timestamp())
            else:
                expiry_ts = int(float(end_date))
        except Exception:
            pass

    return {
        "venue": "polymarket",
        "id": market.get("condition_id", market.get("id", "")),
        "question": market.get("question", market.get("title", "")),
        "slug": market.get("slug", ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "expiry_ts": expiry_ts,
        "active": market.get("active", True),
        "volume": float(market.get("volume", 0) or 0),
    }


def _is_binary(market: dict) -> bool:
    """Check if a market is binary (YES/NO)."""
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            import json
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
    return len(market.get("clobTokenIds", "").split(",")) == 2 if market.get("clobTokenIds") else False
