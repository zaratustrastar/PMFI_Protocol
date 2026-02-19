#!/usr/bin/env python3
"""Debug script to test market discovery from both venues.

Run: cd bot && python -m arb_monitor._debug_discovery
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from arb_monitor.adapters.polymarket import get_polymarket_markets, get_discovery_stats
from arb_monitor.adapters.kalshi import get_kalshi_markets, get_exclusion_stats
from arb_monitor.core.filters import classify_sport
from arb_monitor.core.matcher import find_pairs


def _print_sport_samples(markets, sport_name: str, venue: str, limit: int = 20):
    sport_markets = [m for m in markets if m.sport == sport_name]
    if not sport_markets:
        return
    print(f"\n   {venue} {sport_name.upper()} markets ({len(sport_markets)} total, showing {min(limit, len(sport_markets))}):")
    for m in sport_markets[:limit]:
        tk = m.team_key or "—"
        cat = m.meta.get("event_category", "") if m.meta else ""
        cat_str = f" cat={cat}" if cat else ""
        print(f"     exp={m.expiryTs}  team_key={tk:30s}{cat_str}  {m.title[:70]}")


def main():
    print("=" * 70)
    print("ARB MONITOR - Market Discovery Debug")
    print("=" * 70)

    print("\n📊 Fetching Polymarket markets...")
    poly = get_polymarket_markets()
    poly_stats = get_discovery_stats()
    print(f"\n   Discovery breakdown:")
    print(f"     Total fetched:          {poly_stats.get('fetchedTotal', '?')}")
    print(f"     Excluded closed:        {poly_stats.get('excludedClosed', '?')}")
    print(f"     Excluded archived:      {poly_stats.get('excludedArchived', '?')}")
    print(f"     Excluded missing tokens: {poly_stats.get('excludedMissingTokens', '?')}")
    print(f"     Excluded expiry:        {poly_stats.get('excludedExpiry', '?')}")
    print(f"     Included final:         {poly_stats.get('includedFinal', '?')}")
    print(f"   Total normalized: {len(poly)}")

    sport_counts: dict[str, int] = {}
    team_key_count = 0
    for m in poly:
        s = m.sport or "uncategorized"
        sport_counts[s] = sport_counts.get(s, 0) + 1
        if m.team_key:
            team_key_count += 1
    print(f"   Sport breakdown: {sport_counts}")
    print(f"   Markets with team_key: {team_key_count}")

    print(f"\n   Sample titles (first 10):")
    for m in poly[:10]:
        tag = f"[{m.sport or '?'}]" if m.sport else "[—]"
        tk = f"tk={m.team_key}" if m.team_key else ""
        print(f"     {tag:12s} exp={m.expiryTs}  {tk:30s}  {m.title[:70]}")

    _print_sport_samples(poly, "esports", "Polymarket")
    _print_sport_samples(poly, "nba", "Polymarket")
    _print_sport_samples(poly, "sports", "Polymarket")

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
    team_key_count = 0
    for m in kalshi:
        s = m.sport or "uncategorized"
        sport_counts[s] = sport_counts.get(s, 0) + 1
        cat = m.meta.get("event_category", "unknown") if m.meta else "unknown"
        category_counts[cat] = category_counts.get(cat, 0) + 1
        if m.team_key:
            team_key_count += 1
    print(f"   Sport breakdown: {sport_counts}")
    print(f"   Kalshi category breakdown: {category_counts}")
    print(f"   Markets with team_key: {team_key_count}")

    print(f"\n   Sample titles (first 10):")
    for m in kalshi[:10]:
        tag = f"[{m.sport or '?'}]" if m.sport else "[—]"
        cat = m.meta.get("event_category", "") if m.meta else ""
        tk = f"tk={m.team_key}" if m.team_key else ""
        print(f"     {tag:12s} cat={cat:20s} {tk:30s}  {m.title[:60]}")

    _print_sport_samples(kalshi, "esports", "Kalshi")
    _print_sport_samples(kalshi, "nba", "Kalshi")
    _print_sport_samples(kalshi, "sports", "Kalshi")

    print("\n" + "-" * 70)

    print("\n🔗 Matching...")
    all_poly = poly
    all_kalshi = kalshi
    pairs = find_pairs(all_poly, all_kalshi)
    print(f"   Matched: {len(pairs)} pairs")

    if pairs:
        print(f"\n   Top matches:")
        for p in pairs[:10]:
            sim = p.get("similarity", 0)
            sport = p.get("sport", "?")
            ptk = p["polymarket"].get("team_key", "—")
            ktk = p["kalshi"].get("team_key", "—")
            print(f"     [{sport}] sim={sim:.2f}  ptk={ptk}  ktk={ktk}")
            print(f"       PM: {p['polymarket']['question'][:70]}")
            print(f"       KL: {p['kalshi']['question'][:70]}")

    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(poly)} Poly markets, {len(kalshi)} Kalshi markets, {len(pairs)} matched pairs")
    print("=" * 70)


if __name__ == "__main__":
    main()
