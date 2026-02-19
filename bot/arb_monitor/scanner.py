"""Background scanner that periodically fetches markets, matches pairs, and detects arb opportunities."""

import time
import threading
from .config import SCAN_INTERVAL_SECONDS
from .adapters import polymarket as poly_adapter
from .adapters import opinion as opinion_adapter
from .core.filters import filter_markets_by_sports
from .core.matcher import find_pairs
from .core.arb_engine import analyze_pair
from .storage import arb_store, arb_cache


def log(msg: str):
    print(f"🔎 [Arb/Scanner] {msg}")


def run_scan():
    """Execute a single scan cycle."""
    start = time.time()
    log("Starting scan cycle...")

    try:
        cached_poly = arb_cache.get("poly_markets")
        if cached_poly is not None:
            poly_raw = cached_poly
            log(f"Using cached Polymarket data ({len(poly_raw)} markets)")
        else:
            poly_raw = poly_adapter.fetch_all_active_markets(max_pages=5)
            arb_cache.set("poly_markets", poly_raw)

        cached_opinion = arb_cache.get("opinion_markets")
        if cached_opinion is not None:
            opinion_raw = cached_opinion
            log(f"Using cached Opinion data ({len(opinion_raw)} markets)")
        else:
            opinion_raw = opinion_adapter.fetch_all_active_markets(max_pages=5)
            arb_cache.set("opinion_markets", opinion_raw)

        poly_normalized = [poly_adapter.normalize_market(m) for m in poly_raw]
        opinion_normalized = [opinion_adapter.normalize_market(m) for m in opinion_raw]

        poly_sports = filter_markets_by_sports(poly_normalized)
        opinion_sports = filter_markets_by_sports(opinion_normalized)

        log(f"Sports markets: {len(poly_sports)} Poly, {len(opinion_sports)} Opinion")

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


def scanner_loop():
    """Background loop that runs scans at regular intervals."""
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
    """Start the background scanner thread."""
    global _scanner_thread
    if _scanner_thread and _scanner_thread.is_alive():
        log("Scanner already running")
        return
    _scanner_thread = threading.Thread(target=scanner_loop, daemon=True, name="arb-scanner")
    _scanner_thread.start()
    log("Scanner thread started")
