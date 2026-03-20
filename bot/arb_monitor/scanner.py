"""Background scanner that periodically fetches markets, matches pairs, and detects arb opportunities.

Uses PMXT unified stack for Polymarket × Kalshi discovery and orderbooks.
Seed pairs from seedPairs.json are always included for guaranteed coverage.

DEPRECATION NOTE:
  When ARB_USE_ODDPOOL_ONLY=true (default), all legacy matching paths are DISABLED:
  - AI/LLM matcher (ai_matcher.find_opinion_pairs) — preserved but bypassed
  - Opinion SSE collector / Opinion scanner — preserved but bypassed
  - Fuzzy Kalshi matcher (matcher.find_pairs) — preserved but bypassed
  Oddpool /arb-current is the sole source of matched pairs for the pArbitrage vault.
  All old code files are preserved for reference.
"""

import time
import threading
from .config import SCAN_INTERVAL_SECONDS, OPENAI_API_KEY, ARB_USE_ODDPOOL_ONLY
from .adapters.pmxt_adapter import (
    fetch_polymarket_markets, fetch_kalshi_markets,
    load_seed_pairs, fetch_seed_pair_markets,
    get_poly_discovery_stats, get_kalshi_discovery_stats,
)
from .adapters.opinion import get_opinion_markets
from .core.matcher import find_pairs
from .core.arb_engine import analyze_pair
from .core.ai_matcher import find_opinion_pairs
from .core.opinion_arb_engine import analyze_opinion_pair
from .storage import arb_store, arb_cache


def log(msg: str):
    print(f"🔎 [Arb/Scanner] {msg}")


_last_discovery_stats: dict = {}
_tracked_pairs: list[dict] = []
_tracked_pairs_lock = threading.Lock()
_scanner_health: dict = {
    "polyMarketsFetched": 0,
    "kalshiMarketsFetched": 0,
    "pairsMatched": 0,
    "lastError": None,
    "lastScanTimestamp": 0,
    "lastScanMs": 0,
    "scanCount": 0,
}


def get_discovery_stats() -> dict:
    return dict(_last_discovery_stats)


def get_scanner_health() -> dict:
    return {
        "scannerRunning": _scanner_thread is not None and _scanner_thread.is_alive(),
        "polyMarketsFetched": _scanner_health.get("polyMarketsFetched", 0),
        "kalshiMarketsFetched": _scanner_health.get("kalshiMarketsFetched", 0),
        "pairsMatched": _scanner_health.get("pairsMatched", 0),
        "lastError": _scanner_health.get("lastError"),
        "lastScanTimestamp": _scanner_health.get("lastScanTimestamp", 0),
        "lastScanMs": _scanner_health.get("lastScanMs", 0),
        "scanCount": _scanner_health.get("scanCount", 0),
    }


def get_tracked_pairs() -> list[dict]:
    with _tracked_pairs_lock:
        return list(_tracked_pairs)


def run_debug_analysis(max_pairs: int = 25) -> dict:
    pairs = get_tracked_pairs()[:max_pairs]
    if not pairs:
        return {"opportunities": [], "watchlist": [], "nearArbs": [], "pairsAnalyzed": 0}

    opportunities = []
    watchlist = []
    near_arbs = []
    for pair in pairs:
        try:
            result = analyze_pair(pair, debug=True)
            if result is None:
                continue
            rtype = result.get("type", "")
            if rtype == "opportunity":
                opportunities.append(result)
            elif rtype == "near_arb":
                near_arbs.append(result)
            else:
                watchlist.append(result)
        except Exception as e:
            log(f"Debug analysis error for {pair.get('pair_id', '?')}: {e}")

    opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)
    near_arbs.sort(key=lambda x: x.get("minCost", 2))
    watchlist.sort(key=lambda x: x.get("expiryTs", 0))

    return {
        "opportunities": opportunities,
        "nearArbs": near_arbs,
        "watchlist": watchlist,
        "pairsAnalyzed": len(pairs),
    }


def run_scan():
    global _last_discovery_stats, _tracked_pairs, _scanner_health
    start = time.time()

    # Short-circuit: when Oddpool is sole source, the pArb execution loop handles
    # opportunity fetching and capital deployment. The legacy PMXT discovery pipeline
    # is not needed and is fully bypassed here to avoid dependency on PMXT infrastructure.
    if ARB_USE_ODDPOOL_ONLY:
        log("ℹ️ ARB_USE_ODDPOOL_ONLY=true — PMXT discovery pipeline is DISABLED. "
            "pArb execution loop handles opportunities via Oddpool.")
        _scanner_health["lastScanTimestamp"] = int(time.time())
        _scanner_health["scanCount"] = _scanner_health.get("scanCount", 0) + 1
        _scanner_health["lastError"] = None
        _last_discovery_stats = {
            "mode": "oddpool_only",
            "legacy_scanner": "disabled",
            "note": "Oddpool /arb-current is sole source; pArb execution loop active",
        }
        return

    log("Starting scan cycle (legacy PMXT mode)...")

    try:
        cached_poly = arb_cache.get("poly_normalized")
        if cached_poly is not None:
            poly_markets = cached_poly
            log(f"Using cached Polymarket data ({len(poly_markets)} markets)")
        else:
            poly_markets, poly_stats = fetch_polymarket_markets()
            if poly_markets:
                arb_cache.set("poly_normalized", poly_markets)

        cached_kalshi = arb_cache.get("kalshi_normalized")
        if cached_kalshi is not None:
            kalshi_markets = cached_kalshi
            log(f"Using cached Kalshi data ({len(kalshi_markets)} markets)")
        else:
            kalshi_markets, kalshi_stats = fetch_kalshi_markets()
            if kalshi_markets:
                arb_cache.set("kalshi_normalized", kalshi_markets)

        # Seed/direct pairs in legacy mode only
        seed_direct_pairs = []
        seed_pairs_config = load_seed_pairs()
        if seed_pairs_config:
            seed_poly, seed_kalshi = fetch_seed_pair_markets(seed_pairs_config)
            poly_ids = {m.marketId for m in poly_markets}
            kalshi_ids = {m.marketId for m in kalshi_markets}
            for m in seed_poly:
                if m.marketId not in poly_ids:
                    poly_markets.append(m)
                    poly_ids.add(m.marketId)
            for m in seed_kalshi:
                if m.marketId not in kalshi_ids:
                    kalshi_markets.append(m)
                    kalshi_ids.add(m.marketId)
            seed_direct_pairs = _build_seed_direct_pairs(seed_poly, seed_kalshi)
            log(f"After seed injection: {len(poly_markets)} Poly, {len(kalshi_markets)} Kalshi, {len(seed_direct_pairs)} direct seed pairs")

        _scanner_health["polyMarketsFetched"] = len(poly_markets)
        _scanner_health["kalshiMarketsFetched"] = len(kalshi_markets)
        _scanner_health["lastScanTimestamp"] = int(time.time())
        _scanner_health["scanCount"] = _scanner_health.get("scanCount", 0) + 1

        log(f"Discovered: {len(poly_markets)} Poly, {len(kalshi_markets)} Kalshi")

        if len(poly_markets) == 0 and len(kalshi_markets) == 0:
            err_msg = "Both Polymarket and Kalshi returned 0 markets — likely PMXT server issue"
            log(f"❌ {err_msg}")
            _scanner_health["lastError"] = err_msg
            arb_store.set_error(err_msg)
            return
        elif len(poly_markets) == 0:
            _scanner_health["lastError"] = "Polymarket returned 0 markets"
            log(f"⚠️ {_scanner_health['lastError']}")
        elif len(kalshi_markets) == 0:
            _scanner_health["lastError"] = "Kalshi returned 0 markets — may be rate limited"
            log(f"⚠️ {_scanner_health['lastError']}")
        else:
            _scanner_health["lastError"] = None

        _last_discovery_stats = {
            "polymarket_total": len(poly_markets),
            "kalshi_total": len(kalshi_markets),
            "poly_sample_titles": [m.title for m in poly_markets[:5]],
            "kalshi_sample_titles": [m.title for m in kalshi_markets[:5]],
            "poly_sport_breakdown": _sport_breakdown(poly_markets),
            "kalshi_sport_breakdown": _sport_breakdown(kalshi_markets),
            "poly_stats": get_poly_discovery_stats(),
            "kalshi_stats": get_kalshi_discovery_stats(),
        }

        if ARB_USE_ODDPOOL_ONLY:
            log("⚠️ ARB_USE_ODDPOOL_ONLY=true — fuzzy Kalshi matcher is DISABLED (Oddpool is sole source)")
            fuzzy_pairs = []
        else:
            fuzzy_pairs = find_pairs(poly_markets, kalshi_markets)

        seen_pair_ids = set()
        pairs = []
        for p in seed_direct_pairs:
            pid = p.get("pair_id", "")
            if pid not in seen_pair_ids:
                seen_pair_ids.add(pid)
                pairs.append(p)
        for p in fuzzy_pairs:
            pid = p.get("pair_id", "")
            if pid not in seen_pair_ids:
                seen_pair_ids.add(pid)
                pairs.append(p)

        _scanner_health["pairsMatched"] = len(pairs)
        log(f"Matched {len(pairs)} pairs ({len(seed_direct_pairs)} seed + {len(fuzzy_pairs)} fuzzy), analyzing prices...")

        # --- Opinion × Polymarket scan (AI-enhanced matching) ---
        # DEPRECATED: Disabled when ARB_USE_ODDPOOL_ONLY=true
        # Code preserved for reference. Opinion.Markets arb is a separate future task.
        opinion_pairs = []
        if ARB_USE_ODDPOOL_ONLY:
            log("⚠️ ARB_USE_ODDPOOL_ONLY=true — Opinion AI matcher is DISABLED (Oddpool is sole source)")
        elif OPENAI_API_KEY:
            try:
                cached_opinion = arb_cache.get("opinion_normalized")
                if cached_opinion is not None:
                    opinion_markets = cached_opinion
                    log(f"Using cached Opinion data ({len(opinion_markets)} markets)")
                else:
                    opinion_markets = get_opinion_markets()
                    if opinion_markets:
                        arb_cache.set("opinion_normalized", opinion_markets)
                    log(f"Fetched {len(opinion_markets)} Opinion markets")

                _scanner_health["opinionMarketsFetched"] = len(opinion_markets)

                if opinion_markets and poly_markets:
                    opinion_pairs = find_opinion_pairs(poly_markets, opinion_markets)
                    log(f"Opinion×Poly: {len(opinion_pairs)} AI-matched pairs")
                    for p in opinion_pairs:
                        pid = p.get("pair_id", "")
                        if pid not in seen_pair_ids:
                            seen_pair_ids.add(pid)
            except Exception as e:
                log(f"⚠️ Opinion scan error: {e}")
        else:
            log("Skipping Opinion scan (OPENAI_API_KEY not set)")

        with _tracked_pairs_lock:
            _tracked_pairs = list(pairs) + opinion_pairs

        _inject_kalshi_prices_into_pairs(pairs, kalshi_markets)

        opportunities = []
        watchlist = []
        near_arbs = []

        for pair in pairs:
            try:
                result = analyze_pair(pair, debug=False)
                if result is None:
                    continue
                rtype = result.get("type", "")
                if rtype == "opportunity":
                    opportunities.append(result)
                elif rtype == "near_arb":
                    near_arbs.append(result)
                else:
                    watchlist.append(result)
            except Exception as e:
                log(f"Error analyzing Kalshi pair {pair.get('pair_id', '?')}: {e}")
                continue

        op_opps = 0
        for pair in opinion_pairs:
            try:
                result = analyze_opinion_pair(pair, debug=False)
                if result is None:
                    continue
                rtype = result.get("type", "")
                if rtype == "opportunity":
                    opportunities.append(result)
                    op_opps += 1
                elif rtype == "near_arb":
                    near_arbs.append(result)
                else:
                    watchlist.append(result)
            except Exception as e:
                log(f"Error analyzing Opinion pair {pair.get('pair_id', '?')}: {e}")
                continue

        opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)
        near_arbs.sort(key=lambda x: x.get("minCost", 2))
        watchlist.sort(key=lambda x: x.get("expiryTs", 0))

        elapsed_ms = int((time.time() - start) * 1000)
        _scanner_health["lastScanMs"] = elapsed_ms
        _scanner_health["opinionPairsMatched"] = len(opinion_pairs)
        _scanner_health["opinionOpportunities"] = op_opps

        total_pairs = len(pairs) + len(opinion_pairs)
        arb_store.update(
            opportunities=opportunities,
            watchlist=near_arbs + watchlist,
            pairs_tracked=total_pairs,
            scan_ms=elapsed_ms,
        )

        log(
            f"Scan complete in {elapsed_ms}ms: {len(opportunities)} opps "
            f"({op_opps} opinion), {len(near_arbs)} near-arbs, "
            f"{len(watchlist)} watchlist, {total_pairs} pairs total"
        )

    except Exception as e:
        log(f"Scan error: {e}")
        import traceback
        traceback.print_exc()
        _scanner_health["lastError"] = str(e)
        _scanner_health["lastScanTimestamp"] = int(time.time())
        arb_store.set_error(str(e))


def _build_seed_direct_pairs(seed_poly: list, seed_kalshi: list) -> list[dict]:
    from .core.matcher import find_pairs as _find_pairs
    if not seed_poly or not seed_kalshi:
        return []
    pairs = _find_pairs(seed_poly, seed_kalshi, min_similarity=0.20)
    for p in pairs:
        p["seed_direct"] = True
        if p["similarity"] < 0.35:
            p["similarity"] = max(p["similarity"], 0.35)
    log(f"Seed direct matching: {len(seed_poly)} poly x {len(seed_kalshi)} kalshi → {len(pairs)} pairs")
    return pairs


def _inject_kalshi_prices_into_pairs(pairs: list[dict], kalshi_markets: list):
    kalshi_by_id = {m.marketId: m for m in kalshi_markets}
    for pair in pairs:
        kl = pair.get("kalshi", {})
        kl_id = kl.get("id", "")
        km = kalshi_by_id.get(kl_id)
        if km:
            kl["yes_price"] = km.meta.get("yes_price", 0)
            kl["no_price"] = km.meta.get("no_price", 0)


def _sport_breakdown(markets) -> dict:
    counts: dict[str, int] = {}
    for m in markets:
        s = m.sport or "uncategorized"
        counts[s] = counts.get(s, 0) + 1
    return counts


def scanner_loop():
    log(f"Scanner started (interval={SCAN_INTERVAL_SECONDS}s)")
    time.sleep(5)
    while True:
        try:
            run_scan()
        except Exception as e:
            log(f"Scanner loop error: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


_scanner_thread = None


def start_scanner():
    global _scanner_thread
    if _scanner_thread and _scanner_thread.is_alive():
        log("Scanner already running")
        return
    _scanner_thread = threading.Thread(target=scanner_loop, daemon=True, name="arb-scanner")
    _scanner_thread.start()
    log("Scanner thread started")
