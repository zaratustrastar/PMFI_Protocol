"""Arb Engine - detects arbitrage opportunities from matched market pairs using orderbook prices.

Arb math:
  Route A: poly YES ask + kalshi NO ask
  Route B: kalshi YES ask + poly NO ask
  Arb exists if min(routeA, routeB) < 1.0
  edge = 1 - cost, roi = edge / cost * 100

Negative-edge watchlist items are hidden by default (debug=True to show).
"""

import time
from ..adapters import polymarket as poly_adapter
from ..adapters import kalshi as kalshi_adapter


def log(msg: str):
    print(f"⚡ [Arb/Engine] {msg}")


def analyze_pair(pair: dict, debug: bool = False) -> dict | None:
    """Analyze a matched pair for arbitrage opportunities using live orderbook data.

    Args:
        pair: Matched pair dict from matcher
        debug: If True, include debugPrices and show negative-edge watchlist items
    """
    pm = pair["polymarket"]
    km = pair["kalshi"]

    pm_yes_token = pm.get("yes_token")
    pm_no_token = pm.get("no_token")
    km_ticker = km.get("id") or km.get("marketId") or km.get("ticker") or km.get("yesTokenId")

    warnings = []

    if not pm_yes_token or not km_ticker:
        return None

    pm_yes_prices = poly_adapter.get_best_prices(pm_yes_token)

    if pm_no_token:
        pm_no_prices = poly_adapter.get_best_prices(pm_no_token)
    else:
        pm_no_prices = {"best_ask": None}
        warnings.append("poly_no_token_missing")

    km_prices = kalshi_adapter.get_best_prices(km_ticker, debug=debug)

    km_yes_ask = km_prices.get("yes_best_ask") or km_prices.get("best_ask")
    km_no_ask = km_prices.get("no_best_ask")

    routes = []

    pm_yes_ask = pm_yes_prices.get("best_ask")
    if pm_yes_ask is not None and km_no_ask is not None:
        cost = pm_yes_ask + km_no_ask
        edge = round(1.0 - cost, 4)
        roi = round(edge / cost * 100, 2) if cost > 0 else 0
        legs = [
            {
                "venue": "polymarket",
                "side": "YES",
                "tokenId": pm_yes_token,
                "price": pm_yes_ask,
                "size": pm_yes_prices.get("ask_size") or 0,
            },
            {
                "venue": "kalshi",
                "side": "NO",
                "tokenId": km_ticker,
                "price": km_no_ask,
                "size": km_prices.get("no_ask_size"),
            },
        ]
        routes.append({
            "route": "poly_YES + kalshi_NO",
            "min_cost": round(cost, 4),
            "edge": edge,
            "roi": roi,
            "legs": legs,
        })

    pm_no_ask = pm_no_prices.get("best_ask")
    if km_yes_ask is not None and pm_no_ask is not None:
        cost = km_yes_ask + pm_no_ask
        edge = round(1.0 - cost, 4)
        roi = round(edge / cost * 100, 2) if cost > 0 else 0
        legs = [
            {
                "venue": "kalshi",
                "side": "YES",
                "tokenId": km_ticker,
                "price": km_yes_ask,
                "size": km_prices.get("yes_ask_size"),
            },
            {
                "venue": "polymarket",
                "side": "NO",
                "tokenId": pm_no_token,
                "price": pm_no_ask,
                "size": pm_no_prices.get("ask_size") or 0,
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
            "kalshi_yes_ask": km_yes_ask,
            "kalshi_no_ask": km_no_ask,
            "kalshi_ticker": km_ticker,
            "raw_kalshi": km_prices.get("raw"),
        }

    arb_routes = [r for r in routes if r["min_cost"] < 1.0]

    if not arb_routes:
        watchlist_item = _build_watchlist_item(
            pair, pm_yes_prices, pm_no_prices, km_prices, km_ticker, debug=debug
        )
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

    result = {
        "type": "opportunity",
        "pairId": pair["pair_id"],
        "title": pair["title"],
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


def _build_watchlist_item(pair, pm_yes, pm_no, km_prices, km_ticker, debug: bool = False) -> dict | None:
    """Build a watchlist item for pairs without a live arb but close enough to track.

    Negative-edge items (best_cost >= 1.0) are hidden unless debug=True.
    Always includes legs from the best route.
    """
    candidates = []

    pm_y_ask = pm_yes.get("best_ask")
    km_n_ask = km_prices.get("no_best_ask")
    if pm_y_ask is not None and km_n_ask is not None:
        legs = [
            {"venue": "polymarket", "side": "YES", "tokenId": None, "price": pm_y_ask, "size": pm_yes.get("ask_size") or 0},
            {"venue": "kalshi", "side": "NO", "tokenId": km_ticker, "price": km_n_ask, "size": km_prices.get("no_ask_size")},
        ]
        candidates.append(("poly_YES + kalshi_NO", pm_y_ask + km_n_ask, legs))

    km_y_ask = km_prices.get("yes_best_ask") or km_prices.get("best_ask")
    pm_n_ask = pm_no.get("best_ask")
    if km_y_ask is not None and pm_n_ask is not None:
        legs = [
            {"venue": "kalshi", "side": "YES", "tokenId": km_ticker, "price": km_y_ask, "size": km_prices.get("yes_ask_size")},
            {"venue": "polymarket", "side": "NO", "tokenId": None, "price": pm_n_ask, "size": pm_no.get("ask_size") or 0},
        ]
        candidates.append(("kalshi_YES + poly_NO", km_y_ask + pm_n_ask, legs))

    if not candidates:
        return {
            "type": "watchlist",
            "pairId": pair["pair_id"],
            "title": pair["title"],
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

    best_route, best_cost, best_legs = min(candidates, key=lambda x: x[1])
    edge = round(1.0 - best_cost, 4)

    if best_cost >= 1.0 and not debug:
        return None

    return {
        "type": "watchlist",
        "pairId": pair["pair_id"],
        "title": pair["title"],
        "sport": pair.get("sport"),
        "expiryTs": pair.get("expiry_ts", 0),
        "minCost": round(best_cost, 4),
        "edge": edge,
        "roi": round(edge / best_cost * 100, 2) if best_cost > 0 else 0,
        "route": best_route,
        "confidence": 0,
        "legs": best_legs,
        "updatedTs": int(time.time()),
        "warnings": ["no_arb_currently"] if best_cost >= 1.0 else ["near_arb"],
    }


def _has_real_sizes(legs: list[dict]) -> bool:
    """Check if any leg has a real (non-None, non-zero) size."""
    return any(leg.get("size") is not None and leg.get("size", 0) > 0 for leg in legs)


def _calc_min_size(legs: list[dict]) -> float:
    """Calculate minimum size across legs, ignoring None sizes."""
    real_sizes = [leg["size"] for leg in legs if leg.get("size") is not None and leg["size"] > 0]
    return min(real_sizes) if real_sizes else 0


def _calc_confidence(edge: float, min_size: float, similarity: float) -> float:
    """Calculate confidence score 0-1 based on edge, liquidity, and match quality."""
    edge_score = min(edge / 0.10, 1.0) * 0.4
    liq_score = min(min_size / 100, 1.0) * 0.3
    match_score = similarity * 0.3
    return round(edge_score + liq_score + match_score, 3)
