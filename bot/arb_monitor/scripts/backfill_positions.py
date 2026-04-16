"""One-time backfill script: populate arb_positions + arb_executions
from live Polymarket and Kalshi portfolio APIs.

Usage (run once on VPS after git pull):
    python -m bot.arb_monitor.scripts.backfill_positions

The script:
  1. Fetches all open Polymarket positions for POLY_PROXY_ADDRESS
  2. Fetches all open Kalshi positions via RSA-authenticated portfolio API
  3. Prints a side-by-side table and prompts you to confirm each pairing
  4. Inserts confirmed pairs into arb_positions + arb_executions

Confirmed pairs use the Polymarket position's avg_price as entry price for
the Poly leg and the Kalshi position's avg_price as entry price for the
Kalshi leg. Shares are set to the Polymarket position size.
"""

import os
import sys
import json
import time
import difflib
import requests

POLY_CLOB_URL  = os.environ.get("POLY_CLOB_URL",  "https://clob.polymarket.com")
POLY_GAMMA_URL = os.environ.get("POLY_GAMMA_URL", "https://gamma-api.polymarket.com")
PROXY_ADDRESS  = os.environ.get("POLY_PROXY_ADDRESS", "")


def log(msg: str):
    print(f"[backfill] {msg}", flush=True)


# ── Polymarket positions ──────────────────────────────────────────────────────

def fetch_poly_positions() -> list[dict]:
    """GET /data/positions?user=ADDRESS — returns all open Poly positions."""
    if not PROXY_ADDRESS:
        log("⚠️ POLY_PROXY_ADDRESS not set")
        return []
    url = f"{POLY_CLOB_URL}/data/positions"
    params = {"user": PROXY_ADDRESS, "sizeThreshold": "0.001"}
    log(f"Fetching Polymarket positions for {PROXY_ADDRESS[:16]}…")
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            log(f"⚠️ Poly positions HTTP {resp.status_code}: {resp.text[:120]}")
            return []
        data = resp.json()
        positions = data if isinstance(data, list) else data.get("data", [])
        log(f"  → {len(positions)} Poly position(s) returned")
        return positions
    except Exception as e:
        log(f"❌ Error fetching Poly positions: {e}")
        return []


def fetch_gamma_market(token_id: str) -> dict:
    """Look up market metadata by token (asset) id from Gamma API."""
    try:
        resp = requests.get(
            f"{POLY_GAMMA_URL}/markets",
            params={"clob_token_ids": token_id},
            timeout=10,
        )
        if resp.status_code == 200:
            items = resp.json()
            return items[0] if items else {}
    except Exception as e:
        log(f"⚠️ Gamma lookup error for {token_id[:16]}: {e}")
    return {}


# ── Kalshi positions ──────────────────────────────────────────────────────────

def fetch_kalshi_positions() -> list[dict]:
    """GET /portfolio/positions with RSA auth."""
    try:
        from bot.arb_monitor.core.kalshi_auth import get_kalshi_headers, kalshi_auth_available
        from bot.arb_monitor.config import KALSHI_BASE_URL
    except ImportError:
        log("⚠️ kalshi_auth not importable — check PYTHONPATH")
        return []

    if not kalshi_auth_available():
        log("⚠️ Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)")
        return []

    url = f"{KALSHI_BASE_URL}/portfolio/positions"
    headers = get_kalshi_headers("GET", url)
    if not headers:
        log("⚠️ Kalshi RSA signing failed")
        return []
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            log(f"⚠️ Kalshi positions HTTP {resp.status_code}: {resp.text[:120]}")
            return []
        data = resp.json()
        positions = data.get("market_positions", data.get("positions", data if isinstance(data, list) else []))
        # Keep only positions with non-zero holdings
        open_pos = [p for p in positions if (p.get("position", 0) != 0 or p.get("yes_position", 0) != 0 or p.get("no_position", 0) != 0)]
        log(f"  → {len(open_pos)} open Kalshi position(s)")
        return open_pos
    except Exception as e:
        log(f"❌ Error fetching Kalshi positions: {e}")
        return []


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    import psycopg2
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url)


def insert_pair(
    pair_id: str,
    poly_yes_token: str,
    poly_no_token: str,
    kalshi_ticker: str,
    kalshi_side: str,
    shares: float,
    cost_usdc: float,
    expiry_ts: int,
    poly_title: str,
    kalshi_title: str,
    poly_price: float,
    kalshi_price: float,
):
    try:
        from bot.arb_monitor.core.arb_positions_db import upsert_position, get_db_conn
        from bot.arb_monitor.core.arb_executor import log_execution_to_db
    except ImportError:
        log("⚠️ Cannot import arb_positions_db / arb_executor — check PYTHONPATH")
        return False

    ok1 = upsert_position(
        pair_id=pair_id,
        poly_yes_token=poly_yes_token,
        poly_no_token=poly_no_token,
        kalshi_ticker=kalshi_ticker,
        kalshi_side=kalshi_side,
        shares=shares,
        cost_basis_usdc=cost_usdc,
        expiry_ts=expiry_ts,
        poly_title=poly_title,
        kalshi_title=kalshi_title,
    )
    log_execution_to_db(
        pair_id=pair_id, leg=1, venue="polymarket", side="YES_BUY",
        price=poly_price, size=shares, success=True, error="", order_id="backfill",
    )
    log_execution_to_db(
        pair_id=pair_id, leg=2, venue="kalshi", side=f"{kalshi_side}_BUY",
        price=kalshi_price, size=shares, success=True, error="", order_id="backfill",
    )
    log(f"{'✅' if ok1 else '⚠️'} Inserted pair_id={pair_id} shares={shares:.4f}")
    return ok1


# ── Matching helpers ──────────────────────────────────────────────────────────

def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def best_kalshi_match(poly_title: str, kalshi_positions: list[dict]) -> tuple[dict | None, float]:
    best, best_score = None, 0.0
    for kp in kalshi_positions:
        title = kp.get("market_title", kp.get("ticker", ""))
        score = title_similarity(poly_title, title)
        if score > best_score:
            best, best_score = kp, score
    return best, best_score


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== pARB position backfill ===")

    poly_positions = fetch_poly_positions()
    kalshi_positions = fetch_kalshi_positions()

    if not poly_positions:
        log("No Polymarket positions found — nothing to backfill")
        return

    log(f"\n{'─'*70}")
    log(f"Found {len(poly_positions)} Poly position(s) and {len(kalshi_positions)} Kalshi position(s)")
    log(f"{'─'*70}\n")

    confirmed_pairs = []
    used_kalshi = set()

    for pp in poly_positions:
        token_id    = pp.get("asset_id", pp.get("token_id", ""))
        poly_size   = float(pp.get("size", pp.get("shares", 0)))
        poly_price  = float(pp.get("avg_price", pp.get("average_price", 0)))
        poly_side   = pp.get("outcome", pp.get("side", "YES")).upper()

        if poly_size < 0.001:
            continue

        # Fetch market title from Gamma
        log(f"Looking up market for token {token_id[:20]}…")
        market = fetch_gamma_market(token_id)
        poly_title    = market.get("question", market.get("title", token_id[:40]))
        poly_yes_token = market.get("clobTokenIds", [token_id])[0] if market else token_id
        poly_no_token  = market.get("clobTokenIds", ["", ""])[1] if len(market.get("clobTokenIds", [])) > 1 else ""
        expiry_ts     = int(market.get("endDateIso", "0").replace("-","").replace("T","").replace(":","").replace("Z","")[:10]) if market.get("endDateIso") else 0
        try:
            import datetime
            dt = market.get("endDateIso", "")
            if dt:
                expiry_ts = int(datetime.datetime.fromisoformat(dt.rstrip("Z")).timestamp())
        except Exception:
            expiry_ts = 0

        log(f"\nPoly position: {poly_title[:60]}")
        log(f"  token={token_id[:20]}  side={poly_side}  size={poly_size:.4f}  avg_price={poly_price:.4f}")

        # Try auto-match
        best_k, score = best_kalshi_match(poly_title, [kp for kp in kalshi_positions if id(kp) not in used_kalshi])
        if best_k:
            k_title  = best_k.get("market_title", best_k.get("ticker", "?"))
            k_ticker = best_k.get("ticker", "")
            k_yes    = int(best_k.get("yes_position", best_k.get("position", 0)))
            k_no     = int(best_k.get("no_position", 0))
            k_side   = "YES" if k_yes > 0 else "NO"
            k_size   = max(k_yes, k_no)
            k_price  = float(best_k.get("avg_price", 0)) / 100.0 if best_k.get("avg_price", 0) > 1 else float(best_k.get("avg_price", 0))
            log(f"  Best Kalshi match (score={score:.2f}): {k_title[:60]}")
            log(f"    ticker={k_ticker}  side={k_side}  size={k_size}  avg_price={k_price:.4f}")
        else:
            log("  No Kalshi match found automatically")
            k_title = k_ticker = k_side = ""
            k_size = k_price = 0

        log("")
        log("Kalshi positions available:")
        for i, kp in enumerate(kalshi_positions):
            if id(kp) in used_kalshi:
                continue
            kt = kp.get("market_title", kp.get("ticker", "?"))
            ky = int(kp.get("yes_position", kp.get("position", 0)))
            kn = int(kp.get("no_position", 0))
            log(f"  [{i}] {kt[:60]}  YES={ky}  NO={kn}")

        choice = input(
            f"\nEnter Kalshi index to pair with this Poly position "
            f"(default={list(kalshi_positions).index(best_k) if best_k and best_k in kalshi_positions else '?'}, "
            f"s=skip): "
        ).strip().lower()

        if choice == "s":
            log("Skipping this position.")
            continue

        if choice == "" and best_k:
            selected_k = best_k
        else:
            try:
                idx = int(choice)
                selected_k = kalshi_positions[idx]
            except (ValueError, IndexError):
                log("Invalid choice — skipping")
                continue

        used_kalshi.add(id(selected_k))

        k_title  = selected_k.get("market_title", selected_k.get("ticker", ""))
        k_ticker = selected_k.get("ticker", "")
        k_yes    = int(selected_k.get("yes_position", selected_k.get("position", 0)))
        k_no     = int(selected_k.get("no_position", 0))
        k_side   = "YES" if k_yes > 0 else "NO"
        k_size   = max(k_yes, k_no)
        k_price_raw = float(selected_k.get("avg_price", 0))
        k_price  = k_price_raw / 100.0 if k_price_raw > 1 else k_price_raw

        # Use Poly position size; Kalshi should match but use min to be safe
        shares = min(poly_size, float(k_size)) if k_size > 0 else poly_size
        cost_usdc = shares * (poly_price + k_price)

        # pair_id: deterministic from sorted token + ticker
        pair_id = f"backfill_{token_id[:12]}_{k_ticker}"

        confirmed_pairs.append({
            "pair_id": pair_id,
            "poly_yes_token": poly_yes_token,
            "poly_no_token": poly_no_token,
            "kalshi_ticker": k_ticker,
            "kalshi_side": k_side,
            "shares": shares,
            "cost_usdc": cost_usdc,
            "expiry_ts": expiry_ts,
            "poly_title": poly_title,
            "kalshi_title": k_title,
            "poly_price": poly_price,
            "kalshi_price": k_price,
        })

    if not confirmed_pairs:
        log("\nNo pairs confirmed — nothing inserted.")
        return

    log(f"\n{'─'*70}")
    log(f"About to insert {len(confirmed_pairs)} pair(s) into arb_positions / arb_executions:")
    for p in confirmed_pairs:
        log(f"  pair_id={p['pair_id']}")
        log(f"    Poly:  {p['poly_title'][:55]}  shares={p['shares']:.4f}  price={p['poly_price']:.4f}")
        log(f"    Kalshi: {p['kalshi_title'][:55]}  side={p['kalshi_side']}  price={p['kalshi_price']:.4f}")
    log(f"{'─'*70}")

    confirm = input("\nProceed? [y/N]: ").strip().lower()
    if confirm != "y":
        log("Aborted.")
        return

    for p in confirmed_pairs:
        insert_pair(**p)

    log(f"\n✅ Backfill complete — {len(confirmed_pairs)} pair(s) inserted.")
    log("Restart the Flask app on the VPS so the NAV endpoint picks up the new rows.")


if __name__ == "__main__":
    main()
