"""Normalized market model shared across adapters."""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class NormalizedMarket:
    venue: str
    marketId: str
    title: str
    expiryTs: int
    yesTokenId: str
    noTokenId: str
    sport: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def question(self) -> str:
        return self.title
