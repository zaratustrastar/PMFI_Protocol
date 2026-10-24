"""Prediction.com adapter: cross-venue arbitrage signals → the engine's existing ArbOpportunity.

Official API: https://prediction.com/api/docs  (base https://prediction.com/api/v2, header X-API-Key)
  GET /v2/arb?min_roi=&platforms=polymarket,kalshi&limit=   (Dev plan and up)
    {as_of, delay_seconds, count, opportunities: [{
        group_id, group_title, event_date, event_type, roi_pct (true percent, net of entry fees),
        total_cost, max_wager_usd, detected_at,
        legs: [{side "yes"|"no", platform, market_id, source_url, price, liquidity_usd,
                fee_usd (ONE contract at the quoted price)}, ...]}]}

What this adapter trusts from the provider: only WHICH two markets form a pair, the direction and
a size hint (max_wager_usd). Everything that decides money is recomputed here and again by the
executor from the venues' own live order books:
  - Polymarket `market_id` is a numeric Gamma market id. It is resolved with Gamma
    /markets/{id} (clobTokenIds + outcomes → YES/NO token ids, conditionId, endDate, live flags),
    the slug in source_url must match Gamma's, and the token we would buy is price-checked
    against the CLOB's live best ask (ARB_PREDICTION_PRICE_TOLERANCE, default 0.10).
  - Kalshi `market_id` is a market-level ticker (the executor's resolve_market_ticker fast path).
    The market is read from Kalshi (status must be active, close_time cross-checked against
    Polymarket's endDate: ARB_PREDICTION_MAX_EXPIRY_GAP_DAYS, default 7).
  - Prices: v2 price endpoints use cents (0-100) while /v2/arb legs use 0-1; both are accepted and
    normalised to dollars. Null (no resting order) is never treated as 0: the pair is skipped.
  - Net edge is computed at the actual size (min(max_wager_usd, ARB_MAX_PAIR_USDC)) with each
    venue's taker fee formula (core/venue_fees.py), using the worse of provider and live prices.
    The provider's headline roi_pct is kept in `raw` for comparison only.
  - Staleness: the signal time is as_of − delay_seconds; signals older than
    ARB_STALE_QUOTE_SECONDS are dropped (the execution loop applies the same limit via fetched_at).
    A body without as_of, or whose signal time is already too old, is dropped whole before any venue
    lookup.

Quota (API plans meter requests per second, per month and, for /v2/arb, per month separately):
  - at most one /v2/arb request per PREDICTION_COM_MIN_POLL_SECONDS (default 30); in between the last
    body is reused (and still passes the freshness check above);
  - from the X-RateLimit-Remaining-{Second,Month,Arb-Month} headers the next request is spaced so the
    remaining monthly calls last until the month ends (UTC), keeping PREDICTION_COM_QUOTA_RESERVE
    calls (default 50) for manual checks; 0 remaining per second waits one second;
  - a 429 waits Retry-After (seconds or an HTTP date; 60 s if absent) before the next request.
QUOTA exposes the pacing state for monitoring.

Scoring is the Oddpool scorer unchanged: score = net_cents/100 × deployable × time_factor.
Nothing here places orders or touches custody; execution stays in core/arb_executor.py.
"""

import concurrent.futures as _cf
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from ..config import (
    ARB_MAX_PAIR_USDC, ARB_RISK_BUFFER_PCT, KALSHI_BASE_URL, POLY_GAMMA_URL,
)
from .. import http_client
from .oddpool import ArbOpportunity


def log(msg: str):
    print(f"🔮 [Arb/Prediction.com] {msg}")


def base_url() -> str:
    return os.environ.get("PREDICTION_COM_BASE_URL", "https://prediction.com/api/v2").rstrip("/")


def api_key() -> str:
    return os.environ.get("PREDICTION_COM_API_KEY", "")


def _f(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


# Last rate-limit headers seen (X-RateLimit-*): exposed for monitoring and the connectivity check.
RATE_LIMITS: dict = {}
# Pacing state: next_allowed (epoch s), why, and the last 200 body with the time it was fetched.
QUOTA: dict = {"next_allowed": 0.0, "why": "", "body": None, "body_at": 0.0}
_QUOTA_LOCK = threading.Lock()
STATS: dict = {}
_STATS_LOCK = threading.Lock()


def _count(reason: str) -> None:
    with _STATS_LOCK:
        STATS[reason] = STATS.get(reason, 0) + 1


def _price(v) -> Optional[float]:
    """Dollars in (0, 1). Accepts 0-1 or cents (0-100). None / 0 / out of range → None."""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x > 1.0:
        x /= 100.0
    if not (0.0 < x < 1.0):
        return None
    return round(x, 6)


def _ts(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e12 else 1.0)
    s = str(v).strip()
    try:
        if len(s) == 10 and s[4] == "-":  # date only: end of that day UTC
            return datetime.fromisoformat(s).replace(hour=23, minute=59, tzinfo=timezone.utc).timestamp()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _slug_from_url(url: str) -> str:
    m = re.search(r"polymarket\.com/(?:[a-z]{2}/)?(?:market|event)/([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


def _time_factor(days: float) -> Optional[float]:
    """oddpool.normalize_opportunity's tiers; None = skip (< 1 day)."""
    if days < 1:
        return None
    if days < 7:
        return 0.2
    if days <= 90:
        return 1.0
    if days <= 180:
        return 0.8
    return 0.5


# ── Provider ────────────────────────────────────────────────────────────────────

def _header(headers, name: str) -> Optional[float]:
    for k, v in headers.items():
        if k.lower() == name.lower():
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _retry_after(headers, now: float) -> float:
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if raw is None:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            return max(0.0, parsedate_to_datetime(raw).timestamp() - now)
        except (TypeError, ValueError):
            return 60.0


def _seconds_to_month_end(now: float) -> float:
    d = datetime.fromtimestamp(now, tz=timezone.utc)
    nxt = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc)
    return max(1.0, nxt.timestamp() - now)


def next_request_delay(headers, now: float) -> tuple[float, str]:
    """Seconds until the next /v2/arb request may be sent, from the plan's rate-limit headers."""
    delay, why = _f("PREDICTION_COM_MIN_POLL_SECONDS", "30"), "min poll interval"
    reserve = _f("PREDICTION_COM_QUOTA_RESERVE", "50")
    month_left = _seconds_to_month_end(now)
    for name in ("X-RateLimit-Remaining-Arb-Month", "X-RateLimit-Remaining-Month"):
        rem = _header(headers, name)
        if rem is None:
            continue
        usable = rem - reserve
        d = month_left if usable <= 0 else month_left / usable
        if d > delay:
            delay, why = d, f"{name}={int(rem)} (reserve {int(reserve)}) spread to month end"
    if _header(headers, "X-RateLimit-Remaining-Second") == 0 and delay < 1:
        delay, why = 1.0, "per-second limit"
    return delay, why


def fetch_arb_raw(session=None) -> dict:
    """GET /v2/arb, paced to the plan's quota. Returns the JSON body (the last one while paced); {} on
    429 / missing key (logged, never raises for those)."""
    key = api_key()
    if not key:
        log("⚠️ PREDICTION_COM_API_KEY not set — no Prediction.com opportunities")
        return {}
    now = time.time()
    with _QUOTA_LOCK:
        if now < QUOTA["next_allowed"]:
            log(f"⏳ quota pacing: next /arb request in {QUOTA['next_allowed'] - now:.0f}s ({QUOTA['why']})")
            return QUOTA["body"] or {}
    params = {
        "platforms": os.environ.get("PREDICTION_COM_PLATFORMS", "polymarket,kalshi"),
        "limit": int(os.environ.get("PREDICTION_COM_LIMIT", "100")),
        "min_roi": _f("PREDICTION_COM_MIN_ROI", "0"),
    }
    resp = (session or requests).get(
        f"{base_url()}/arb", params=params,
        headers={"X-API-Key": key, "accept": "application/json"}, timeout=20,
    )
    RATE_LIMITS.clear()
    RATE_LIMITS.update({k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")})
    if resp.status_code == 429:
        wait = _retry_after(resp.headers, now)
        with _QUOTA_LOCK:
            QUOTA.update(next_allowed=now + wait, why="429 Retry-After", body=None, body_at=0.0)
        log(f"⏳ rate limited (429), waiting {wait:.0f}s; limits={RATE_LIMITS}")
        return {}
    delay, why = next_request_delay(resp.headers, now)
    with _QUOTA_LOCK:
        QUOTA.update(next_allowed=now + delay, why=why)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Prediction.com /arb refused ({resp.status_code}): check the API key and plan (Dev+)")
    resp.raise_for_status()
    body = resp.json()
    with _QUOTA_LOCK:
        QUOTA.update(body=body, body_at=now)
    return body


# ── Venue lookups (cached) ──────────────────────────────────────────────────────

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600


def _cached(key, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[1] < _CACHE_TTL:
            return hit[0]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (val, now)
    return val


def gamma_market(market_id: str) -> Optional[dict]:
    def load():
        resp = http_client.get(f"{POLY_GAMMA_URL}/markets/{market_id}", venue="gamma", timeout=10)
        return resp.json() if resp is not None else None
    return _cached(("gamma", market_id), load)


def kalshi_market(ticker: str) -> Optional[dict]:
    def load():
        url = f"{KALSHI_BASE_URL}/markets/{ticker}"
        headers = {"accept": "application/json"}
        try:
            from ..core.kalshi_auth import get_kalshi_headers, kalshi_auth_available
            if kalshi_auth_available():
                headers.update(get_kalshi_headers("GET", url) or {})
        except Exception:
            pass
        resp = http_client.get(url, venue="kalshi", headers=headers, timeout=10, bypass_proxy=True)
        if resp is None:
            return None
        body = resp.json()
        return body.get("market") if isinstance(body, dict) else None
    return _cached(("kalshi", ticker), load)


def poly_best_ask(token_id: str) -> Optional[float]:
    from .polymarket import get_best_prices
    return get_best_prices(token_id).get("best_ask")


def kalshi_ask(market: dict, side: str) -> Optional[float]:
    """Best ask for YES/NO from a Kalshi market object: `<side>_ask_dollars` (current API), else the
    legacy cents field. Missing / zero → None."""
    s = side.lower()
    return _price(market.get(f"{s}_ask_dollars")) or _price(market.get(f"{s}_ask"))


def _poly_tokens(m: dict) -> tuple[str, str]:
    from .polymarket import _extract_yes_no_ordered, _parse_clob_token_ids
    ids = _parse_clob_token_ids(m.get("clobTokenIds"))
    if len(ids) != 2:
        return "", ""
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except ValueError:
            outcomes = None
    if not (isinstance(outcomes, list) and sorted(str(o).strip().upper() for o in outcomes) == ["NO", "YES"]):
        return "", ""  # not a binary Yes/No market: direction would be ambiguous
    yes, no = _extract_yes_no_ordered(ids, m)
    return yes, no


# ── Normalisation ───────────────────────────────────────────────────────────────

def normalize(entry: dict, signal_ts: float, now: Optional[float] = None) -> tuple[Optional[ArbOpportunity], str]:
    """One /v2/arb opportunity → (ArbOpportunity, "ok") or (None | display-only opp, reason)."""
    now = now or time.time()
    legs = entry.get("legs") or []
    poly = [l for l in legs if str(l.get("platform", "")).lower() == "polymarket"]
    kal = [l for l in legs if str(l.get("platform", "")).lower() == "kalshi"]
    if len(legs) != 2 or len(poly) != 1 or len(kal) != 1:
        return None, "unsupported_platforms"
    poly, kal = poly[0], kal[0]
    poly_side, kal_side = str(poly.get("side", "")).lower(), str(kal.get("side", "")).lower()
    if {poly_side, kal_side} != {"yes", "no"}:
        return None, "sides_not_complementary"
    p_quote, k_quote = _price(poly.get("price")), _price(kal.get("price"))
    if p_quote is None or k_quote is None:
        return None, "null_price"
    max_age = _f("ARB_STALE_QUOTE_SECONDS", "120")
    if now - signal_ts > max_age:
        return None, "stale_signal"

    # Polymarket: Gamma id → tokens, condition, expiry; slug must match the provider's link.
    pm_id = str(poly.get("market_id") or "")
    m = gamma_market(pm_id) if pm_id.isdigit() else None
    if not m:
        return None, "poly_market_unresolved"
    url_slug = _slug_from_url(poly.get("source_url", ""))
    if url_slug and url_slug != str(m.get("slug", "")).lower():
        return None, "poly_slug_mismatch"
    if m.get("closed") or m.get("active") is False or m.get("acceptingOrders") is False or m.get("enableOrderBook") is False:
        return None, "poly_market_not_live"
    yes_tok, no_tok = _poly_tokens(m)
    if not yes_tok or not no_tok:
        return None, "poly_tokens_unresolved"
    buy_tok = yes_tok if poly_side == "yes" else no_tok

    # Kalshi: market-level ticker, must be active.
    ticker = str(kal.get("market_id") or "")
    km = kalshi_market(ticker) if ticker else None
    if not km:
        return None, "kalshi_market_unresolved"
    if str(km.get("status", "")).lower() not in ("active", "open"):
        return None, "kalshi_market_not_active"

    # Same event? Resolution dates must agree.
    p_end, k_end = _ts(m.get("endDate")), _ts(km.get("close_time") or km.get("expiration_time"))
    gap_days = _f("ARB_PREDICTION_MAX_EXPIRY_GAP_DAYS", "7")
    if p_end and k_end and abs(p_end - k_end) > gap_days * 86400:
        return None, "expiry_mismatch"
    ends = [t for t in (p_end, k_end, _ts(entry.get("event_date"))) if t]
    if not ends:
        return None, "no_expiry"
    expiry_ts = int(min(ends))
    days = (expiry_ts - now) / 86400.0
    time_factor = _time_factor(days)
    if time_factor is None:
        return None, "expires_within_1d"

    # Live prices: the worse of the provider quote and the venue's own best ask.
    p_live = poly_best_ask(buy_tok)
    tol = _f("ARB_PREDICTION_PRICE_TOLERANCE", "0.10")
    if p_live is None or abs(p_live - p_quote) > tol:
        return None, "poly_price_mismatch"
    k_live = kalshi_ask(km, kal_side)
    if k_live is None:
        return None, "kalshi_null_price"
    pp, kp = max(p_quote, p_live), max(k_quote, k_live)

    # Size and fees at that size.
    from ..core.venue_fees import pair_net_edge_cents, poly_fee_info
    try:
        wager = float(entry.get("max_wager_usd") or 0)
    except (TypeError, ValueError):
        wager = 0.0
    deployable = min(wager, ARB_MAX_PAIR_USDC)
    if deployable < 10:
        return None, "below_min_size"
    contracts = int(deployable / (pp + kp))
    rate, exp, fee_src = poly_fee_info(m.get("conditionId"))
    net_cents = pair_net_edge_cents(pp, kp, contracts, rate, exp, "kalshi")
    net_edge_pct = net_cents - ARB_RISK_BUFFER_PCT
    if net_edge_pct <= 0:
        return None, "unprofitable_after_fees"
    score = (net_cents / 100.0) * deployable * time_factor

    raw = dict(entry)
    raw["_engine"] = {
        "signal_ts": signal_ts, "poly_live_ask": p_live, "kalshi_live_ask": k_live,
        "contracts": contracts, "poly_fee_rate": rate, "poly_fee_exponent": exp, "poly_fee_source": fee_src,
        "net_cents": round(net_cents, 4), "provider_roi_pct": entry.get("roi_pct"),
        "condition_id": m.get("conditionId"), "neg_risk": bool(m.get("negRisk")),
    }
    opp = ArbOpportunity(
        pair_id=f"pcom:{entry.get('group_id')}:{pm_id}:{ticker}:{poly_side}",
        poly_yes_token=yes_tok,
        poly_no_token=no_tok,
        kalshi_ticker=ticker,
        poly_yes_ask=pp,
        kalshi_yes_ask=kp,
        gross_edge_pct=net_cents,
        expiry_ts=expiry_ts,
        days_to_expiry=days,
        pnl_velocity=net_cents / max(days, 0.5),
        net_edge_pct=net_edge_pct,
        fillable_size_usdc=deployable,
        score=score,
        poly_title=str(entry.get("group_title") or m.get("question") or ""),
        kalshi_title=str(km.get("title") or km.get("yes_sub_title") or ticker),
        kalshi_side="NO" if poly_side == "yes" else "YES",  # executor buys the opposite side on Kalshi
        venue2="kalshi",
        outcome_key="yes",
        is_display_only=False,
        raw=raw,
        fetched_at=signal_ts,
    )
    return opp, "ok"


def fetch_opportunities(session=None) -> list[ArbOpportunity]:
    body = fetch_arb_raw(session)
    entries = body.get("opportunities") or []
    if not entries:
        return []
    as_of = _ts(body.get("as_of"))
    if as_of is None:
        log("⚠️ /arb body has no as_of — freshness unknown, dropped")
        return []
    signal_ts = as_of - float(body.get("delay_seconds") or 0)
    max_age = float(os.environ.get("ARB_STALE_QUOTE_SECONDS", "120"))
    if time.time() - signal_ts > max_age:
        log(f"⏭ /arb signals are {time.time() - signal_ts:.0f}s old (> {max_age:.0f}s) — dropped")
        return []
    with _STATS_LOCK:
        STATS.clear()
    out: list[ArbOpportunity] = []

    def one(e):
        try:
            return normalize(e, signal_ts)
        except Exception as ex:  # one bad entry never stops the cycle
            log(f"⚠️ normalize error for group={e.get('group_id')}: {ex}")
            return None, "error"

    with _cf.ThreadPoolExecutor(max_workers=min(len(entries), 16)) as pool:
        for opp, reason in pool.map(one, entries):
            _count(reason)
            if opp is not None:
                out.append(opp)
    out.sort(key=lambda o: o.score, reverse=True)
    log(f"{len(out)}/{len(entries)} executable after verification; reasons={dict(STATS)} limits={RATE_LIMITS}")
    return out
