"""Matcher - finds matching markets across Opinion and Polymarket using fuzzy text matching."""

import re
from difflib import SequenceMatcher


def log(msg: str):
    print(f"🔗 [Arb/Matcher] {msg}")


def normalize_text(text: str) -> str:
    """Normalize question text for comparison."""
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    stopwords = {"will", "the", "a", "an", "to", "in", "of", "for", "on", "at", "by", "is", "be"}
    words = [w for w in t.split() if w not in stopwords]
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    """Calculate similarity between two question strings."""
    na = normalize_text(a)
    nb = normalize_text(b)
    return SequenceMatcher(None, na, nb).ratio()


def find_pairs(poly_markets: list[dict], opinion_markets: list[dict],
               min_similarity: float = 0.65) -> list[dict]:
    """Find matching market pairs between Polymarket and Opinion."""
    pairs = []
    used_opinion = set()

    for pm in poly_markets:
        pq = pm.get("question", "")
        if not pq:
            continue

        best_match = None
        best_score = 0.0

        for i, om in enumerate(opinion_markets):
            if i in used_opinion:
                continue
            oq = om.get("question", "")
            if not oq:
                continue

            score = similarity(pq, oq)
            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = (i, om)

        if best_match:
            idx, om = best_match
            used_opinion.add(idx)
            pair_id = f"{pm['venue']}:{pm['id']}___{om['venue']}:{om['id']}"
            sport = pm.get("sport") or om.get("sport")
            expiry = pm.get("expiry_ts") or om.get("expiry_ts")

            pairs.append({
                "pair_id": pair_id,
                "title": pm.get("question", ""),
                "sport": sport,
                "expiry_ts": expiry or 0,
                "similarity": round(best_score, 3),
                "polymarket": pm,
                "opinion": om,
            })

    log(f"Found {len(pairs)} matched pairs from {len(poly_markets)} Poly x {len(opinion_markets)} Opinion markets")
    return pairs
