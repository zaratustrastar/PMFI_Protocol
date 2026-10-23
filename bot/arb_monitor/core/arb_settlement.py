"""Position settlement checker.

Base mode (live V2): legacy behaviour, unchanged — a position whose leg prices indicate a
winner (or whose combined bid >= 1.00) is marked 'settled' and drops out of NAV.

Robinhood mode (MILORD V1 / V3 vault), C2: no position value is dropped until its cash is
observed at a venue.
    open --(winning leg detected)--> pending_redeem --(payout seen in venue cash)--> redeemed
  * pending_redeem positions keep being valued at COST BASIS (not at the resolved payout):
    no unrealised gain is booked, and the gain is realised when cash lands.
  * EARLY_EXIT (combined bid >= 1.00) is only a recommendation: nothing is sold, so the
    position stays 'open' at cost basis.
  * Automatic Safe/CTF recovery is opt-in via arb_ctf_redemption; live route unverified.
    Legacy disabled-mode hooks:
      request_venue_redemption()  — Polymarket CTF redeemPositions / Kalshi settlement
      observe_venue_payout()      — evidence that the payout reached venue cash
    Until automatic recovery is configured and verified, an operator records
    it with arb_positions_db.mark_position_redeemed(pair_id, payout_usdc). The reporter
    refuses to sign while a Kalshi-won position is pending_redeem (Kalshi auto-credits cash,
    which would otherwise be counted twice).
"""

from .arb_positions_db import get_open_positions, settle_position


def log(msg):
    print(f"🏁 [ArbSettlement] {msg}", flush=True)


def request_venue_redemption(position: dict, venue: str):
    from . import arb_ctf_redemption as ctf
    if not ctf.enabled():
        log('CTF automatic redemption disabled pending isolated custody and route verification')
        return None
    return ctf.tick(position)


def observe_venue_payout(position: dict, venue: str):
    from decimal import Decimal
    from . import arb_ctf_redemption as ctf
    from . import arb_ledger
    for row in arb_ledger.read_rows(ctf.journal_path(), strict=True):
        if row['pair_id'] == position['pair_id'] and row['status'] == 'reconciled':
            return Decimal(row['payout_raw']) / Decimal(1000000)
    return None


def _detect(pos: dict):
    """Return (trigger, proceeds, our_poly, kalshi_bid) or None when prices are missing."""
    pair_id = pos.get("pair_id", "")
    poly_yes_token = pos.get("poly_yes_token", "")
    kalshi_ticker = pos.get("kalshi_ticker", "")
    kalshi_side = pos.get("kalshi_side", "NO").upper()
    kalshi_title = pos.get("kalshi_title", "")
    shares = float(pos.get("shares", 0))
    from ..adapters.polymarket import get_best_prices as pp
    from ..adapters.kalshi import get_best_prices as kp, resolve_market_ticker, fetch_orderbook_depth
    poly_yes_bid = pp(poly_yes_token).get("best_bid")
    resolved = resolve_market_ticker(kalshi_ticker, outcome_key="no" if kalshi_side=="NO" else "yes", label_hint=kalshi_title)
    actual = resolved if resolved else kalshi_ticker
    log(f"Resolved {kalshi_ticker} -> {actual}")
    kdata = kp(actual)
    kalshi_bid = kdata.get("no_best_bid") if kalshi_side=="NO" else kdata.get("yes_best_bid")
    if kalshi_bid is None:
        ob = fetch_orderbook_depth(actual, depth=5)
        if ob:
            fp = ob.get("orderbook_fp", {})
            lvls = fp.get("no_dollars", []) if kalshi_side=="NO" else fp.get("yes_dollars", [])
            if lvls:
                kalshi_bid = max(float(l[0]) for l in lvls)
                log(f"Kalshi bid from orderbook: {kalshi_bid}")
    if poly_yes_bid is None or kalshi_bid is None:
        log(f"{pair_id}: missing prices poly={poly_yes_bid} kalshi={kalshi_bid}")
        return None
    our_poly = poly_yes_bid if kalshi_side=="NO" else 1.0 - poly_yes_bid
    combined = our_poly + kalshi_bid
    log(f"{pair_id}: poly={our_poly:.4f} kalshi={kalshi_bid:.4f} combined={combined:.4f}")
    trigger = None
    proceeds = 0.0
    if our_poly >= 0.98:
        trigger, proceeds = "POLY_SIDE_WON", shares * 1.0
    elif kalshi_bid >= 0.98:
        trigger, proceeds = "KALSHI_SIDE_WON", shares * 1.0
    elif combined >= 1.00:
        trigger, proceeds = "EARLY_EXIT", shares * combined
    return trigger, proceeds, combined


def _check_robinhood(positions) -> int:
    from .arb_positions_db import mark_position_resolved
    resolved_count = 0
    for pos in positions:
        pair_id = pos.get("pair_id", "")
        shares = float(pos.get("shares", 0))
        if not pos.get("poly_yes_token") or not pos.get("kalshi_ticker") or shares <= 0:
            continue
        try:
            det = _detect(pos)
            if det is None:
                continue
            trigger, proceeds, combined = det
            if trigger == "EARLY_EXIT":
                log(f"{pair_id}: EARLY_EXIT available (combined={combined:.4f}) — recommendation only, "
                    f"no sale executed, position stays open at cost basis")
                continue
            if trigger is None:
                log(f"{pair_id} not resolved yet combined={combined:.4f}")
                continue
            venue = "polymarket" if trigger == "POLY_SIDE_WON" else "kalshi"
            if mark_position_resolved(pair_id, venue, proceeds):
                resolved_count += 1
                log(f"{pair_id}: {trigger} → pending_redeem on {venue} (expected payout {proceeds:.4f}); "
                    f"still valued at cost basis until the payout is observed")
                try:
                    request_venue_redemption(pos, venue)
                except NotImplementedError as e:
                    log(f"⚠️ {e}")
        except Exception as e:
            log(f"Error {pair_id}: {e}")
    return resolved_count


def check_and_settle_positions():
    from ..config import is_robinhood_funding
    from . import arb_ctf_redemption as ctf
    if ctf.enabled():
        from .arb_positions_db import get_positions_by_status
        count = 0
        positions = get_positions_by_status(('open','pending_redeem'))
        # CTF redeems a whole selected token balance. Never assign shared holdings twice.
        for pos in positions:
            if sum(p['poly_yes_token'] == pos['poly_yes_token'] or p['kalshi_ticker'] == pos['kalshi_ticker'] for p in positions) != 1:
                log(f"{pos['pair_id']}: shared market holdings require reconciliation")
                continue
            try:
                result = request_venue_redemption(pos, 'polymarket')
                count += result == 'reconciled'
                log(f"{pos['pair_id']}: {result}")
            except Exception as e:
                log(f"{pos['pair_id']}: redemption blocked: {type(e).__name__}")
        return count
    positions = get_open_positions()
    if not positions:
        return 0
    if is_robinhood_funding():
        return _check_robinhood(positions)
    settled_count = 0
    for pos in positions:
        pair_id = pos.get("pair_id", "")
        poly_yes_token = pos.get("poly_yes_token", "")
        kalshi_ticker = pos.get("kalshi_ticker", "")
        shares = float(pos.get("shares", 0))
        cost_basis = float(pos.get("cost_basis_usdc", 0))
        if not poly_yes_token or not kalshi_ticker or shares <= 0:
            continue
        try:
            det = _detect(pos)
            if det is None:
                continue
            trigger, proceeds, combined = det
            if trigger:
                pnl = proceeds - cost_basis
                log(f"SETTLING {pair_id} trigger={trigger} pnl={pnl:.4f}")
                if settle_position(pair_id, settled_pnl_usdc=pnl):
                    log(f"Settled {pair_id} pnl={pnl:.4f}")
                    settled_count += 1
            else:
                log(f"{pair_id} not settled yet combined={combined:.4f}")
        except Exception as e:
            log(f"Error {pair_id}: {e}")
    return settled_count
