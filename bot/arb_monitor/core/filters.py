"""Filters for categorizing and filtering markets by sport/category."""

from ..config import SPORTS_KEYWORDS, ALL_SPORT_KEYWORDS


def classify_sport(question: str) -> str | None:
    """Classify a market question into a sport category. Returns None if no match."""
    q = question.lower()
    for category, keywords in SPORTS_KEYWORDS.items():
        for kw in keywords:
            if kw in q:
                return category
    return None


def is_sports_market(question: str) -> bool:
    """Check if a market question relates to sports/esports."""
    return classify_sport(question) is not None


def matches_sport_filter(question: str, sport_filters: list[str]) -> bool:
    """Check if a market matches any of the requested sport filters."""
    if not sport_filters:
        return True
    sport = classify_sport(question)
    if not sport:
        return False
    return sport in sport_filters


def filter_markets_by_sports(markets: list[dict], sport_filters: list[str] | None = None) -> list[dict]:
    """Filter normalized markets to only include sports/esports-related ones."""
    result = []
    for m in markets:
        q = m.get("question", "")
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
