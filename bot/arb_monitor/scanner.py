"""Background scanner that periodically fetches markets, matches pairs, and detects arb opportunities."""

import time
import threading
from .config import SCAN_INTERVAL_SECONDS
from .adapters.polymarket import get_polymarket_markets
from .adapters.kalshi import get_kalshi_markets
from .core.filters import classify_sport
from .core.matcher import find_pairs
from .core.arb_engine import analyze_pair
from .storage import arb_store, arb_cache


def log(msg: str):
    print(f"🔎 [Arb/Scanner] {msg}")


_last_discovery_stats: dict = {}
_tracked_pairs: list[dict] = []
_tracked_pairs_lock = threading.Lock()
_scanner_health: dict = {
    "polyMarketsFetched": 0,
    "kalshiMarketsFetched": 0,
    "lastError": None,
    "lastScanTimestamp": 0,
}


def get_discovery_stats() -> dict:
    return dict(_last_discovery_stats)


def get_scanner_health() -> dict:
    return {
        "scannerRunning": _scanner_thread is not None and _scanner_thread.is_alive(),
        "polyMarketsFetched": _scanner_health.get("polyMarketsFetched", 0),
        "kalshiMarketsFetched": _scanner_health.get("kalshiMarketsFetched", 0),
        "lastError": _scanner_health.get("lastError"),
        "lastScanTimestamp": _scanner_health.get("lastScanTimestamp", 0),
    }


def get_tracked_pairs() -> list[dict]:
    with _tracked_pairs_lock:
        return list(_tracked_pairs)


def run_debug_analysis(max_pairs: int = 25) -> dict:
    """Re-analyze tracked pairs with debug=True for on-demand debug requests.

    Returns dict with opportunities and watchlist including debugPrices.
    """
    pairs = get_tracked_pairs()[:max_pairs]
    if not pairs:
        return {"opportunities": [], "watchlist": [], "pairsAnalyzed": 0}

    opportunities = []
    watchlist = []
    for pair in pairs:
        try:
            result = analyze_pair(pair, debug=True)
            if result is None:
                continue
            if result.get("type") == "opportunity":
                opportunities.append(result)
            else:
                watchlist.append(result)
        except Exception as e:
            log(f"Debug analysis error for {pair.get('pair_id', '?')}: {e}")

    opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)
    watchlist.sort(key=lambda x: x.get("expiryTs", 0))

    return {
        "opportunities": opportunities,
        "watchlist": watchlist,
        "pairsAnalyzed": len(pairs),
    }


def run_scan():
    global _last_discovery_stats, _tracked_pairs, _scanner_health
    start = time.time()
    log("Starting scan cycle...")

    try:
        cached_poly = arb_cache.get("poly_normalized")
        if cached_poly is not None:
            poly_markets = cached_poly
            log(f"Using cached Polymarket data ({len(poly_markets)} markets)")
        else:
            poly_markets = get_polymarket_markets()
            if poly_markets:
                arb_cache.set("poly_normalized", poly_markets)

        cached_kalshi = arb_cache.get("kalshi_normalized")
        if cached_kalshi is not None:
            kalshi_markets = cached_kalshi
            log(f"Using cached Kalshi data ({len(kalshi_markets)} markets)")
        else:
            kalshi_markets = get_kalshi_markets()
            if kalshi_markets:
                arb_cache.set("kalshi_normalized", kalshi_markets)

        _scanner_health["polyMarketsFetched"] = len(poly_markets)
        _scanner_health["kalshiMarketsFetched"] = len(kalshi_markets)
        _scanner_health["lastScanTimestamp"] = int(time.time())

        log(f"Discovered: {len(poly_markets)} Poly, {len(kalshi_markets)} Kalshi")

        if len(poly_markets) == 0 and len(kalshi_markets) == 0:
            err_msg = "Both Polymarket and Kalshi returned 0 markets — likely a network/proxy issue"
            log(f"❌ {err_msg}")
            _scanner_health["lastError"] = err_msg
            arb_store.set_error(err_msg)
            return
        elif len(poly_markets) == 0:
            _scanner_health["lastError"] = "Polymarket returned 0 markets — API may be blocked"
            log(f"⚠️ {_scanner_health['lastError']}")
        elif len(kalshi_markets) == 0:
            _scanner_health["lastError"] = "Kalshi returned 0 markets — API may be blocked"
            log(f"⚠️ {_scanner_health['lastError']}")
        else:
            _scanner_health["lastError"] = None

        poly_sports = [m for m in poly_markets if m.sport]
        kalshi_sports = [m for m in kalshi_markets if m.sport]

        _last_discovery_stats = {
            "polymarket_total": len(poly_markets),
            "polymarket_sports": len(poly_sports),
            "kalshi_total": len(kalshi_markets),
            "kalshi_sports": len(kalshi_sports),
            "poly_sample_titles": [m.title for m in poly_markets[:5]],
            "kalshi_sample_titles": [m.title for m in kalshi_markets[:5]],
            "poly_sport_breakdown": _sport_breakdown(poly_markets),
            "kalshi_sport_breakdown": _sport_breakdown(kalshi_markets),
        }

        pairs = find_pairs(poly_markets, kalshi_markets)
        log(f"Matched {len(pairs)} pairs, analyzing orderbooks...")

        with _tracked_pairs_lock:
            _tracked_pairs = list(pairs)

        opportunities = []
        watchlist = []

        for pair in pairs:
            try:
                result = analyze_pair(pair, debug=False)
                if result is None:
                    continue
                if result.get("type") == "opportunity":
                    opportunities.append(result)
                else:
                    watchlist.append(result)
            except Exception as e:
                log(f"Error analyzing pair {pair.get('pair_id', '?')}: {e}")
                continue

        opportunities.sort(key=lambda x: x.get("edge", 0), reverse=True)
        watchlist.sort(key=lambda x: x.get("expiryTs", 0))

        elapsed_ms = int((time.time() - start) * 1000)
        arb_store.update(
            opportunities=opportunities,
            watchlist=watchlist,
            pairs_tracked=len(pairs),
            scan_ms=elapsed_ms,
        )

        log(f"Scan complete in {elapsed_ms}ms: {len(opportunities)} opportunities, {len(watchlist)} watchlist, {len(pairs)} pairs")

    except Exception as e:
        log(f"Scan error: {e}")
        import traceback
        traceback.print_exc()
        _scanner_health["lastError"] = str(e)
        _scanner_health["lastScanTimestamp"] = int(time.time())
        arb_store.set_error(str(e))


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
