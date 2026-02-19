"""Filters for categorizing and filtering markets by sport/category and expiry window."""

import re
import time
from typing import Optional
from ..config import SPORTS_KEYWORDS, ALL_SPORT_KEYWORDS, ARB_EXPIRY_WINDOW_DAYS
from ..models import NormalizedMarket

_KEYWORD_PATTERNS: dict[str, list[re.Pattern]] = {}

def _get_patterns() -> dict[str, list[re.Pattern]]:
    if not _KEYWORD_PATTERNS:
        for category, keywords in SPORTS_KEYWORDS.items():
            _KEYWORD_PATTERNS[category] = [
                re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
                for kw in keywords
            ]
    return _KEYWORD_PATTERNS


def classify_sport(title: str) -> Optional[str]:
    patterns = _get_patterns()
    for category, pats in patterns.items():
        for pat in pats:
            if pat.search(title):
                return category
    return None


def is_sports_market(title: str) -> bool:
    return classify_sport(title) is not None


def matches_sport_filter(title: str, sport_filters: list[str]) -> bool:
    if not sport_filters:
        return True
    sport = classify_sport(title)
    if not sport:
        return False
    return sport in sport_filters


def filter_by_expiry(markets: list[NormalizedMarket], window_days: Optional[int] = None) -> list[NormalizedMarket]:
    if window_days is None:
        window_days = ARB_EXPIRY_WINDOW_DAYS
    now = int(time.time())
    max_ts = now + (window_days * 86400)

    result = []
    for m in markets:
        if m.expiryTs <= 0:
            result.append(m)
            continue
        if now < m.expiryTs < max_ts:
            result.append(m)
    return result


def filter_markets_by_sports(markets: list[dict], sport_filters: list[str] | None = None) -> list[dict]:
    result = []
    for m in markets:
        q = m.get("question", m.get("title", ""))
        if sport_filters:
            if matches_sport_filter(q, sport_filters):
                m["sport"] = classify_sport(q)
                result.append(m)
        else:
            sport = classify_sport(q)
            if sport:
                m["sport"] = sport
                result.append(m)
    return result
