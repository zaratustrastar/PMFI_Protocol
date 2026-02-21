"""Matcher - finds matching markets across Polymarket and a second venue (Opinion for MVP).

Matching strategy:
  1. If both markets have a team_key (matchup with vs/@/versus), use team_key exact match
     weighted 0.7, plus token jaccard on titles weighted 0.3.
  2. If neither has a team_key (non-matchup markets), use token jaccard on titles.
  3. Expiry proximity gates: 12h for esports, 24h for nba/sports/mma.
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


def compute_similarity(pm: NormalizedMarket, km: NormalizedMarket) -> float:
    pm_title = _normalize_vs(pm.title)
    km_title = _normalize_vs(km.title)

    if pm.team_key and km.team_key:
        team_exact = 1.0 if pm.team_key == km.team_key else 0.0
        jaccard = token_jaccard(pm_title, km_title)
        return 0.7 * team_exact + 0.3 * jaccard

    return token_jaccard(pm_title, km_title)


def _expiry_close_enough(pm: NormalizedMarket, km: NormalizedMarket) -> bool:
    if pm.expiryTs <= 0 or km.expiryTs <= 0:
        return True

    sport = pm.sport or km.sport or ""
    gate = EXPIRY_GATES.get(sport, DEFAULT_EXPIRY_GATE)
    return abs(pm.expiryTs - km.expiryTs) <= gate


def find_pairs(poly_markets: list[NormalizedMarket], venue_b_markets: list[NormalizedMarket],
               min_similarity: float = 0.45) -> list[dict]:
    pairs = []
    used_b: set[int] = set()

    for pm in poly_markets:
        if not pm.title:
            continue

        best_match = None
        best_score = 0.0

        pm_pred = _extract_predicate(pm.title)

        for i, bm in enumerate(venue_b_markets):
            if i in used_b:
                continue
            if not bm.title:
                continue

            if not _expiry_close_enough(pm, bm):
                continue

            bm_pred = _extract_predicate(bm.title)
            if not _predicates_compatible(pm_pred, bm_pred):
                continue

            score = compute_similarity(pm, bm)

            if pm_pred != bm_pred and pm_pred != _PREDICATE_OTHER and bm_pred != _PREDICATE_OTHER:
                if score < 0.75:
                    continue

            if score > best_score and score >= min_similarity:
                best_score = score
                best_match = (i, bm)

        if best_match:
            idx, bm = best_match
            used_b.add(idx)

            if not pm.yesTokenId or not pm.noTokenId:
                log(f"Skipping pair (Polymarket tokens incomplete): {pm.marketId} yes={pm.yesTokenId!r} no={pm.noTokenId!r}")
                continue

            if not bm.yesTokenId or not bm.noTokenId:
                log(f"Skipping pair ({bm.venue} tokens incomplete): {bm.marketId} yes={bm.yesTokenId!r} no={bm.noTokenId!r}")
                continue

            venue_b_name = bm.venue or "opinion"
            pair_id = f"polymarket:{pm.marketId}___{venue_b_name}:{bm.marketId}"
            sport = pm.sport or bm.sport
            expiry = pm.expiryTs or bm.expiryTs

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
                    "team_key": pm.team_key,
                    "yes_token": pm.yesTokenId,
                    "no_token": pm.noTokenId,
                    "expiry_ts": pm.expiryTs,
                    "sport": pm.sport,
                },
                "opinion": {
                    "venue": venue_b_name,
                    "id": bm.marketId,
                    "question": bm.title,
                    "team_key": bm.team_key,
                    "yes_token": bm.yesTokenId,
                    "no_token": bm.noTokenId,
                    "expiry_ts": bm.expiryTs,
                    "sport": bm.sport,
                },
            })

    log(f"Found {len(pairs)} matched pairs from {len(poly_markets)} Poly x {len(venue_b_markets)} {venue_b_markets[0].venue if venue_b_markets else 'venue_b'} markets")
    return pairs
