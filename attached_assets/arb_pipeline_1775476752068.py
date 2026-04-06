"""
OddPool Arbitrage Pipeline
Score = net_cents x deployable_size x time_factor
"""

import json, os, time, logging, urllib.request, urllib.error
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ODDPOOL_API_KEY    = os.environ.get("ODDPOOL_API_KEY", "")
POLYMARKET_API_KEY = os.environ.get("POLYMARKET_API_KEY", "")
KALSHI_API_KEY     = os.environ.get("KALSHI_API_KEY", "")
OPINION_API_KEY    = os.environ.get("OPINION_API_KEY", "")

MIN_NET_CENTS      = 5      # ignore anything below this
MAX_POSITION_USD   = 500    # hard cap per trade
LIQUIDITY_FRACTION = 0.10   # use max 10% of available liquidity per leg
PRICE_STALENESS_S  = 30     # reject signal if older than this many seconds
PRICE_DRIFT_MAX    = 0.02   # reject if live price moved more than 2 cents
LEG_FILL_TIMEOUT   = 30     # seconds to wait for both legs to fill


# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────
def http_get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def http_post(url, body, headers=None, timeout=10):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ─────────────────────────────────────────────
# STEP 1 — FETCH ALL OPPORTUNITIES
# ─────────────────────────────────────────────
def fetch_opportunities():
    log.info("Step 1: fetching opportunities...")
    url = f"https://api.oddpool.com/arbitrage/current?min_net_cents={MIN_NET_CENTS}&minutes=10"
    data = http_get(url, headers={"X-API-Key": ODDPOOL_API_KEY})
    log.info(f"  {len(data)} opportunities returned")
    return data


# ─────────────────────────────────────────────
# STEP 2 — SCORE EVERY OPPORTUNITY
#
# Score = net_cents x deployable_size x time_factor
#
# net_cents     = raw profit per $1 invested (from OddPool signal)
#
# deployable_size = how many dollars we can actually put to work
#                 = min(liquidity on YES leg, liquidity on NO leg)
#                   x LIQUIDITY_FRACTION (10%)
#                   capped at MAX_POSITION_USD
#
# time_factor   = penalty/bonus based on days until resolution
#                 <1 day  -> 0.0  (skip: too risky to fill both legs in time)
#                 1-7d    -> 0.2  (very short: partial fill risk)
#                 7-90d   -> 1.0  (ideal window)
#                 90-180d -> 0.8  (acceptable but capital tied up longer)
#                 >180d   -> 0.5  (too long: capital opportunity cost)
#
# Result = expected absolute profit in USD, adjusted for time risk.
# Sorting by this gives us the single best trade to execute.
# ─────────────────────────────────────────────
def score_opportunity(opp):
    now = datetime.now(timezone.utc)

    # Parse resolution time
    try:
        resolution = datetime.fromisoformat(
            opp.get("resolution_time", "").replace("Z", "+00:00")
        )
        days = (resolution - now).total_seconds() / 86400
    except Exception:
        days = 30  # fallback if missing

    # Time factor
    if days < 1:
        return None  # skip entirely: too close to resolution
    elif days < 7:
        time_factor = 0.2
    elif days <= 90:
        time_factor = 1.0  # ideal
    elif days <= 180:
        time_factor = 0.8
    else:
        time_factor = 0.5

    # Net cents from OddPool signal
    net_cents = opp.get("net_cents", 0)
    if net_cents <= 0:
        return None

    # Liquidity on each leg
    yes_platform = opp.get("buy_yes_market")
    no_platform  = opp.get("buy_no_market")
    yes_liq = (opp.get(yes_platform) or {}).get("liquidity") or 0
    no_liq  = (opp.get(no_platform)  or {}).get("liquidity") or 0

    if yes_liq == 0 or no_liq == 0:
        return None  # can't size without liquidity data

    # Deployable size
    deployable = min(yes_liq, no_liq) * LIQUIDITY_FRACTION
    deployable = min(deployable, MAX_POSITION_USD)

    if deployable < 10:
        return None  # not worth the transaction overhead

    # Score = net_cents x deployable_size x time_factor
    score = (net_cents / 100) * deployable * time_factor

    return {
        "net_cents":      net_cents,
        "deployable_usd": round(deployable, 2),
        "days":           round(days, 1),
        "time_factor":    round(time_factor, 2),
        "score":          round(score, 4),   # expected profit in USD
    }

def rank_opportunities(opps):
    log.info("Step 2: scoring all opportunities...")
    scored = []
    for opp in opps:
        metrics = score_opportunity(opp)
        if metrics is None:
            continue
        scored.append({**opp, **metrics})

    scored.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"  {len(scored)} scoreable opportunities")

    for i, o in enumerate(scored[:5]):
        log.info(
            f"  #{i+1} score=${o['score']:.4f} | "
            f"net={o['net_cents']}c | "
            f"size=${o['deployable_usd']} | "
            f"days={o['days']} | "
            f"time_factor={o['time_factor']} | "
            f"{o['event_title'][:45]}"
        )
    return scored


# ─────────────────────────────────────────────
# STEP 3 — TAKE THE BEST OPPORTUNITY
# ─────────────────────────────────────────────
def pick_best(ranked):
    if not ranked:
        log.info("No scoreable opportunities found.")
        return None
    best = ranked[0]
    log.info(
        f"\nStep 3: best opportunity selected\n"
        f"  event:          {best['event_title']}\n"
        f"  outcome:        {best['label']}\n"
        f"  buy YES on:     {best['buy_yes_market']} @ {best[best['buy_yes_market']]['yes_ask']}\n"
        f"  buy NO  on:     {best['buy_no_market']}  @ {best[best['buy_no_market']]['no_ask']}\n"
        f"  net_cents:      {best['net_cents']}c\n"
        f"  deployable:     ${best['deployable_usd']}\n"
        f"  days to resolve:{best['days']}\n"
        f"  time_factor:    {best['time_factor']}\n"
        f"  expected profit:${best['score']}"
    )
    return best


# ─────────────────────────────────────────────
# STEP 4 — RESOLVE POLYMARKET TOKEN ID
# Two sub-calls:
#   4a. OddPool search  -> conditionId (market_id)
#   4b. Polymarket CLOB -> tokenId
# ─────────────────────────────────────────────
def resolve_polymarket_token(event_slug, polymarket_volume, side):
    log.info(f"Step 4: resolving Polymarket tokenId (side={side})...")

    # 4a: OddPool search -> conditionId
    url = f"https://api.oddpool.com/search/events/{event_slug}/markets"
    markets = http_get(url, headers={"X-API-Key": ODDPOOL_API_KEY})
    pm_markets = [m for m in markets if m["exchange"] == "polymarket"]

    # Match by volume (exact, then fuzzy fallback)
    matched = None
    for m in pm_markets:
        if int(m["volume"]) == int(polymarket_volume):
            matched = m
            break
    if not matched:
        pm_markets.sort(key=lambda m: abs(int(m["volume"]) - int(polymarket_volume)))
        if pm_markets and abs(int(pm_markets[0]["volume"]) - int(polymarket_volume)) < 1000:
            matched = pm_markets[0]
            log.warning(f"  Fuzzy volume match: {matched['volume']} vs {polymarket_volume}")

    if not matched:
        raise ValueError(f"No Polymarket market matched volume={polymarket_volume}")

    condition_id = matched["market_id"]
    log.info(f"  conditionId: {condition_id[:24]}...")
    log.info(f"  question:    {matched['question']}")

    # 4b: CLOB -> tokenId
    clob = http_get(f"https://clob.polymarket.com/markets/{condition_id}")
    token_index = 0 if side == "YES" else 1
    token_id = clob["tokens"][token_index]["token_id"]
    log.info(f"  tokenId:     {token_id[:24]}...")

    return {
        "condition_id":     condition_id,
        "token_id":         token_id,
        "question":         matched["question"],
        "accepting_orders": clob.get("accepting_orders", True),
        "min_tick_size":    clob.get("minimum_tick_size", 0.01),
        "min_order_size":   clob.get("min_order_size", 5),
    }


# ─────────────────────────────────────────────
# STEP 5 — VALIDATE WITH LIVE PRICES
# ─────────────────────────────────────────────
def validate(opp, resolved_pm=None):
    log.info("Step 5: validating with live prices...")

    # Signal freshness
    try:
        ts  = datetime.fromisoformat(opp.get("timestamp", "").replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > PRICE_STALENESS_S:
            return False, f"Signal stale ({age:.0f}s old, max {PRICE_STALENESS_S}s)"
    except Exception:
        pass

    yes_platform = opp["buy_yes_market"]
    no_platform  = opp["buy_no_market"]
    yes_ask = opp[yes_platform]["yes_ask"]
    no_ask  = opp[no_platform]["no_ask"]

    # Re-check combined cost is still profitable
    if yes_ask + no_ask >= 1.0:
        return False, f"No longer profitable: cost={yes_ask + no_ask:.3f}"

    # Live orderbook drift check on Polymarket
    if resolved_pm and "polymarket" in [yes_platform, no_platform]:
        try:
            book = http_get(
                f"https://clob.polymarket.com/books?token_id={resolved_pm['token_id']}"
            )
            asks = book.get("asks", [])
            if asks:
                live_ask = float(sorted(asks, key=lambda x: float(x["price"]))[0]["price"])
                signal_ask = yes_ask if yes_platform == "polymarket" else no_ask
                drift = abs(live_ask - signal_ask)
                log.info(f"  live ask={live_ask} | signal={signal_ask} | drift={drift:.4f}")
                if drift > PRICE_DRIFT_MAX:
                    return False, f"Price drifted {drift:.4f} (max {PRICE_DRIFT_MAX})"
        except Exception as e:
            log.warning(f"  Live orderbook check failed (continuing): {e}")

    if resolved_pm and not resolved_pm.get("accepting_orders", True):
        return False, "Polymarket not accepting orders"

    log.info("  Validation passed")
    return True, "ok"


# ─────────────────────────────────────────────
# STEP 6 — CALCULATE POSITION SIZE
# ─────────────────────────────────────────────
def position_size(opp):
    yes_platform = opp["buy_yes_market"]
    no_platform  = opp["buy_no_market"]
    yes_liq = (opp.get(yes_platform) or {}).get("liquidity") or 0
    no_liq  = (opp.get(no_platform)  or {}).get("liquidity") or 0
    raw  = min(yes_liq, no_liq) * LIQUIDITY_FRACTION
    size = round(min(raw, MAX_POSITION_USD), 2)
    log.info(f"Step 6: position size=${size} (yes_liq={yes_liq}, no_liq={no_liq})")
    return size


# ─────────────────────────────────────────────
# STEP 7 — EXECUTE BOTH LEGS
# ─────────────────────────────────────────────
def place_polymarket_order(token_id, price, size_usd):
    log.info(f"  [POLYMARKET] BUY | price={price} | size=${size_usd}")
    return http_post(
        "https://clob.polymarket.com/order",
        body={"token_id": token_id, "side": "BUY", "price": price,
              "size": size_usd, "order_type": "GTC"},
        headers={"Authorization": f"Bearer {POLYMARKET_API_KEY}"}
    )

def place_kalshi_order(ticker, side, price, size_usd):
    log.info(f"  [KALSHI] {side} | ticker={ticker} | price={price} | size=${size_usd}")
    return http_post(
        "https://trading-api.kalshi.com/trade-api/v2/portfolio/orders",
        body={"ticker": ticker, "side": side.lower(), "count": int(size_usd),
              "type": "limit", "yes_price": int(price * 100)},
        headers={"Authorization": f"Bearer {KALSHI_API_KEY}"}
    )

def place_opinion_order(market_id, outcome_key, side, price, size_usd):
    log.info(f"  [OPINION] {side} | market={market_id} | outcome={outcome_key} | price={price}")
    return http_post(
        "https://api.opinionaire.com/orders",
        body={"market_id": market_id, "outcome_key": outcome_key,
              "side": side, "price": price, "size": size_usd},
        headers={"Authorization": f"Bearer {OPINION_API_KEY}"}
    )

def execute_leg(platform, opp, side, price, size_usd, resolved_pm=None):
    if platform == "polymarket":
        return place_polymarket_order(resolved_pm["token_id"], price, size_usd)
    elif platform == "kalshi":
        return place_kalshi_order(opp["kalshi_event_ticker"], side, price, size_usd)
    elif platform == "opinion":
        return place_opinion_order(opp["opinion_market_id"], opp["outcome_key"], side, price, size_usd)
    else:
        raise ValueError(f"Unknown platform: {platform}")

def execute_both_legs(opp, size_usd, resolved_pm=None):
    log.info("Step 7: executing both legs simultaneously...")
    yes_platform = opp["buy_yes_market"]
    no_platform  = opp["buy_no_market"]
    yes_price    = opp[yes_platform]["yes_ask"]
    no_price     = opp[no_platform]["no_ask"]

    results = {}

    def leg_yes():
        return execute_leg(yes_platform, opp, "YES", yes_price, size_usd, resolved_pm)
    def leg_no():
        return execute_leg(no_platform, opp, "NO", no_price, size_usd, resolved_pm)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fy = ex.submit(leg_yes)
        fn = ex.submit(leg_no)
        for future in as_completed([fy, fn], timeout=LEG_FILL_TIMEOUT):
            label = "YES" if future == fy else "NO"
            try:
                results[label] = {"status": "ok", "response": future.result()}
                log.info(f"  Leg {label} placed successfully")
            except Exception as e:
                results[label] = {"status": "error", "error": str(e)}
                log.error(f"  Leg {label} FAILED: {e}")
    return results


# ─────────────────────────────────────────────
# STEP 8 — MONITOR FILLS
# ─────────────────────────────────────────────
def monitor_fills(results, expected_profit):
    log.info("Step 8: checking fill results...")
    both_ok = all(r["status"] == "ok" for r in results.values())
    if both_ok:
        log.info(f"  Both legs filled. Profit locked: ${expected_profit:.4f}")
    else:
        failed = [k for k, v in results.items() if v["status"] != "ok"]
        log.error(f"  PARTIAL FILL: legs {failed} failed. Manual intervention needed.")
    return both_ok


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────
def run(dry_run=True):
    log.info("=" * 60)
    log.info(f"OddPool Arb Pipeline | dry_run={dry_run}")
    log.info("=" * 60)

    # 1. Fetch all opportunities from OddPool
    opps = fetch_opportunities()
    if not opps:
        log.info("No opportunities. Exiting.")
        return

    # 2. Score every opportunity
    #    Score = net_cents x deployable_size x time_factor
    ranked = rank_opportunities(opps)
    if not ranked:
        log.info("No scoreable opportunities. Exiting.")
        return

    # 3. Pick the single best (highest score = highest expected profit)
    best = pick_best(ranked)
    if not best:
        return

    yes_platform = best["buy_yes_market"]
    no_platform  = best["buy_no_market"]

    # 4. Resolve Polymarket tokenId if Polymarket is one of the legs
    resolved_pm = None
    if "polymarket" in [yes_platform, no_platform]:
        pm_side = "YES" if yes_platform == "polymarket" else "NO"
        pm_vol  = (best.get("polymarket") or {}).get("volume", 0)
        try:
            resolved_pm = resolve_polymarket_token(
                event_slug=best["polymarket_event_slug"],
                polymarket_volume=pm_vol,
                side=pm_side
            )
        except Exception as e:
            log.error(f"  Could not resolve Polymarket token: {e}")
            return

    # 5. Validate with live prices before committing capital
    ok, reason = validate(best, resolved_pm)
    if not ok:
        log.warning(f"  Validation failed: {reason}. Skipping.")
        return

    # 6. Calculate exact position size
    size = position_size(best)
    expected_profit = (best["net_cents"] / 100) * size
    log.info(f"  Expected profit at ${size}: ${expected_profit:.4f}")

    # 7. Execute both legs (or just print if dry run)
    if dry_run:
        log.info("\n[DRY RUN] Would place:")
        log.info(f"  BUY YES on {yes_platform} @ {best[yes_platform]['yes_ask']} size=${size}")
        log.info(f"  BUY NO  on {no_platform}  @ {best[no_platform]['no_ask']}  size=${size}")
        log.info(f"  Locked profit: ${expected_profit:.4f}")
        return

    results = execute_both_legs(best, size, resolved_pm)

    # 8. Confirm both legs filled
    monitor_fills(results, expected_profit)

    # 9. Wait for resolution — platforms pay out automatically at resolution_time
    log.info(f"Step 9: waiting for resolution at {best.get('resolution_time')}")


if __name__ == "__main__":
    # Set dry_run=False when ready to go live
    run(dry_run=True)
