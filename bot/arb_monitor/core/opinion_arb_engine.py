"""Arb engine for Polymarket × Opinion.Markets pairs.

Fetches live orderbooks from both platforms and computes arb routes:
  Route A: poly YES ask  + opinion NO ask  — buy YES on Poly, NO on Opinion
  Route B: opinion YES ask + poly NO ask  — buy YES on Opinion, NO on Poly

Arb exists when min(routeA, routeB) < 1.0.
"""

import time
from urllib.parse import quote_plus

from ..adapters import pmxt_adapter
from ..adapters.opinion import fetch_orderbook as fetch_opinion_orderbook
from ..config import NEAR_ARB_MAX_COST, MIN_PRICE_THRESHOLD


def log(msg: str):
    print(f"⚡ [OpinionArb] {msg}")


def _opinion_url(market_id: str, title: str) -> str:
    if market_id:
        return f"https://opinion.trade/market/{market_id}"
    if title:
        return f"https://opinion.trade/search?q={quote_plus(title)}"
    return ""


def _poly_url(pair: dict) -> str:
    url = pair.get("polymarket_url", "")
    pm_id = pair.get("polymarket", {}).get("id", "")
    if url and url.startswith("http"):
        return url
    if pm_id:
        return f"https://polymarket.com/event/{pm_id}"
    return ""


def _best_ask_from_opinion_book(book: dict) -> tuple[float | None, float]:
    """Extract best ask price and size from Opinion orderbook response.

    Opinion returns asks already sorted best-first (lowest ask at index 0).
    Returns None if no asks present.
    """
    if not book:
        return None, 0
    asks = book.get("asks", []) or []
    if not asks:
        return None, 0
    best = asks[0]
    try:
        price = float(best.get("price", 0))
        size = float(best.get("size", 0))
        return (price if price > MIN_PRICE_THRESHOLD else None), size
    except (TypeError, ValueError):
        return None, 0


def analyze_opinion_pair(pair: dict, debug: bool = False) -> dict | None:
    """Analyze a Polymarket × Opinion pair for arbitrage.

    Returns a result dict (opportunity / near_arb / watchlist) or None if skipped.
    """
    pm = pair.get("polymarket", {})
    op = pair.get("opinion", {})

    pm_yes_token = pm.get("yes_token", "")
    pm_no_token = pm.get("no_token", "")
    op_yes_token = op.get("yes_token", "")
    op_no_token = op.get("no_token", "")

    warnings = []

    if not pm_yes_token or not pm_no_token:
        return None
    if not op_yes_token or not op_no_token:
        warnings.append("opinion_token_missing")
        if not debug:
            return None

    poly_url = _poly_url(pair)
    opinion_url = _opinion_url(op.get("id", ""), op.get("question", ""))

    pm_yes_book = pmxt_adapter.fetch_poly_orderbook(pm_yes_token)
    pm_no_book = pmxt_adapter.fetch_poly_orderbook(pm_no_token)

    op_yes_book = fetch_opinion_orderbook(op_yes_token) if op_yes_token else {}
    op_no_book = fetch_opinion_orderbook(op_no_token) if op_no_token else {}

    pm_yes_ask = pm_yes_book.get("best_ask")
    pm_no_ask = pm_no_book.get("best_ask")
    op_yes_ask, op_yes_size = _best_ask_from_opinion_book(op_yes_book)
    op_no_ask, op_no_size = _best_ask_from_opinion_book(op_no_book)

    ai_score = pair.get("ai_score", {})
    routes = []

    if pm_yes_ask is not None and op_no_ask is not None:
        if pm_yes_ask > MIN_PRICE_THRESHOLD and op_no_ask > MIN_PRICE_THRESHOLD:
            cost = pm_yes_ask + op_no_ask
            edge = round(1.0 - cost, 4)
            roi = round(edge / cost * 100, 2) if cost > 0 else 0
            routes.append({
                "route": "poly_yes+opinion_no",
                "cost": cost,
                "edge": edge,
                "roi": roi,
                "legs": [
                    {
                        "platform": "Polymarket",
                        "side": "YES",
                        "price": pm_yes_ask,
                        "size": pm_yes_book.get("ask_size", 0),
                        "url": poly_url,
                    },
                    {
                        "platform": "Opinion",
                        "side": "NO",
                        "price": op_no_ask,
                        "size": op_no_size,
                        "url": opinion_url,
                    },
                ],
            })

    if op_yes_ask is not None and pm_no_ask is not None:
        if op_yes_ask > MIN_PRICE_THRESHOLD and pm_no_ask > MIN_PRICE_THRESHOLD:
            cost = op_yes_ask + pm_no_ask
            edge = round(1.0 - cost, 4)
            roi = round(edge / cost * 100, 2) if cost > 0 else 0
            routes.append({
                "route": "opinion_yes+poly_no",
                "cost": cost,
                "edge": edge,
                "roi": roi,
                "legs": [
                    {
                        "platform": "Opinion",
                        "side": "YES",
                        "price": op_yes_ask,
                        "size": op_yes_size,
                        "url": opinion_url,
                    },
                    {
                        "platform": "Polymarket",
                        "side": "NO",
                        "price": pm_no_ask,
                        "size": pm_no_book.get("ask_size", 0),
                        "url": poly_url,
                    },
                ],
            })

    if not routes:
        if debug:
            return {
                "type": "watchlist",
                "pairType": "poly_opinion",
                "pairId": pair["pair_id"],
                "title": pair["title"],
                "opinionTitle": pair.get("opinion_title", ""),
                "polyUrl": poly_url,
                "opinionUrl": opinion_url,
                "sport": pair.get("sport"),
                "expiryTs": pair.get("expiry_ts", 0),
                "minCost": None,
                "edge": 0,
                "roi": 0,
                "route": "no_prices",
                "confidence": ai_score.get("confidence", 0),
                "aiScore": ai_score,
                "legs": [],
                "updatedTs": int(time.time()),
                "warnings": warnings + ["no_prices"],
            }
        return None

    best = min(routes, key=lambda r: r["cost"])
    min_cost = best["cost"]
    best_edge = best["edge"]
    best_roi = best["roi"]

    base = {
        "pairType": "poly_opinion",
        "pairId": pair["pair_id"],
        "title": pair["title"],
        "opinionTitle": pair.get("opinion_title", ""),
        "polyUrl": poly_url,
        "opinionUrl": opinion_url,
        "sport": pair.get("sport"),
        "expiryTs": pair.get("expiry_ts", 0),
        "minCost": round(min_cost, 4),
        "edge": best_edge,
        "roi": best_roi,
        "route": best["route"],
        "confidence": ai_score.get("confidence", 0),
        "aiScore": ai_score,
        "legs": best["legs"],
        "updatedTs": int(time.time()),
        "warnings": warnings,
        "priceSource": "live_orderbook",
    }

    if best_edge > 0:
        return {**base, "type": "opportunity"}
    elif min_cost <= NEAR_ARB_MAX_COST:
        return {**base, "type": "near_arb"}
    else:
        return {**base, "type": "watchlist"}
