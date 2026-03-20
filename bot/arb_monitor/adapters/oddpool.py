"""Oddpool adapter - fetches cross-venue arbitrage opportunities from Oddpool /arb-current API.

Oddpool is the sole source of matched Polymarket × Kalshi pairs for the pArbitrage vault.
All previous LLM/fuzzy matching is bypassed when ARB_USE_ODDPOOL_ONLY=true.

Normalizes each entry into an ArbOpportunity dataclass and sorts by pnl_velocity
(= gross_edge_pct / max(days_to_expiry, 0.5)) descending to prioritise highest PnL velocity.
"""

import time
import os
import requests
from dataclasses import dataclass
from typing import Optional
from ..config import ODDPOOL_API_KEY, ODDPOOL_BASE_URL


def log(msg: str):
    print(f"🔀 [Arb/Oddpool] {msg}")


@dataclass
class ArbOpportunity:
    pair_id: str
    poly_yes_token: str
    poly_no_token: str
    kalshi_ticker: str
    poly_yes_ask: float
    kalshi_yes_ask: float
    gross_edge_pct: float
    expiry_ts: int
    days_to_expiry: float
    pnl_velocity: float
    poly_title: str = ""
    kalshi_title: str = ""
    # Which side to buy on Kalshi to complete the arb with Poly YES.
    # "YES"  → buy Kalshi YES (market is already the mirror of Poly, e.g. "Will X NOT happen?")
    # "NO"   → buy Kalshi NO  (market is the same direction as Poly; NO completes the spread)
    # Defaults to "YES" when Oddpool does not specify; update if their schema adds a side field.
    kalshi_side: str = "YES"
    raw: dict = None

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "poly_yes_token": self.poly_yes_token,
            "poly_no_token": self.poly_no_token,
            "kalshi_ticker": self.kalshi_ticker,
            "poly_yes_ask": self.poly_yes_ask,
            "kalshi_yes_ask": self.kalshi_yes_ask,
            "gross_edge_pct": round(self.gross_edge_pct, 6),
            "expiry_ts": self.expiry_ts,
            "days_to_expiry": round(self.days_to_expiry, 3),
            "pnl_velocity": round(self.pnl_velocity, 6),
            "poly_title": self.poly_title,
            "kalshi_title": self.kalshi_title,
            "kalshi_side": self.kalshi_side,
        }


def _headers() -> dict:
    headers = {"accept": "application/json"}
    if ODDPOOL_API_KEY:
        headers["X-API-Key"] = ODDPOOL_API_KEY
        headers["Authorization"] = f"Bearer {ODDPOOL_API_KEY}"
    return headers


def fetch_arb_current() -> list[dict]:
    """Fetch /arb-current from Oddpool API. Returns raw list of opportunity dicts."""
    url = f"{ODDPOOL_BASE_URL}/arb-current"
    log(f"Fetching {url}")
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        if resp.status_code != 200:
            log(f"❌ HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("opportunities", data.get("data", data.get("arb", [])))
        log(f"⚠️ Unexpected response type: {type(data)}")
        return []
    except Exception as e:
        log(f"❌ fetch_arb_current error: {e}")
        return []


def _parse_expiry(raw_entry: dict) -> int:
    """Parse expiry timestamp from Oddpool entry."""
    for field in ("expiry_ts", "expiry", "expires_at", "close_time", "end_time"):
        val = raw_entry.get(field)
        if not val:
            continue
        try:
            if isinstance(val, (int, float)):
                ts = int(val)
                if ts > 1e12:
                    ts = ts // 1000
                return ts
            from datetime import datetime
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            continue
    return 0


def normalize_opportunity(entry: dict) -> Optional[ArbOpportunity]:
    """Normalize a raw Oddpool arb entry into an ArbOpportunity dataclass."""
    try:
        poly_yes_token = (
            entry.get("poly_yes_token") or
            entry.get("polymarket_yes_token") or
            entry.get("poly_token_id") or
            ""
        )
        poly_no_token = (
            entry.get("poly_no_token") or
            entry.get("polymarket_no_token") or
            ""
        )
        kalshi_ticker = (
            entry.get("kalshi_ticker") or
            entry.get("kalshi_market_ticker") or
            entry.get("kalshi_id") or
            ""
        )

        if not poly_yes_token or not kalshi_ticker:
            log(f"⚠️ Skipping entry missing poly_yes_token or kalshi_ticker: {entry}")
            return None

        poly_yes_ask = float(
            entry.get("poly_yes_ask") or
            entry.get("polymarket_yes_ask") or
            entry.get("poly_ask") or
            0
        )
        kalshi_yes_ask = float(
            entry.get("kalshi_yes_ask") or
            entry.get("kalshi_ask") or
            0
        )

        edge_from_api = (
            entry.get("gross_edge_pct") or
            entry.get("edge_pct") or
            entry.get("edge")
        )
        if edge_from_api is not None:
            gross_edge_pct = float(edge_from_api)
        elif poly_yes_ask > 0 and kalshi_yes_ask > 0:
            # Cross-venue YES+YES spread: edge = 1 - poly_yes_ask - kalshi_yes_ask
            gross_edge_pct = max(0.0, 1.0 - poly_yes_ask - kalshi_yes_ask)
        else:
            gross_edge_pct = 0.0

        expiry_ts = _parse_expiry(entry)
        now = time.time()
        days_to_expiry = max(0, (expiry_ts - now) / 86400) if expiry_ts > now else 0

        pnl_velocity = gross_edge_pct / max(days_to_expiry, 0.5)

        pair_id = (
            entry.get("pair_id") or
            f"poly:{poly_yes_token[:16]}___kalshi:{kalshi_ticker}"
        )

        poly_title = entry.get("poly_title") or entry.get("polymarket_title") or ""
        kalshi_title = entry.get("kalshi_title") or entry.get("kalshi_market_title") or ""

        # Determine which side to buy on Kalshi for the arb leg.
        # Oddpool may provide "kalshi_side": "YES" or "NO" explicitly.
        # - "YES": Kalshi market is defined opposite to Poly (e.g. "Will X NOT happen?"), so buying
        #          Kalshi YES is the complementary side that locks in the spread with Poly YES.
        # - "NO":  Kalshi market is same-direction as Poly; buying Kalshi NO completes the arb.
        # If Oddpool does not provide this field, we infer from context:
        #   prefer "NO" when `kalshi_no_ask` is present and cheaper than YES (true NO-hedge),
        #   otherwise default to "YES".
        kalshi_side_raw = (
            entry.get("kalshi_side") or
            entry.get("kalshi_arb_side") or
            entry.get("kalshi_leg_side") or
            ""
        ).upper()
        if kalshi_side_raw in ("YES", "NO"):
            kalshi_side = kalshi_side_raw
        else:
            # Infer: if Oddpool provides kalshi_no_ask and that's what forms the edge, use NO
            kalshi_no_ask_raw = entry.get("kalshi_no_ask") or entry.get("kalshi_no_price")
            if kalshi_no_ask_raw is not None:
                kalshi_no_ask = float(kalshi_no_ask_raw)
                # Edge is 1 - poly_yes_ask - kalshi_no_ask in this case
                no_edge = 1.0 - poly_yes_ask - kalshi_no_ask if poly_yes_ask > 0 and kalshi_no_ask > 0 else -1
                yes_edge = 1.0 - poly_yes_ask - kalshi_yes_ask if poly_yes_ask > 0 and kalshi_yes_ask > 0 else -1
                kalshi_side = "NO" if no_edge > yes_edge else "YES"
                if kalshi_side == "NO":
                    # Override the ask to the NO ask for the edge/pricing formulas
                    kalshi_yes_ask = kalshi_no_ask
                    gross_edge_pct = max(0.0, no_edge)
                    pnl_velocity = gross_edge_pct / max(days_to_expiry, 0.5)
            else:
                kalshi_side = "YES"

        return ArbOpportunity(
            pair_id=pair_id,
            poly_yes_token=poly_yes_token,
            poly_no_token=poly_no_token,
            kalshi_ticker=kalshi_ticker,
            poly_yes_ask=poly_yes_ask,
            kalshi_yes_ask=kalshi_yes_ask,
            gross_edge_pct=gross_edge_pct,
            expiry_ts=expiry_ts,
            days_to_expiry=days_to_expiry,
            pnl_velocity=pnl_velocity,
            poly_title=poly_title,
            kalshi_title=kalshi_title,
            kalshi_side=kalshi_side,
            raw=entry,
        )
    except Exception as e:
        log(f"❌ normalize_opportunity error: {e}, entry={entry}")
        return None


def fetch_opportunities() -> list[ArbOpportunity]:
    """Fetch, normalize, and sort opportunities by pnl_velocity descending."""
    raw_entries = fetch_arb_current()
    log(f"Fetched {len(raw_entries)} raw entries from Oddpool")

    opportunities = []
    for entry in raw_entries:
        opp = normalize_opportunity(entry)
        if opp is not None:
            opportunities.append(opp)

    opportunities.sort(key=lambda o: o.pnl_velocity, reverse=True)
    log(f"Normalized {len(opportunities)} valid opportunities (sorted by pnl_velocity)")
    return opportunities
