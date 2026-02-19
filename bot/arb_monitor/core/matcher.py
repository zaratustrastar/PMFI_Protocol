"""Matcher - finds matching markets across Opinion and Polymarket using fuzzy text matching."""

import re
from difflib import SequenceMatcher
from ..models import NormalizedMarket


def log(msg: str):
    print(f"🔗 [Arb/Matcher] {msg}")


def normalize_text(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    stopwords = {"will", "the", "a", "an", "to", "in", "of", "for", "on", "at", "by", "is", "be"}
    words = [w for w in t.split() if w not in stopwords]
    return " ".join(words)


def similarity(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    return SequenceMatcher(None, na, nb).ratio()


def find_pairs(poly_markets: list[NormalizedMarket], opinion_markets: list[NormalizedMarket],
               min_similarity: float = 0.65) -> list[dict]:
    pairs = []
    used_opinion: set[int] = set()

    for pm in poly_markets:
        if not pm.title:
            continue

        best_match = None
        best_score = 0.0

        for i, om in enumerate(opinion_markets):
            if i in used_opinion:
                continue
            if not om.title:
                continue

            score = similarity(pm.title, om.title)
            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = (i, om)

        if best_match:
            idx, om = best_match
            used_opinion.add(idx)
            pair_id = f"polymarket:{pm.marketId}___opinion:{om.marketId}"
            sport = pm.sport or om.sport
            expiry = pm.expiryTs or om.expiryTs

            pairs.append({
                "pair_id": pair_id,
                "title": pm.title,
                "sport": sport,
                "expiry_ts": expiry or 0,
                "similarity": round(best_score, 3),
                "polymarket": {
                    "venue": "polymarket",
                    "id": pm.marketId,
                    "question": pm.title,
                    "yes_token": pm.yesTokenId,
                    "no_token": pm.noTokenId,
                    "expiry_ts": pm.expiryTs,
                    "sport": pm.sport,
                },
                "opinion": {
                    "venue": "opinion",
                    "id": om.marketId,
                    "question": om.title,
                    "yes_token": om.yesTokenId,
                    "no_token": om.noTokenId,
                    "expiry_ts": om.expiryTs,
                    "sport": om.sport,
                },
            })

    log(f"Found {len(pairs)} matched pairs from {len(poly_markets)} Poly x {len(opinion_markets)} Opinion markets")
    return pairs
