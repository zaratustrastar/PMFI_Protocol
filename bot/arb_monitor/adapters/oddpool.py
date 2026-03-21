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
  net_edge_pct   = net_cents - slippage_guard_pct - risk_buffer_pct
  annualized_return = (1 + net_edge_pct/100)^(365 / max(days_to_expiry, 0.5)) - 1
  confidence     = logistic function of min(poly_liq, venue2_liq); 0 when net_edge <= 0
  fillable_size  = min(real_poly_book_depth @ poly_ask, ARB_FILLABLE_FRACTION × bottleneck_liq)
                   (falls back to heuristic when token unresolved or book unavailable)
  score          = annualized_return × confidence × fillable_size

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
    ARB_SLIPPAGE_GUARD_BPS, ARB_RISK_BUFFER_PCT, ARB_FILLABLE_FRACTION,
)

# ── Polymarket orderbook scorer cache ────────────────────────────────────────
# Keyed by YES token ID. Value: (poly_fillable_contracts, cached_at).
# TTL: 5 minutes — short enough to track liquidity changes between cycles,
# long enough to avoid hammering the CLOB when many opportunities resolve to
# the same token (e.g., YES and NO sides of the same market).
_BOOK_CACHE: dict[str, tuple[int, float]] = {}
_BOOK_CACHE_TTL = 300  # seconds

def log(msg: str):
    print(f"🔀 [Arb/Oddpool] {msg}")


# ── Polymarket slug → CLOB token ID cache ────────────────────────────────────
# Keyed by slug string. Value: (yes_token, no_token, cached_at) or (None, None, t).
# TTL: 60 minutes so token IDs are re-validated occasionally without hammering Gamma.
_SLUG_CACHE: dict[str, tuple[Optional[str], Optional[str], float]] = {}
_SLUG_CACHE_TTL = 3600  # seconds


def _resolve_poly_token(slug: str) -> Optional[str]:
    """Resolve a Polymarket event slug to a CLOB YES token ID.

    Uses a 60-minute in-memory cache. Returns None when the slug cannot be resolved
    (network error, not found, etc.). Failure is cached briefly (5 min) to avoid
    hammering Gamma on every cycle for a permanently missing market.
    """
    if not slug:
        return None

    now = time.time()
    cached = _SLUG_CACHE.get(slug)
    if cached is not None:
        yes_tok, _no_tok, cached_at = cached
        ttl = 300 if yes_tok is None else _SLUG_CACHE_TTL  # short TTL for failures
        if now - cached_at < ttl:
            return yes_tok  # None = previously failed (display-only until TTL expires)

    try:
        from .polymarket import lookup_token_ids_by_slug
        result = lookup_token_ids_by_slug(slug)
        if result:
            yes_tok, no_tok = result
            _SLUG_CACHE[slug] = (yes_tok, no_tok, now)
            log(f"✅ Token resolved for slug={slug!r}: {yes_tok[:16]}...")
            return yes_tok
        else:
            _SLUG_CACHE[slug] = (None, None, now)
            log(f"⚠️ Could not resolve token for slug={slug!r} — display-only until retry")
            return None
    except Exception as e:
        _SLUG_CACHE[slug] = (None, None, now)
        log(f"⚠️ Token resolution error for slug={slug!r}: {e}")
        return None


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

        # Resolve token early so the scorer (Step 4) can use it for real book depth.
        # Cached for 60 min; failures cached 5 min to avoid Gamma hammering.
        resolved_token = _resolve_poly_token(polymarket_slug)

        # ── Profit-maximising scorer ──────────────────────────────────────────
        # Step 1: net edge — subtract expected slippage and execution risk buffer.
        #   ARB_SLIPPAGE_GUARD_BPS is in bps (e.g. 50 bps = 0.5%), convert to pct.
        #   ARB_RISK_BUFFER_PCT is already in pct (e.g. 0.1 = 0.1%).
        slippage_pct = ARB_SLIPPAGE_GUARD_BPS / 100.0
        net_edge_pct = net_cents - slippage_pct - ARB_RISK_BUFFER_PCT

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

        # Step 4: fillable size — use real Polymarket ask-ladder depth when the YES
        #   token has been resolved; fall back to the heuristic fraction of reported
        #   liquidity otherwise (e.g., display-only markets whose tokens are pending).
        #
        #   Real depth walk: walk book asks at prices <= our_poly_ask to count contracts
        #   immediately available at the quoted spread.  Result is cached 5 min per token
        #   to avoid hammering the CLOB for every opportunity in the same cycle.
        #
        #   Heuristic fallback: ARB_FILLABLE_FRACTION × bottleneck_liq (same as before).
        #   Floor at $10 so tiny-but-valid markets still receive a non-zero score.
        fillable_size_usdc = max(10.0, bottleneck_liq * ARB_FILLABLE_FRACTION)  # default heuristic
        if resolved_token and our_poly_ask > 0:
            now_book = time.time()
            cached_book = _BOOK_CACHE.get(resolved_token)
            if cached_book is not None and (now_book - cached_book[1]) < _BOOK_CACHE_TTL:
                poly_contracts_fillable = cached_book[0]
            else:
                try:
                    from .polymarket import fetch_orderbook, compute_fillable_contracts
                    book = fetch_orderbook(resolved_token)
                    if book:
                        # max fill price = our quoted ask (depth at the spread, not below)
                        poly_contracts_fillable, _ = compute_fillable_contracts(book, our_poly_ask)
                        _BOOK_CACHE[resolved_token] = (poly_contracts_fillable, now_book)
                    else:
                        poly_contracts_fillable = -1  # signal: book unavailable
                except Exception as _e:
                    log(f"⚠️ Book depth fetch error for scorer (token={resolved_token[:16]}): {_e}")
                    poly_contracts_fillable = -1

            if poly_contracts_fillable >= 0:
                # Convert contracts → USDC at the quoted poly ask price
                poly_fillable_usdc = poly_contracts_fillable * our_poly_ask
                # Take the minimum of real poly depth and the heuristic so we're
                # never MORE optimistic than the reported liquidity suggests.
                fillable_size_usdc = max(10.0, min(poly_fillable_usdc, bottleneck_liq * ARB_FILLABLE_FRACTION))
                log(
                    f"📏 [Scorer] real poly depth: {poly_contracts_fillable} contracts "
                    f"= ${poly_fillable_usdc:.0f} USDC → fillable_size=${fillable_size_usdc:.0f}"
                )
            # else: book unavailable — keep heuristic computed above

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

        # resolved_token was set earlier (before scorer) — reuse it here.
        poly_yes_token = resolved_token if resolved_token else polymarket_slug
        is_display_only = resolved_token is None  # False when we have a real token ID

        log(
            f"{'✅' if not is_display_only else '👁'} pair={pair_id!r} edge={gross_edge_pct:.2f}¢ "
            f"poly={our_poly_ask:.2f} venue2({venue2}/{kalshi_side})={our_venue2_ask:.2f} "
            f"days={days_to_expiry:.1f} token={'resolved' if not is_display_only else 'slug-only'} "
            f"title={event_title[:40]!r}"
        )

        return ArbOpportunity(
            pair_id=pair_id,
            poly_yes_token=poly_yes_token,    # real CLOB token ID when resolved, slug otherwise
            poly_no_token="",
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
            is_display_only=is_display_only,  # False = real token resolved, execution allowed
            raw=entry,
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
