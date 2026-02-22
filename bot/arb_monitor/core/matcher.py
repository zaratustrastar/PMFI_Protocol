"""Matcher - finds matching markets across Polymarket and Kalshi.

Matching strategy (constrained hybrid):
  1. Predicate gating: hard block on ENDORSE↔WIN mismatches (computed first, zero cost).
  2. Expiry gate: markets must be within time window per sport category.
  3. Fast pass: token Jaccard on titles to build top-K shortlist.
  4. Refine: Levenshtein on shortlist only (expensive, constrained to top candidates).
  5. Combined score: 0.6 * Jaccard + 0.4 * Levenshtein (like reference bot).
  6. Team key boost: if both have matching team_key, boost to max(score, 0.90).
"""

import re
from ..models import NormalizedMarket


def log(msg: str):
    print(f"🔗 [Arb/Matcher] {msg}")


EXPIRY_GATES = {
    "esports": 12 * 3600,
    "nba": 24 * 3600,
    "nfl": 24 * 3600,
    "soccer": 24 * 3600,
    "mma": 24 * 3600,
    "sports": 24 * 3600,
}

DEFAULT_EXPIRY_GATE = 72 * 3600

JACCARD_SHORTLIST_K = 5
MIN_JACCARD_FOR_LEVENSHTEIN = 0.15


def _normalize_vs(text: str) -> str:
    t = re.sub(r'\s+(?:vs\.?|versus|@)\s+', ' vs ', text, flags=re.IGNORECASE)
    return t


def _tokenize(text: str) -> set[str]:
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    stopwords = {"will", "the", "a", "an", "to", "in", "of", "for", "on", "at", "by", "is", "be"}
    tokens = {w for w in t.split() if w not in stopwords and len(w) > 1}
    return tokens


_PREDICATE_ENDORSE = "ENDORSE"
_PREDICATE_WIN_PRIMARY = "WIN_PRIMARY"
_PREDICATE_WIN_GENERAL = "WIN_GENERAL"
_PREDICATE_OTHER = "OTHER"


def _extract_predicate(title: str) -> str:
    t = title.lower()
    if "endorse" in t:
        return _PREDICATE_ENDORSE
    if "win" in t or "winner" in t:
        if any(kw in t for kw in ("primary", "nominee", "nomination", "runoff")):
            return _PREDICATE_WIN_PRIMARY
        if any(kw in t for kw in ("election", "general", "electoral")):
            return _PREDICATE_WIN_GENERAL
        return _PREDICATE_WIN_PRIMARY
    return _PREDICATE_OTHER


def _predicates_compatible(pred_a: str, pred_b: str) -> bool:
    if pred_a == _PREDICATE_ENDORSE and pred_b in (_PREDICATE_WIN_PRIMARY, _PREDICATE_WIN_GENERAL):
        return False
    if pred_b == _PREDICATE_ENDORSE and pred_a in (_PREDICATE_WIN_PRIMARY, _PREDICATE_WIN_GENERAL):
        return False
    if pred_a == _PREDICATE_ENDORSE and pred_b == _PREDICATE_OTHER:
        return False
    if pred_b == _PREDICATE_ENDORSE and pred_a == _PREDICATE_OTHER:
        return False
    return True


def token_jaccard(a: str, b: str) -> float:
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta or not tb:
        return 0.0
    intersection = ta & tb
    union = ta | tb
    return len(intersection) / len(union) if union else 0.0


def _levenshtein_similarity(s1: str, s2: str) -> float:
    s1 = s1.lower()
    s2 = s2.lower()
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i] + [0] * len2
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr

    max_len = max(len1, len2)
    return 1.0 - prev[len2] / max_len


def combined_similarity(a: str, b: str) -> float:
    jaccard = token_jaccard(a, b)
    lev = _levenshtein_similarity(a, b)
    return 0.6 * jaccard + 0.4 * lev


def compute_similarity(pm: NormalizedMarket, km: NormalizedMarket) -> float:
    pm_title = _normalize_vs(pm.title)
    km_title = _normalize_vs(km.title)

    score = combined_similarity(pm_title, km_title)

    if pm.team_key and km.team_key and pm.team_key == km.team_key:
        score = max(score, 0.90)

    return score


def _expiry_close_enough(pm: NormalizedMarket, km: NormalizedMarket) -> bool:
    if pm.expiryTs <= 0 or km.expiryTs <= 0:
        return True

    sport = pm.sport or km.sport or ""
    gate = EXPIRY_GATES.get(sport, DEFAULT_EXPIRY_GATE)
    return abs(pm.expiryTs - km.expiryTs) <= gate


def find_pairs(poly_markets: list[NormalizedMarket], kalshi_markets: list[NormalizedMarket],
               min_similarity: float = 0.35) -> list[dict]:
    pairs = []
    used_k: set[int] = set()

    for pm in poly_markets:
        if not pm.title:
            continue

        pm_title_norm = _normalize_vs(pm.title)
        pm_pred = _extract_predicate(pm.title)

        candidates = []
        for i, km in enumerate(kalshi_markets):
            if i in used_k:
                continue
            if not km.title:
                continue

            if not _expiry_close_enough(pm, km):
                continue

            km_pred = _extract_predicate(km.title)
            if not _predicates_compatible(pm_pred, km_pred):
                continue

            km_title_norm = _normalize_vs(km.title)
            jaccard = token_jaccard(pm_title_norm, km_title_norm)

            if pm_pred != km_pred and pm_pred != _PREDICATE_OTHER and km_pred != _PREDICATE_OTHER:
                if jaccard < 0.30:
                    continue

            if pm.team_key and km.team_key and pm.team_key == km.team_key:
                jaccard = max(jaccard, 0.50)

            if jaccard >= MIN_JACCARD_FOR_LEVENSHTEIN:
                candidates.append((i, km, jaccard, km_title_norm))

        candidates.sort(key=lambda x: x[2], reverse=True)
        top_candidates = candidates[:JACCARD_SHORTLIST_K]

        best_match = None
        best_score = 0.0

        for i, km, jaccard, km_title_norm in top_candidates:
            lev = _levenshtein_similarity(pm_title_norm, km_title_norm)
            score = 0.6 * jaccard + 0.4 * lev

            if pm.team_key and km.team_key and pm.team_key == km.team_key:
                score = max(score, 0.90)

            km_pred = _extract_predicate(km.title)
            if pm_pred != km_pred and pm_pred != _PREDICATE_OTHER and km_pred != _PREDICATE_OTHER:
                if score < 0.75:
                    continue

            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = (i, km)

        if best_match:
            idx, km = best_match
            used_k.add(idx)

            if not pm.yesTokenId or not pm.noTokenId:
                log(f"Skipping pair (Polymarket tokens incomplete): {pm.marketId}")
                continue

            if not km.yesTokenId or not km.noTokenId:
                log(f"Skipping pair (Kalshi tokens incomplete): {km.marketId}")
                continue

            pair_id = f"polymarket:{pm.marketId}___kalshi:{km.marketId}"
            sport = pm.sport or km.sport
            expiry = pm.expiryTs or km.expiryTs

            pairs.append({
                "pair_id": pair_id,
                "title": pm.title,
                "kalshi_title": km.title,
                "sport": sport,
                "expiry_ts": expiry or 0,
                "similarity": round(best_score, 3),
                "polymarket_url": pm.meta.get("url", ""),
                "kalshi_ticker": km.marketId,
                "polymarket": {
                    "venue": "polymarket",
                    "id": pm.marketId,
                    "question": pm.title,
                    "team_key": pm.team_key,
                    "yes_token": pm.yesTokenId,
                    "no_token": pm.noTokenId,
                    "expiry_ts": pm.expiryTs,
                    "sport": pm.sport,
                    "volume": pm.meta.get("volume", 0),
                },
                "kalshi": {
                    "venue": "kalshi",
                    "id": km.marketId,
                    "question": km.title,
                    "team_key": km.team_key,
                    "yes_token": km.yesTokenId,
                    "no_token": km.noTokenId,
                    "expiry_ts": km.expiryTs,
                    "sport": km.sport,
                    "volume": km.meta.get("volume", 0),
                },
            })

    log(f"Found {len(pairs)} matched pairs from {len(poly_markets)} Poly x {len(kalshi_markets)} Kalshi markets")
    return pairs
