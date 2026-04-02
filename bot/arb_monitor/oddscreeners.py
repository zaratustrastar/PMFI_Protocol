"""OddScreeners SSE Feed Collector + Arb Verifier.

Connects to OddScreeners SSE endpoint to receive pre-matched Polymarket vs Opinion pairs.
Then verifies opportunities by fetching fresh orderbooks from both venues.
"""

import json
import re
import time
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import requests

from .config import MIN_PRICE_THRESHOLD


SSE_URL = "https://www.oddscreeners.com/api/arbitage/stream"
SSE_PARAMS = {
    "priceMode": "asks",
    "minArbPct": "0.1",
    "limit": "100",
    "scanMode": "full",
}
SSE_HEADERS = {
    "Accept": "text/event-stream",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

MIN_VERIFIED_EDGE_PCT = 0.5
VERIFY_INTERVAL_SECONDS = 30
HEARTBEAT_TIMEOUT_SECONDS = 90
MAX_STORED_PAIRS = 500
MAX_RECONNECT_DELAY = 120
NEAR_EXPIRY_HOURS = 3
PAIR_STALE_SECONDS = 7200       # drop pairs not seen in SSE for >2 hours
TOKEN_CACHE_TTL_SECONDS = 3600  # re-fetch token IDs once per hour per pair


def log(msg: str):
    print(f"🔍 [OddScreeners] {msg}")


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00e2\u0080\u0099", "'")
    text = text.replace("\u00e2\u0080\u0093", "-")
    text = text.replace("\u00e2\u0080\u009c", '"')
    text = text.replace("\u00e2\u0080\u009d", '"')
    return text.strip()


def _parse_expiry(end_date: str) -> int:
    if not end_date:
        return 0
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def _extract_poly_slug(poly_url: str) -> str:
    if not poly_url:
        return ""
    m = re.search(r"polymarket\.com/event/([^/?#]+)", poly_url)
    return m.group(1) if m else ""


class OddScreenersPairStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._pairs: dict[str, dict] = {}
        self._verified: list[dict] = []
        self._verified_at: float = 0
        self._status = {
            "connected": False,
            "lastEventAt": 0,
            "lastError": None,
            "reconnectCount": 0,
            "pairsStored": 0,
            "verifiedCount": 0,
        }

    def upsert_pair(self, pair: dict):
        pair_id = pair.get("id", "")
        if not pair_id:
            return
        now = time.time()
        with self._lock:
            existing = self._pairs.get(pair_id)
            if existing:
                pair["firstSeenAt"] = existing.get("firstSeenAt", now)
            else:
                pair["firstSeenAt"] = now
            pair["lastSeenAt"] = now
            self._pairs[pair_id] = pair
            if len(self._pairs) > MAX_STORED_PAIRS:
                oldest_keys = sorted(self._pairs, key=lambda k: self._pairs[k].get("lastSeenAt", 0))
                for k in oldest_keys[:len(self._pairs) - MAX_STORED_PAIRS]:
                    del self._pairs[k]
            self._status["pairsStored"] = len(self._pairs)

    def get_all_pairs(self) -> list[dict]:
        with self._lock:
            return list(self._pairs.values())

    def set_verified(self, verified: list[dict]):
        with self._lock:
            self._verified = verified
            self._verified_at = time.time()
            self._status["verifiedCount"] = len(verified)

    def get_verified(self) -> tuple[list[dict], float]:
        with self._lock:
            return list(self._verified), self._verified_at

    def update_status(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)

    def get_status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def remove_stale_pairs(self, max_age_seconds: int) -> int:
        """Remove pairs not updated by SSE for longer than max_age_seconds. Returns count removed."""
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._pairs.items()
                     if now - v.get("lastSeenAt", 0) > max_age_seconds]
            for k in stale:
                del self._pairs[k]
            self._status["pairsStored"] = len(self._pairs)
            return len(stale)


oddscreeners_store = OddScreenersPairStore()


def _parse_sse_events(response):
    event_type = None
    data_lines = []

    for line in response.iter_lines(decode_unicode=True):
        if line is None:
            continue
        line = line.rstrip("\r\n") if isinstance(line, str) else line.decode("utf-8", errors="replace").rstrip("\r\n")

        if line == "":
            if event_type and data_lines:
                data_str = "\n".join(data_lines)
                yield event_type, data_str
            event_type = None
            data_lines = []
            continue

        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def _process_pair(raw: dict) -> dict:
    return {
        "id": raw.get("id", ""),
        "opinionMarketId": raw.get("opinionMarketId", ""),
        "title": _clean_text(raw.get("title", "")),
        "parentTitle": _clean_text(raw.get("parentTitle") or ""),
        "outcome": _clean_text(raw.get("outcome", "")),
        "imageUrl": raw.get("imageUrl", ""),
        "endDate": raw.get("endDate", ""),
        "expiryTs": _parse_expiry(raw.get("endDate", "")),
        "polyTitle": _clean_text(raw.get("poly", {}).get("title", "")),
        "polyUrl": raw.get("poly", {}).get("url", ""),
        "polySlug": _extract_poly_slug(raw.get("poly", {}).get("url", "")),
        "opinionTitle": _clean_text(raw.get("opinion", {}).get("title", "")),
        "opinionUrl": raw.get("opinion", {}).get("url", ""),
        "strategy": raw.get("strategy", []),
        "prices": raw.get("prices", {}),
        "sizes": raw.get("sizes", {}),
        "arbPct": raw.get("arbPct", 0),
        "similarity": raw.get("similarity", 0),
        "isWhitelisted": raw.get("isWhitelisted", False),
        "autoMatched": raw.get("autoMatched", False),
        "priceMode": raw.get("priceMode", ""),
        "label": raw.get("label", ""),
    }


_active_response: Optional[requests.Response] = None
_active_response_lock = threading.Lock()


def _force_disconnect():
    with _active_response_lock:
        resp = _active_response
        if resp:
            try:
                resp.close()
            except Exception:
                pass


def _run_heartbeat_watchdog():
    while True:
        time.sleep(30)
        status = oddscreeners_store.get_status()
        last_event = status.get("lastEventAt", 0)
        if last_event > 0 and status.get("connected", False):
            elapsed = time.time() - last_event
            if elapsed > HEARTBEAT_TIMEOUT_SECONDS:
                log(f"⚠️ Heartbeat watchdog: no events for {elapsed:.0f}s, forcing reconnect")
                _force_disconnect()


def _run_sse_collector():
    global _active_response
    reconnect_count = 0
    delay = 2

    while True:
        try:
            log(f"Connecting to SSE feed (attempt {reconnect_count + 1})...")
            oddscreeners_store.update_status(connected=False)

            resp = requests.get(
                SSE_URL,
                params=SSE_PARAMS,
                headers=SSE_HEADERS,
                stream=True,
                timeout=(15, 120),
            )

            if resp.status_code != 200:
                err = f"SSE returned HTTP {resp.status_code}"
                log(f"❌ {err}")
                oddscreeners_store.update_status(lastError=err)
                raise ConnectionError(err)

            with _active_response_lock:
                _active_response = resp

            log("✅ Connected to SSE feed")
            oddscreeners_store.update_status(connected=True, lastError=None)
            delay = 2
            batch_count = 0
            _logged_raw_sample = False

            for event_type, data_str in _parse_sse_events(resp):
                now = time.time()
                oddscreeners_store.update_status(lastEventAt=now)

                if event_type == "progress":
                    continue

                if event_type == "batch":
                    try:
                        batch = json.loads(data_str)
                        rows = batch.get("rows", [])
                        for raw in rows:
                            if not _logged_raw_sample and rows:
                                log(f"📋 Raw SSE sample keys — top: {list(raw.keys())}, "
                                    f"poly: {list(raw.get('poly', {}).keys())}, "
                                    f"opinion: {list(raw.get('opinion', {}).keys())}, "
                                    f"prices: {list(raw.get('prices', {}).keys())}, "
                                    f"strategy[0]: {raw.get('strategy', [None])[0]}")
                                _logged_raw_sample = True
                            pair = _process_pair(raw)
                            oddscreeners_store.upsert_pair(pair)
                        batch_count += 1
                        log(f"Batch #{batch_count}: {len(rows)} pairs (total stored: {oddscreeners_store.get_status()['pairsStored']})")
                    except json.JSONDecodeError as e:
                        log(f"⚠️ Batch JSON parse error: {e}")

                elif event_type == "match":
                    try:
                        raw = json.loads(data_str)
                        pair = _process_pair(raw)
                        oddscreeners_store.upsert_pair(pair)
                    except json.JSONDecodeError as e:
                        log(f"⚠️ Match JSON parse error: {e}")

        except Exception as e:
            reconnect_count += 1
            oddscreeners_store.update_status(
                connected=False,
                lastError=str(e),
                reconnectCount=reconnect_count,
            )
            with _active_response_lock:
                _active_response = None
            log(f"⚠️ SSE disconnected: {e}, reconnecting in {delay}s (attempt {reconnect_count})")
            time.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)


def _run_verifier():
    from .adapters.polymarket import (
        get_best_prices as poly_best_prices,
        lookup_token_ids_by_slug,
    )
    from .adapters.opinion import (
        get_best_prices as opinion_best_prices,
        lookup_token_ids_by_market_id,
    )

    # (poly_slug, label, expiry_ts) → {polyYesToken, polyNoToken, opYesToken, opNoToken, cachedAt}
    # Keyed by (slug, label, expiry) so different outcomes within the same multi-market
    # event each resolve to their own YES/NO token pair.
    _token_cache: dict = {}

    while True:
        try:
            # 1. Evict pairs that oddscreeners stopped reporting (no longer real arbs)
            removed = oddscreeners_store.remove_stale_pairs(PAIR_STALE_SECONDS)
            if removed:
                log(f"🗑️ Evicted {removed} stale pairs (not seen in SSE for >{PAIR_STALE_SECONDS}s)")

            pairs = oddscreeners_store.get_all_pairs()
            if not pairs:
                log("No pairs to verify yet, waiting...")
                time.sleep(VERIFY_INTERVAL_SECONDS)
                continue

            now = int(time.time())
            near_expiry_cutoff = now + NEAR_EXPIRY_HOURS * 3600
            live_count = 0
            fallback_count = 0
            skipped_count = 0
            verified = []

            for pair in pairs:
                try:
                    expiry = pair.get("expiryTs", 0)
                    if expiry > 0 and expiry < near_expiry_cutoff:
                        skipped_count += 1
                        continue

                    pair_id = pair.get("id", "")
                    poly_slug = pair.get("polySlug", "")
                    op_market_id = str(pair.get("opinionMarketId", "") or "")
                    prices = pair.get("prices", {})  # SSE cached prices (fallback)

                    # Extract label and expiry for sub-market disambiguation.
                    # Many Polymarket events contain multiple binary markets (one per
                    # outcome). Without label+expiry, lookup_token_ids_by_slug picks
                    # an arbitrary market — causing wrong YES/NO token IDs or no match.
                    pair_label = (
                        pair.get("label", "")
                        or pair.get("outcome", "")
                        or pair.get("polyLabel", "")
                        or ""
                    )
                    pair_expiry = expiry  # already parsed above

                    # 2. Refresh token ID cache if stale or missing.
                    # Cache keyed by (slug, label, expiry) so different outcomes in the
                    # same multi-market event each resolve to their own token pair.
                    cache_key = (poly_slug, pair_label, pair_expiry)
                    cached = _token_cache.get(cache_key, {})
                    cache_age = now - cached.get("cachedAt", 0)
                    # Use a short TTL for failed resolutions so we retry quickly;
                    # successful resolutions are cached for the full TOKEN_CACHE_TTL_SECONDS.
                    miss_ttl = 60  # 60s retry for unresolved tokens
                    hit_ttl  = TOKEN_CACHE_TTL_SECONDS
                    effective_ttl = hit_ttl if cached.get("polyYesToken") else miss_ttl
                    if cache_age > effective_ttl or not cached.get("polyYesToken"):
                        poly_tokens = (
                            lookup_token_ids_by_slug(
                                poly_slug,
                                label=pair_label,
                                resolution_ts=pair_expiry if pair_expiry else None,
                            )
                            if poly_slug else None
                        )
                        op_tokens = (
                            lookup_token_ids_by_market_id(op_market_id)
                            if op_market_id and op_market_id != "0" else None
                        )
                        cached = {
                            "polyYesToken": poly_tokens[0] if poly_tokens else None,
                            "polyNoToken":  poly_tokens[1] if poly_tokens else None,
                            "opYesToken":   op_tokens[0]   if op_tokens   else None,
                            "opNoToken":    op_tokens[1]   if op_tokens   else None,
                            "cachedAt": now,
                        }
                        _token_cache[cache_key] = cached
                        log(
                            f"  Token cache refresh [{pair_id[:10]}] "
                            f"slug={poly_slug!r} label={pair_label!r}: "
                            f"poly={'✅' if poly_tokens else '❌'} "
                            f"opinion={'✅' if op_tokens else '❌'}"
                        )

                    # 2b. Require resolved token IDs for execution.
                    # If Polymarket tokens are missing we cannot place a CLOB order —
                    # mark the pair display-only and skip it from the executable list.
                    poly_tokens_resolved = bool(cached.get("polyYesToken") and cached.get("polyNoToken"))
                    op_tokens_needed = bool(op_market_id and op_market_id != "0")
                    op_tokens_resolved = bool(cached.get("opYesToken") and cached.get("opNoToken"))
                    is_executable = poly_tokens_resolved and (not op_tokens_needed or op_tokens_resolved)

                    if not poly_tokens_resolved:
                        log(
                            f"  ⏭️ [{pair_id[:10]}] Poly tokens unresolved "
                            f"(slug={poly_slug!r} label={pair_label!r}) — display-only, skipping"
                        )
                        skipped_count += 1
                        continue

                    # 3. Fetch live orderbook ask prices
                    live_poly_yes = live_poly_no = live_op_yes = live_op_no = None
                    using_live = False

                    if cached.get("polyYesToken") and cached.get("polyNoToken"):
                        py_book = poly_best_prices(cached["polyYesToken"])
                        pn_book = poly_best_prices(cached["polyNoToken"])
                        live_poly_yes = py_book.get("best_ask")
                        live_poly_no  = pn_book.get("best_ask")

                    if cached.get("opYesToken") and cached.get("opNoToken"):
                        op_book = opinion_best_prices(cached["opYesToken"], cached["opNoToken"])
                        live_op_yes = op_book.get("yes_best_ask")
                        live_op_no  = op_book.get("no_best_ask")

                    # 4. Decide price source: live preferred, SSE cached as fallback
                    if (live_poly_yes is not None and live_poly_no is not None
                            and live_op_yes is not None and live_op_no is not None):
                        poly_yes = live_poly_yes
                        poly_no  = live_poly_no
                        op_yes   = live_op_yes
                        op_no    = live_op_no
                        using_live = True
                        live_count += 1
                    else:
                        # Fall back to SSE prices (still useful, but may be slightly stale)
                        py_raw = prices.get("polyYes")
                        pn_raw = prices.get("polyNo")
                        oy_raw = prices.get("opYes")
                        on_raw = prices.get("opNo")
                        if None in (py_raw, pn_raw, oy_raw, on_raw):
                            skipped_count += 1
                            continue
                        poly_yes = py_raw / 100.0
                        poly_no  = pn_raw / 100.0
                        op_yes   = oy_raw / 100.0
                        op_no    = on_raw / 100.0
                        fallback_count += 1

                    # 5. Calculate both routes
                    routes = []

                    if poly_yes > MIN_PRICE_THRESHOLD and op_no > MIN_PRICE_THRESHOLD:
                        cost = poly_yes + op_no
                        edge = round(1.0 - cost, 4)
                        roi  = round(edge / cost * 100, 2) if cost > 0 else 0
                        if edge > 0:
                            routes.append({
                                "route": "poly_YES + opinion_NO",
                                "cost": round(cost, 4),
                                "edge": edge,
                                "roi": roi,
                                "legs": [
                                    {"venue": "polymarket", "side": prices.get("polyYesLabel", "YES"), "price": poly_yes},
                                    {"venue": "opinion",    "side": prices.get("opNoLabel", "NO"),   "price": op_no},
                                ],
                            })

                    if op_yes > MIN_PRICE_THRESHOLD and poly_no > MIN_PRICE_THRESHOLD:
                        cost = op_yes + poly_no
                        edge = round(1.0 - cost, 4)
                        roi  = round(edge / cost * 100, 2) if cost > 0 else 0
                        if edge > 0:
                            routes.append({
                                "route": "opinion_YES + poly_NO",
                                "cost": round(cost, 4),
                                "edge": edge,
                                "roi": roi,
                                "legs": [
                                    {"venue": "opinion",    "side": prices.get("opYesLabel", "YES"),  "price": op_yes},
                                    {"venue": "polymarket", "side": prices.get("polyNoLabel", "NO"),  "price": poly_no},
                                ],
                            })

                    if not routes:
                        continue

                    best = max(routes, key=lambda r: r["edge"])
                    if best["roi"] < MIN_VERIFIED_EDGE_PCT:
                        continue

                    warnings = []
                    if not using_live:
                        warnings.append("sse_price_fallback")
                    if not is_executable:
                        warnings.append("display_only_tokens_unresolved")

                    verified.append({
                        "type": "opportunity",
                        "source": "oddscreeners",
                        "pairId": pair_id,
                        "title": pair.get("title", ""),
                        "polyTitle": pair.get("polyTitle", ""),
                        "opinionTitle": pair.get("opinionTitle", ""),
                        "polyUrl": pair.get("polyUrl", ""),
                        "opinionUrl": pair.get("opinionUrl", ""),
                        "polySlug": poly_slug,
                        "label": pair_label,
                        "expiryTs": expiry,
                        "minCost": best["cost"],
                        "edge": best["edge"],
                        "roi": best["roi"],
                        "route": best["route"],
                        "legs": best["legs"],
                        "strategy": pair.get("strategy", []),
                        "arbPctSignal": pair.get("arbPct", 0),
                        "similarity": pair.get("similarity", 0),
                        "sizes": pair.get("sizes", {}),
                        "priceSource": "live_orderbook" if using_live else "sse_cache",
                        "updatedTs": int(time.time()),
                        "warnings": warnings,
                        # Resolved CLOB token IDs — present only when is_display_only=False.
                        # Executors must check is_display_only before placing orders.
                        "is_display_only": not is_executable,
                        "polyYesToken": cached.get("polyYesToken"),
                        "polyNoToken":  cached.get("polyNoToken"),
                        "opYesToken":   cached.get("opYesToken"),
                        "opNoToken":    cached.get("opNoToken"),
                    })

                except Exception as e:
                    log(f"⚠️ Verify error for {pair.get('id', '?')}: {e}")

            verified.sort(key=lambda x: x.get("edge", 0), reverse=True)
            oddscreeners_store.set_verified(verified)
            executable_count = sum(1 for v in verified if not v.get("is_display_only"))
            display_only_count = len(verified) - executable_count
            log(
                f"✅ Verified {len(verified)} opps from {len(pairs)} pairs "
                f"(executable={executable_count} display_only={display_only_count} "
                f"live={live_count} fallback={fallback_count} skipped={skipped_count})"
            )

        except Exception as e:
            log(f"❌ Verifier error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(VERIFY_INTERVAL_SECONDS)


_started = False


def start_oddscreeners():
    global _started
    if _started:
        return
    _started = True

    collector_thread = threading.Thread(target=_run_sse_collector, daemon=True, name="oddscreeners-sse")
    collector_thread.start()
    log("SSE collector thread started")

    watchdog_thread = threading.Thread(target=_run_heartbeat_watchdog, daemon=True, name="oddscreeners-watchdog")
    watchdog_thread.start()
    log("Heartbeat watchdog thread started")

    verifier_thread = threading.Thread(target=_run_verifier, daemon=True, name="oddscreeners-verifier")
    verifier_thread.start()
    log("Verifier thread started")
