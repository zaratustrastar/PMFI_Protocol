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
    from .adapters.polymarket import get_best_prices as poly_best_prices
    from .adapters.opinion import get_best_prices as opinion_best_prices

    while True:
        try:
            pairs = oddscreeners_store.get_all_pairs()
            if not pairs:
                time.sleep(VERIFY_INTERVAL_SECONDS)
                continue

            now = int(time.time())
            near_expiry_cutoff = now + NEAR_EXPIRY_HOURS * 3600

            verified = []
            for pair in pairs:
                try:
                    expiry = pair.get("expiryTs", 0)
                    if expiry > 0 and expiry < near_expiry_cutoff:
                        continue

                    prices = pair.get("prices", {})
                    poly_yes_price = prices.get("polyYes")
                    poly_no_price = prices.get("polyNo")
                    op_yes_price = prices.get("opYes")
                    op_no_price = prices.get("opNo")

                    if poly_yes_price is None or poly_no_price is None:
                        continue
                    if op_yes_price is None or op_no_price is None:
                        continue

                    poly_yes = poly_yes_price / 100.0
                    poly_no = poly_no_price / 100.0
                    op_yes = op_yes_price / 100.0
                    op_no = op_no_price / 100.0

                    routes = []

                    if poly_yes > MIN_PRICE_THRESHOLD and op_no > MIN_PRICE_THRESHOLD:
                        cost = poly_yes + op_no
                        edge = round(1.0 - cost, 4)
                        roi = round(edge / cost * 100, 2) if cost > 0 else 0
                        if edge > 0:
                            routes.append({
                                "route": "poly_YES + opinion_NO",
                                "cost": round(cost, 4),
                                "edge": edge,
                                "roi": roi,
                                "legs": [
                                    {"venue": "polymarket", "side": prices.get("polyYesLabel", "YES"), "price": poly_yes},
                                    {"venue": "opinion", "side": prices.get("opNoLabel", "NO"), "price": op_no},
                                ],
                            })

                    if op_yes > MIN_PRICE_THRESHOLD and poly_no > MIN_PRICE_THRESHOLD:
                        cost = op_yes + poly_no
                        edge = round(1.0 - cost, 4)
                        roi = round(edge / cost * 100, 2) if cost > 0 else 0
                        if edge > 0:
                            routes.append({
                                "route": "opinion_YES + poly_NO",
                                "cost": round(cost, 4),
                                "edge": edge,
                                "roi": roi,
                                "legs": [
                                    {"venue": "opinion", "side": prices.get("opYesLabel", "YES"), "price": op_yes},
                                    {"venue": "polymarket", "side": prices.get("polyNoLabel", "NO"), "price": poly_no},
                                ],
                            })

                    if not routes:
                        continue

                    best = max(routes, key=lambda r: r["edge"])
                    if best["roi"] < MIN_VERIFIED_EDGE_PCT:
                        continue

                    poly_url = pair.get("polyUrl", "")
                    opinion_url = pair.get("opinionUrl", "")

                    verified.append({
                        "type": "opportunity",
                        "source": "oddscreeners",
                        "pairId": pair.get("id", ""),
                        "title": pair.get("title", ""),
                        "polyTitle": pair.get("polyTitle", ""),
                        "opinionTitle": pair.get("opinionTitle", ""),
                        "polyUrl": poly_url,
                        "opinionUrl": opinion_url,
                        "expiryTs": pair.get("expiryTs", 0),
                        "minCost": best["cost"],
                        "edge": best["edge"],
                        "roi": best["roi"],
                        "route": best["route"],
                        "legs": best["legs"],
                        "strategy": pair.get("strategy", []),
                        "arbPctSignal": pair.get("arbPct", 0),
                        "similarity": pair.get("similarity", 0),
                        "sizes": pair.get("sizes", {}),
                        "updatedTs": int(time.time()),
                        "warnings": [],
                    })

                except Exception as e:
                    log(f"⚠️ Verify error for {pair.get('id', '?')}: {e}")

            verified.sort(key=lambda x: x.get("edge", 0), reverse=True)
            oddscreeners_store.set_verified(verified)
            log(f"Verified {len(verified)} opportunities from {len(pairs)} pairs")

        except Exception as e:
            log(f"❌ Verifier error: {e}")

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
