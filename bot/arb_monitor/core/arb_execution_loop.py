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
from ..core.arb_funder import run_tend_tick, get_servicer_deployable_usdc, _get_platform_balance
from ..core.arb_reporter import run_reporter_tick
from ..core.arb_positions_db import (
    upsert_position, get_open_positions, get_total_deployed_usdc
)
from ..config import (
    ODDPOOL_POLL_INTERVAL,
    ARB_MIN_EDGE_PCT,
    ARB_MIN_SCORE,
    ARB_MAX_PAIR_USDC,
    ARB_MAX_DEPLOYED_USDC,
    ARB_USE_ODDPOOL_ONLY,
)

# Expiry guard: disabled by default (Oddpool's actionable filter already removes
# markets where liquidity is too thin or settlement is imminent).
# Set ARB_MIN_HOURS_TO_EXPIRY > 0 to add a manual override if needed.
ARB_MIN_HOURS_TO_EXPIRY = float(os.environ.get("ARB_MIN_HOURS_TO_EXPIRY", "0"))
ARB_MIN_DAYS_TO_EXPIRY = ARB_MIN_HOURS_TO_EXPIRY / 24.0

# ── Opinion skip cache ────────────────────────────────────────────────────────
# Tracks (opinion_market_id, outcome_key) pairs that recently returned
# "opinion_token_unresolvable". Keyed on the tuple — not just market_id —
# because market 340 may be resolvable for one child outcome but not another.
# TTL matches the executor's token cache (30 min) so a refresh may fix it.
_OPINION_SKIP_CACHE: dict[tuple[str, str], float] = {}
_OPINION_SKIP_TTL = 1800  # 30 minutes

# ── Per-pair failure cooldown ─────────────────────────────────────────────────
# After a pair fails with capital committed (leg failure / post-order network
# error), suppress retrying the same pair for _PAIR_FAIL_TTL seconds.
# This prevents the bot from hammering the same top-ranked opportunity every
# cycle when the underlying issue (e.g. proxy flap, stale order book) is
# transient rather than permanent.
_PAIR_FAIL_CACHE: dict[str, float] = {}
_PAIR_FAIL_TTL = int(os.environ.get("ARB_PAIR_FAIL_COOLDOWN_SECS", "300"))  # 5 minutes


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


def _get_liquidity_state():
    """Read liquidity state for the current cycle. Returns None if V2 not configured."""
    vault_address   = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    if not vault_address or not servicer_wallet:
        return None
    try:
        from .arb_liquidity import compute_liquidity_state
        return compute_liquidity_state(vault_address, servicer_wallet)
    except Exception as e:
        log(f"⚠️ Liquidity state error (non-fatal): {e}")
        return None


def _execution_cycle():
    """Run a single execution cycle: fetch, prioritize, execute eligible opportunities."""
    log("⚡ Starting execution cycle")

    # ── V2 Reporter: waterfall sweep + report() if cooldown/early-trigger ─────
    try:
        run_reporter_tick()
    except Exception as e:
        log(f"⚠️ Reporter tick raised unexpectedly (non-fatal): {e}")

    # ── Liquidity state: gate trading decisions this cycle ────────────────────
    liq = _get_liquidity_state()
    if liq and liq.ok:
        log(
            f"📊 Cycle liquidity: idle={liq.idle_available:.2f} "
            f"required={liq.required_idle:.2f} "
            f"pending_redeem={liq.pending_redeem_value:.2f} "
            f"deployable={liq.deployable_capital:.2f} "
            f"under_pressure={liq.under_pressure} "
            f"unwind_needed={liq.unwind_needed}"
        )

        if liq.unwind_needed:
            log(
                f"🛑 TRADE DEPLOYMENT BLOCKED: unwind_needed=True "
                f"(shortfall={liq.shortfall:.2f} USDC exceeds free_cash={liq.free_cash:.2f} "
                f"+ settled={liq.settled_proceeds:.2f}). "
                f"No new positions until redemption shortfall is resolved."
            )
            # Still run tend() so vault idle flows to servicer during pressure
            try:
                run_tend_tick()
            except Exception as e:
                log(f"⚠️ Tend tick raised unexpectedly (non-fatal): {e}")
            return

        if liq.under_pressure:
            log(
                f"⚠️ Redemption pressure active (shortfall={liq.shortfall:.2f} USDC) — "
                f"new trade deployment throttled to deployable_capital={liq.deployable_capital:.2f} USDC"
            )
    else:
        liq = None  # treat as unknown; allow trading but log

    # ── Tend tick: move vault idle USDC → servicer (no float maintenance) ────
    # Capital is NOT pre-distributed to platforms here. Exact amounts are sent
    # directly to both platforms when a specific trade is selected and sized.
    try:
        run_tend_tick()
    except Exception as e:
        log(f"⚠️ Tend tick raised unexpectedly (non-fatal): {e}")

    # ── Servicer capital check: log deployable capital for this cycle ─────────
    servicer_deployable = 0.0
    poly_platform_bal   = 0.0
    kalshi_platform_bal = 0.0
    opinion_platform_bal = 0.0
    try:
        servicer_deployable = get_servicer_deployable_usdc()
        log(f"💼 Cycle deployable capital: {servicer_deployable:.4f} USDC in servicer wallet")
    except Exception as e:
        log(f"⚠️ Could not read servicer capital (non-fatal, continuing): {e}")

    # Read existing platform balances — these can cover gap top-ups without needing
    # additional servicer capital (with gap-based deposits, only the delta is sent).
    try:
        poly_platform_bal    = _get_platform_balance("polymarket")
        kalshi_platform_bal  = _get_platform_balance("kalshi")
        opinion_platform_bal = _get_platform_balance("opinion")
        log(
            f"💼 Platform balances: poly={poly_platform_bal:.4f} "
            f"kalshi={kalshi_platform_bal:.4f} opinion={opinion_platform_bal:.4f} USDC"
        )
    except Exception as e:
        log(f"⚠️ Could not read platform balances (non-fatal): {e}")

    total_available_capital = (
        servicer_deployable + poly_platform_bal +
        max(kalshi_platform_bal, opinion_platform_bal)
    )

    if total_available_capital <= 0.10:
        log(
            f"⚠️ No deployable capital anywhere (servicer={servicer_deployable:.4f} "
            f"poly={poly_platform_bal:.4f} kalshi={kalshi_platform_bal:.4f} "
            f"opinion={opinion_platform_bal:.4f}) — "
            "skipping trade execution (waiting for vault tend() or new deposits)"
        )
        return

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

    # ── Opinion categorical pre-warm ─────────────────────────────────────────
    # Resolve all (parent_id, outcome_key) pairs for Opinion opportunities BEFORE
    # the execution loop starts. This populates the in-process token cache so
    # that the pre-flight check inside execute_arb() always hits the cache path
    # rather than making a live API call under execution pressure.
    #
    # We skip pairs already in _OPINION_SKIP_CACHE — those recently failed and
    # retrying immediately would just waste API quota.
    _opinion_opps = [
        opp for opp in opportunities
        if getattr(opp, "venue2", "kalshi") == "opinion"
        and not getattr(opp, "is_display_only", True)
    ]
    if _opinion_opps:
        from ..core.arb_executor import _opinion_resolve_tokens
        _PREWARM_BUDGET_SECS = 30
        _prewarm_deadline = time.time() + _PREWARM_BUDGET_SECS
        log(f"🔍 [OpinionPreWarm] Resolving tokens for {len(_opinion_opps)} Opinion opportunities (budget={_PREWARM_BUDGET_SECS}s)...")
        _prewarm_resolved = 0
        _prewarm_skipped = 0
        for _opp in _opinion_opps:
            if time.time() >= _prewarm_deadline:
                _remaining = len(_opinion_opps) - _prewarm_resolved - _prewarm_skipped
                log(f"⏱️ [OpinionPreWarm] Time budget exhausted after {_prewarm_resolved} resolved, {_remaining} deferred to pre-flight")
                break
            _mid = getattr(_opp, "opinion_market_id", "")
            _hint = getattr(_opp, "outcome_key", "")
            if not _mid:
                continue
            _skip_key = (_mid, _hint)
            _skip_ts = _OPINION_SKIP_CACHE.get(_skip_key, 0.0)
            if _skip_ts > 0 and time.time() < _skip_ts + _OPINION_SKIP_TTL:
                log(f"🔍 [OpinionPreWarm] Skipping {_opp.pair_id} — in skip cache")
                _prewarm_skipped += 1
                continue
            try:
                _label = getattr(_opp, "label", "") or ""
                _pre = _opinion_resolve_tokens(_mid, outcome_hint=_hint, label_hint=_label)
                if _pre:
                    log(f"✅ [OpinionPreWarm] {_opp.pair_id}: parent={_mid!r} → child={_pre[0]!r}")
                    _prewarm_resolved += 1
                    if _skip_key in _OPINION_SKIP_CACHE:
                        del _OPINION_SKIP_CACHE[_skip_key]
                        log(f"🔓 [OpinionPreWarm] {_opp.pair_id}: cleared skip cache — resolution succeeded")
                else:
                    log(f"⚠️ [OpinionPreWarm] {_opp.pair_id}: could not resolve parent={_mid!r} hint={_hint!r}")
            except Exception as _pw_exc:
                log(f"⚠️ [OpinionPreWarm] {_opp.pair_id}: exception: {_pw_exc}")

    executed = 0
    skipped_thin = 0
    skipped_caps = 0
    skipped_expiry = 0
    skipped_display = 0
    skipped_opinion_token = 0  # Opinion categorical parent → child resolution failed
    depth_insufficient = 0   # aborted: depth-capped size < 1 contract at profitable price
    depth_unavailable = 0    # aborted: could not fetch orderbook to verify depth

    # Errors where NO capital was moved — safe to skip to next opportunity.
    # Everything else (funding_failed, leg1_failed, leg2_failed) means capital
    # was committed and we must stop the cycle to avoid scatter-shot deposits.
    # NOTE: "funding_insufficient" is the pre-TX servicer gate (no money moved),
    # distinct from "funding_failed" which fires after a deposit TX was sent.
    _PRE_FUNDING_ERRORS = (
        "depth_insufficient",
        "depth_unavailable",
        "oddpool_edge_too_thin",
        "poly_slippage_exceeded",
        "kalshi_slippage_exceeded",
        "opinion_slippage_exceeded",
        "poly_orderbook_missing",
        "kalshi_orderbook_missing",
        "opinion_orderbook_missing",
        "poly_no_token_missing",
        "trade_too_small",
        "cannot_compute_contracts",
        "opinion_token_unresolvable",  # categorical parent → child resolution failed
        "funding_insufficient",        # servicer pre-check refused before any TX sent
    )

    # ── Liquidity budget: caps total new deployment this cycle ────────────────
    # If under_pressure, deployable_capital bounds how much USDC we can commit to
    # new positions this cycle. Decremented with each trade so the cap is cycle-wide.
    liq_budget_remaining: float | None = liq.deployable_capital if (liq and liq.ok and liq.under_pressure) else None

    for opp in opportunities:
        if not _loop_running:
            break

        # ── Liquidity budget check (under redemption pressure only) ───────
        if liq_budget_remaining is not None and liq_budget_remaining <= 0:
            log(
                f"🛑 Stopping deployment: liquidity budget exhausted "
                f"(deployed all available deployable_capital this cycle)"
            )
            skipped_caps += len(opportunities) - executed - skipped_thin - skipped_expiry - skipped_display
            break

        # ── Display-only guard: skip markets whose Poly token ID is not yet resolved ──
        if getattr(opp, "is_display_only", True):
            skipped_display += 1
            log(
                f"👁 Skipping {opp.pair_id}: token not yet resolved from slug "
                f"(will retry on next Oddpool cycle)"
            )
            continue

        # ── Opinion skip cache: skip (market_id, outcome_key) pairs that recently ──
        # returned opinion_token_unresolvable, keyed on the exact (parent, outcome)
        # tuple so different outcomes of the same categorical parent are independent.
        if getattr(opp, "venue2", "kalshi") == "opinion":
            _skip_key = (
                getattr(opp, "opinion_market_id", ""),
                getattr(opp, "outcome_key", ""),
            )
            _skip_ts = _OPINION_SKIP_CACHE.get(_skip_key, 0.0)
            if _skip_ts > 0 and time.time() < _skip_ts + _OPINION_SKIP_TTL:
                remaining = int(_skip_ts + _OPINION_SKIP_TTL - time.time())
                skipped_opinion_token += 1
                log(
                    f"⏭ Skipping {opp.pair_id}: opinion token unresolvable "
                    f"(market={_skip_key[0]!r} outcome={_skip_key[1]!r}), "
                    f"retry in {remaining}s"
                )
                continue

        # ── Per-pair failure cooldown: skip pairs that recently failed with capital committed ──
        _fail_ts = _PAIR_FAIL_CACHE.get(opp.pair_id, 0.0)
        if _fail_ts > 0 and time.time() < _fail_ts + _PAIR_FAIL_TTL:
            _fail_remaining = int(_fail_ts + _PAIR_FAIL_TTL - time.time())
            log(
                f"⏭ Skipping {opp.pair_id}: failed {int(time.time() - _fail_ts)}s ago "
                f"(cooldown={_PAIR_FAIL_TTL}s, retry in {_fail_remaining}s)"
            )
            skipped_caps += 1
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

        # ── Score guard: skip pairs the scoring engine rates as near-zero ─────
        # score = annualized_return × confidence × fillable_size_usdc.
        # A score below ARB_MIN_SCORE means the trade would generate negligible
        # return even if it fills perfectly — not worth execution overhead.
        pair_score = getattr(opp, "score", 0.0)
        if pair_score < ARB_MIN_SCORE:
            skipped_thin += 1
            log(
                f"⏭ Skipping {opp.pair_id}: score={pair_score:.2f} < "
                f"min_score={ARB_MIN_SCORE:.2f} (net_edge={net_edge:.4f}%)"
            )
            continue

        # ── Capital caps: respect per-pair and total limits ────────────────
        ok, reason = can_deploy_capital(opp)
        if not ok:
            skipped_caps += 1
            log(f"⏭ Skipping {opp.pair_id}: cap check failed — {reason}")
            continue

        size_usdc = compute_trade_size(opp)

        # ── Liquidity-aware size cap (applies only under redemption pressure) ─
        if liq_budget_remaining is not None:
            size_usdc = min(size_usdc, liq_budget_remaining)
            if size_usdc > 0:
                log(
                    f"💧 {opp.pair_id}: size capped to {size_usdc:.2f} USDC "
                    f"by liquidity budget (remaining={liq_budget_remaining:.2f})"
                )

        # ── Capital cap: size to what's actually available (servicer + existing platforms) ──
        # With gap-based deposits, servicer only needs to cover the delta between
        # current platform balance and required leg amount. Include existing platform
        # balances so we don't block trades that are already partially or fully funded.
        v2_bal = kalshi_platform_bal if getattr(opp, "venue2", "kalshi") == "kalshi" else opinion_platform_bal
        opp_total_available = servicer_deployable + poly_platform_bal + v2_bal
        if size_usdc > opp_total_available:
            log(
                f"💼 {opp.pair_id}: size_usdc={size_usdc:.2f} capped to "
                f"opp_total_available={opp_total_available:.2f} USDC "
                f"(servicer={servicer_deployable:.2f} + poly={poly_platform_bal:.2f} "
                f"+ {getattr(opp, 'venue2', 'kalshi')}={v2_bal:.2f})"
            )
            size_usdc = opp_total_available

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
            elif result.error.startswith("opinion_token_unresolvable"):
                # Record in skip cache so this (market_id, outcome_key) is not
                # retried for the next _OPINION_SKIP_TTL seconds.
                _cache_key = (
                    getattr(opp, "opinion_market_id", ""),
                    getattr(opp, "outcome_key", ""),
                )
                _OPINION_SKIP_CACHE[_cache_key] = time.time()
                log(
                    f"⏭ {opp.pair_id}: opinion_token_unresolvable — cached "
                    f"(market={_cache_key[0]!r} outcome={_cache_key[1]!r}), "
                    f"will skip for {_OPINION_SKIP_TTL}s"
                )

        if not result.success:
            err = result.error or ""
            # Pre-funding failures: no capital was moved on-chain.
            # Safe to skip to the next-best opportunity this cycle.
            if any(err.startswith(e) for e in _PRE_FUNDING_ERRORS):
                log(
                    f"⏭ {opp.pair_id}: pre-funding failure ({err[:100]}) — "
                    f"no capital moved, trying next opportunity"
                )
                continue

            # Capital was committed (funding attempt, leg failure, post-fill unwind).
            # Stop the cycle — attempting more trades risks scattering capital across
            # multiple venues without completing any trade cleanly.
            # Cache the pair so it is skipped for _PAIR_FAIL_TTL seconds — prevents
            # the same opportunity dominating the top of the ranked list every cycle
            # when the root cause (e.g. proxy flap) is transient.
            _PAIR_FAIL_CACHE[opp.pair_id] = time.time()
            log(
                f"❌ {opp.pair_id}: execution stopped cycle — {err} "
                f"unwound={result.unwound} — pair suppressed for {_PAIR_FAIL_TTL}s"
            )
            break

        # ── Success: persist position and stop cycle ──────────────────────────
        # One coordinated trade per cycle: pick best opportunity → fund exactly →
        # execute both legs together → stop. Next cycle starts fresh.
        cost_deployed = getattr(result, "total_cost_usdc", size_usdc)
        servicer_deployable = max(0.0, servicer_deployable - cost_deployed)
        log(
            f"💼 Servicer deployable after {opp.pair_id}: "
            f"deployed={cost_deployed:.2f} remaining={servicer_deployable:.2f}"
        )
        if liq_budget_remaining is not None:
            liq_budget_remaining -= cost_deployed
            log(
                f"💧 Liquidity budget after {opp.pair_id}: "
                f"deployed={cost_deployed:.2f} remaining={liq_budget_remaining:.2f}"
            )
        log(f"✅ Execution succeeded for {opp.pair_id} — stopping cycle (one trade per cycle)")
        try:
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
        break  # Always stop after one successful trade

    log(
        f"⚡ Cycle complete: executed={executed} "
        f"skipped_display_only={skipped_display} "
        f"skipped_thin_edge={skipped_thin} "
        f"skipped_expiry={skipped_expiry} "
        f"skipped_caps={skipped_caps} "
        f"skipped_opinion_token={skipped_opinion_token} "
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
