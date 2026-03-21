"""Oddpool adapter — fetches cross-venue arb opportunities from Oddpool /arbitrage/current API.

Actual Oddpool API (https://api.oddpool.com/arbitrage/current):
  - Response: plain JSON array (no root wrapper key)
  - Per-entry fields:
      event_id, event_title, kalshi_event_ticker, polymarket_event_slug,
      opinion_market_id, market_type, outcome_key, label,
      timestamp, resolution_time,
      kalshi:     { yes_ask, no_ask, volume, volume_24h, open_interest }
      polymarket: { yes_ask, no_ask, volume, volume_24h, liquidity }
      opinion:    { yes_ask, no_ask, volume, volume_24h, liquidity }
      buy_yes_market, buy_no_market, gross_cents, fee_cents, net_cents

Normalizes each entry into an ArbOpportunity dataclass and sorts by pnl_velocity
(= gross_edge_pct / max(days_to_expiry, 0.5)) descending to prioritise highest PnL velocity.

gross_edge_pct is stored in PERCENT units (e.g. 1.0 = 1% = 1 cent per dollar).
This matches the frontend formula: edge = gross_edge_pct / 100 → display (edge * 100)%.
"""

import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config import ODDPOOL_API_KEY, ODDPOOL_BASE_URL


def log(msg: str):
    print(f"🔀 [Arb/Oddpool] {msg}")


@dataclass
class ArbOpportunity:
    pair_id: str
    poly_yes_token: str        # polymarket_event_slug (slug, not a token address)
    poly_no_token: str         # empty — Oddpool doesn't return token addresses
    kalshi_ticker: str         # kalshi_event_ticker
    poly_yes_ask: float        # price we pay on Polymarket leg (in dollars, e.g. 0.39)
    kalshi_yes_ask: float      # price we pay on Kalshi/Opinion leg (in dollars, e.g. 0.60)
    gross_edge_pct: float      # profit in PERCENT units (e.g. 1.0 = 1% = 1¢ per dollar)
    expiry_ts: int             # Unix timestamp of resolution_time
    days_to_expiry: float
    pnl_velocity: float        # gross_edge_pct / max(days_to_expiry, 0.5)
    poly_title: str = ""       # event_title
    kalshi_title: str = ""     # label (outcome label)
    kalshi_side: str = "NO"    # which side we buy on venue2: "YES" or "NO"
    venue2: str = "kalshi"     # "kalshi" or "opinion"
    opinion_market_id: str = ""
    opinion_slug: str = ""
    # True when poly_yes_token is a slug (not a real Polymarket token address).
    # Oddpool /arbitrage/current does not return token IDs — only event slugs.
    # Execution code must check this flag before attempting to place orders.
    is_display_only: bool = True
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "poly_yes_token": self.poly_yes_token,
            "poly_no_token": self.poly_no_token,
            "kalshi_ticker": self.kalshi_ticker,
            "poly_yes_ask": self.poly_yes_ask,
            "kalshi_yes_ask": self.kalshi_yes_ask,
            "gross_edge_pct": round(self.gross_edge_pct, 4),
            "net_cents": round(self.gross_edge_pct, 2),   # alias: same value, clearer name
            "expiry_ts": self.expiry_ts,
            "days_to_expiry": round(self.days_to_expiry, 3),
            "pnl_velocity": round(self.pnl_velocity, 4),
            "poly_title": self.poly_title,
            "kalshi_title": self.kalshi_title,
            "kalshi_side": self.kalshi_side,
            "venue2": self.venue2,
            "opinion_market_id": self.opinion_market_id,
            "opinion_slug": self.opinion_slug,
            "is_display_only": self.is_display_only,
        }


def _headers() -> dict:
    headers = {"accept": "application/json"}
    if ODDPOOL_API_KEY:
        headers["X-API-Key"] = ODDPOOL_API_KEY
    return headers


_ODDPOOL_RAW_CACHE: dict = {}


def fetch_arb_current_raw() -> tuple[int, object]:
    """Fetch /arbitrage/current from Oddpool API. Returns (status_code, raw_json).

    Caches the last raw response in _ODDPOOL_RAW_CACHE so /api/arb-vault/raw
    can expose the actual Oddpool response structure without a second network call.
    """
    url = f"{ODDPOOL_BASE_URL}/arbitrage/current"
    log(f"Fetching {url}")
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        raw = None
        try:
            raw = resp.json()
        except Exception:
            raw = resp.text[:500]
        _ODDPOOL_RAW_CACHE["status"] = resp.status_code
        _ODDPOOL_RAW_CACHE["body"] = raw
        _ODDPOOL_RAW_CACHE["url"] = url
        if resp.status_code != 200:
            log(f"❌ HTTP {resp.status_code}: {str(raw)[:200]}")
        else:
            count = len(raw) if isinstance(raw, list) else "?"
            log(f"✅ HTTP 200 — {count} entries from Oddpool")
        return resp.status_code, raw
    except Exception as e:
        log(f"❌ fetch_arb_current_raw error: {e}")
        _ODDPOOL_RAW_CACHE["error"] = str(e)
        return 0, None


def fetch_arb_current() -> list[dict]:
    """Fetch /arbitrage/current from Oddpool API. Returns raw list of opportunity dicts."""
    status, data = fetch_arb_current_raw()
    if status != 200 or data is None:
        return []
    if isinstance(data, list):
        return data
    # Oddpool docs say it's a plain array — if we get a dict, log and try common keys
    if isinstance(data, dict):
        for key in ("opportunities", "data", "arb", "arbs", "results", "items"):
            if key in data and isinstance(data[key], list):
                log(f"⚠️ Got dict wrapper with key '{key}' — extracted {len(data[key])} entries")
                return data[key]
        log(f"⚠️ Unexpected dict response. Root keys: {list(data.keys())}")
        return []
    log(f"⚠️ Unexpected response type: {type(data)}")
    return []


def _parse_resolution_time(entry: dict) -> int:
    """Parse expiry timestamp from resolution_time or fallback numeric fields."""
    # Primary: ISO string resolution_time
    resolution_time = entry.get("resolution_time") or entry.get("timestamp") or ""
    if resolution_time:
        try:
            dt = datetime.fromisoformat(str(resolution_time).replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            pass
    # Fallback: numeric fields
    for field_name in ("expiry_ts", "expiry", "expires_at", "close_time", "end_time"):
        val = entry.get(field_name)
        if val:
            try:
                ts = int(float(val))
                if ts > 1_000_000_000_000:
                    ts //= 1000
                return ts
            except Exception:
                pass
    return 0


def normalize_opportunity(entry: dict) -> Optional[ArbOpportunity]:
    """Normalize a raw Oddpool /arbitrage/current entry into an ArbOpportunity.

    Actual Oddpool field structure:
      - event_id, event_title, outcome_key, label
      - kalshi_event_ticker, polymarket_event_slug, opinion_market_id
      - resolution_time
      - kalshi: { yes_ask, no_ask, ... }
      - polymarket: { yes_ask, no_ask, ... }
      - opinion: { yes_ask, no_ask, ... }
      - buy_yes_market, buy_no_market  ("polymarket"/"kalshi"/"opinion")
      - net_cents, gross_cents, fee_cents
    """
    try:
        event_id = entry.get("event_id") or ""
        outcome_key = entry.get("outcome_key") or "yes"
        pair_id = f"{event_id}_{outcome_key}"

        event_title = entry.get("event_title") or ""
        label = entry.get("label") or ""
        kalshi_ticker = entry.get("kalshi_event_ticker") or entry.get("kalshi_ticker") or ""
        polymarket_slug = entry.get("polymarket_event_slug") or entry.get("polymarket_slug") or ""
        opinion_market_id_raw = entry.get("opinion_market_id") or ""
        opinion_market_id = str(opinion_market_id_raw) if opinion_market_id_raw else ""

        # Nested price sub-objects
        poly_data = entry.get("polymarket") or {}
        kalshi_data = entry.get("kalshi") or {}
        opinion_data = entry.get("opinion") or {}

        poly_yes_ask = float(poly_data.get("yes_ask") or 0)
        poly_no_ask = float(poly_data.get("no_ask") or 0)
        kalshi_yes_ask_raw = float(kalshi_data.get("yes_ask") or 0)
        kalshi_no_ask_raw = float(kalshi_data.get("no_ask") or 0)
        opinion_yes_ask_raw = float(opinion_data.get("yes_ask") or 0)
        opinion_no_ask_raw = float(opinion_data.get("no_ask") or 0)

        buy_yes_market = (entry.get("buy_yes_market") or "").lower()
        buy_no_market = (entry.get("buy_no_market") or "").lower()

        # Determine venue2 and prices
        if "opinion" in (buy_yes_market, buy_no_market):
            venue2 = "opinion"
        elif kalshi_ticker:
            venue2 = "kalshi"
        else:
            venue2 = buy_no_market or "kalshi"

        # Map buy directions to what we actually pay on each leg
        # buy_yes_market = which venue we buy YES on
        # buy_no_market  = which venue we buy NO on
        if buy_yes_market == "polymarket":
            # We buy YES on Poly, the other side (NO or YES) on venue2
            our_poly_ask = poly_yes_ask
            poly_side_label = "YES"  # unused in dataclass but for clarity
            if venue2 == "opinion":
                our_venue2_ask = opinion_no_ask_raw if buy_no_market == "opinion" else opinion_yes_ask_raw
                kalshi_side = "NO" if buy_no_market == "opinion" else "YES"
            else:
                # venue2 == "kalshi"
                our_venue2_ask = kalshi_no_ask_raw
                kalshi_side = "NO"
        elif buy_yes_market in ("kalshi", "opinion"):
            # We buy YES on Kalshi/Opinion, NO on Poly
            our_poly_ask = poly_no_ask
            if venue2 == "opinion":
                our_venue2_ask = opinion_yes_ask_raw
            else:
                our_venue2_ask = kalshi_yes_ask_raw
            kalshi_side = "YES"
        else:
            # Fallback: unknown direction — use raw yes prices
            our_poly_ask = poly_yes_ask
            our_venue2_ask = kalshi_yes_ask_raw
            kalshi_side = "NO"

        # Edge units:
        #   Oddpool returns net_cents / gross_cents as CENTS per dollar (e.g. 1.0 = 1¢ profit).
        #   ArbOpportunity.gross_edge_pct is stored in PERCENT units (1.0 = 1%).
        #   Since 1 cent per $1 invested == 1% profit, we assign gross_edge_pct = gross_cents
        #   DIRECTLY (no division by 100).
        #
        #   Frontend verification:
        #     _oddpoolToCard: edge = gross_edge_pct / 100  (e.g. 1.0/100 = 0.01)
        #     webRenderArbCard: edgePct = (edge * 100).toFixed(1) → "1.0"  → badge shows "1.0%"
        #   Using net_cents/100 as the task description suggested would give gross_edge_pct=0.01
        #   → edge=0.0001 → display "0.0%" which is wrong.
        net_cents = float(entry.get("net_cents") or 0)
        gross_cents = float(entry.get("gross_cents") or net_cents)
        gross_edge_pct = gross_cents  # direct: 1 cent/dollar == 1% → frontend renders "1.0%"

        # If Oddpool returned 0 cents but we have prices, compute from scratch (fallback)
        if gross_edge_pct == 0 and our_poly_ask > 0 and our_venue2_ask > 0:
            gross_edge_pct = max(0.0, (1.0 - our_poly_ask - our_venue2_ask) * 100)

        expiry_ts = _parse_resolution_time(entry)
        now = time.time()
        days_to_expiry = max(0.0, (expiry_ts - now) / 86400) if expiry_ts > now else 0.0
        pnl_velocity = gross_edge_pct / max(days_to_expiry, 0.5)

        log(
            f"✅ pair={pair_id!r} edge={gross_edge_pct:.2f}¢ "
            f"poly={our_poly_ask:.2f} venue2({venue2}/{kalshi_side})={our_venue2_ask:.2f} "
            f"days={days_to_expiry:.1f} title={event_title[:50]!r}"
        )

        return ArbOpportunity(
            pair_id=pair_id,
            poly_yes_token=polymarket_slug,   # slug used as identifier; no token address from Oddpool
            poly_no_token="",
            kalshi_ticker=kalshi_ticker,
            poly_yes_ask=our_poly_ask,
            kalshi_yes_ask=our_venue2_ask,
            gross_edge_pct=gross_edge_pct,
            expiry_ts=expiry_ts,
            days_to_expiry=days_to_expiry,
            pnl_velocity=pnl_velocity,
            poly_title=event_title,
            kalshi_title=label or event_title,
            kalshi_side=kalshi_side,
            venue2=venue2,
            opinion_market_id=opinion_market_id,
            opinion_slug=opinion_market_id,   # use id as slug if no separate slug field
            is_display_only=True,             # Oddpool doesn't return Poly token IDs
            raw=entry,
        )
    except Exception as e:
        log(f"❌ normalize_opportunity error: {e}, entry keys={list(entry.keys())}")
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
    log(f"Normalized {len(opportunities)}/{len(raw_entries)} valid opportunities (sorted by pnl_velocity)")
    return opportunities
