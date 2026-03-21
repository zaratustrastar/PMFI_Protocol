"""Arb Execution Loop - continuously deploys capital into the highest-scoring opportunities.

Ranking: score = annualized_return × confidence × fillable_size_usdc
  annualized_return = (1 + net_edge/100)^(365/days) - 1   [captures compounding]
  confidence        = logistic(bottleneck_liquidity)        [fill reliability]
  fillable_size     = ARB_FILLABLE_FRACTION × min(poly_liq, venue2_liq)  [capacity]

The score naturally outperforms pnl_velocity because it:
  - Correctly annualises/compounds across different holding periods
  - Weights capacity so deep markets beat tiny edge-only markets
  - Zeroes out when net_edge is non-positive (fees+slippage eat the trade)
  - Penalises illiquid legs via confidence

Runs as a daemon thread started at bot startup. Polls Oddpool every ODDPOOL_POLL_INTERVAL
seconds and executes eligible opportunities in score order (highest first).
"""

import time
import threading
import os
from ..adapters.oddpool import fetch_opportunities, ArbOpportunity
from ..core.arb_executor import execute_arb
from ..core.arb_positions_db import (
    upsert_position, get_open_positions, get_total_deployed_usdc
)
from ..config import (
    ODDPOOL_POLL_INTERVAL,
    ARB_MIN_EDGE_PCT,
    ARB_MAX_PAIR_USDC,
    ARB_MAX_DEPLOYED_USDC,
    ARB_USE_ODDPOOL_ONLY,
)

# Expiry guard: disabled by default (Oddpool's actionable filter already removes
# markets where liquidity is too thin or settlement is imminent).
# Set ARB_MIN_HOURS_TO_EXPIRY > 0 to add a manual override if needed.
ARB_MIN_HOURS_TO_EXPIRY = float(os.environ.get("ARB_MIN_HOURS_TO_EXPIRY", "0"))
ARB_MIN_DAYS_TO_EXPIRY = ARB_MIN_HOURS_TO_EXPIRY / 24.0


def log(msg: str):
    print(f"🔁 [ArbExecLoop] {msg}")


_loop_thread: threading.Thread | None = None
_loop_running = False


def can_deploy_capital(opportunity: ArbOpportunity) -> tuple[bool, str]:
    """Check per-pair and total caps before deploying capital.

    Returns (ok, reason). Uses live DB state — never cached values.
    """
    try:
        total_deployed = get_total_deployed_usdc()
        if total_deployed >= ARB_MAX_DEPLOYED_USDC:
            return False, f"total_deployed={total_deployed:.2f} >= MAX={ARB_MAX_DEPLOYED_USDC:.2f}"

        open_positions = get_open_positions()
        pair_positions = [p for p in open_positions if p.get("pair_id") == opportunity.pair_id]
        pair_deployed = sum(p.get("cost_basis_usdc", 0) for p in pair_positions)
        if pair_deployed >= ARB_MAX_PAIR_USDC:
            return False, f"pair_deployed={pair_deployed:.2f} >= PER_PAIR_MAX={ARB_MAX_PAIR_USDC:.2f}"

        remaining_pair = ARB_MAX_PAIR_USDC - pair_deployed
        remaining_total = ARB_MAX_DEPLOYED_USDC - total_deployed
        return True, f"can_deploy={min(remaining_pair, remaining_total):.2f} USDC available"
    except Exception as e:
        log(f"⚠️ cap_check error: {e}")
        return False, f"cap_check_error: {e}"


def compute_trade_size(opportunity: ArbOpportunity) -> float:
    """Compute how much USDC to deploy into this opportunity.

    Respects three constraints (takes the minimum):
      1. fillable_size_usdc: conservative estimate of what the market can absorb
         at the quoted spread (ARB_FILLABLE_FRACTION × bottleneck liquidity).
      2. Per-pair cap: ARB_MAX_PAIR_USDC minus what's already in this pair.
      3. Total vault cap: ARB_MAX_DEPLOYED_USDC minus total deployed USDC.

    Returns 0 if no budget remains under any constraint.
    """
    try:
        total_deployed = get_total_deployed_usdc()
        remaining_total = max(0, ARB_MAX_DEPLOYED_USDC - total_deployed)

        open_positions = get_open_positions()
        pair_positions = [p for p in open_positions if p.get("pair_id") == opportunity.pair_id]
        pair_deployed = sum(p.get("cost_basis_usdc", 0) for p in pair_positions)
        remaining_pair = max(0, ARB_MAX_PAIR_USDC - pair_deployed)

        # Scorer's fillable estimate caps deployment to market-absorb capacity
        fillable = getattr(opportunity, "fillable_size_usdc", ARB_MAX_PAIR_USDC)
        fillable = max(0.0, fillable)

        size = min(fillable, remaining_pair, remaining_total, ARB_MAX_PAIR_USDC)
        return max(0.0, size)
    except Exception as e:
        log(f"⚠️ compute_trade_size error: {e}")
        return 0.0


def _execution_cycle():
    """Run a single execution cycle: fetch, prioritize, execute eligible opportunities."""
    log("⚡ Starting execution cycle")

    if not ARB_USE_ODDPOOL_ONLY:
        log("ℹ️ ARB_USE_ODDPOOL_ONLY=false — execution loop skipped (legacy mode)")
        return

    try:
        opportunities = fetch_opportunities()
    except Exception as e:
        log(f"❌ Failed to fetch Oddpool opportunities: {e}")
        return

    if not opportunities:
        log("ℹ️ No opportunities from Oddpool this cycle")
        return

    log(f"📊 {len(opportunities)} opportunities fetched, sorted by score desc")

    executed = 0
    skipped_thin = 0
    skipped_caps = 0
    skipped_expiry = 0
    skipped_display = 0
    depth_insufficient = 0   # aborted: depth-capped size < 1 contract at profitable price
    depth_unavailable = 0    # aborted: could not fetch orderbook to verify depth

    for opp in opportunities:
        if not _loop_running:
            break

        # ── Display-only guard: skip markets whose Poly token ID is not yet resolved ──
        if getattr(opp, "is_display_only", True):
            skipped_display += 1
            log(
                f"👁 Skipping {opp.pair_id}: token not yet resolved from slug "
                f"(will retry on next Oddpool cycle)"
            )
            continue

        # ── Expiry guard: don't enter markets closing too soon ─────────────
        if opp.days_to_expiry < ARB_MIN_DAYS_TO_EXPIRY:
            skipped_expiry += 1
            log(
                f"⏭ Skipping {opp.pair_id}: expires in {opp.days_to_expiry*24:.1f}h "
                f"< MIN={ARB_MIN_HOURS_TO_EXPIRY:.0f}h"
            )
            continue

        # ── Edge guard: skip opportunities where net_edge (after fees + slippage +
        #    risk buffer) is below the minimum. Using net_edge_pct here means we
        #    never enter a trade that looks profitable on paper but loses money
        #    once real execution costs are accounted for.
        net_edge = getattr(opp, "net_edge_pct", opp.gross_edge_pct)
        if net_edge < ARB_MIN_EDGE_PCT:
            skipped_thin += 1
            log(
                f"⏭ Skipping {opp.pair_id}: net_edge={net_edge:.4f}% < "
                f"min_edge={ARB_MIN_EDGE_PCT:.4f}% (gross={opp.gross_edge_pct:.4f}%)"
            )
            continue

        # ── Capital caps: respect per-pair and total limits ────────────────
        ok, reason = can_deploy_capital(opp)
        if not ok:
            skipped_caps += 1
            log(f"⏭ Skipping {opp.pair_id}: cap check failed — {reason}")
            continue

        size_usdc = compute_trade_size(opp)
        if size_usdc <= 0:
            log(f"⏭ Skipping {opp.pair_id}: no budget remaining")
            skipped_caps += 1
            continue

        net_edge_log = getattr(opp, "net_edge_pct", opp.gross_edge_pct)
        ar_log = getattr(opp, "annualized_return", 0.0)
        conf_log = getattr(opp, "confidence", 0.0)
        score_log = getattr(opp, "score", 0.0)
        log(
            f"🚀 Executing pair_id={opp.pair_id} venue2={opp.venue2} | "
            f"score={score_log:.1f} AR={ar_log:.2f}× "
            f"net_edge={net_edge_log:.2f}% conf={conf_log:.2f} | "
            f"days={opp.days_to_expiry:.2f}d size=${size_usdc:.2f}"
        )

        result = execute_arb(opp, size_usdc=size_usdc)
        executed += 1

        # Track depth-related execution failures for cycle summary
        if not result.success and result.error:
            if result.error.startswith("depth_insufficient"):
                depth_insufficient += 1
            elif result.error.startswith("depth_unavailable"):
                depth_unavailable += 1

        if result.success:
            log(f"✅ Execution succeeded for {opp.pair_id}")
            try:
                # Use actual fill data from executor, not quoted/estimated values
                kalshi_side = getattr(result, "kalshi_side", opp.kalshi_side)
                upsert_position(
                    pair_id=opp.pair_id,
                    poly_yes_token=opp.poly_yes_token,
                    kalshi_ticker=opp.kalshi_ticker,
                    shares=result.filled_shares,
                    cost_basis_usdc=result.total_cost_usdc,
                    expiry_ts=opp.expiry_ts or 0,
                    status="open",
                    poly_title=opp.poly_title,
                    kalshi_title=opp.kalshi_title,
                    kalshi_side=kalshi_side,
                )
                log(
                    f"✅ Position persisted for {opp.pair_id}: "
                    f"shares={result.filled_shares:.4f} "
                    f"cost={result.total_cost_usdc:.4f} USDC"
                )
            except Exception as e:
                log(f"⚠️ Failed to persist position for {opp.pair_id}: {e}")
        else:
            log(
                f"❌ Execution failed for {opp.pair_id}: {result.error} "
                f"unwound={result.unwound}"
            )

    log(
        f"⚡ Cycle complete: executed={executed} "
        f"skipped_display_only={skipped_display} "
        f"skipped_thin_edge={skipped_thin} "
        f"skipped_expiry={skipped_expiry} "
        f"skipped_caps={skipped_caps} "
        f"depth_insufficient={depth_insufficient} "
        f"depth_unavailable={depth_unavailable}"
    )


def _execution_loop_worker():
    """Daemon thread: continuously run execution cycles."""
    global _loop_running
    log("🟢 Arb execution loop started")

    while _loop_running:
        try:
            _execution_cycle()
        except Exception as e:
            log(f"❌ Unhandled error in execution cycle: {e}")

        if _loop_running:
            log(f"😴 Sleeping {ODDPOOL_POLL_INTERVAL}s until next cycle...")
            time.sleep(ODDPOOL_POLL_INTERVAL)

    log("🔴 Arb execution loop stopped")


def start_execution_loop():
    """Start the background arb execution loop daemon thread."""
    global _loop_thread, _loop_running
    if _loop_thread is not None and _loop_thread.is_alive():
        log("⚠️ Execution loop is already running")
        return

    _loop_running = True
    _loop_thread = threading.Thread(target=_execution_loop_worker, daemon=True, name="arb-exec-loop")
    _loop_thread.start()
    log(f"🚀 Execution loop daemon thread started (poll_interval={ODDPOOL_POLL_INTERVAL}s)")


def stop_execution_loop():
    """Stop the background arb execution loop."""
    global _loop_running
    _loop_running = False
    log("🛑 Execution loop stop requested")
