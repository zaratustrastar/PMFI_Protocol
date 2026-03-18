"""AI-enhanced market matcher using GPT-4o-mini to compare resolution rules.

Flow:
  1. Fast Jaccard pre-filter shortlists candidates (same logic as fuzzy matcher).
  2. For each shortlisted (poly, opinion) candidate pair:
     - Check JSON file cache keyed by pair IDs (TTL = 7 days).
     - On cache miss: send both titles + resolution rules to GPT-4o-mini.
     - LLM returns: {match, confidence, title_score, rules_score, entity_score,
                     time_score, reason}
     - Cache the result.
  3. Accept the pair if confidence >= MIN_CONFIDENCE AND rules_score >= MIN_RULES_SCORE.
"""

import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from typing import Optional

from ..models import NormalizedMarket
from ..config import (
    OPENAI_API_KEY,
    AI_MATCH_CACHE_PATH,
    AI_MATCH_CACHE_TTL,
    AI_MATCH_MIN_CONFIDENCE,
    AI_MATCH_MIN_RULES_SCORE,
)


def log(msg: str):
    print(f"🤖 [AI Matcher] {msg}")


_JACCARD_SHORTLIST_K = 5
_MIN_JACCARD_PREFILTER = 0.20  # raised from 0.12 to cut false-positive LLM calls

_DIRECTION_MARKET_KEYWORDS = (
    "up or down", "hourly", "1hr", "15m", "30m", "4hr", "daily close",
)

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "to", "in", "of", "for", "on", "at", "by",
    "is", "be", "before", "end", "any", "during", "after", "or", "and",
    "not", "no", "yes", "if", "than", "that", "this", "it", "its", "has",
    "have", "had", "do", "does", "did", "was", "were", "been", "are", "am",
    "with", "from", "as", "but", "so", "just", "more", "most", "some",
    "other", "what", "which", "who", "when", "where", "why", "about",
    "between", "through", "into", "over", "under", "then", "up", "down",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_cache_lock = threading.Lock()
_cache: dict = {}
_cache_loaded = False


def _load_cache():
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    try:
        if os.path.exists(AI_MATCH_CACHE_PATH):
            with open(AI_MATCH_CACHE_PATH, "r") as f:
                _cache = json.load(f)
            log(f"Loaded {len(_cache)} cached pair scores from {AI_MATCH_CACHE_PATH}")
    except Exception as e:
        log(f"⚠️ Failed to load cache: {e}")
        _cache = {}
    _cache_loaded = True


def _save_cache():
    try:
        with open(AI_MATCH_CACHE_PATH, "w") as f:
            json.dump(_cache, f)
    except Exception as e:
        log(f"⚠️ Failed to save cache: {e}")


def _tokenize(text: str) -> set[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _fmt_expiry(ts: int) -> str:
    if not ts:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)


_SYSTEM_PROMPT = """You are an expert prediction market analyst. Given two binary prediction markets from different platforms, determine whether they resolve on exactly the same real-world event with the same outcome condition.

Respond ONLY with a JSON object (no markdown, no explanation outside the JSON):
{
  "match": true/false,
  "confidence": 0-100,
  "title_score": 0-100,
  "rules_score": 0-100,
  "entity_score": 0-100,
  "time_score": 0-100,
  "reason": "one sentence"
}

Scoring guidance:
- title_score: how similar the titles are (same subject, same question)
- rules_score: how well the resolution criteria agree (this is the most important)
- entity_score: shared named entities (teams, people, companies)
- time_score: alignment of resolution timing / expiry dates
- confidence: overall confidence this is a true match (weighted average, emphasis on rules_score)

Return match=false if the markets might look similar but resolve on different games, dates, or conditions."""


def _call_llm(poly: NormalizedMarket, opinion: NormalizedMarket) -> Optional[dict]:
    if not OPENAI_API_KEY:
        log("⚠️ OPENAI_API_KEY not set — skipping LLM scoring")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except ImportError:
        log("⚠️ openai package not installed — skipping LLM scoring")
        return None

    desc_a = (poly.description or "")[:800] or "(no resolution rules provided)"
    desc_b = (opinion.description or "")[:800] or "(no resolution rules provided)"

    user_msg = (
        f"Market A (Polymarket):\n"
        f"Title: {poly.title}\n"
        f"Resolution rules: {desc_a}\n"
        f"Expires: {_fmt_expiry(poly.expiryTs)}\n\n"
        f"Market B (Opinion.Markets):\n"
        f"Title: {opinion.title}\n"
        f"Resolution rules: {desc_b}\n"
        f"Expires: {_fmt_expiry(opinion.expiryTs)}"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        return result
    except json.JSONDecodeError as e:
        log(f"⚠️ LLM returned non-JSON for {poly.marketId} x {opinion.marketId}: {e}")
        return None
    except Exception as e:
        log(f"⚠️ LLM call failed for {poly.marketId} x {opinion.marketId}: {e}")
        return None


def _score_pair(poly: NormalizedMarket, opinion: NormalizedMarket) -> Optional[dict]:
    _load_cache()
    cache_key = f"{poly.marketId}:{opinion.marketId}"

    with _cache_lock:
        cached = _cache.get(cache_key)

    if cached:
        age = time.time() - cached.get("cachedAt", 0)
        if age < AI_MATCH_CACHE_TTL:
            return cached

    log(f"LLM scoring: {poly.title[:50]} × {opinion.title[:50]}")
    result = _call_llm(poly, opinion)
    if result is None:
        return None

    result["cachedAt"] = int(time.time())
    result["polyId"] = poly.marketId
    result["opinionId"] = opinion.marketId

    with _cache_lock:
        _cache[cache_key] = result
        _save_cache()

    return result


def find_opinion_pairs(
    poly_markets: list[NormalizedMarket],
    opinion_markets: list[NormalizedMarket],
) -> list[dict]:
    """Match Polymarket against Opinion.Markets using Jaccard pre-filter + LLM scoring.

    Returns list of pair dicts compatible with scanner.py expectations.
    """
    if not poly_markets or not opinion_markets:
        log("Nothing to match (empty market list)")
        return []

    log(f"Matching {len(poly_markets)} Poly × {len(opinion_markets)} Opinion markets")

    # Pre-filter Opinion markets: remove direction/hourly markets that can never match Poly
    def _is_direction(title: str) -> bool:
        t = title.lower()
        return any(kw in t for kw in _DIRECTION_MARKET_KEYWORDS)

    opinion_filtered = [m for m in opinion_markets if not _is_direction(m.title)]
    filtered_count = len(opinion_markets) - len(opinion_filtered)
    if filtered_count:
        log(f"Filtered {filtered_count} direction/hourly Opinion markets before matching")

    op_tokenized = [(_tokenize(m.title), m) for m in opinion_filtered]
    pairs = []
    used_op_ids: set = set()
    llm_calls = 0
    cache_hits = 0
    rejected = 0
    team_key_hits = 0

    for pm in poly_markets:
        if not pm.title or not pm.yesTokenId or not pm.noTokenId:
            continue

        pm_tokens = _tokenize(pm.title)
        if not pm_tokens:
            continue

        # ── Fast path: team_key hard-match (no LLM needed) ─────────────────
        if pm.team_key:
            for _, om in op_tokenized:
                if om.marketId in used_op_ids:
                    continue
                if om.team_key and om.team_key == pm.team_key:
                    used_op_ids.add(om.marketId)
                    team_key_hits += 1
                    pair_id = f"polymarket:{pm.marketId}___opinion:{om.marketId}"
                    sport = pm.sport or om.sport
                    expiry = pm.expiryTs or om.expiryTs
                    pairs.append({
                        "pair_id": pair_id,
                        "title": pm.title,
                        "opinion_title": om.title,
                        "sport": sport,
                        "expiry_ts": expiry or 0,
                        "similarity": 1.0,
                        "ai_score": {
                            "confidence": 95,
                            "title_score": 95,
                            "rules_score": 90,
                            "entity_score": 95,
                            "time_score": 95,
                            "reason": f"team_key hard-match: {pm.team_key}",
                        },
                        "polymarket_url": pm.meta.get("url", ""),
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
                        "opinion": {
                            "venue": "opinion",
                            "id": om.marketId,
                            "question": om.title,
                            "team_key": om.team_key,
                            "yes_token": om.yesTokenId,
                            "no_token": om.noTokenId,
                            "expiry_ts": om.expiryTs,
                            "sport": om.sport,
                            "volume": om.meta.get("volume", 0),
                        },
                    })
                    log(
                        f"✅ Team-key match: {pm.title[:50]} × {om.title[:50]} "
                        f"(team_key={pm.team_key})"
                    )
                    break  # one Opinion market per Poly market
        # ────────────────────────────────────────────────────────────────────

        candidates = []
        for op_tokens, om in op_tokenized:
            if om.marketId in used_op_ids:
                continue
            j = _jaccard(pm_tokens, op_tokens)
            if j >= _MIN_JACCARD_PREFILTER:
                candidates.append((j, om))

        if not candidates:
            continue

        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[:_JACCARD_SHORTLIST_K]

        for jaccard_score, om in candidates:
            if not om.yesTokenId or not om.noTokenId:
                continue

            cache_key = f"{pm.marketId}:{om.marketId}"
            with _cache_lock:
                cached = _cache.get(cache_key)

            if cached and (time.time() - cached.get("cachedAt", 0)) < AI_MATCH_CACHE_TTL:
                score = cached
                cache_hits += 1
            else:
                score = _score_pair(pm, om)
                llm_calls += 1

            if score is None:
                rejected += 1
                continue

            confidence = score.get("confidence", 0)
            rules_score = score.get("rules_score", 0)
            is_match = score.get("match", False)

            if not is_match or confidence < AI_MATCH_MIN_CONFIDENCE or rules_score < AI_MATCH_MIN_RULES_SCORE:
                rejected += 1
                continue

            if om.marketId in used_op_ids:
                rejected += 1
                continue

            used_op_ids.add(om.marketId)

            pair_id = f"polymarket:{pm.marketId}___opinion:{om.marketId}"
            sport = pm.sport or om.sport
            expiry = pm.expiryTs or om.expiryTs

            pairs.append({
                "pair_id": pair_id,
                "title": pm.title,
                "opinion_title": om.title,
                "sport": sport,
                "expiry_ts": expiry or 0,
                "similarity": round(jaccard_score, 3),
                "ai_score": {
                    "confidence": confidence,
                    "title_score": score.get("title_score", 0),
                    "rules_score": rules_score,
                    "entity_score": score.get("entity_score", 0),
                    "time_score": score.get("time_score", 0),
                    "reason": score.get("reason", ""),
                },
                "polymarket_url": pm.meta.get("url", ""),
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
                "opinion": {
                    "venue": "opinion",
                    "id": om.marketId,
                    "question": om.title,
                    "team_key": om.team_key,
                    "yes_token": om.yesTokenId,
                    "no_token": om.noTokenId,
                    "expiry_ts": om.expiryTs,
                    "sport": om.sport,
                    "volume": om.meta.get("volume", 0),
                },
            })
            log(
                f"✅ Matched: {pm.title[:45]} × {om.title[:45]} "
                f"(conf={confidence}, rules={rules_score}, jaccard={jaccard_score:.2f})"
            )
            break

    log(
        f"Done: {len(pairs)} pairs accepted ({team_key_hits} via team_key, "
        f"{len(pairs) - team_key_hits} via LLM), {rejected} rejected, "
        f"{llm_calls} LLM calls, {cache_hits} cache hits"
    )
    return pairs
