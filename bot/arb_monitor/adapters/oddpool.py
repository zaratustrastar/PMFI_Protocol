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

Scoring (profit-maximising):
  net_edge_pct   = net_cents - risk_buffer_pct
                   (net_cents is already fee-adjusted by Oddpool; ARB_SLIPPAGE_GUARD_BPS
                    is an execution-time staleness check only, not a scoring cost)
  annualized_return = (1 + net_edge_pct/100)^(365 / max(days_to_expiry, 0.5)) - 1
  confidence     = logistic function of min(poly_liq, venue2_liq); 0 when net_edge <= 0
  fillable_size  = CLOB ask-ladder walk (max_price = 1 - venue2_ask - ARB_MIN_EDGE_PCT) when
                   price-validated token IDs are available; else Oddpool liquidity proxy
  score          = annualized_return × confidence × fillable_size  (0 when CLOB depth = 0)

Opportunities are sorted by score descending. Deployment caps (ARB_MAX_PAIR_USDC etc.)
are applied at execution time; fillable_size intentionally has no artificial cap here.

gross_edge_pct is stored in PERCENT units (e.g. 1.0 = 1% = 1 cent per dollar).
This matches the frontend formula: edge = gross_edge_pct / 100 → display (edge * 100)%.
"""

import math
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from ..config import (
    ODDPOOL_API_KEY, ODDPOOL_BASE_URL,
    ARB_RISK_BUFFER_PCT, ARB_FILLABLE_FRACTION,
    ARB_MIN_EDGE_PCT,
)

def log(msg: str):
    print(f"🔀 [Arb/Oddpool] {msg}")


# ── Polymarket slug → CLOB token ID cache ────────────────────────────────────
# Keyed by (slug, label_normalized). Value: (yes_token, no_token, cached_at).
# Using a (slug, label) composite key so different outcomes of a multi-outcome event
# (e.g. NBA Champion 2026 → Houston, Lakers) each resolve to their own token pair.
# TTL: 60 minutes so token IDs are re-validated occasionally without hammering Gamma.
_SLUG_CACHE: dict[tuple[str, str], tuple[Optional[str], Optional[str], float]] = {}
_SLUG_CACHE_TTL = 3600  # seconds


def _slug_cache_key(slug: str, label: str) -> tuple[str, str]:
    """Normalise cache key from slug + label."""
    return (slug, label.lower().replace("_", " ").replace("-", " ").strip())


def _fetch_ask_price(token_id: str) -> Optional[float]:
    """Fetch the best ask price for a Polymarket CLOB token.

    Used to validate token-to-outcome assignment at resolution time.

    Asks are the only real, actionable buy liquidity — someone has actively posted
    shares for sale at this price.  Oddpool's own price quotes come from the same
    CLOB asks, so comparing ask-to-ask gives the most direct validation signal.

    Wrong-market tokens (near-settled markets) show stale high asks (~99¢), which
    creates a large delta vs Oddpool's live quote — exactly what we want to detect.
    Returns None if the orderbook fetch fails or the token is empty.
    """
    if not token_id:
        return None
    try:
        from .polymarket import get_best_prices
        prices = get_best_prices(token_id)
        return prices.get("best_ask")
    except Exception as e:
        log(f"⚠️ _fetch_ask_price({token_id[:16]}...): {e}")
        return None


def _resolve_poly_tokens(
    slug: str,
    label: str = "",
    expected_poly_ask: Optional[float] = None,
    buying_poly_no: bool = False,
    resolution_ts: Optional[int] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a Polymarket event slug to a (YES token, NO token) pair.

    Uses the label to pick the right market within multi-outcome events (e.g. NBA
    Champion — Houston vs. LA Lakers). Cache key is (slug, normalised_label) so each
    team/candidate gets its own entry.

    `resolution_ts` (Oddpool's resolution_time as Unix timestamp) is forwarded to
    lookup_token_ids_by_slug → _pick_best_market to apply temporal scoring: the
    sub-market whose endDate is closest to resolution_ts wins ties and gets a
    positive bonus, while sub-markets from prior periods (endDate > 30d earlier)
    are hard-skipped. This disambiguates recurrent slugs with multiple editions.

    When expected_poly_ask is provided, the resolved tokens are price-validated:
    - Fetch best_ask for both YES and NO tokens from the Poly CLOB.
    - `buying_poly_no` specifies which token we actually buy on Poly:
        False → buying YES  (expected_poly_ask ≈ YES ask)
        True  → buying NO   (expected_poly_ask ≈ NO  ask)
    - Accept tokens if the "buying" token's ask is closest AND within 30¢.
    - Swap YES/NO order if the "other" token's ask is closer (Gamma inverted order).
    - Cache as failure (5-min TTL) if neither token is within 30¢ — Gamma resolved
      the wrong market entirely (e.g. wrong sub-market in multi-outcome events).

    Returns (None, None) on failure (display-only, retry on next cycle).
    """
    if not slug:
        return (None, None)

    key = _slug_cache_key(slug, label)
    now = time.time()
    cached = _SLUG_CACHE.get(key)
    if cached is not None:
        yes_tok, no_tok, cached_at = cached
        ttl = 300 if yes_tok is None else _SLUG_CACHE_TTL
        if now - cached_at < ttl:
            return (yes_tok, no_tok)

    try:
        from .polymarket import lookup_token_ids_by_slug
        result = lookup_token_ids_by_slug(slug, label=label, resolution_ts=resolution_ts)
        if result:
            yes_tok, no_tok = result

            # ── Price-validate resolved tokens ─────────────────────────────
            # Gamma sometimes maps a slug to the wrong sub-market (e.g. "will BTC
            # exceed $90k?" resolves to the settled "exceeded $85k?" market instead).
            # Both wrong-market tokens then show stale high asks (~99¢), and the
            # slippage check fires every cycle so no trades ever execute.
            #
            # Strategy: fetch best_ask for both tokens and compare against Oddpool's
            # quoted price (our_poly_ask — itself a CLOB ask).  Ask-to-ask comparison
            # is the most direct validation: asks are real, buyable liquidity.
            # `buying_poly_no` tells us which token we're buying:
            #   False → YES token ask should ≈ expected_poly_ask
            #   True  → NO  token ask should ≈ expected_poly_ask
            #
            # Tolerance 0.30 (30¢): wide enough for normal spread + minor price
            # movement since Oddpool priced the opportunity; tight enough to reject
            # wrong-market tokens that differ by 50–90¢.
            if expected_poly_ask is not None:
                _TOLERANCE = 0.30
                yes_ask = _fetch_ask_price(yes_tok)
                no_ask  = _fetch_ask_price(no_tok)
                yes_delta = abs(yes_ask - expected_poly_ask) if yes_ask is not None else 999.0
                no_delta  = abs(no_ask  - expected_poly_ask) if no_ask  is not None else 999.0

                # "buying" token is the one we expect to match expected_poly_ask.
                # "other" token should NOT match — if it does, Gamma inverted the order.
                if not buying_poly_no:
                    buying_label, buying_delta, buying_ask_val = "YES", yes_delta, yes_ask
                    other_label,  other_delta,  other_ask_val  = "NO",  no_delta,  no_ask
                else:
                    buying_label, buying_delta, buying_ask_val = "NO",  no_delta,  no_ask
                    other_label,  other_delta,  other_ask_val  = "YES", yes_delta, yes_ask

                if buying_delta <= other_delta and buying_delta < _TOLERANCE:
                    log(
                        f"✅ Token ask validated ({buying_label} correct): "
                        f"{buying_label} ask={buying_ask_val:.4f} Δ={buying_delta:.4f} | "
                        f"{other_label} ask={other_ask_val} Δ={other_delta:.4f} | "
                        f"expected={expected_poly_ask:.4f} slug={slug!r}"
                    )
                    # correct order — proceed
                elif other_delta < buying_delta and other_delta < _TOLERANCE:
                    log(
                        f"🔄 Token YES/NO order corrected: "
                        f"{other_label} ask={other_ask_val:.4f} Δ={other_delta:.4f} closer than "
                        f"{buying_label} ask={buying_ask_val} Δ={buying_delta:.4f} → swapping | "
                        f"expected={expected_poly_ask:.4f} slug={slug!r}"
                    )
                    yes_tok, no_tok = no_tok, yes_tok
                else:
                    log(
                        f"⚠️ Token ask mismatch — wrong market for slug={slug!r}: "
                        f"YES ask={yes_ask} (Δ={yes_delta:.4f}) NO ask={no_ask} (Δ={no_delta:.4f}) "
                        f"buying={buying_label} expected={expected_poly_ask:.4f} "
                        f"— neither within {_TOLERANCE:.2f} → display-only (5-min retry)"
                    )
                    _SLUG_CACHE[key] = (None, None, now)
                    return (None, None)

            _SLUG_CACHE[key] = (yes_tok, no_tok, now)
            log(
                f"✅ Tokens resolved slug={slug!r} label={label!r}: "
                f"YES={yes_tok[:16]}... NO={no_tok[:16]}..."
            )
            return (yes_tok, no_tok)
        else:
            _SLUG_CACHE[key] = (None, None, now)
            log(f"⚠️ Could not resolve tokens for slug={slug!r} label={label!r} — display-only")
            return (None, None)
    except Exception as e:
        _SLUG_CACHE[key] = (None, None, now)
        log(f"⚠️ Token resolution error for slug={slug!r} label={label!r}: {e}")
        return (None, None)


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
    pnl_velocity: float        # gross_edge_pct / max(days_to_expiry, 0.5) [legacy, kept for display]

    # ── Profit-maximising scorer fields ──────────────────────────────────────
    # net_edge_pct: gross edge minus expected slippage and risk buffer (in %)
    net_edge_pct: float = 0.0
    # annualized_return: (1 + net_edge/100)^(365/days) - 1; captures compounding
    annualized_return: float = 0.0
    # confidence: [0.0, 1.0] — liquidity-weighted fill probability; 0.0 if net_edge <= 0
    confidence: float = 0.0
    # fillable_size_usdc: conservative estimate of deployable capital (BEFORE deployment caps)
    fillable_size_usdc: float = 0.0
    # score: annualized_return × confidence × fillable_size — primary ranking signal
    score: float = 0.0
    # ─────────────────────────────────────────────────────────────────────────

    poly_title: str = ""       # event_title
    kalshi_title: str = ""     # label (outcome label)
    kalshi_side: str = "NO"    # which side we buy on venue2: "YES" or "NO"
    venue2: str = "kalshi"     # "kalshi" or "opinion"
    opinion_market_id: str = ""
    opinion_slug: str = ""
    # outcome_key: "yes" or "no" from Oddpool — which outcome this arb pair covers.
    # Stored here so the executor can pass it to the Kalshi event→market resolver.
    outcome_key: str = "yes"
    # True when poly_yes_token is a slug (not a real Polymarket token address).
    # Oddpool /arbitrage/current does not return token IDs — only event slugs.
    # Execution code must check this flag before attempting to place orders.
    is_display_only: bool = True
    raw: dict = field(default_factory=dict)
    # Unix timestamp when this opportunity was fetched from Oddpool. Used by the
    # executor to enforce a freshness guard when falling back to Oddpool prices.
    fetched_at: float = 0.0

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
            # scorer fields
            "net_edge_pct": round(self.net_edge_pct, 4),
            "annualized_return": round(self.annualized_return, 4),
            "confidence": round(self.confidence, 4),
            "fillable_size_usdc": round(self.fillable_size_usdc, 2),
            "score": round(self.score, 4),
            # metadata
            "poly_title": self.poly_title,
            "kalshi_title": self.kalshi_title,
            "kalshi_side": self.kalshi_side,
            "venue2": self.venue2,
            "opinion_market_id": self.opinion_market_id,
            "opinion_slug": self.opinion_slug,
            "outcome_key": self.outcome_key,
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

        # Resolve both YES and NO token IDs early so the scorer (Step 4) can use them
        # for real book depth. Uses label (e.g. "Houston Rockets") or outcome_key (e.g. "hou")
        # to pick the right market within multi-outcome events (NBA Champion, elections, etc.).
        # Prefers label; falls back to outcome_key when label is empty.
        # Cached 60 min; failures 5 min to avoid hammering Gamma.
        # Pass expected_poly_ask so _resolve_poly_tokens can price-validate the resolved tokens
        # against Oddpool's quote — catching wrong-market resolutions (both tokens ~99¢).
        match_hint = label or outcome_key
        # buying_poly_no: True when the Poly leg buys NO (buy_yes_market is venue2)
        _buying_poly_no = buy_yes_market != "polymarket"
        resolved_yes_token, resolved_no_token = _resolve_poly_tokens(
            polymarket_slug,
            match_hint,
            expected_poly_ask=our_poly_ask if our_poly_ask > 0 else None,
            buying_poly_no=_buying_poly_no,
            resolution_ts=expiry_ts if expiry_ts > 0 else None,
        )

        # ── Profit-maximising scorer ──────────────────────────────────────────
        # Step 1: net edge.
        #   Oddpool's net_cents is already fee-adjusted (they apply their own cost
        #   model before returning it).  We only subtract our own small safety buffer
        #   (ARB_RISK_BUFFER_PCT, default 0.1%) to absorb residual execution risk
        #   (partial fills, minor spread widening, etc.).
        #
        #   ARB_SLIPPAGE_GUARD_BPS is an EXECUTION-TIME guard only — it aborts a
        #   trade when the live CLOB price has moved more than N bps against us since
        #   Oddpool quoted it (staleness check).  It is NOT an expected execution cost
        #   and must not be subtracted here.  Doing so (200 bps = 2%) would drive
        #   net_edge_pct negative for every opportunity in the feed, zeroing all scores
        #   and preventing any trades from executing.
        net_edge_pct = net_cents - ARB_RISK_BUFFER_PCT

        # Step 2: annualized return — converts absolute edge into an annual rate,
        #   correctly handling compounding (short-dated trades compound faster).
        #   Use max(days_to_expiry, 0.5) so markets closing in hours still score.
        if net_edge_pct > 0:
            net_frac = net_edge_pct / 100.0
            effective_days = max(days_to_expiry, 0.5)
            annualized_return = (1.0 + net_frac) ** (365.0 / effective_days) - 1.0
        else:
            annualized_return = 0.0

        # Step 3: liquidity-based confidence.
        #   Uses the thinner of the two legs (bottleneck) as the limiting factor.
        #   Logistic: $0 → 0.05, $5k → 0.50, $20k → 0.80, $100k → 0.95.
        #   Zero confidence when net_edge is non-positive.
        poly_liq = float(poly_data.get("liquidity") or poly_data.get("volume") or 0)
        if venue2 == "opinion":
            venue2_liq = float(opinion_data.get("liquidity") or opinion_data.get("volume") or 0)
        else:
            # Kalshi doesn't always report liquidity; use open_interest then volume as proxies
            venue2_liq = float(
                kalshi_data.get("open_interest")
                or kalshi_data.get("liquidity")
                or kalshi_data.get("volume")
                or 0
            )
        bottleneck_liq = min(poly_liq, venue2_liq) if venue2_liq > 0 else poly_liq
        if net_edge_pct <= 0 or bottleneck_liq <= 0:
            confidence = 0.0
        else:
            # k = 5000 → half-confidence at $5k liquidity; tuned for prediction markets
            confidence = max(0.05, min(1.0, bottleneck_liq / (bottleneck_liq + 5000.0)))

        # Step 4: fillable size — walk the real Polymarket CLOB ask ladder when we have
        #   price-validated token IDs (the price validation in _resolve_poly_tokens ensures
        #   they point to the correct market).  This replaces the Oddpool liquidity proxy
        #   that was used when token IDs were frequently wrong (returning 0 depth).
        #
        #   max_poly_price: highest price we can pay on the Poly leg and still keep at
        #   least ARB_MIN_EDGE_PCT edge.  Walk asks from lowest upward; stop when price
        #   exceeds this ceiling.  fillable_contracts × our_poly_ask gives USDC cost.
        #
        #   Fallback hierarchy (most to least preferred):
        #     1. Real CLOB depth  (validated token IDs available)
        #     2. Oddpool liquidity proxy  (token IDs unavailable or book fetch failed)
        #
        #   Score = 0 when real depth returns 0 contracts — this market never reaches
        #   the executor, saving unnecessary execution-time API calls.
        if resolved_yes_token and our_poly_ask > 0 and our_venue2_ask > 0:
            try:
                from .polymarket import fetch_orderbook, compute_fillable_contracts
                # Which token do we actually BUY on Poly?
                #   buy_yes_market=="polymarket" → buying YES on Poly → use yes_token
                #   buy_yes_market in ("kalshi","opinion") → buying NO on Poly → use no_token
                depth_token = resolved_yes_token if buy_yes_market == "polymarket" else resolved_no_token
                if depth_token:
                    max_poly_price = max(0.0, 1.0 - our_venue2_ask - ARB_MIN_EDGE_PCT)
                    book = fetch_orderbook(depth_token)
                    if book:
                        poly_fillable, poly_usdc = compute_fillable_contracts(book, max_poly_price)
                        if poly_fillable > 0:
                            # Use actual USDC cost from the ask-ladder walk rather than
                            # fillable_contracts × our_poly_ask: the walk accounts for
                            # varying prices across levels, giving a more accurate total.
                            fillable_size_usdc = poly_usdc
                            log(
                                f"📏 CLOB depth pre-filter: {poly_fillable} contracts at "
                                f"≤{max_poly_price:.4f} → ${fillable_size_usdc:.2f} USDC fillable"
                            )
                        else:
                            fillable_size_usdc = 0.0
                            log(
                                f"📏 CLOB depth pre-filter: 0 contracts at ≤{max_poly_price:.4f} "
                                f"— no profitable depth, score → 0"
                            )
                    else:
                        fillable_size_usdc = max(10.0, bottleneck_liq * ARB_FILLABLE_FRACTION)
                        log(
                            f"⚠️ CLOB fetch failed for depth pre-filter — "
                            f"using Oddpool proxy ${fillable_size_usdc:.0f}"
                        )
                else:
                    fillable_size_usdc = max(10.0, bottleneck_liq * ARB_FILLABLE_FRACTION)
            except Exception as e:
                log(f"⚠️ CLOB depth pre-filter error: {e} — using Oddpool liquidity proxy")
                fillable_size_usdc = max(10.0, bottleneck_liq * ARB_FILLABLE_FRACTION)
        else:
            # Token IDs not yet resolved (display-only) — use Oddpool proxy so the
            # opportunity still gets scored and displayed while resolution retries.
            fillable_size_usdc = max(10.0, bottleneck_liq * ARB_FILLABLE_FRACTION)

        # Step 5: composite score — natural language: "expected annualised dollar edge"
        #   Ties together quality (annualized_return), reliability (confidence), and
        #   capacity (fillable_size). Greedy sort on this is globally near-optimal
        #   because capital units with the highest score-per-dollar come first.
        score = annualized_return * confidence * fillable_size_usdc

        log(
            f"📊 Scored pair={pair_id!r}: "
            f"net_edge={net_edge_pct:.2f}% AR={annualized_return:.2f}× "
            f"conf={confidence:.2f} fill={fillable_size_usdc:.0f} score={score:.1f} "
            f"| gross={gross_edge_pct:.2f}¢ days={days_to_expiry:.1f}"
        )
        # ─────────────────────────────────────────────────────────────────────

        # resolved_yes_token / resolved_no_token were set earlier (before scorer).
        poly_yes_token = resolved_yes_token if resolved_yes_token else polymarket_slug
        poly_no_token = resolved_no_token or ""
        is_display_only = resolved_yes_token is None  # False when we have real token IDs

        log(
            f"{'✅' if not is_display_only else '👁'} pair={pair_id!r} edge={gross_edge_pct:.2f}¢ "
            f"poly={our_poly_ask:.2f} venue2({venue2}/{kalshi_side})={our_venue2_ask:.2f} "
            f"days={days_to_expiry:.1f} token={'resolved' if not is_display_only else 'slug-only'} "
            f"no_token={'set' if poly_no_token else 'missing'} "
            f"title={event_title[:40]!r}"
        )

        return ArbOpportunity(
            pair_id=pair_id,
            poly_yes_token=poly_yes_token,    # real CLOB YES token ID when resolved, slug otherwise
            poly_no_token=poly_no_token,       # real CLOB NO token ID (empty if not resolved)
            kalshi_ticker=kalshi_ticker,
            poly_yes_ask=our_poly_ask,
            kalshi_yes_ask=our_venue2_ask,
            gross_edge_pct=gross_edge_pct,
            expiry_ts=expiry_ts,
            days_to_expiry=days_to_expiry,
            pnl_velocity=pnl_velocity,
            net_edge_pct=net_edge_pct,
            annualized_return=annualized_return,
            confidence=confidence,
            fillable_size_usdc=fillable_size_usdc,
            score=score,
            poly_title=event_title,
            kalshi_title=label or event_title,
            kalshi_side=kalshi_side,
            venue2=venue2,
            opinion_market_id=opinion_market_id,
            opinion_slug=opinion_market_id,
            outcome_key=outcome_key,
            is_display_only=is_display_only,  # False = real token resolved, execution allowed
            raw=entry,
            fetched_at=time.time(),
        )
    except Exception as e:
        log(f"❌ normalize_opportunity error: {e}, entry keys={list(entry.keys())}")
        return None


def fetch_opportunities() -> list[ArbOpportunity]:
    """Fetch, normalize, and sort opportunities by score descending.

    Score = annualized_return × confidence × fillable_size_usdc
    This ranking maximises expected risk-adjusted annualised dollar return
    and naturally deprioritises thin/negative-edge and illiquid opportunities
    without requiring manual threshold tuning for each dimension separately.
    """
    raw_entries = fetch_arb_current()
    log(f"Fetched {len(raw_entries)} raw entries from Oddpool")

    opportunities = []
    for entry in raw_entries:
        opp = normalize_opportunity(entry)
        if opp is not None:
            opportunities.append(opp)

    opportunities.sort(key=lambda o: o.score, reverse=True)
    log(
        f"Normalized {len(opportunities)}/{len(raw_entries)} valid opportunities "
        f"(sorted by score = annualized_return × confidence × fillable_size)"
    )
    if opportunities:
        top = opportunities[0]
        log(
            f"🏆 Top opportunity: pair={top.pair_id!r} "
            f"score={top.score:.1f} AR={top.annualized_return:.2f}× "
            f"net_edge={top.net_edge_pct:.2f}% conf={top.confidence:.2f} "
            f"fill=${top.fillable_size_usdc:.0f} days={top.days_to_expiry:.1f}"
        )
    return opportunities
