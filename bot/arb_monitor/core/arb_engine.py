"""Arb Engine - detects arbitrage opportunities from matched market pairs using orderbook prices."""

import time
from ..adapters import polymarket as poly_adapter
from ..adapters import opinion as opinion_adapter


def log(msg: str):
    print(f"⚡ [Arb/Engine] {msg}")


def analyze_pair(pair: dict) -> dict | None:
    """Analyze a matched pair for arbitrage opportunities using live orderbook data.

    Strategy: Buy YES on one venue at ask, buy NO on the other at ask.
    If total cost < 1.0, there's an arb (guaranteed $1 payout minus cost).
    """
    pm = pair["polymarket"]
    om = pair["opinion"]

    pm_yes_token = pm.get("yes_token")
    pm_no_token = pm.get("no_token")
    om_yes_token = om.get("yes_token")
    om_no_token = om.get("no_token")

    if not pm_yes_token or not om_yes_token:
        return None

    pm_yes_prices = poly_adapter.get_best_prices(pm_yes_token)
    pm_no_prices = poly_adapter.get_best_prices(pm_no_token) if pm_no_token else {"best_ask": None}

    om_yes_prices = opinion_adapter.get_best_prices(om_yes_token)
    om_no_prices = opinion_adapter.get_best_prices(om_no_token) if om_no_token else {"best_ask": None}

    routes = []

    pm_yes_ask = pm_yes_prices.get("best_ask")
    om_no_ask = om_no_prices.get("best_ask")
    if pm_yes_ask and om_no_ask:
        cost = pm_yes_ask + om_no_ask
        if cost < 1.0:
            edge = round(1.0 - cost, 4)
            roi = round(edge / cost * 100, 2) if cost > 0 else 0
            routes.append({
                "route": "poly_YES + opinion_NO",
                "min_cost": round(cost, 4),
                "edge": edge,
                "roi": roi,
                "legs": [
                    {
                        "venue": "polymarket",
                        "side": "YES",
                        "tokenId": pm_yes_token,
                        "price": pm_yes_ask,
                        "size": pm_yes_prices.get("ask_size", 0),
                    },
                    {
                        "venue": "opinion",
                        "side": "NO",
                        "tokenId": om_no_token,
                        "price": om_no_ask,
                        "size": om_no_prices.get("ask_size", 0),
                    },
                ],
            })

    om_yes_ask = om_yes_prices.get("best_ask")
    pm_no_ask = pm_no_prices.get("best_ask")
    if om_yes_ask and pm_no_ask:
        cost = om_yes_ask + pm_no_ask
        if cost < 1.0:
            edge = round(1.0 - cost, 4)
            roi = round(edge / cost * 100, 2) if cost > 0 else 0
            routes.append({
                "route": "opinion_YES + poly_NO",
                "min_cost": round(cost, 4),
                "edge": edge,
                "roi": roi,
                "legs": [
                    {
                        "venue": "opinion",
                        "side": "YES",
                        "tokenId": om_yes_token,
                        "price": om_yes_ask,
                        "size": om_yes_prices.get("ask_size", 0),
                    },
                    {
                        "venue": "polymarket",
                        "side": "NO",
                        "tokenId": pm_no_token,
                        "price": pm_no_ask,
                        "size": pm_no_prices.get("ask_size", 0),
                    },
                ],
            })

    if not routes:
        watchlist_item = _build_watchlist_item(pair, pm_yes_prices, pm_no_prices, om_yes_prices, om_no_prices)
        return watchlist_item

    best = max(routes, key=lambda r: r["edge"])

    min_size = float("inf")
    for leg in best["legs"]:
        if leg["size"] > 0:
            min_size = min(min_size, leg["size"])
    if min_size == float("inf"):
        min_size = 0

    confidence = _calc_confidence(best["edge"], min_size, pair.get("similarity", 0))

    warnings = []
    if min_size < 10:
        warnings.append("low_liquidity")
    if pair.get("similarity", 1.0) < 0.8:
        warnings.append("fuzzy_match")
    if best["edge"] < 0.02:
        warnings.append("thin_edge")

    return {
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


def _build_watchlist_item(pair, pm_yes, pm_no, om_yes, om_no) -> dict:
    """Build a watchlist item for pairs without a live arb but close enough to track."""
    costs = []

    pm_y_ask = pm_yes.get("best_ask")
    om_n_ask = om_no.get("best_ask")
    if pm_y_ask and om_n_ask:
        costs.append(("poly_YES + opinion_NO", pm_y_ask + om_n_ask))

    om_y_ask = om_yes.get("best_ask")
    pm_n_ask = pm_no.get("best_ask")
    if om_y_ask and pm_n_ask:
        costs.append(("opinion_YES + poly_NO", om_y_ask + pm_n_ask))

    if not costs:
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

    best_route, best_cost = min(costs, key=lambda x: x[1])
    edge = round(1.0 - best_cost, 4)

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
        "legs": [],
        "updatedTs": int(time.time()),
        "warnings": ["no_arb_currently"],
    }


def _calc_confidence(edge: float, min_size: float, similarity: float) -> float:
    """Calculate confidence score 0-1 based on edge, liquidity, and match quality."""
    edge_score = min(edge / 0.10, 1.0) * 0.4
    liq_score = min(min_size / 100, 1.0) * 0.3
    match_score = similarity * 0.3
    return round(edge_score + liq_score + match_score, 3)
