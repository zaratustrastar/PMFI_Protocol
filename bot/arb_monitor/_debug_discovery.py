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
from arb_monitor.adapters.opinion import get_opinion_markets
from arb_monitor.core.filters import classify_sport
from arb_monitor.core.matcher import find_pairs


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

    print("\n💭 Fetching Opinion markets...")
    opinion = get_opinion_markets()
    print(f"   Total after expiry filter: {len(opinion)}")

    sport_counts = {}
    for m in opinion:
        s = m.sport or "uncategorized"
        sport_counts[s] = sport_counts.get(s, 0) + 1
    print(f"   Sport breakdown: {sport_counts}")

    print(f"\n   Sample titles (first 10):")
    for m in opinion[:10]:
        tag = f"[{m.sport or '?'}]" if m.sport else "[—]"
        exp = f"exp={m.expiryTs}" if m.expiryTs else "no-exp"
        tok = "✓tokens" if m.yesTokenId and m.noTokenId else "✗tokens"
        print(f"     {tag:12s} {tok}  {exp}  {m.title[:80]}")

    print("\n" + "-" * 70)

    poly_sports = [m for m in poly if m.sport]
    opinion_sports = [m for m in opinion if m.sport]
    print(f"\n🔗 Matching pairs: {len(poly_sports)} Poly sports x {len(opinion_sports)} Opinion sports")

    pairs = find_pairs(poly_sports, opinion_sports)
    print(f"   Matched: {len(pairs)} pairs")

    if pairs:
        print(f"\n   Top matches:")
        for p in pairs[:10]:
            sim = p.get("similarity", 0)
            sport = p.get("sport", "?")
            print(f"     [{sport}] sim={sim:.2f}  {p['title'][:70]}")
            oq = p["opinion"]["question"]
            if oq != p["title"]:
                print(f"       ↔ {oq[:70]}")

    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(poly)} Poly markets, {len(opinion)} Opinion markets, {len(pairs)} matched pairs")
    print("=" * 70)


if __name__ == "__main__":
    main()
