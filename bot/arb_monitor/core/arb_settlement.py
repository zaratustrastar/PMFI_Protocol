from .arb_positions_db import get_open_positions, settle_position

def log(msg):
    print(f"🏁 [ArbSettlement] {msg}", flush=True)

def check_and_settle_positions():
    positions = get_open_positions()
    if not positions:
        return 0
    settled_count = 0
    for pos in positions:
        pair_id = pos.get("pair_id", "")
        poly_yes_token = pos.get("poly_yes_token", "")
        kalshi_ticker = pos.get("kalshi_ticker", "")
        kalshi_side = pos.get("kalshi_side", "NO").upper()
        kalshi_title = pos.get("kalshi_title", "")
        shares = float(pos.get("shares", 0))
        cost_basis = float(pos.get("cost_basis_usdc", 0))
        if not poly_yes_token or not kalshi_ticker or shares <= 0:
            continue
        try:
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
                continue
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
