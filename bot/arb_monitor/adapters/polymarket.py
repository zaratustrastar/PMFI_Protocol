"""Polymarket adapter - fetches markets and orderbook data via Gamma API."""

import json
import time
from datetime import datetime, timezone
from typing import Optional
from ..config import (
    POLY_GAMMA_URL, POLY_CLOB_URL,
    ARB_MAX_PAGES_POLY, ARB_PAGE_SIZE_POLY, ARB_EXPIRY_WINDOW_DAYS,
    ODDPOOL_BASE_URL, ODDPOOL_API_KEY,
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

    Also hard-rejects markets whose endDate is more than 2 hours in the past —
    defence against Gamma's stale active/closed flags (common during resolution).
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
    # Hard endDate check — catch resolved markets Gamma hasn't closed yet
    end_ts = _parse_expiry(m)
    if end_ts > 0 and end_ts < (time.time() - 7200):
        end_date_str = m.get("endDate", m.get("end_date_iso", ""))
        log(f"⚠️ skipping expired sub-market endDate={end_date_str!r} (ts={end_ts}) — Gamma flag lag")
        return False
    return True


def _keywords_from_title(title: str) -> str:
    """Extract up to 5 search keywords from an Oddpool event_title.

    Removes common English stop-words and very short tokens so the remaining
    words are distinctive enough to narrow down a Gamma text search.

    Examples:
      "FOMC April 29 Rate Decision" → "FOMC April Rate Decision"
      "2026 FIFA World Cup Group B Winner" → "FIFA World Cup Group"
    """
    import re as _re
    if not title:
        return ""
    _STOP = {
        "the", "a", "an", "of", "in", "for", "to", "and", "or", "at", "on",
        "by", "is", "be", "will", "with", "from", "who", "what", "when",
        "which", "that", "it", "its", "this", "these", "those", "vs", "v",
    }
    tokens = _re.findall(r"[a-zA-Z0-9]+", title)
    filtered = [t for t in tokens if len(t) >= 2 and t.lower() not in _STOP]
    return " ".join(filtered[:5])


def _title_similarity(a: str, b: str) -> float:
    """Jaccard word-overlap similarity between two event titles.

    Tokenises both strings to lowercase alphanumeric words (≥2 chars),
    then returns |intersection| / |union|.  Used by Path O to rank Oddpool
    Search results when no exact title match is found.

    Examples:
      "FOMC April 2026 Rate Decision" vs "FOMC April 29, 2026 rate decision" → ~0.71
      "NBA Champion 2026" vs "NBA Champion 2025-26" → ~0.50
    """
    import re as _re
    def _tok(s: str) -> set:
        return {t for t in _re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 2}
    ta, tb = _tok(a), _tok(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _collect_from_events(events: list, slug_hint: str) -> tuple[list, int]:
    """Collect live (clob_ids, market) tuples from a Gamma events list."""
    all_markets: list[tuple[list[str], dict]] = []
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
        log(f"🚫 events slug={slug_hint!r}: skipped {skipped} closed/archived/inactive/expired markets")
    return all_markets, skipped


def _collect_from_markets(markets_raw, slug_hint: str) -> tuple[list, int]:
    """Collect live (clob_ids, market) tuples from a flat Gamma markets list."""
    if isinstance(markets_raw, dict):
        markets_raw = markets_raw.get("markets", [markets_raw])
    if not isinstance(markets_raw, list):
        return [], 0
    all_markets: list[tuple[list[str], dict]] = []
    skipped = 0
    for m in markets_raw:
        if not _is_market_live(m):
            skipped += 1
            continue
        clob_ids = _parse_clob_token_ids(m.get("clobTokenIds"))
        if len(clob_ids) >= 2:
            all_markets.append((clob_ids, m))
    if skipped:
        log(f"🚫 markets slug={slug_hint!r}: skipped {skipped} closed/archived/inactive/expired markets")
    return all_markets, skipped


def _sanity_gate(
    chosen_ids: list[str],
    chosen_m: dict,
    buying_no: bool,
    slug: str,
    label: str,
    path_name: str,
) -> Optional[tuple[str, str]]:
    """Post-resolution settled-market gate (buy-side only).

    If the token we are buying has a bid ≥ 0.98, it has already won and there
    is no edge.  We only check the buy-side to avoid false rejections when the
    opposite side bids near $1 (e.g. 98% YES market is still tradeable when we
    buy NO at 2¢).

    Returns (yes_token, no_token) on pass, None on rejection.
    """
    buy_id   = chosen_ids[1] if buying_no else chosen_ids[0]
    other_id = chosen_ids[0] if buying_no else chosen_ids[1]
    side_name = "NO" if buying_no else "YES"
    buy_bid   = get_best_prices(buy_id).get("best_bid")
    other_bid = get_best_prices(other_id).get("best_bid")
    if buy_bid is not None and buy_bid >= 0.98:
        log(
            f"⚠️ {path_name} settled-market gate: {side_name}_bid={buy_bid:.2f} ≥ 0.98, "
            f"buy-side token won — rejecting slug={slug!r} label={label!r}"
        )
        return None
    if other_bid is not None and other_bid >= 0.98:
        log(
            f"ℹ️ {path_name} other-side bid={other_bid:.2f} ≥ 0.98 "
            f"(opposite likely winner) — buying {side_name}, not rejected"
        )
    matched = chosen_m.get("groupItemTitle") or chosen_m.get("question", "")[:50]
    log(
        f"✅ {path_name} slug={slug!r} label={label!r} → {matched!r} "
        f"YES={chosen_ids[0][:12]}... NO={chosen_ids[1][:12]}..."
    )
    return (chosen_ids[0], chosen_ids[1])


def lookup_token_ids_by_slug(
    slug: str,
    label: str = "",
    resolution_ts: Optional[int] = None,
    buying_no: bool = False,
    event_title: str = "",
) -> Optional[tuple[str, str]]:
    """Fetch YES/NO CLOB token IDs for a Polymarket event.

    The upstream ``slug`` (from Oddpool's ``polymarket_event_slug``) is treated
    as a *hint*, not a canonical Gamma identifier.  Oddpool uses its own internal
    event keys that often differ from Polymarket's URL slugs.

    Resolution strategy (tried in order, stops at first success):

      Path 0a — ``GET /events/slug/{slug}``    (REST path endpoint)
      Path 0b — ``GET /markets/slug/{slug}``   (REST path endpoint)
      Path 1  — ``GET /events?slug={slug}``    (query-param, active filter)
      Path 2  — ``GET /markets?slug={slug}``   (query-param, active filter)
      Path O  — Oddpool Search API bridge (requires ODDPOOL_API_KEY):
                   1. GET /search/events?q={event_title}&exchange=polymarket
                   2. Exact title match → fallback Jaccard similarity (≥0.25)
                   3. GET /search/events/{event_id}/markets
                   4. Exact label/question match → fallback _label_match_score
                   5. PRIMARY:  Gamma by market_id (condition_id)
                   6. FALLBACK: Gamma by real slug returned by Oddpool Search

    For multi-outcome events (elections, sports finals) ``label`` is used to
    pick the right sub-market.  ``resolution_ts`` breaks ties between editions
    of recurrent events.  ``buying_no`` limits the post-resolution sanity gate
    to the token we are actually buying so we don't false-reject live markets.
    """
    if not slug:
        return None

    _filters_active = {"active": "true", "closed": "false", "archived": "false"}

    def _try_events_path(url: str, path_name: str) -> Optional[tuple[str, str]]:
        try:
            resp = http_client.get(url, venue="polymarket", params=_filters_active, timeout=5)
            if not (resp and resp.status_code == 200):
                return None
            payload = resp.json()
            events = payload if isinstance(payload, list) else payload.get("events", [payload])
            all_m, _ = _collect_from_events(events, slug)
            if not all_m:
                return None
            pick = _pick_best_market(all_m, label, resolution_ts=resolution_ts)
            if pick is None:
                log(f"⚠️ {path_name}: no market passed label/period filter slug={slug!r} label={label!r}")
                return None
            return _sanity_gate(pick[0], pick[1], buying_no, slug, label, path_name)
        except Exception as _e:
            log(f"⚠️ {path_name} failed for slug={slug!r}: {_e}")
            return None

    def _try_markets_path(url: str, path_name: str, extra_params: Optional[dict] = None) -> Optional[tuple[str, str]]:
        try:
            params = dict(_filters_active)
            params["limit"] = 50
            if extra_params:
                params.update(extra_params)
            resp = http_client.get(url, venue="polymarket", params=params, timeout=5)
            if not (resp and resp.status_code == 200):
                return None
            all_m, _ = _collect_from_markets(resp.json(), slug)
            if not all_m:
                return None
            pick = _pick_best_market(all_m, label, resolution_ts=resolution_ts)
            if pick is None:
                log(f"⚠️ {path_name}: no market passed label/period filter slug={slug!r} label={label!r}")
                return None
            return _sanity_gate(pick[0], pick[1], buying_no, slug, label, path_name)
        except Exception as _e:
            log(f"⚠️ {path_name} failed for slug={slug!r}: {_e}")
            return None

    try:
        # ── Path 0a: /events/slug/{slug} (REST path endpoint) ─────────────────
        result = _try_events_path(f"{POLY_GAMMA_URL}/events/slug/{slug}", "Path 0a (/events/slug/)")
        if result:
            return result

        # ── Path 0b: /markets/slug/{slug} (REST path endpoint) ────────────────
        result = _try_markets_path(f"{POLY_GAMMA_URL}/markets/slug/{slug}", "Path 0b (/markets/slug/)")
        if result:
            return result

        # ── Path 1: /events?slug=... (query param, active filter) ─────────────
        try:
            resp1 = http_client.get(
                f"{POLY_GAMMA_URL}/events",
                venue="polymarket",
                params={"slug": slug, "limit": 3, **_filters_active},
                timeout=5,
            )
            if resp1 and resp1.status_code == 200:
                payload1 = resp1.json()
                events1 = payload1 if isinstance(payload1, list) else payload1.get("events", [payload1])
                all_m1, _ = _collect_from_events(events1, slug)
                if all_m1:
                    pick1 = _pick_best_market(all_m1, label, resolution_ts=resolution_ts)
                    if pick1 is not None:
                        result = _sanity_gate(pick1[0], pick1[1], buying_no, slug, label, "Path 1 (/events?slug=)")
                        if result:
                            return result
        except Exception as _e1:
            log(f"⚠️ Path 1 (/events?slug=) failed for slug={slug!r}: {_e1}")

        # ── Path 2: /markets?slug=... (query param, active filter) ────────────
        try:
            resp2 = http_client.get(
                f"{POLY_GAMMA_URL}/markets",
                venue="polymarket",
                params={"slug": slug, "limit": 10, **_filters_active},
                timeout=5,
            )
            if resp2 and resp2.status_code == 200:
                all_m2, _ = _collect_from_markets(resp2.json(), slug)
                if all_m2:
                    pick2 = _pick_best_market(all_m2, label, resolution_ts=resolution_ts)
                    if pick2 is not None:
                        result = _sanity_gate(pick2[0], pick2[1], buying_no, slug, label, "Path 2 (/markets?slug=)")
                        if result:
                            return result
        except Exception as _e2:
            log(f"⚠️ Path 2 (/markets?slug=) failed for slug={slug!r}: {_e2}")

        # ── Path O: Oddpool Search API → real Polymarket condition_id / slug ───
        # Oddpool's /search/events endpoint is the authoritative bridge between
        # its own internal event identifiers and canonical Polymarket market rows.
        # The returned market_id is a Polymarket condition ID (hex hash) and the
        # slug is a genuine Polymarket market slug — both work with Gamma directly.
        #
        # Steps (user-specified):
        #   1. GET /search/events?q={event_title}&exchange=polymarket
        #   2. Exact title match → fallback Jaccard similarity (threshold 0.25)
        #   3. GET /search/events/{event_id}/markets
        #   4. Exact question/label match → fallback _label_match_score similarity
        #   5. PRIMARY resolve: Gamma by market_id (condition_id)
        #   6. FALLBACK resolve: Gamma by slug
        if event_title and ODDPOOL_API_KEY:
            try:
                _op_headers = {
                    "X-API-Key": ODDPOOL_API_KEY,
                    "accept": "application/json",
                }
                log(f"🔎 Path O: searching Oddpool for event_title={event_title!r}")

                # Step 1 — search events
                search_resp = http_client.get(
                    f"{ODDPOOL_BASE_URL}/search/events",
                    venue="oddpool_search",
                    params={"q": event_title, "exchange": "polymarket", "limit": 10},
                    headers=_op_headers,
                    timeout=8,
                )
                if search_resp and search_resp.status_code == 200:
                    events_found = search_resp.json()
                    if isinstance(events_found, dict):
                        events_found = events_found.get("data", events_found.get("results", []))

                    if events_found:
                        # Step 2 — exact title match, fallback Jaccard
                        title_lower = event_title.lower().strip()
                        best_event = None
                        best_event_score = -1.0
                        for ev in events_found:
                            ev_title = (ev.get("event_title") or ev.get("title") or "").strip()
                            if ev_title.lower() == title_lower:
                                best_event = ev
                                best_event_score = 1.0
                                log(f"🔎 Path O: exact title match → {ev_title!r}")
                                break
                            sim = _title_similarity(event_title, ev_title)
                            if sim > best_event_score:
                                best_event_score = sim
                                best_event = ev

                        if best_event and best_event_score >= 0.25:
                            event_id = (best_event.get("event_id") or best_event.get("id") or "").strip()
                            log(
                                f"🔎 Path O: matched event event_id={event_id!r} "
                                f"title={best_event.get('event_title', '')!r} score={best_event_score:.3f}"
                            )

                            if event_id:
                                # Step 3 — get markets for this event
                                mkts_resp = http_client.get(
                                    f"{ODDPOOL_BASE_URL}/search/events/{event_id}/markets",
                                    venue="oddpool_search",
                                    headers=_op_headers,
                                    timeout=8,
                                )
                                if mkts_resp and mkts_resp.status_code == 200:
                                    op_markets = mkts_resp.json()
                                    if isinstance(op_markets, dict):
                                        op_markets = op_markets.get("data", op_markets.get("results", []))

                                    # Keep only active/open markets
                                    op_markets = [
                                        m for m in (op_markets or [])
                                        if m.get("status", "active") in ("active", "open")
                                    ]

                                    if op_markets:
                                        # Step 4 — exact label/question match, fallback similarity
                                        label_lower = label.lower().strip()
                                        best_market = None
                                        best_mkt_score = -1.0
                                        for om in op_markets:
                                            q_lower = (om.get("question") or "").lower().strip()
                                            # Exact: label is a substring of the question
                                            if label_lower and label_lower in q_lower:
                                                best_market = om
                                                best_mkt_score = 1.0
                                                log(f"🔎 Path O: exact label match in question={q_lower!r}")
                                                break
                                            lm = _label_match_score(label, om) if label else 0.5
                                            if lm > best_mkt_score:
                                                best_mkt_score = lm
                                                best_market = om

                                        # Last resort: first active market (single-market events)
                                        if best_market is None:
                                            best_market = op_markets[0]

                                        condition_id = (best_market.get("market_id") or "").strip()
                                        real_slug    = (best_market.get("slug") or "").strip()
                                        log(
                                            f"🔎 Path O: using market question={best_market.get('question', '')!r} "
                                            f"condition_id={condition_id!r} slug={real_slug!r} "
                                            f"label_score={best_mkt_score:.3f}"
                                        )

                                        # Step 5 PRIMARY — resolve via Gamma by condition_id.
                                        # Gamma's REST path /markets/{id} and query params
                                        # (?condition_id=, ?id=) do not reliably filter by
                                        # condition ID (they return unrelated markets or 422).
                                        # We try /markets?conditionId= as the only candidate;
                                        # if it returns the right market it is used, otherwise
                                        # we fall through to the slug path immediately.
                                        if condition_id:
                                            result = _try_markets_path(
                                                f"{POLY_GAMMA_URL}/markets",
                                                "Path O PRIMARY (conditionId)",
                                                extra_params={"conditionId": condition_id},
                                            )
                                            if result:
                                                return result
                                            log(
                                                f"🔎 Path O PRIMARY conditionId miss "
                                                f"(Gamma ignores param) — falling through to slug"
                                            )

                                        # Step 6 FALLBACK (and primary in practice) — resolve
                                        # via Gamma using the real Polymarket slug returned by
                                        # Oddpool Search. This slug IS canonical Polymarket and
                                        # works directly with Gamma's /events/slug/ endpoint.
                                        if real_slug:
                                            result = _try_events_path(
                                                f"{POLY_GAMMA_URL}/events/slug/{real_slug}",
                                                "Path O (slug→events/slug/)",
                                            )
                                            if result:
                                                return result
                                            result = _try_markets_path(
                                                f"{POLY_GAMMA_URL}/markets/slug/{real_slug}",
                                                "Path O (slug→markets/slug/)",
                                            )
                                            if result:
                                                return result
                                            result = _try_markets_path(
                                                f"{POLY_GAMMA_URL}/markets",
                                                "Path O (slug=param)",
                                                extra_params={"slug": real_slug},
                                            )
                                            if result:
                                                return result

                                        log(
                                            f"⚠️ Path O: all Gamma lookups failed for "
                                            f"condition_id={condition_id!r} slug={real_slug!r}"
                                        )
                                    else:
                                        log(f"⚠️ Path O: no active markets for event_id={event_id!r}")
                                else:
                                    st = mkts_resp.status_code if mkts_resp else "None"
                                    log(f"⚠️ Path O: /search/events/{event_id}/markets → HTTP {st}")
                        else:
                            log(
                                f"⚠️ Path O: no event matched event_title={event_title!r} "
                                f"(best_score={best_event_score:.3f} < 0.25, "
                                f"candidates={[ev.get('event_title','') for ev in events_found[:3]]})"
                            )
                    else:
                        log(f"⚠️ Path O: /search/events returned 0 results for q={event_title!r}")
                else:
                    st = search_resp.status_code if search_resp else "None"
                    log(f"⚠️ Path O: /search/events HTTP {st} for q={event_title!r}")

            except Exception as _oe:
                log(f"⚠️ Path O (Oddpool Search) exception for slug={slug!r}: {_oe}")

        elif event_title and not ODDPOOL_API_KEY:
            log(f"⚠️ Path O skipped: ODDPOOL_API_KEY not set — set it to enable Oddpool Search resolution")
        else:
            log(f"🔍 All slug paths failed for slug={slug!r} and no event_title provided — cannot resolve")

    except Exception as e:
        log(f"⚠️ lookup_token_ids_by_slug({slug!r}): {e}")

    log(f"⚠️ lookup_token_ids_by_slug: no tokens found for slug={slug!r} label={label!r} title={event_title!r}")
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
    resolution_ts: Optional[int] = None,
) -> Optional[tuple[list[str], dict]]:
    """Pick the best-matching market from a list of (clob_ids, market_dict) pairs.

    Scoring combines two signals:
      label_score   — text similarity between Oddpool's label and the market title
                      (0.0–1.0 from _label_match_score)
      temporal_bonus — how close the market's endDate is to Oddpool's resolution_time
                      (0.0–1.0; 1.0 when delta=0d, 0.0 when delta≥30d)

    Combined score = label_score + temporal_bonus (max 2.0, min score to pass = 0.5).

    Why temporal_bonus matters: for slug `aapl-above-in-march-2026`, Gamma returns
    sub-markets from March 2025 AND March 2026. Both have identical label scores (the
    price level text matches equally). The temporal bonus breaks the tie by favouring
    the sub-market whose endDate is closest to Oddpool's resolution_time — which is
    always the LIVE one Oddpool quoted.

    Hard skip: if resolution_ts is provided and a candidate's endDate is more than
    30 days before resolution_ts, it is from a prior period — skip it entirely.

    Returns None when:
      - no candidate survives the period filter, OR
      - a label is provided but best combined score < 0.5 (wrong market, reject)
    The caller treats None as "no match" (display-only).

    Note: the len==1 short-circuit is intentionally removed. A single surviving
    candidate is often from the wrong period and must still pass label + temporal scoring.
    """
    if not label:
        # No label hint — return first candidate with no scoring
        chosen_ids, chosen_m = candidates[0]
        return (_extract_yes_no_ordered(chosen_ids, chosen_m), chosen_m)

    # label_score must independently meet this threshold — temporal_bonus cannot
    # rescue a poor label match. This prevents time-proximity from selecting the
    # wrong market (e.g. "Carolina Panthers" for label "Las Vegas Raiders").
    # Lowered from 0.5 → 0.3 because Oddpool outcome labels like "above_1_2t"
    # only partially overlap with Polymarket titles like "Above $1.2T" (score ≈ 0.33).
    _MIN_LABEL_SCORE = 0.3

    best_combined = -1.0
    best_clob: list[str] = []
    best_m: dict = {}
    best_label_score = -1.0

    for clob_ids, m in candidates:
        label_score = _label_match_score(label, m)

        # Hard label gate: reject before computing temporal bonus.
        # Temporal bonus is only for tie-breaking among candidates that already
        # pass the label threshold — it cannot substitute for a label match.
        if label_score < _MIN_LABEL_SCORE:
            continue

        # Temporal bonus: prefer candidate whose endDate ≈ Oddpool's resolution_time.
        # High when delta=0d (1.0), zero when delta≥30d; used only for disambiguation
        # between multiple candidates that all cleared the label threshold.
        temporal_bonus = 0.0
        if resolution_ts:
            end_ts = _parse_expiry(m)
            if end_ts > 0:
                delta_days = abs(end_ts - resolution_ts) / 86400
                # Hard skip: market ended > 30 days before the Oddpool opportunity's expiry
                if end_ts < (resolution_ts - 86400 * 30):
                    log(
                        f"⏭ _pick_best_market: skipping market ended {delta_days:.0f}d before "
                        f"resolution_ts — wrong period "
                        f"({m.get('groupItemTitle') or m.get('question', '')[:35]!r})"
                    )
                    continue
                # Smooth bonus: 1.0 at delta=0d → 0.0 at delta=30d
                temporal_bonus = max(0.0, 1.0 - delta_days / 30.0)

        combined = label_score + temporal_bonus
        if combined > best_combined:
            best_combined = combined
            best_label_score = label_score
            best_clob = clob_ids
            best_m = m

    if not best_m:
        log(
            f"⚠️ _pick_best_market: no candidates with label_score ≥ {_MIN_LABEL_SCORE} "
            f"for label={label!r} resolution_ts={resolution_ts} — rejecting"
        )
        return None

    ordered = _extract_yes_no_ordered(best_clob, best_m)
    log(
        f"🎯 _pick_best_market: label={label!r} label_score={best_label_score:.2f} "
        f"combined={best_combined:.2f} "
        f"matched={best_m.get('groupItemTitle') or best_m.get('question', '')[:40]!r} "
        f"outcomes={best_m.get('outcomes')} "
        f"YES={ordered[0][:12] if ordered else 'n/a'}... "
        f"NO={ordered[1][:12] if len(ordered) > 1 else 'n/a'}..."
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
    # CLOB API returns asks in descending order (highest first); sort ascending so
    # we walk from the cheapest level upward and break correctly at max_fill_price.
    asks = sorted(book.get("asks", []), key=lambda x: float(x.get("price", 0)))
    contracts = 0
    usdc_cost = 0.0

    for level in asks:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
        except (ValueError, TypeError):
            continue

        if price > max_fill_price:
            break  # sorted ascending; once we exceed the limit all remaining levels are worse

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

    # The CLOB API returns asks in descending order (highest price first).
    # asks[0] is the synthetic ceiling order (e.g. 0.999), NOT the best ask.
    # We must find the true best prices by taking min(asks) and max(bids).
    best_bid_entry = max(bids, key=lambda x: float(x["price"])) if bids else None
    best_ask_entry = min(asks, key=lambda x: float(x["price"])) if asks else None

    best_bid = float(best_bid_entry["price"]) if best_bid_entry else None
    best_ask = float(best_ask_entry["price"]) if best_ask_entry else None
    bid_size = float(best_bid_entry.get("size", 0)) if best_bid_entry else 0
    ask_size = float(best_ask_entry.get("size", 0)) if best_ask_entry else 0

    neg_risk = bool(book.get("neg_risk", False))
    last_trade_price = book.get("last_trade_price")

    log(
        f"📖 get_best_prices({token_id[:16]}...): "
        f"best_ask={best_ask} best_bid={best_bid} "
        f"neg_risk={neg_risk} last_trade={last_trade_price} "
        f"asks={len(asks)} bids={len(bids)}"
    )

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "neg_risk": neg_risk,
        "last_trade_price": float(last_trade_price) if last_trade_price else None,
    }
