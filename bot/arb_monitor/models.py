"""Normalized market model shared across adapters."""

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

_VS_PATTERN = re.compile(r'\s+(?:vs\.?|versus|@)\s+', re.IGNORECASE)


def extract_team_key(title: str) -> Optional[str]:
    """Extract canonical team_key from matchup titles containing vs / @ / versus.

    Returns sorted "teamA|teamB" or None if no matchup detected.
    """
    parts = _VS_PATTERN.split(title, maxsplit=1)
    if len(parts) != 2:
        return None

    team_a = _clean_team(parts[0])
    team_b = _clean_team(parts[1])

    if not team_a or not team_b:
        return None

    sorted_teams = sorted([team_a, team_b])
    return f"{sorted_teams[0]}|{sorted_teams[1]}"


def _clean_team(raw: str) -> str:
    t = raw.strip().lower()
    t = re.sub(r'\s*[:\-–—]\s*.*$', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


@dataclass
class NormalizedMarket:
    venue: str
    marketId: str
    title: str
    expiryTs: int
    yesTokenId: str
    noTokenId: str
    sport: Optional[str] = None
    team_key: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def question(self) -> str:
        return self.title
