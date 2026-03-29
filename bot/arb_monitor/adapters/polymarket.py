"""Polymarket adapter - fetches markets and orderbook data via Gamma API."""

import json
import time
from datetime import datetime, timezone
from typing import Optional
from ..config import (
    POLY_GAMMA_URL, POLY_CLOB_URL,
    ARB_MAX_PAGES_POLY, ARB_PAGE_SIZE_POLY, ARB_EXPIRY_WINDOW_DAYS,
)
from .. import http_client
from ..models import NormalizedMarket, extract_team_key


_last_poly_stats: dict = {}


def log(msg: str):
    print(f"📊 [Arb/Polymarket] {msg}")


def get_discovery_stats() -> dict:
    return dict(_last_poly_stats)


def _parse_clob_token_ids(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except (json.JSONDecodeError, TypeError):
            parts = [p.strip().strip('"').strip("'") for p in raw.split(",") if p.strip()]
            return parts
    return []


def _parse_expiry(market: dict) -> int:
    end_date = market.get("endDate", market.get("end_date_iso", ""))
    if not end_date:
        return 0
    try:
        if isinstance(end_date, (int, float)):
            return int(end_date)
        if "T" in str(end_date):
            dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            return int(dt.timestamp())
        return int(float(end_date))
    except Exception:
        return 0


def fetch_all_active_markets() -> tuple[list[dict], dict]:
    global _last_poly_stats
    stats = {
        "fetchedTotal": 0,
        "excludedClosed": 0,
        "excludedArchived": 0,
        "excludedMissingTokens": 0,
        "excludedExpiry": 0,
        "includedFinal": 0,
    }

    now = int(time.time())
    max_expiry = now + ARB_EXPIRY_WINDOW_DAYS * 86400

    seen_ids: set[str] = set()
    accepted: list[dict] = []

    api_errors: list[str] = []

    for page in range(ARB_MAX_PAGES_POLY):
        offset = page * ARB_PAGE_SIZE_POLY
        url = f"{POLY_GAMMA_URL}/markets"
        params = {
            "closed": "false",
            "limit": ARB_PAGE_SIZE_POLY,
            "offset": offset,
        }
        log(f"Fetching page {page + 1}: offset={offset} limit={ARB_PAGE_SIZE_POLY}")
        resp = http_client.get(url, venue="polymarket", params=params)
        if resp is None:
            err = f"Page {page + 1}: API returned None (likely proxy/network/Cloudflare error)"
            log(f"❌ {err}")
            api_errors.append(err)
            break
        if resp.status_code != 200:
            err = f"Page {page + 1}: API returned HTTP {resp.status_code}"
            log(f"❌ {err}")
            api_errors.append(err)
            break

        try:
            markets = resp.json()
            if not isinstance(markets, list):
                markets = markets.get("data", markets.get("markets", []))
        except Exception as e:
            err = f"Page {page + 1}: JSON parse error: {e}"
            log(f"❌ {err}")
            api_errors.append(err)
            break

        if not markets:
            log(f"Page {page + 1}: empty response, stopping")
            break

        page_new = 0
        for m in markets:
            stats["fetchedTotal"] += 1

            mid = m.get("id", m.get("condition_id", m.get("conditionId", "")))
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)

            if m.get("closed") is True or str(m.get("closed", "")).lower() == "true":
                stats["excludedClosed"] += 1
                continue

            if m.get("archived") is True:
                stats["excludedArchived"] += 1
                continue

            clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
            if len(clob_ids) < 2:
                stats["excludedMissingTokens"] += 1
                continue

            expiry_ts = _parse_expiry(m)
            if expiry_ts <= now or expiry_ts > max_expiry:
                stats["excludedExpiry"] += 1
                continue

            m["_parsed_clob_ids"] = clob_ids
            m["_parsed_expiry"] = expiry_ts
            accepted.append(m)
            page_new += 1

        log(f"Page {page + 1}: {len(markets)} fetched, {page_new} new accepted")

        if len(markets) < ARB_PAGE_SIZE_POLY:
            break

    stats["includedFinal"] = len(accepted)
    stats["apiErrors"] = api_errors
    _last_poly_stats = stats
    log(f"Total: {stats['fetchedTotal']} fetched → {stats['includedFinal']} included "
        f"(closed={stats['excludedClosed']}, archived={stats['excludedArchived']}, "
        f"missingTokens={stats['excludedMissingTokens']}, expiry={stats['excludedExpiry']})")
    if api_errors:
        log(f"⚠️ API errors during fetch: {api_errors}")
    return accepted, stats


def normalize_market(market: dict) -> NormalizedMarket | None:
    """Normalize a raw Polymarket market dict into a NormalizedMarket.

    Returns None if the market lacks exactly 2 CLOB token IDs (non-binary market).
    """
    clob_ids = market.get("_parsed_clob_ids", [])
    if not clob_ids:
        raw_clob = market.get("clobTokenIds")
        if isinstance(raw_clob, str):
            try:
                raw_clob = json.loads(raw_clob)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(raw_clob, list):
            clob_ids = [str(t) for t in raw_clob if t]
        else:
            clob_ids = []

    if len(clob_ids) < 2:
        mid = market.get("id", market.get("condition_id", ""))
        question = market.get("question", market.get("title", ""))[:60]
        log(f"⚠️ Skipping market {mid} (need 2 clobTokenIds, got {len(clob_ids)}): {question}")
        return None

    yes_token = ""
    no_token = ""

    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = None

    if isinstance(outcomes, list) and len(clob_ids) >= 2:
        outcome_labels = [str(o).upper() for o in outcomes]
        if len(outcome_labels) >= 2:
            for i, label in enumerate(outcome_labels):
                if label == "YES" and i < len(clob_ids):
                    yes_token = clob_ids[i]
                elif label == "NO" and i < len(clob_ids):
                    no_token = clob_ids[i]
    if not yes_token and len(clob_ids) >= 2:
        yes_token = clob_ids[0]
        no_token = clob_ids[1]

    if not yes_token or not no_token:
        mid = market.get("id", market.get("condition_id", ""))
        question = market.get("question", market.get("title", ""))[:60]
        log(f"⚠️ Skipping market {mid} (could not resolve YES/NO tokens): {question}")
        return None

    expiry_ts = market.get("_parsed_expiry") or _parse_expiry(market)

    question = market.get("question", market.get("title", ""))
    mid = market.get("id", market.get("condition_id", market.get("conditionId", "")))
    description = (market.get("description", "") or "").strip()

    team_key = extract_team_key(question)

    return NormalizedMarket(
        venue="polymarket",
        marketId=mid,
        title=question,
        expiryTs=expiry_ts,
        yesTokenId=yes_token,
        noTokenId=no_token,
        team_key=team_key,
        description=description,
        meta={
            "slug": market.get("slug", ""),
            "volume": float(market.get("volume", 0) or 0),
        },
    )


def get_polymarket_markets() -> list[NormalizedMarket]:
    from ..core.filters import classify_sport
    raw, _stats = fetch_all_active_markets()
    normalized = []
    for m in raw:
        nm = normalize_market(m)
        if nm is not None:
            sport = classify_sport(nm.title)
            nm.sport = sport
            normalized.append(nm)
    return normalized


def _label_match_score(label: str, market: dict) -> float:
    """Score how well a Gamma market matches an Oddpool label.

    Returns 0.0–1.0. Used to pick the right market within a multi-outcome
    event (e.g. "hou" matching "Houston Rockets" within NBA Champion 2026).
    """
    if not label:
        return 0.0
    label_n = label.lower().replace("_", " ").replace("-", " ").strip()
    label_parts = set(label_n.split())

    candidates = [
        (market.get("groupItemTitle") or ""),
        (market.get("question") or ""),
        (market.get("description") or ""),
        (market.get("title") or ""),
    ]
    best = 0.0
    for candidate in candidates:
        if not candidate:
            continue
        c = candidate.lower()
        # Exact substring
        if label_n in c:
            best = max(best, 1.0)
            continue
        # Word overlap score
        c_parts = set(c.replace("-", " ").replace("_", " ").split())
        overlap = label_parts & c_parts
        if overlap:
            score = len(overlap) / max(len(label_parts), 1)
            # Partial token match: "hou" inside "houston"
            for lp in label_parts:
                if len(lp) >= 3:
                    for cp in c_parts:
                        if lp in cp or cp in lp:
                            score = max(score, 0.5)
            best = max(best, score)
    return best


def _is_market_live(m: dict) -> bool:
    """Return True only when a market is genuinely open and tradeable.

    Filters out settled/resolved markets before label-matching so that old
    seasons (e.g. NBA Champion 2024-25, already resolved) are never returned
    for an opportunity referencing the current live season.

    Checks both boolean flags and their common string representations because
    the Gamma API occasionally returns "true"/"false" strings.
    """
    def _bool(val) -> bool:
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    if _bool(m.get("closed", False)):
        return False
    if _bool(m.get("archived", False)):
        return False
    # active defaults to True when absent (open markets may omit it)
    active_raw = m.get("active")
    if active_raw is not None and not _bool(active_raw):
        return False
    return True


def lookup_token_ids_by_slug(slug: str, label: str = "") -> Optional[tuple[str, str]]:
    """Fetch YES/NO CLOB token IDs for a Polymarket event by its URL slug.

    Returns (yes_token_id, no_token_id) or None on failure.

    For multi-outcome events (NBA Champion, election candidates, etc.) the event
    contains many markets — one per outcome. The optional `label` parameter (from
    Oddpool's entry["label"]) is used to score and pick the correct market rather
    than blindly returning the first one. Without label, falls back to first market.

    Filtering strategy (guards against resolved/old-season markets):
      1. API-level: pass active=true, closed=false to both endpoints.
      2. In-memory: skip markets where _is_market_live() returns False.
      3. Post-resolution sanity gate: if both YES and NO asks are ≥ 0.98 the
         market is settled — reject and return None.

    Tries the /events endpoint first (event slug → markets), then /markets with slug filter.
    """
    if not slug:
        return None
    try:
        # ── Path 1: /events endpoint ──────────────────────────────────────────
        resp = http_client.get(
            f"{POLY_GAMMA_URL}/events",
            venue="polymarket",
            params={"slug": slug, "limit": 3, "active": "true", "closed": "false"},
            timeout=10,
        )
        if resp and resp.status_code == 200:
            payload = resp.json()
            events = payload if isinstance(payload, list) else payload.get("events", [payload])
            # Collect live markets only — skip settled/archived/closed ones.
            all_markets = []
            skipped = 0
            for event in events:
                for m in event.get("markets", []):
                    if not _is_market_live(m):
                        skipped += 1
                        continue
                    clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
                    if len(clob_ids) >= 2:
                        all_markets.append((clob_ids, m))
            if skipped:
                log(f"🚫 /events slug={slug!r}: skipped {skipped} closed/archived/inactive markets")
            if all_markets:
                chosen_ids, chosen_m = _pick_best_market(all_markets, label)
                # ── Post-resolution sanity gate ───────────────────────────────
                # If both YES and NO asks are ≥ 0.98 the market is settled even
                # though the API said active=true (cache lag or API inconsistency).
                # Reject before returning to avoid triggering the ask-mismatch
                # rejection path in _resolve_poly_tokens with a confusing error.
                yes_ask_raw = get_best_prices(chosen_ids[0]).get("best_ask")
                no_ask_raw  = get_best_prices(chosen_ids[1]).get("best_ask")
                if (yes_ask_raw is not None and no_ask_raw is not None
                        and yes_ask_raw >= 0.98 and no_ask_raw >= 0.98):
                    log(
                        f"⚠️ Post-resolution settled-market gate: both tokens at "
                        f"YES={yes_ask_raw:.3f} NO={no_ask_raw:.3f} — rejecting "
                        f"slug={slug!r} label={label!r}"
                    )
                    return None
                log(
                    f"✅ Token lookup for slug={slug!r} label={label!r}: "
                    f"matched={chosen_m.get('groupItemTitle') or chosen_m.get('question', '')[:40]!r} "
                    f"YES={chosen_ids[0][:12]}... NO={chosen_ids[1][:12]}..."
                )
                return (chosen_ids[0], chosen_ids[1])

        # ── Path 2: /markets fallback ─────────────────────────────────────────
        resp2 = http_client.get(
            f"{POLY_GAMMA_URL}/markets",
            venue="polymarket",
            params={"slug": slug, "limit": 10, "active": "true", "closed": "false"},
            timeout=10,
        )
        if resp2 and resp2.status_code == 200:
            markets = resp2.json()
            if isinstance(markets, dict):
                markets = markets.get("markets", [markets])
            all_markets = []
            skipped = 0
            for m in (markets if isinstance(markets, list) else []):
                if not _is_market_live(m):
                    skipped += 1
                    continue
                clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
                if len(clob_ids) >= 2:
                    all_markets.append((clob_ids, m))
            if skipped:
                log(f"🚫 /markets slug={slug!r}: skipped {skipped} closed/archived/inactive markets")
            if all_markets:
                chosen_ids, chosen_m = _pick_best_market(all_markets, label)
                # Post-resolution sanity gate (same as Path 1)
                yes_ask_raw = get_best_prices(chosen_ids[0]).get("best_ask")
                no_ask_raw  = get_best_prices(chosen_ids[1]).get("best_ask")
                if (yes_ask_raw is not None and no_ask_raw is not None
                        and yes_ask_raw >= 0.98 and no_ask_raw >= 0.98):
                    log(
                        f"⚠️ Post-resolution settled-market gate (fallback): both tokens at "
                        f"YES={yes_ask_raw:.3f} NO={no_ask_raw:.3f} — rejecting "
                        f"slug={slug!r} label={label!r}"
                    )
                    return None
                log(
                    f"✅ Token lookup (markets fallback) slug={slug!r} label={label!r}: "
                    f"matched={chosen_m.get('groupItemTitle') or chosen_m.get('question', '')[:40]!r} "
                    f"YES={chosen_ids[0][:12]}..."
                )
                return (chosen_ids[0], chosen_ids[1])
    except Exception as e:
        log(f"⚠️ lookup_token_ids_by_slug({slug!r}): {e}")
    log(f"⚠️ lookup_token_ids_by_slug: no tokens found for slug={slug!r} label={label!r}")
    return None


def _extract_yes_no_ordered(clob_ids: list[str], market: dict) -> list[str]:
    """Return [yes_token, no_token] using the market's 'outcomes' field.

    Polymarket's Gamma API does not guarantee that clobTokenIds[0] is YES.
    Many markets (especially elections and multi-outcome events) have NO at
    index 0 and YES at index 1. Using the outcomes array to look up the correct
    positions prevents YES/NO token swaps that cause large false-slippage rejections.

    Falls back to [ids[0], ids[1]] when outcomes are absent or unrecognised.
    """
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            import json as _json
            outcomes = _json.loads(outcomes)
        except Exception:
            outcomes = None
    if isinstance(outcomes, list) and len(outcomes) >= 2 and len(clob_ids) >= 2:
        yes_tok = ""
        no_tok = ""
        for i, label_raw in enumerate(outcomes):
            if i >= len(clob_ids):
                break
            upper = str(label_raw).upper().strip()
            if upper == "YES" and not yes_tok:
                yes_tok = clob_ids[i]
            elif upper == "NO" and not no_tok:
                no_tok = clob_ids[i]
        if yes_tok and no_tok:
            return [yes_tok, no_tok]
    # Fallback: assume original order
    return list(clob_ids[:2])


def _pick_best_market(
    candidates: list[tuple[list[str], dict]],
    label: str,
) -> tuple[list[str], dict]:
    """Pick the best-matching market from a list of (clob_ids, market_dict) pairs.

    If label is provided, scores each market against the label and returns the
    highest-scoring one. Falls back to the first candidate when all scores are 0.
    """
    if not label or len(candidates) == 1:
        chosen_ids, chosen_m = candidates[0]
        return (_extract_yes_no_ordered(chosen_ids, chosen_m), chosen_m)
    best_score = -1.0
    best_clob: list[str] = []
    best_m: dict = {}
    for clob_ids, m in candidates:
        score = _label_match_score(label, m)
        if score > best_score:
            best_score = score
            best_clob = clob_ids
            best_m = m
    ordered = _extract_yes_no_ordered(best_clob, best_m)
    log(
        f"🎯 _pick_best_market: label={label!r} score={best_score:.2f} "
        f"matched={best_m.get('groupItemTitle') or best_m.get('question', '')[:40]!r} "
        f"outcomes={best_m.get('outcomes')} "
        f"YES={ordered[0][:12] if ordered else 'n/a'}... NO={ordered[1][:12] if len(ordered) > 1 else 'n/a'}..."
    )
    return (ordered, best_m)


def fetch_orderbook(token_id: str) -> Optional[dict]:
    url = f"{POLY_CLOB_URL}/book"
    resp = http_client.get(url, venue="polymarket", params={"token_id": token_id}, timeout=10)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception as e:
        log(f"Orderbook error for {token_id}: {e}")
        return None


def compute_fillable_contracts(book: dict, max_fill_price: float) -> tuple[int, float]:
    """Walk the ask ladder and return contracts fillable at or below max_fill_price.

    Polymarket order books list asks sorted best-first (lowest price first).
    We accumulate volume at each price level until we hit a level that would
    breach max_fill_price — anything beyond that would eat into the arb edge.

    Args:
        book:           Raw order book dict with "asks" list of {"price", "size"} entries.
        max_fill_price: Maximum price per contract we are willing to pay on this leg.
                        Caller derives this as: 1.0 - live_leg2_ask - min_edge_pct
                        (i.e. the breakeven ask price on the Poly leg given the other leg's cost).

    Returns:
        (contracts_fillable, usdc_cost) — integer contracts and total USDC cost.
        Returns (0, 0.0) when no depth exists below max_fill_price.
    """
    asks = book.get("asks", [])
    contracts = 0
    usdc_cost = 0.0

    for level in asks:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
        except (ValueError, TypeError):
            continue

        if price > max_fill_price:
            break  # asks are sorted best-first; once we exceed the limit we're done

        level_contracts = int(size)  # Polymarket trades in whole-number contract sizes
        if level_contracts > 0:
            contracts += level_contracts
            usdc_cost += level_contracts * price

    log(
        f"📏 [Poly] depth walk: max_fill_price={max_fill_price:.4f} "
        f"→ {contracts} contracts fillable (${usdc_cost:.2f} USDC) "
        f"across {len(asks)} ask levels"
    )
    return contracts, usdc_cost


def get_best_prices(token_id: str) -> dict:
    if not token_id:
        log("⚠️ poly_orderbook_missing: no token_id provided")
        return {"best_bid": None, "best_ask": None, "bid_size": 0, "ask_size": 0, "warning": "poly_orderbook_missing"}

    book = fetch_orderbook(token_id)
    if not book:
        log(f"⚠️ poly_orderbook_missing: fetch returned None for token {token_id[:16]}...")
        return {"best_bid": None, "best_ask": None, "bid_size": 0, "ask_size": 0, "warning": "poly_orderbook_missing"}

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    bid_size = float(bids[0].get("size", 0)) if bids else 0
    ask_size = float(asks[0].get("size", 0)) if asks else 0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }
