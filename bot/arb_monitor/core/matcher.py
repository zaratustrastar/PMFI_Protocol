"""Matcher - finds matching markets across Polymarket and Kalshi.

Matching strategy (hardened v2):
  1. Tokenize with expanded stopwords + boilerplate removal.
  2. Drop pure year/time tokens unless both sides share a non-time anchor.
  3. Topic classification (crypto, geopolitics, companies, politics, sports) — block cross-topic.
  4. Predicate gating with expanded verb classes (acquire≠expel, nominate≠invade, etc.).
  5. Anchor entity requirement: at least one shared non-stopword token len>=4 OR shared curated keyword.
  6. Fast pass: token Jaccard on titles to build top-K shortlist.
  7. Refine: Levenshtein on shortlist only (expensive, constrained to top candidates).
  8. Combined score: 0.6 * Jaccard + 0.4 * Levenshtein.
  9. Team key boost: if both have matching team_key, boost to max(score, 0.90).
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

STOPWORDS = frozenset({
    "will", "the", "a", "an", "to", "in", "of", "for", "on", "at", "by",
    "is", "be", "before", "end", "any", "member", "during", "after",
    "or", "and", "not", "no", "yes", "if", "than", "that", "this",
    "it", "its", "has", "have", "had", "do", "does", "did", "was",
    "were", "been", "being", "are", "am", "with", "from", "as",
    "but", "so", "just", "more", "most", "some", "other", "each",
    "all", "both", "few", "many", "much", "very", "also", "how",
    "what", "which", "who", "whom", "when", "where", "why",
    "about", "between", "through", "into", "over", "under",
    "again", "once", "here", "there", "then", "up", "down",
    "out", "off", "above", "below",
})

PURE_YEAR_RE = re.compile(r'^20[2-3]\d$')
TIME_TOKENS = frozenset({
    "year", "month", "week", "day", "hour",
    "january", "jan", "february", "feb", "march", "mar",
    "april", "apr", "may", "june", "jun", "july", "jul",
    "august", "aug", "september", "sep", "october", "oct",
    "november", "nov", "december", "dec",
    "q1", "q2", "q3", "q4",
})

ANCHOR_KEYWORDS = frozenset({
    "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "xrp", "dogecoin",
    "greenland", "openai", "tesla", "ukraine", "russia", "china", "taiwan",
    "fed", "chair", "warsh", "powell", "yellen", "trump", "biden", "harris",
    "congress", "senate", "house", "supreme", "court", "nato", "eu",
    "spacex", "google", "apple", "amazon", "microsoft", "meta", "nvidia",
    "tiktok", "musk", "bezos", "pope", "vatican", "israel", "gaza", "iran",
    "korea", "tariff", "recession", "inflation", "rate", "gdp",
    "olympics", "fifa", "nba", "nfl", "mlb", "nhl", "ufc",
    "ai", "gpt", "agi", "nuclear", "asteroid", "mars",
})

TOPIC_KEYWORDS = {
    "crypto": {
        "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "xrp",
        "dogecoin", "doge", "cardano", "ada", "polygon", "matic",
        "defi", "nft", "blockchain", "halving", "stablecoin", "usdc",
        "usdt", "binance", "coinbase", "sec", "etf", "altcoin",
        "litecoin", "ripple", "avalanche", "chainlink",
    },
    "geopolitics": {
        "ukraine", "russia", "china", "taiwan", "greenland", "nato",
        "eu", "gaza", "israel", "iran", "korea", "war", "invasion",
        "ceasefire", "sanctions", "annex", "independence", "sovereignty",
        "nuclear", "missile", "troops", "military", "peace",
        "territory", "border", "occupation",
    },
    "companies": {
        "openai", "tesla", "spacex", "google", "apple", "amazon",
        "microsoft", "meta", "nvidia", "tiktok", "twitter",
        "acquired", "acquire", "acquisition", "merger", "ipo",
        "ceo", "founder", "valuation", "stock", "shares",
        "revenue", "earnings", "profit", "market cap",
    },
    "politics": {
        "trump", "biden", "harris", "congress", "senate", "house",
        "president", "election", "vote", "poll", "democrat",
        "republican", "gop", "governor", "mayor", "nominee",
        "impeach", "expelled", "expel", "resign", "indicted",
        "cabinet", "veto", "legislation", "bill", "law",
        "fed", "chair", "warsh", "powell", "yellen",
        "tariff", "recession", "inflation", "rate",
    },
    "sports": {
        "nba", "nfl", "mlb", "nhl", "ufc", "mma", "boxing",
        "fifa", "premier league", "champions league", "olympics",
        "tennis", "golf", "f1", "formula", "ncaa",
        "playoff", "finals", "championship", "mvp", "draft",
        "super bowl", "world cup", "world series",
    },
}

_PREDICATE_ENDORSE = "ENDORSE"
_PREDICATE_WIN_PRIMARY = "WIN_PRIMARY"
_PREDICATE_WIN_GENERAL = "WIN_GENERAL"
_PREDICATE_ACQUIRE = "ACQUIRE"
_PREDICATE_EXPEL = "EXPEL"
_PREDICATE_NOMINATE = "NOMINATE"
_PREDICATE_INDEPENDENCE = "INDEPENDENCE"
_PREDICATE_INVADE = "INVADE"
_PREDICATE_RESIGN = "RESIGN"
_PREDICATE_BAN = "BAN"
_PREDICATE_APPROVE = "APPROVE"
_PREDICATE_OTHER = "OTHER"

_PREDICATE_VERB_CLASSES = {
    _PREDICATE_ACQUIRE: {"acquire", "acquired", "acquisition", "buy", "bought", "purchase", "merge", "merger"},
    _PREDICATE_EXPEL: {"expel", "expelled", "expelling", "expulsion", "remove", "removed", "oust", "ousted", "eject"},
    _PREDICATE_NOMINATE: {"nominate", "nominated", "nomination", "appoint", "appointed", "appointment", "pick", "select"},
    _PREDICATE_INDEPENDENCE: {"independence", "independent", "secede", "secession", "sovereignty", "autonomous"},
    _PREDICATE_INVADE: {"invade", "invaded", "invasion", "annex", "annexed", "annexation", "occupy", "occupied", "seize"},
    _PREDICATE_RESIGN: {"resign", "resigned", "resignation", "step down", "quit"},
    _PREDICATE_BAN: {"ban", "banned", "banning", "prohibit", "prohibited", "block", "blocked", "restrict"},
    _PREDICATE_APPROVE: {"approve", "approved", "approval", "pass", "passed", "ratify", "ratified", "enact"},
    _PREDICATE_ENDORSE: {"endorse", "endorsed", "endorsement", "endors", "backing", "back"},
    _PREDICATE_WIN_PRIMARY: {"win", "winner", "primary", "nominee", "nomination", "runoff"},
    _PREDICATE_WIN_GENERAL: {"election", "general", "electoral"},
}

_INCOMPATIBLE_PREDICATES = {
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_EXPEL}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_NOMINATE}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_INDEPENDENCE}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_RESIGN}),
    frozenset({_PREDICATE_ACQUIRE, _PREDICATE_BAN}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_NOMINATE}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_INDEPENDENCE}),
    frozenset({_PREDICATE_EXPEL, _PREDICATE_APPROVE}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_EXPEL}),
    frozenset({_PREDICATE_NOMINATE, _PREDICATE_BAN}),
    frozenset({_PREDICATE_INDEPENDENCE, _PREDICATE_INVADE}),
    frozenset({_PREDICATE_INDEPENDENCE, _PREDICATE_ACQUIRE}),
    frozenset({_PREDICATE_RESIGN, _PREDICATE_NOMINATE}),
    frozenset({_PREDICATE_RESIGN, _PREDICATE_APPROVE}),
    frozenset({_PREDICATE_BAN, _PREDICATE_APPROVE}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_WIN_PRIMARY}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_WIN_GENERAL}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_EXPEL}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_ACQUIRE}),
    frozenset({_PREDICATE_ENDORSE, _PREDICATE_INVADE}),
}


def _normalize_vs(text: str) -> str:
    t = re.sub(r'\s+(?:vs\.?|versus|@)\s+', ' vs ', text, flags=re.IGNORECASE)
    return t


def _tokenize(text: str) -> set[str]:
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    tokens = {w for w in t.split() if w not in STOPWORDS and len(w) > 1}
    return tokens


def _is_time_token(token: str) -> bool:
    if PURE_YEAR_RE.match(token):
        return True
    return token in TIME_TOKENS


def _filter_time_tokens(tokens_a: set[str], tokens_b: set[str]) -> tuple[set[str], set[str]]:
    non_time_a = {t for t in tokens_a if not _is_time_token(t)}
    non_time_b = {t for t in tokens_b if not _is_time_token(t)}

    shared_non_time = non_time_a & non_time_b
    if shared_non_time:
        return non_time_a, non_time_b

    return non_time_a, non_time_b


def _classify_topic(tokens: set[str], title_lower: str) -> str | None:
    best_topic = None
    best_hits = 0

    for topic, keywords in TOPIC_KEYWORDS.items():
        hits = 0
        for kw in keywords:
            if " " in kw:
                if kw in title_lower:
                    hits += 1
            elif kw in tokens:
                hits += 1

        if hits > best_hits:
            best_hits = hits
            best_topic = topic

    return best_topic if best_hits >= 1 else None


def _topics_compatible(topic_a: str | None, topic_b: str | None) -> bool:
    if topic_a is None or topic_b is None:
        return True
    return topic_a == topic_b


def _extract_predicate(title: str) -> str:
    t = title.lower()

    for pred, verb_set in _PREDICATE_VERB_CLASSES.items():
        for verb in verb_set:
            if " " in verb:
                if verb in t:
                    return pred
            elif re.search(r'\b' + re.escape(verb) + r'\b', t):
                return pred

    return _PREDICATE_OTHER


def _predicates_compatible(pred_a: str, pred_b: str) -> bool:
    if pred_a == _PREDICATE_OTHER or pred_b == _PREDICATE_OTHER:
        return True
    if pred_a == pred_b:
        return True
    pair = frozenset({pred_a, pred_b})
    return pair not in _INCOMPATIBLE_PREDICATES


def _find_shared_anchors(tokens_a: set[str], tokens_b: set[str]) -> set[str]:
    shared = tokens_a & tokens_b

    anchors = set()
    for t in shared:
        if _is_time_token(t):
            continue
        if t in ANCHOR_KEYWORDS:
            anchors.add(t)
        elif len(t) >= 4:
            anchors.add(t)

    return anchors


def _has_anchor(tokens_a: set[str], tokens_b: set[str], sport_a: str | None, sport_b: str | None) -> bool:
    if sport_a or sport_b:
        return True

    return len(_find_shared_anchors(tokens_a, tokens_b)) > 0


def token_jaccard(a: str, b: str) -> float:
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta or not tb:
        return 0.0

    ta, tb = _filter_time_tokens(ta, tb)
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


def _evaluate_candidate(pm_title_norm: str, km_title_norm: str,
                         pm: NormalizedMarket, km: NormalizedMarket,
                         pm_pred: str, km_pred: str) -> dict:
    tokens_a = _tokenize(pm_title_norm)
    tokens_b = _tokenize(km_title_norm)

    tokens_a_filtered, tokens_b_filtered = _filter_time_tokens(tokens_a, tokens_b)

    topic_a = _classify_topic(tokens_a, pm_title_norm.lower())
    topic_b = _classify_topic(tokens_b, km_title_norm.lower())

    shared_anchors = _find_shared_anchors(tokens_a_filtered, tokens_b_filtered)

    intersection = tokens_a_filtered & tokens_b_filtered
    union = tokens_a_filtered | tokens_b_filtered
    jaccard = len(intersection) / len(union) if union else 0.0

    lev = _levenshtein_similarity(pm_title_norm, km_title_norm)
    final_score = 0.6 * jaccard + 0.4 * lev

    if pm.team_key and km.team_key and pm.team_key == km.team_key:
        final_score = max(final_score, 0.90)

    reject_reasons = []
    accept_reasons = []

    if not _predicates_compatible(pm_pred, km_pred):
        reject_reasons.append(f"predicate_conflict: {pm_pred} vs {km_pred}")
        final_score = 0.0

    if not _topics_compatible(topic_a, topic_b):
        reject_reasons.append(f"topic_mismatch: {topic_a} vs {topic_b}")
        final_score = 0.0

    if not _has_anchor(tokens_a_filtered, tokens_b_filtered, pm.sport, km.sport):
        reject_reasons.append("no_shared_anchor_entity")
        final_score = 0.0

    if final_score > 0 and not reject_reasons:
        if pm.team_key and km.team_key and pm.team_key == km.team_key:
            accept_reasons.append("team_key_match")
        if shared_anchors:
            accept_reasons.append(f"anchors: {', '.join(sorted(shared_anchors)[:5])}")
        if topic_a and topic_a == topic_b:
            accept_reasons.append(f"same_topic: {topic_a}")

    return {
        "tokensA": sorted(tokens_a),
        "tokensB": sorted(tokens_b),
        "sharedAnchors": sorted(shared_anchors),
        "topicA": topic_a,
        "topicB": topic_b,
        "predicateA": pm_pred,
        "predicateB": km_pred,
        "jaccard": round(jaccard, 4),
        "levenshtein": round(lev, 4),
        "finalScore": round(final_score, 4),
        "whyAccepted": "; ".join(accept_reasons) if accept_reasons else None,
        "whyRejected": "; ".join(reject_reasons) if reject_reasons else None,
    }


def find_pairs(poly_markets: list[NormalizedMarket], kalshi_markets: list[NormalizedMarket],
               min_similarity: float = 0.35) -> list[dict]:
    pairs = []
    used_k: set[int] = set()

    for pm in poly_markets:
        if not pm.title:
            continue

        pm_title_norm = _normalize_vs(pm.title)
        pm_pred = _extract_predicate(pm.title)
        pm_tokens = _tokenize(pm_title_norm)
        pm_tokens_filtered, _ = _filter_time_tokens(pm_tokens, pm_tokens)
        pm_topic = _classify_topic(pm_tokens, pm_title_norm.lower())

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
            km_tokens = _tokenize(km_title_norm)
            km_tokens_filtered, _ = _filter_time_tokens(km_tokens, km_tokens)
            km_topic = _classify_topic(km_tokens, km_title_norm.lower())

            if not _topics_compatible(pm_topic, km_topic):
                continue

            _, filtered_b = _filter_time_tokens(pm_tokens, km_tokens)
            pm_filt, km_filt = _filter_time_tokens(pm_tokens, km_tokens)

            if not _has_anchor(pm_filt, km_filt, pm.sport, km.sport):
                continue

            intersection = pm_filt & km_filt
            union = pm_filt | km_filt
            jaccard = len(intersection) / len(union) if union else 0.0

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
                "kalshi_ticker": km.meta.get("event_ticker", km.marketId),
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


def debug_match_candidates(poly_markets: list[NormalizedMarket],
                            kalshi_markets: list[NormalizedMarket],
                            top_n: int = 50) -> list[dict]:
    candidates = []

    for pm in poly_markets[:200]:
        if not pm.title:
            continue
        pm_title_norm = _normalize_vs(pm.title)
        pm_pred = _extract_predicate(pm.title)

        for km in kalshi_markets[:200]:
            if not km.title:
                continue
            km_title_norm = _normalize_vs(km.title)
            km_pred = _extract_predicate(km.title)

            eval_result = _evaluate_candidate(
                pm_title_norm, km_title_norm, pm, km, pm_pred, km_pred
            )

            if eval_result["finalScore"] > 0.05 or eval_result["whyRejected"]:
                candidates.append({
                    "polyTitle": pm.title,
                    "kalshiTitle": km.title,
                    "polyId": pm.marketId,
                    "kalshiId": km.marketId,
                    **eval_result,
                })

    candidates.sort(key=lambda x: x["finalScore"], reverse=True)

    accepted = [c for c in candidates if c["whyRejected"] is None and c["finalScore"] >= 0.35]
    rejected_interesting = [c for c in candidates if c["whyRejected"] is not None][:20]

    result = accepted[:top_n]
    remaining = top_n - len(result)
    if remaining > 0:
        result.extend(rejected_interesting[:remaining])

    return result[:top_n]
