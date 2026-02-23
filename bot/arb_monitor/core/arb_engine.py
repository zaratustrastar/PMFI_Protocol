"""Arb Engine - detects arbitrage opportunities from matched Polymarket × Kalshi pairs.

Uses PMXT unified stack:
  - Polymarket: real orderbook asks via PMXT fetch_order_book (no auth needed)
  - Kalshi: listing prices from market data (orderbook requires auth)

Arb math:
  Route A: poly YES ask + kalshi NO price
  Route B: kalshi YES price + poly NO ask
  Arb exists if min(routeA, routeB) < 1.0

Near-arb: pairs where minCost <= NEAR_ARB_MAX_COST (default 1.01) shown on watchlist
so UI isn't empty even without live arbs.
"""

import time
from urllib.parse import quote_plus
from ..adapters import pmxt_adapter
from ..config import NEAR_ARB_MAX_COST, MIN_PRICE_THRESHOLD


def log(msg: str):
    print(f"⚡ [Arb/Engine] {msg}")


def _build_urls(pair: dict) -> tuple[str, str]:
    poly_url = pair.get("polymarket_url", "")
    pm_id = pair.get("polymarket", {}).get("id", "")

    if poly_url and poly_url.startswith("http"):
        pass
    elif pm_id:
        poly_url = f"https://polymarket.com/event/{pm_id}"
    else:
        poly_url = ""

    kalshi_title = pair.get("kalshi_title", "") or pair.get("kalshi", {}).get("title", "") or pair.get("kalshi", {}).get("question", "")
    if kalshi_title:
        kalshi_url = f"https://kalshi.com/search?q={quote_plus(kalshi_title)}&order_by=querymatch"
    else:
        kalshi_url = ""
    return poly_url, kalshi_url


def analyze_pair(pair: dict, debug: bool = False) -> dict | None:
    pm = pair["polymarket"]
    kl = pair["kalshi"]

    pm_yes_token = pm.get("yes_token")
    pm_no_token = pm.get("no_token")
    kl_yes_token = kl.get("yes_token")
    kl_no_token = kl.get("no_token")

    warnings = []

    if not pm_yes_token or not pm_no_token:
        return None
    if not kl_yes_token or not kl_no_token:
        warnings.append("kalshi_token_missing")
        if debug:
            _poly_url, _kalshi_url = _build_urls(pair)
            return {
                "type": "watchlist",
                "pairId": pair["pair_id"],
                "title": pair["title"],
                "polyUrl": _poly_url,
                "kalshiUrl": _kalshi_url,
                "sport": pair.get("sport"),
                "expiryTs": pair.get("expiry_ts", 0),
                "minCost": None,
                "edge": 0,
                "roi": 0,
                "route": "no_prices",
                "confidence": 0,
                "legs": [],
                "updatedTs": int(time.time()),
                "warnings": warnings,
                "debugPrices": {
                    "poly_yes_token": pm_yes_token,
                    "poly_no_token": pm_no_token,
                    "kalshi_yes_token": kl_yes_token,
                    "kalshi_no_token": kl_no_token,
                },
            }
        return None

    pm_yes_book = pmxt_adapter.fetch_poly_orderbook(pm_yes_token)
    pm_no_book = pmxt_adapter.fetch_poly_orderbook(pm_no_token)

    kl_yes_book = pmxt_adapter.fetch_kalshi_orderbook(kl_yes_token)
    kl_no_book = pmxt_adapter.fetch_kalshi_orderbook(kl_no_token)

    kl_yes_has_book = kl_yes_book.get("source") == "orderbook" and kl_yes_book.get("best_ask") is not None
    kl_no_has_book = kl_no_book.get("source") == "orderbook" and kl_no_book.get("best_ask") is not None

    kl_yes_price = kl.get("yes_price") or pair.get("kalshi", {}).get("yes_price")
    kl_no_price = kl.get("no_price") or pair.get("kalshi", {}).get("no_price")

    kl_prices = {
        "yes_best_ask": kl_yes_book.get("best_ask") if kl_yes_has_book else kl_yes_price,
        "no_best_ask": kl_no_book.get("best_ask") if kl_no_has_book else kl_no_price,
        "yes_ask_size": kl_yes_book.get("ask_size", 0) if kl_yes_has_book else 0,
        "no_ask_size": kl_no_book.get("ask_size", 0) if kl_no_has_book else 0,
        "source": "orderbook" if (kl_yes_has_book or kl_no_has_book) else "listing_price",
    }

    if kl_prices["yes_best_ask"] is None and kl_prices["no_best_ask"] is None:
        log(f"No Kalshi prices for pair {pair.get('pair_id', '?')}: orderbook={kl_yes_book.get('source')}, listing yes={kl_yes_price} no={kl_no_price}")
        warnings.append("no_kalshi_prices")

    routes = []

    pm_yes_ask = pm_yes_book.get("best_ask")
    kl_no_ask = kl_prices.get("no_best_ask")
    if pm_yes_ask is not None and kl_no_ask is not None:
        if pm_yes_ask > MIN_PRICE_THRESHOLD and kl_no_ask > MIN_PRICE_THRESHOLD:
            cost = pm_yes_ask + kl_no_ask
            edge = round(1.0 - cost, 4)
            roi = round(edge / cost * 100, 2) if cost > 0 else 0
            legs = [
                {
                    "venue": "polymarket",
                    "side": "YES",
                    "tokenId": pm_yes_token,
                    "price": pm_yes_ask,
                    "size": pm_yes_book.get("ask_size") or 0,
                },
                {
                    "venue": "kalshi",
                    "side": "NO",
                    "tokenId": kl_no_token,
                    "price": kl_no_ask,
                    "size": kl_prices.get("no_ask_size") or 0,
                },
            ]
            routes.append({
                "route": "poly_YES + kalshi_NO",
                "min_cost": round(cost, 4),
                "edge": edge,
                "roi": roi,
                "legs": legs,
            })

    kl_yes_ask = kl_prices.get("yes_best_ask")
    pm_no_ask = pm_no_book.get("best_ask")
    if kl_yes_ask is not None and pm_no_ask is not None:
        if kl_yes_ask > MIN_PRICE_THRESHOLD and pm_no_ask > MIN_PRICE_THRESHOLD:
            cost = kl_yes_ask + pm_no_ask
            edge = round(1.0 - cost, 4)
            roi = round(edge / cost * 100, 2) if cost > 0 else 0
            legs = [
                {
                    "venue": "kalshi",
                    "side": "YES",
                    "tokenId": kl_yes_token,
                    "price": kl_yes_ask,
                    "size": kl_prices.get("yes_ask_size") or 0,
                },
                {
                    "venue": "polymarket",
                    "side": "NO",
                    "tokenId": pm_no_token,
                    "price": pm_no_ask,
                    "size": pm_no_book.get("ask_size") or 0,
                },
            ]
            routes.append({
                "route": "kalshi_YES + poly_NO",
                "min_cost": round(cost, 4),
                "edge": edge,
                "roi": roi,
                "legs": legs,
            })

    debug_prices = None
    if debug:
        debug_prices = {
            "poly_yes_token": pm_yes_token,
            "poly_no_token": pm_no_token,
            "poly_yes_ask": pm_yes_ask,
            "poly_no_ask": pm_no_ask,
            "poly_yes_bid": pm_yes_book.get("best_bid"),
            "poly_no_bid": pm_no_book.get("best_bid"),
            "kalshi_yes_token": kl_yes_token,
            "kalshi_no_token": kl_no_token,
            "kalshi_yes_ask": kl_prices.get("yes_best_ask"),
            "kalshi_no_ask": kl_prices.get("no_best_ask"),
            "kalshi_source": kl_prices.get("source", "listing_price"),
        }

    arb_routes = [r for r in routes if r["min_cost"] < 1.0 and r["edge"] >= 0.01]

    if not arb_routes:
        watchlist_item = _build_watchlist_item(pair, routes, debug=debug)
        if debug_prices and watchlist_item:
            watchlist_item["debugPrices"] = debug_prices
        return watchlist_item

    best = max(arb_routes, key=lambda r: r["edge"])

    min_size = _calc_min_size(best["legs"])

    confidence = _calc_confidence(best["edge"], min_size, pair.get("similarity", 0))

    if _has_real_sizes(best["legs"]) and min_size < 10:
        warnings.append("low_liquidity")
    if pair.get("similarity", 1.0) < 0.8:
        warnings.append("fuzzy_match")
    if best["edge"] < 0.02:
        warnings.append("thin_edge")
    kalshi_source = kl_prices.get("source", "listing_price")
    is_listing_only = kalshi_source != "orderbook"

    if is_listing_only:
        warnings.append("kalshi_listing_price_only")
        warnings.append("not_executable_without_orderbook")

    poly_url, kalshi_url = _build_urls(pair)
    result = {
        "type": "indicative" if is_listing_only else "opportunity",
        "pairId": pair["pair_id"],
        "title": pair["title"],
        "kalshiTitle": pair.get("kalshi_title", ""),
        "polyUrl": poly_url,
        "kalshiUrl": kalshi_url,
        "sport": pair.get("sport"),
        "expiryTs": pair.get("expiry_ts", 0),
        "minCost": best["min_cost"],
        "edge": best["edge"],
        "roi": best["roi"],
        "route": best["route"],
        "confidence": confidence,
        "legs": best["legs"],
        "updatedTs": int(time.time()),
        "warnings": warnings,
    }

    if debug and debug_prices:
        result["debugPrices"] = debug_prices

    return result


def _build_watchlist_item(pair, routes, debug: bool = False) -> dict | None:
    pm_data = pair.get("polymarket", {})
    kl_data = pair.get("kalshi", {})

    if not routes:
        if debug:
            _poly_url2, _kalshi_url2 = _build_urls(pair)
            return {
                "type": "watchlist",
                "pairId": pair["pair_id"],
                "title": pair["title"],
                "kalshiTitle": pair.get("kalshi_title", ""),
                "polyUrl": _poly_url2,
                "kalshiUrl": _kalshi_url2,
                "sport": pair.get("sport"),
                "expiryTs": pair.get("expiry_ts", 0),
                "minCost": None,
                "edge": 0,
                "roi": 0,
                "route": "no_prices",
                "confidence": 0,
                "legs": [],
                "updatedTs": int(time.time()),
                "warnings": ["no_orderbook_data"],
            }
        return None

    best = min(routes, key=lambda r: r["min_cost"])
    best_cost = best["min_cost"]

    if best_cost > NEAR_ARB_MAX_COST and not debug:
        return None

    edge = round(1.0 - best_cost, 4)
    roi = round(edge / best_cost * 100, 2) if best_cost > 0 else 0

    if roi < 1.0:
        return None

    is_near_arb = best_cost <= NEAR_ARB_MAX_COST and best_cost >= 1.0

    item_warnings = []
    if best_cost >= 1.0:
        item_warnings.append("no_arb_currently")
    if is_near_arb:
        item_warnings.append("near_arb")

    w_poly_url, w_kalshi_url = _build_urls(pair)
    return {
        "type": "near_arb" if is_near_arb else "watchlist",
        "pairId": pair["pair_id"],
        "title": pair["title"],
        "kalshiTitle": pair.get("kalshi_title", ""),
        "polyUrl": w_poly_url,
        "kalshiUrl": w_kalshi_url,
        "sport": pair.get("sport"),
        "expiryTs": pair.get("expiry_ts", 0),
        "minCost": round(best_cost, 4),
        "edge": edge,
        "roi": roi,
        "route": best["route"],
        "confidence": 0,
        "legs": best["legs"],
        "updatedTs": int(time.time()),
        "warnings": item_warnings,
        "similarity": pair.get("similarity", 0),
    }


def _has_real_sizes(legs: list[dict]) -> bool:
    return any(leg.get("size") is not None and leg.get("size", 0) > 0 for leg in legs)


def _calc_min_size(legs: list[dict]) -> float:
    real_sizes = [leg["size"] for leg in legs if leg.get("size") is not None and leg["size"] > 0]
    return min(real_sizes) if real_sizes else 0


def _calc_confidence(edge: float, min_size: float, similarity: float) -> float:
    edge_score = min(edge / 0.10, 1.0) * 0.4
    liq_score = min(min_size / 100, 1.0) * 0.3
    match_score = similarity * 0.3
    return round(edge_score + liq_score + match_score, 3)
