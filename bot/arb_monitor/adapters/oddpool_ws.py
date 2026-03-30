"""Oddpool WebSocket adapter — streams CLOB token IDs and live book data.

Connects to wss://feeds.oddpool.com/ws, authenticates, subscribes to
book:{event_id}:{outcome_key} channels, and collects YES + NO token IDs
alongside live bid/ask prices and depth for up to 10 events (Pro tier limit).

This eliminates the 3-hop Oddpool REST → Gamma → CLOB chain used by the
slug-based token resolver. The WS delivers CLOB token_id in every message,
so one network call replaces three sequential ones.

Message shape (from Oddpool docs):
  {
    "event_key": "fomc-2026-04-29",
    "outcome": "hold",
    "venue": "polymarket",
    "token": "yes",                       # "yes" or "no"
    "venue_id": {
      "condition_id": "0x36e8ca2...",
      "token_id": "63586620628..."         # ← Polymarket CLOB token ID
    },
    "update_type": "snapshot"|"delta",
    "best_bid": "0.955",
    "best_ask": "0.965",
    "mid": "0.960",
    "bid_depth_usd": 47141.97,
    "ask_depth_usd": 23456.78,
    "levels": [{"side": "bid|ask", "price": "0.955", "size": 8300.00}, ...]
  }
"""

import asyncio
import json
import time
from typing import Optional

def log(msg: str):
    print(f"📡 [Arb/OddpoolWS] {msg}")


WS_MAX_EVENTS = 10  # Oddpool Pro tier: max 10 concurrent events per connection

# Per-scan-cycle in-memory cache to avoid reconnecting within the same cycle
_WS_CACHE: dict = {}          # (event_id, outcome_key) → book data
_WS_CACHE_TS: float = 0.0
_WS_CACHE_TTL: float = 50.0  # seconds; slightly less than 60-second cron interval


async def _async_fetch_book_snapshots(
    event_outcomes: list[tuple[str, str]],
    api_key: str,
    ws_url: str,
    timeout: float,
) -> dict:
    """Internal async implementation — connects, auths, subscribes, collects."""
    import websockets  # deferred import so module loads even if websockets absent

    results: dict[tuple[str, str], dict] = {}

    if not api_key:
        log("⚠️ No ODDPOOL_API_KEY — skipping WS fetch (set ODDPOOL_API_KEY in .env)")
        return results

    # Deduplicate and cap at Pro tier limit
    seen: set = set()
    pairs: list[tuple[str, str]] = []
    for eid, okey in event_outcomes:
        k = (eid, okey)
        if k not in seen and eid:
            seen.add(k)
            pairs.append(k)
            if len(pairs) >= WS_MAX_EVENTS:
                break

    if not pairs:
        return results

    channels = [f"book:{eid}:{okey}" for eid, okey in pairs]
    pending: dict[tuple[str, str], dict] = {k: {} for k in pairs}

    try:
        async with websockets.connect(
            ws_url,
            open_timeout=8,
            close_timeout=3,
            ping_interval=None,
        ) as ws:
            # ── Step 1: Authenticate ─────────────────────────────────────────
            await ws.send(json.dumps({"action": "auth", "api_key": api_key}))
            try:
                auth_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                auth_resp = json.loads(auth_raw)
                tier = auth_resp.get("tier") or auth_resp.get("plan") or "unknown"
                log(f"🔑 Authenticated — tier={tier!r}")
            except asyncio.TimeoutError:
                log("⚠️ No auth response received within 5s — proceeding anyway")
            except Exception as e:
                log(f"⚠️ Auth parse error: {e}")

            # ── Step 2: Subscribe ────────────────────────────────────────────
            await ws.send(json.dumps({"action": "subscribe", "channels": channels}))
            log(f"📬 Subscribed to {len(channels)} book channels")

            # ── Step 3: Collect snapshots ────────────────────────────────────
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout

            while loop.time() < deadline:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break

                # Early exit if all pairs have both YES and NO tokens
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

                venue = msg.get("venue", "")
                if venue != "polymarket":
                    continue  # only care about Poly token IDs

                eid = msg.get("event_key", "")
                okey = msg.get("outcome", "")
                token_side = (msg.get("token") or "").lower()  # "yes" or "no"
                k = (eid, okey)

                if k not in pending:
                    continue

                venue_id = msg.get("venue_id") or {}
                token_id = str(venue_id.get("token_id", "")).strip()
                if not token_id:
                    continue

                best_ask = _safe_float(msg.get("best_ask"))
                best_bid = _safe_float(msg.get("best_bid"))
                ask_depth = _safe_float(msg.get("ask_depth_usd"))
                bid_depth = _safe_float(msg.get("bid_depth_usd"))
                mid = _safe_float(msg.get("mid"))

                if token_side == "yes":
                    pending[k]["yes_token_id"]  = token_id
                    pending[k]["yes_best_ask"]  = best_ask
                    pending[k]["yes_best_bid"]  = best_bid
                    pending[k]["yes_mid"]       = mid
                    # Prefer YES-side depth (we usually buy YES on Poly)
                    pending[k]["ask_depth_usd"] = ask_depth
                    pending[k]["bid_depth_usd"] = bid_depth
                elif token_side == "no":
                    pending[k]["no_token_id"]  = token_id
                    pending[k]["no_best_ask"]  = best_ask
                    pending[k]["no_best_bid"]  = best_bid
                    pending[k]["no_mid"]       = mid
                    # Only overwrite depth if YES side hasn't set it
                    pending[k].setdefault("ask_depth_usd", ask_depth)
                    pending[k].setdefault("bid_depth_usd", bid_depth)

    except Exception as e:
        log(f"⚠️ WS connection error: {e}")

    # Promote any pair with at least one token resolved
    resolved_count = 0
    for k, data in pending.items():
        yes_id = data.get("yes_token_id", "")
        no_id  = data.get("no_token_id", "")
        if yes_id or no_id:
            results[k] = data
            resolved_count += 1
            eid, okey = k
            log(
                f"📌 ws_resolved {eid}:{okey} "
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


def fetch_book_snapshots(
    event_outcomes: list[tuple[str, str]],
    api_key: str,
    ws_url: str = "wss://feeds.oddpool.com/ws",
    timeout: float = 12.0,
) -> dict:
    """Fetch live book data for a batch of (event_id, outcome_key) pairs.

    Connects to the Oddpool WebSocket, authenticates, subscribes to
    book:{event_id}:{outcome_key} channels, and returns a dict keyed by
    (event_id, outcome_key) containing:

      yes_token_id   — Polymarket CLOB YES token ID (str)
      no_token_id    — Polymarket CLOB NO token ID (str)
      yes_best_ask   — live YES ask price from Oddpool (float)
      no_best_ask    — live NO ask price from Oddpool (float)
      yes_best_bid   — live YES bid price (float)
      no_best_bid    — live NO bid price (float)
      ask_depth_usd  — total ask-side depth in USD for this outcome (float)
      bid_depth_usd  — total bid-side depth in USD for this outcome (float)

    Returns empty dict on any connection/auth failure — callers fall back to
    the Gamma slug-resolution path in that case.
    """
    global _WS_CACHE, _WS_CACHE_TS

    now = time.time()
    # Return the cycle-level cache if it's fresh enough
    if _WS_CACHE and (now - _WS_CACHE_TS) < _WS_CACHE_TTL:
        # Filter the cache to just the requested pairs
        cached_result = {k: v for k, v in _WS_CACHE.items() if k in set(event_outcomes)}
        if len(cached_result) == len([p for p in event_outcomes if p in _WS_CACHE]):
            log(f"♻️ WS cache hit for {len(cached_result)} pairs")
            return cached_result

    try:
        result = asyncio.run(
            _async_fetch_book_snapshots(event_outcomes, api_key, ws_url, timeout)
        )
    except RuntimeError as e:
        # asyncio.run() fails if there's already a running event loop
        log(f"⚠️ asyncio.run failed ({e}) — trying existing event loop")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                _async_fetch_book_snapshots(event_outcomes, api_key, ws_url, timeout)
            )
        except Exception as e2:
            log(f"⚠️ Event loop fallback also failed: {e2}")
            return {}
    except Exception as e:
        log(f"⚠️ fetch_book_snapshots error: {e}")
        return {}

    # Update cycle-level cache
    _WS_CACHE.update(result)
    _WS_CACHE_TS = now
    return result


def invalidate_cache() -> None:
    """Clear the cycle-level WS cache. Call at the start of each scan cycle."""
    global _WS_CACHE, _WS_CACHE_TS
    _WS_CACHE.clear()
    _WS_CACHE_TS = 0.0
