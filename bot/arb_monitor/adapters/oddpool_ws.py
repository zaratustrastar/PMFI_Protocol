"""Oddpool WebSocket adapter — streams CLOB token IDs and live book data.

Design fixes applied (vs. the v1 implementation):

  Bug 1 — event_id ≠ event_key: REST returns integer event_id (e.g. 42); WS
  channels require slug event_key (e.g. "fomc-2026-04-29"). Fixed by fetching
  /websocket/catalog (5-min cache) to build id→key lookup, with title-
  normalization as fallback when catalog lacks integer cross-references.

  Bug 2 — outcome_key ≠ WS outcome slug: REST "yes"/"no" ≠ WS slug "hold"/"25bps_cut".
  Fixed by subscribing to book:{event_key} (no outcome suffix — returns all
  outcomes) and matching incoming msg["outcome"] against normalize_label(entry["label"]).

  Bug 3 — asyncio.run() conflict: crashes if called from an async context.
  Fixed via threading.Thread(daemon=True) with queue.Queue — the thread owns
  an isolated event loop, the caller just joins with a timeout.

WS message shape:
  {
    "event_key": "fomc-2026-04-29",
    "outcome": "hold",
    "venue": "polymarket",
    "token": "yes",
    "venue_id": {"condition_id": "0x...", "token_id": "636..."},
    "update_type": "snapshot"|"delta",
    "best_bid": "0.955", "best_ask": "0.965", "mid": "0.960",
    "bid_depth_usd": 47141.97, "ask_depth_usd": 23456.78
  }
"""

import asyncio
import json
import re
import queue as _queue
import threading
import time
from typing import Optional

import requests as _requests

def log(msg: str):
    print(f"📡 [Arb/OddpoolWS] {msg}")


WS_MAX_EVENTS = 10  # Oddpool Pro tier: max 10 concurrent events per connection

# ── Cycle-level book cache (keyed by (event_key, label_normalized)) ────────
_WS_CACHE: dict = {}
_WS_CACHE_TS: float = 0.0
_WS_CACHE_TTL: float = 50.0

# ── Catalog cache (event_id → event_key lookup) ─────────────────────────────
_CATALOG_CACHE: dict = {}   # int_id → event_key str
_CATALOG_TS: float = 0.0
_CATALOG_TTL: float = 300.0  # 5 minutes


# ── Label / title normalization ───────────────────────────────────────────────

def normalize_label(s: str) -> str:
    """Normalize an outcome label for matching against WS outcome slugs.

    Examples: "25bps Cut" → "25bps_cut", "Hold" → "hold", "Yes" → "yes"
    Lowercases, replaces any non-alphanumeric run with a single underscore,
    strips leading/trailing underscores.
    """
    s = str(s).lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def normalize_title(s: str) -> str:
    """Convert an event title to a candidate event_key slug.

    Example: "FOMC May 2026" → "fomc-may-2026"
    Used as a last-resort fallback when the catalog doesn't cross-reference
    the integer event_id.
    """
    s = str(s).lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── Catalog lookup ────────────────────────────────────────────────────────────

def _fetch_catalog(api_key: str, base_url: str) -> dict:
    """Fetch /websocket/catalog and return {int_event_id: event_key_str}.

    Caches the result for 5 minutes. Returns empty dict on any failure.
    Logs the catalog response shape on first fetch for diagnostics.
    """
    global _CATALOG_CACHE, _CATALOG_TS
    now = time.time()
    if _CATALOG_CACHE and (now - _CATALOG_TS) < _CATALOG_TTL:
        return _CATALOG_CACHE

    url = f"{base_url}/websocket/catalog"
    headers = {"accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        resp = _requests.get(url, headers=headers, timeout=8)
        log(f"🗂 Catalog HTTP {resp.status_code} — {url}")
        if resp.status_code != 200:
            log(f"⚠️ Catalog non-200: {resp.text[:200]}")
            return {}
        data = resp.json()
        # Log the shape for diagnostics (first fetch only)
        if not _CATALOG_CACHE:
            if isinstance(data, list) and data:
                log(f"🗂 Catalog shape: list[{len(data)}], first entry keys: {sorted((data[0] if isinstance(data[0], dict) else {}).keys())}")
            elif isinstance(data, dict):
                log(f"🗂 Catalog shape: dict, root keys: {sorted(data.keys())}")
        # Build id → key mapping — try several common field names
        mapping: dict = {}
        entries = data if isinstance(data, list) else data.get("events", data.get("data", []))
        if not isinstance(entries, list):
            log(f"⚠️ Catalog: unexpected structure — {type(data).__name__}, keys={sorted(data.keys()) if isinstance(data, dict) else 'n/a'}")
            return {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("event_id") or item.get("id")
            raw_key = item.get("event_key") or item.get("key") or item.get("slug")
            if raw_id is not None and raw_key:
                try:
                    mapping[int(raw_id)] = str(raw_key)
                except (ValueError, TypeError):
                    pass
        log(f"🗂 Catalog built: {len(mapping)} id→key entries")
        _CATALOG_CACHE = mapping
        _CATALOG_TS = now
        return mapping
    except Exception as e:
        log(f"⚠️ Catalog fetch error: {e}")
        return {}


def resolve_event_key(
    event_id,
    event_title: str,
    api_key: str,
    base_url: str = "https://api.oddpool.com",
) -> str:
    """Resolve an Oddpool REST event_id to its WS event_key string.

    Priority:
      1. Catalog lookup (integer id → event_key)
      2. event_title normalization as last resort (fragile but better than nothing)

    Always returns a non-empty string; callers should log the result.
    """
    try:
        int_id = int(event_id)
        catalog = _fetch_catalog(api_key, base_url)
        if catalog and int_id in catalog:
            return catalog[int_id]
    except (ValueError, TypeError):
        pass
    # Fallback: normalize the title
    fallback = normalize_title(event_title)
    log(
        f"⚠️ event_id={event_id!r} not in catalog — "
        f"using title fallback: {event_title!r} → {fallback!r}"
    )
    return fallback or str(event_id)


# ── Async WS core ──────────────────────────────────────────────────────────────

async def _async_fetch_book_snapshots(
    event_label_pairs: list[tuple[str, str]],
    api_key: str,
    ws_url: str,
    timeout: float,
) -> dict:
    """Subscribe to book:{event_key} channels (no outcome suffix) and collect
    token IDs for each (event_key, label_normalized) pair.

    Subscribing without an outcome suffix returns *all* outcomes for the event.
    We match incoming messages to the correct pair by comparing
    normalize_label(msg["outcome"]) against each pair's label_normalized.

    Returns dict keyed by (event_key, label_normalized) containing:
      yes_token_id, no_token_id, yes_best_ask, no_best_ask,
      yes_best_bid, no_best_bid, ask_depth_usd, bid_depth_usd
    """
    import websockets

    results: dict[tuple[str, str], dict] = {}

    if not api_key:
        log("⚠️ No ODDPOOL_API_KEY — skipping WS fetch")
        return results

    # Deduplicate pairs, cap at Pro tier limit
    seen: set = set()
    pairs: list[tuple[str, str]] = []
    for ekey, lbl in event_label_pairs:
        k = (ekey, lbl)
        if k not in seen and ekey:
            seen.add(k)
            pairs.append(k)
            if len(pairs) >= WS_MAX_EVENTS:
                break

    if not pairs:
        return results

    # Unique event_key channels (one channel per event, no outcome suffix)
    unique_event_keys = list(dict.fromkeys(ekey for ekey, _ in pairs))
    channels = [f"book:{ekey}" for ekey in unique_event_keys]

    # pending: (event_key, label_normalized) → partial book data
    pending: dict[tuple[str, str], dict] = {k: {} for k in pairs}

    # Reverse map: event_key → list of (event_key, label_normalized) pairs
    # for fast per-message dispatch
    ekey_to_pairs: dict[str, list[tuple[str, str]]] = {}
    for ekey, lbl in pairs:
        ekey_to_pairs.setdefault(ekey, []).append((ekey, lbl))

    try:
        async with websockets.connect(
            ws_url,
            open_timeout=8,
            close_timeout=3,
            ping_interval=None,
        ) as ws:
            # Step 1: Authenticate
            await ws.send(json.dumps({"action": "auth", "api_key": api_key}))
            try:
                auth_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                auth_resp = json.loads(auth_raw)
                tier = auth_resp.get("tier") or auth_resp.get("plan") or "unknown"
                log(f"🔑 Authenticated — tier={tier!r}")
            except asyncio.TimeoutError:
                log("⚠️ No auth response within 5s — proceeding")
            except Exception as e:
                log(f"⚠️ Auth parse error: {e}")

            # Step 2: Subscribe — one channel per unique event_key
            await ws.send(json.dumps({"action": "subscribe", "channels": channels}))
            log(f"📬 Subscribed to {len(channels)} book channels (no outcome suffix)")

            # Step 3: Collect — early exit when all pairs resolved
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout

            while loop.time() < deadline:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break

                complete = sum(
                    1 for v in pending.values()
                    if v.get("yes_token_id") and v.get("no_token_id")
                )
                if complete == len(pairs):
                    log(f"✅ All {len(pairs)} pairs fully resolved — early exit")
                    break

                try:
                    msg_raw = await asyncio.wait_for(
                        ws.recv(), timeout=min(remaining, 2.0)
                    )
                except asyncio.TimeoutError:
                    continue

                try:
                    msg = json.loads(msg_raw)
                except Exception:
                    continue

                # We intentionally accept both update_type="snapshot" and
                # update_type="delta": venue_id.token_id is present in every
                # message from the WS feed regardless of update type.  We only
                # need the token ID and price data, not a fully reconstructed
                # book; filtering to snapshot-only would mean waiting up to
                # 60 s for the next snapshot cycle and would be unnecessarily slow.
                if msg.get("venue") != "polymarket":
                    continue

                msg_ekey       = msg.get("event_key", "")
                msg_outcome    = msg.get("outcome", "")
                msg_outcome_n  = normalize_label(msg_outcome)
                token_side     = (msg.get("token") or "").lower()

                # Match to the pending pair whose label_normalized == msg_outcome_n
                candidates = ekey_to_pairs.get(msg_ekey, [])
                matched_k: Optional[tuple] = None
                for k in candidates:
                    _, lbl_n = k
                    # Exact normalized match; also accept "yes"/"no" REST keys
                    # matching when label is empty or a generic yes/no synonym
                    if lbl_n == msg_outcome_n:
                        matched_k = k
                        break
                    # Loose fallback: if our label is "yes"/"no" and the WS
                    # token side matches, accept (binary markets only)
                    if lbl_n in ("yes", "no") and lbl_n == token_side:
                        matched_k = k
                        break

                if matched_k is None:
                    continue

                venue_id = msg.get("venue_id") or {}
                token_id = str(venue_id.get("token_id", "")).strip()
                if not token_id:
                    continue

                best_ask  = _safe_float(msg.get("best_ask"))
                best_bid  = _safe_float(msg.get("best_bid"))
                ask_depth = _safe_float(msg.get("ask_depth_usd"))
                bid_depth = _safe_float(msg.get("bid_depth_usd"))
                mid       = _safe_float(msg.get("mid"))

                if token_side == "yes":
                    pending[matched_k]["yes_token_id"]  = token_id
                    pending[matched_k]["yes_best_ask"]  = best_ask
                    pending[matched_k]["yes_best_bid"]  = best_bid
                    pending[matched_k]["yes_mid"]       = mid
                    pending[matched_k]["ask_depth_usd"] = ask_depth
                    pending[matched_k]["bid_depth_usd"] = bid_depth
                elif token_side == "no":
                    pending[matched_k]["no_token_id"]  = token_id
                    pending[matched_k]["no_best_ask"]  = best_ask
                    pending[matched_k]["no_best_bid"]  = best_bid
                    pending[matched_k]["no_mid"]       = mid
                    pending[matched_k].setdefault("ask_depth_usd", ask_depth)
                    pending[matched_k].setdefault("bid_depth_usd", bid_depth)

    except Exception as e:
        log(f"⚠️ WS connection error: {e}")

    # Collect resolved pairs
    resolved_count = 0
    for k, data in pending.items():
        yes_id = data.get("yes_token_id", "")
        no_id  = data.get("no_token_id", "")
        if yes_id or no_id:
            results[k] = data
            resolved_count += 1
            ekey, lbl = k
            log(
                f"📌 ws_resolved {ekey}:{lbl} "
                f"YES={yes_id[:16] if yes_id else 'missing'}... "
                f"NO={no_id[:16] if no_id else 'missing'}... "
                f"yes_ask={data.get('yes_best_ask', 0.0):.4f} "
                f"ask_depth=${data.get('ask_depth_usd', 0):.0f}"
            )

    log(f"📊 WS batch complete: {resolved_count}/{len(pairs)} pairs resolved")
    return results


def _safe_float(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


# ── Thread-isolated synchronous wrapper ───────────────────────────────────────

def fetch_book_snapshots(
    event_label_pairs: list[tuple[str, str]],
    api_key: str,
    ws_url: str = "wss://feeds.oddpool.com/ws",
    timeout: float = 12.0,
) -> dict:
    """Fetch live book data for a batch of (event_key, label_normalized) pairs.

    Runs the async WS logic inside a daemon thread that owns a fresh event loop,
    so this function is safe to call from both sync and async contexts.

    Returns dict keyed by (event_key, label_normalized):
      yes_token_id, no_token_id, yes_best_ask, no_best_ask,
      yes_best_bid, no_best_bid, ask_depth_usd, bid_depth_usd

    Returns empty dict on failure; callers fall back to Gamma resolver.
    """
    global _WS_CACHE, _WS_CACHE_TS

    now = time.time()
    if _WS_CACHE and (now - _WS_CACHE_TS) < _WS_CACHE_TTL:
        pair_set = set(event_label_pairs)
        cached = {k: v for k, v in _WS_CACHE.items() if k in pair_set}
        if len(cached) == len([p for p in event_label_pairs if p in _WS_CACHE]):
            log(f"♻️ WS cache hit for {len(cached)} pairs")
            return cached

    result_q: _queue.Queue = _queue.Queue()

    def _thread_run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                res = loop.run_until_complete(
                    _async_fetch_book_snapshots(
                        event_label_pairs, api_key, ws_url, timeout
                    )
                )
                result_q.put(("ok", res))
            finally:
                loop.close()
        except Exception as e:
            result_q.put(("err", e))

    t = threading.Thread(target=_thread_run, daemon=True)
    t.start()
    t.join(timeout=timeout + 8)  # extra headroom for connect + auth round-trip

    try:
        status, val = result_q.get_nowait()
    except _queue.Empty:
        log("⚠️ WS thread timed out — returning empty, Gamma fallback will run")
        return {}

    if status != "ok":
        log(f"⚠️ WS thread raised: {val}")
        return {}

    result: dict = val
    _WS_CACHE.update(result)
    _WS_CACHE_TS = now
    return result


def invalidate_cache() -> None:
    """Clear the cycle-level WS cache. Call at the start of each scan cycle."""
    global _WS_CACHE, _WS_CACHE_TS
    _WS_CACHE.clear()
    _WS_CACHE_TS = 0.0
