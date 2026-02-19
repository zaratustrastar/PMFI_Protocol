#!/usr/bin/env python3
"""Debug script to test market discovery from both venues.

Run: cd bot && python -m arb_monitor._debug_discovery
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from arb_monitor.adapters.polymarket import get_polymarket_markets
from arb_monitor.adapters.kalshi import get_kalshi_markets, get_exclusion_stats
from arb_monitor.core.filters import classify_sport
from arb_monitor.core.matcher import find_pairs


def _print_sport_samples(markets, sport_name: str, limit: int = 10):
    sport_markets = [m for m in markets if m.sport == sport_name]
    if not sport_markets:
        return
    print(f"\n   {sport_name.upper()} samples ({len(sport_markets)} total, showing first {min(limit, len(sport_markets))}):")
    for m in sport_markets[:limit]:
        cat = m.meta.get("event_category", "") if m.meta else ""
        cat_str = f" cat={cat}" if cat else ""
        print(f"     ticker={m.marketId[:50]:50s}{cat_str}  {m.title[:80]}")


def main():
    print("=" * 70)
    print("ARB MONITOR - Market Discovery Debug")
    print("=" * 70)

    print("\n📊 Fetching Polymarket markets...")
    poly = get_polymarket_markets()
    print(f"   Total after expiry filter: {len(poly)}")

    sport_counts: dict[str, int] = {}
    for m in poly:
        s = m.sport or "uncategorized"
        sport_counts[s] = sport_counts.get(s, 0) + 1
    print(f"   Sport breakdown: {sport_counts}")

    print(f"\n   Sample titles (first 10):")
    for m in poly[:10]:
        tag = f"[{m.sport or '?'}]" if m.sport else "[—]"
        exp = f"exp={m.expiryTs}" if m.expiryTs else "no-exp"
        tok = "✓tokens" if m.yesTokenId and m.noTokenId else "✗tokens"
        print(f"     {tag:12s} {tok}  {exp}  {m.title[:80]}")

    print("\n" + "-" * 70)

    print("\n🎯 Fetching Kalshi markets...")
    kalshi = get_kalshi_markets()

    stats = get_exclusion_stats()
    print(f"\n   Exclusion breakdown:")
    print(f"     Total fetched:         {stats['total_fetched']}")
    print(f"     Passed (accepted):     {stats['passed']}")
    print(f"     Excluded mve_parlay:   {stats['mve_parlay']}")
    print(f"     Excluded title_parlay: {stats['title_parlay_keyword']}")
    print(f"     Excluded cap_floor:    {stats['cap_floor_range']}")

    print(f"\n   Total after expiry filter: {len(kalshi)}")

    sport_counts = {}
    category_counts: dict[str, int] = {}
    for m in kalshi:
        s = m.sport or "uncategorized"
        sport_counts[s] = sport_counts.get(s, 0) + 1
        cat = m.meta.get("event_category", "unknown") if m.meta else "unknown"
        category_counts[cat] = category_counts.get(cat, 0) + 1
    print(f"   Sport breakdown: {sport_counts}")
    print(f"   Kalshi category breakdown: {category_counts}")

    print(f"\n   Sample titles (first 10):")
    for m in kalshi[:10]:
        tag = f"[{m.sport or '?'}]" if m.sport else "[—]"
        cat = m.meta.get("event_category", "") if m.meta else ""
        print(f"     {tag:12s} cat={cat:20s} ticker={m.marketId[:40]}  {m.title[:70]}")

    _print_sport_samples(kalshi, "esports")
    _print_sport_samples(kalshi, "nba")
    _print_sport_samples(kalshi, "sports")

    print("\n" + "-" * 70)

    poly_sports = [m for m in poly if m.sport]
    kalshi_sports = [m for m in kalshi if m.sport]
    print(f"\n🔗 Matching pairs: {len(poly_sports)} Poly sports x {len(kalshi_sports)} Kalshi sports")

    pairs = find_pairs(poly_sports, kalshi_sports)
    print(f"   Matched: {len(pairs)} pairs")

    if pairs:
        print(f"\n   Top matches:")
        for p in pairs[:10]:
            sim = p.get("similarity", 0)
            sport = p.get("sport", "?")
            print(f"     [{sport}] sim={sim:.2f}  {p['title'][:70]}")
            kq = p["kalshi"]["question"]
            if kq != p["title"]:
                print(f"       ↔ {kq[:70]}")

    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(poly)} Poly markets, {len(kalshi)} Kalshi markets, {len(pairs)} matched pairs")
    print("=" * 70)


if __name__ == "__main__":
    main()
