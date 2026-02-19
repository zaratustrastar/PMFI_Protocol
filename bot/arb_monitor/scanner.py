"""Background scanner that periodically fetches markets, matches pairs, and detects arb opportunities."""

import time
import threading
from .config import SCAN_INTERVAL_SECONDS
from .adapters.polymarket import get_polymarket_markets
from .adapters.opinion import get_opinion_markets
from .core.filters import classify_sport
from .core.matcher import find_pairs
from .core.arb_engine import analyze_pair
from .storage import arb_store, arb_cache


def log(msg: str):
    print(f"🔎 [Arb/Scanner] {msg}")


_last_discovery_stats: dict = {}


def get_discovery_stats() -> dict:
    return dict(_last_discovery_stats)


def run_scan():
    global _last_discovery_stats
    start = time.time()
    log("Starting scan cycle...")

    try:
        cached_poly = arb_cache.get("poly_normalized")
        if cached_poly is not None:
            poly_markets = cached_poly
            log(f"Using cached Polymarket data ({len(poly_markets)} markets)")
        else:
            poly_markets = get_polymarket_markets()
            arb_cache.set("poly_normalized", poly_markets)

        cached_opinion = arb_cache.get("opinion_normalized")
        if cached_opinion is not None:
            opinion_markets = cached_opinion
            log(f"Using cached Opinion data ({len(opinion_markets)} markets)")
        else:
            opinion_markets = get_opinion_markets()
            arb_cache.set("opinion_normalized", opinion_markets)

        poly_sports = [m for m in poly_markets if m.sport]
        opinion_sports = [m for m in opinion_markets if m.sport]

        log(f"Sports markets: {len(poly_sports)} Poly, {len(opinion_sports)} Opinion (total: {len(poly_markets)} Poly, {len(opinion_markets)} Opinion)")

        _last_discovery_stats = {
            "polymarket_total": len(poly_markets),
            "polymarket_sports": len(poly_sports),
            "opinion_total": len(opinion_markets),
            "opinion_sports": len(opinion_sports),
            "poly_sample_titles": [m.title for m in poly_markets[:5]],
            "opinion_sample_titles": [m.title for m in opinion_markets[:5]],
            "poly_sport_breakdown": _sport_breakdown(poly_markets),
            "opinion_sport_breakdown": _sport_breakdown(opinion_markets),
        }

        pairs = find_pairs(poly_sports, opinion_sports)
        log(f"Matched {len(pairs)} pairs, analyzing orderbooks...")

        opportunities = []
        watchlist = []

        for pair in pairs:
            try:
                result = analyze_pair(pair)
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
