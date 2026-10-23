"""Arb NAV Tracker - computes liquid NAV for pArbitrage vault positions.

NAV = poly_cash + kalshi_cash + opinion_cash + servicer_on_base
      + open_positions_value  (@ $0.99/share — deterministic, no live API calls)

servicer_on_base = USDC held by ARB_SERVICER_WALLET on Base, awaiting distribution
to trading platforms. Folded into polyCash in the signed struct (no separate slot
in the contract). Prevents NAV dip between deposit and platform distribution.

Open position valuation: each arb pair = N contracts where one side resolves to $1
and the other to $0, so floor value = $1/pair. $0.99 shaves 1¢ for conservatism.

settled_pnl intentionally excluded: when a position settles and the winning side is
claimed from the platform, the full proceeds land in platform cash (poly_cash /
kalshi_cash / opinion_cash). Adding settled_pnl on top double-counts the profit.

Signing follows the same ABI-encoded struct hash pattern as the PMFI pARB Vault contract (PMFIArbVaultV1.sol):
  keccak256(abi.encode(NAV_TYPEHASH, totalAssets, polyCash, kalshiCash,
                       openPositionsValue, settledPnl, timestamp, deadline,
                       roundId, vault, chainId, domainSalt))
then prefixed with eth_sign ("\x19Ethereum Signed Message:\n32").
"""

import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from ..adapters.polymarket import get_best_prices as poly_get_best_prices
from ..adapters.kalshi import get_best_prices as kalshi_get_best_prices
from ..config import (
    OPINION_BASE_URL,
    OPINION_API_KEY,
    is_robinhood_funding,
    opinion_enabled,
    peg_max_deviation_bps,
)


def log(msg: str):
    print(f"📈 [Arb/NAV] {msg}")


ARB_VAULT_DOMAIN_SALT = "PMFIArbVaultV2.v1"
NAV_VALIDITY_WINDOW = 30  # 30-second deadline window (contract MAX_NAV_AGE = 30)

# Injectable USDG price in USDC (callable -> float), used only when
# ARB_PEG_MAX_DEVIATION_BPS > 0. Set by the host process or passed to compute_nav().
PEG_PRICE_SOURCE = None

# Position statuses that still carry value (cash NOT yet observed at a venue).
#   open            — both legs held
#   pending_redeem  — market resolved, winning leg not yet paid out (robinhood mode only)
# 'redeemed' (cash observed at the venue) and legacy 'settled' carry no position value.
VALUE_BEARING_STATUSES_RH = ("open", "pending_redeem")


class NavReadError(RuntimeError):
    """A NAV component could not be read. Strict callers (the V2/V3 reporter) must abort
    instead of treating the component as 0 — a silent 0 under-reports assets and moves PPS."""

    def __init__(self, component: str, detail: str):
        super().__init__(f"{component}: {detail}")
        self.component = component
        self.detail = detail


def usdc_to_raw(amount) -> int:
    """6-decimal amount -> integer raw units, exact for anything that came from raw/1e6.

    int(x * 1e6) truncates (0.000249 * 1e6 = 248.999…); going through str() recovers the
    shortest decimal repr, so 249 raw round-trips to 249. Sub-micro dust is rounded DOWN.
    """
    if isinstance(amount, int):
        return amount * 1_000_000
    d = Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return int(d * 1_000_000)


@dataclass
class ArbPosition:
    pair_id: str
    poly_yes_token: str
    kalshi_ticker: str
    shares: float
    cost_basis_usdc: float
    expiry_ts: int
    status: str
    settled_pnl_usdc: float = 0.0
    # Which side was bought on Kalshi ("YES" or "NO")
    # Determines whether to use kalshi_yes_bid or kalshi_no_bid for liquid value.
    kalshi_side: str = "YES"
    # robinhood mode: venue whose leg won once status == "pending_redeem" ("polymarket"|"kalshi")
    resolved_venue: str = ""


@dataclass
class LiquidPositionValue:
    pair_id: str
    poly_yes_bid: Optional[float]
    kalshi_yes_bid: Optional[float]
    liquid_value_per_share: float
    total_liquid_value: float
    shares: float
    warning: str = ""


def fetch_position_liquid_value(position: ArbPosition) -> LiquidPositionValue:
    """Fetch live bids (not asks) for both legs of an open arb position.

    Uses bids because we want liquidation value (what we can sell for today).
    Uses the CORRECT bid side per leg:
    - Poly leg: always YES (we always buy Poly YES for the arb)
    - Kalshi leg: YES bid or NO bid depending on position.kalshi_side
      (e.g. if we bought Kalshi NO, the liquidation bid is kalshi_no_bid)
    """
    log(f"Fetching liquid value for pair_id={position.pair_id} (kalshi_side={position.kalshi_side})")

    poly_prices = poly_get_best_prices(position.poly_yes_token)
    poly_yes_bid = poly_prices.get("best_bid")

    kalshi_prices = kalshi_get_best_prices(position.kalshi_ticker)

    # Determine Kalshi liquidation bid based on which side was bought
    if position.kalshi_side == "NO":
        kalshi_leg_bid = kalshi_prices.get("no_best_bid")
        kalshi_bid_label = "no_bid"
    else:
        kalshi_leg_bid = kalshi_prices.get("yes_best_bid")
        kalshi_bid_label = "yes_bid"

    warning = ""
    if poly_yes_bid is None:
        warning += "poly_bid_missing "
        poly_yes_bid = 0.0
        log(f"⚠️ poly_bid_missing for {position.pair_id}")
    if kalshi_leg_bid is None:
        warning += f"kalshi_{kalshi_bid_label}_missing "
        kalshi_leg_bid = 0.0
        log(f"⚠️ kalshi_{kalshi_bid_label}_missing for {position.pair_id}")

    liquid_value_per_share = poly_yes_bid + kalshi_leg_bid
    total_liquid_value = liquid_value_per_share * position.shares

    log(
        f"Position {position.pair_id}: "
        f"poly_yes_bid={poly_yes_bid}, kalshi_{kalshi_bid_label}={kalshi_leg_bid}, "
        f"liquid_per_share={liquid_value_per_share:.4f}, "
        f"shares={position.shares}, total={total_liquid_value:.4f} USDC"
    )

    return LiquidPositionValue(
        pair_id=position.pair_id,
        poly_yes_bid=poly_yes_bid,
        kalshi_yes_bid=kalshi_leg_bid,
        liquid_value_per_share=liquid_value_per_share,
        total_liquid_value=total_liquid_value,
        shares=position.shares,
        warning=warning.strip(),
    )


def _load_open_positions() -> list[ArbPosition]:
    """Load value-bearing arb positions from the database (lenient: errors -> []).

    Base mode: status = 'open' (legacy query, unchanged). Robinhood mode also loads
    'pending_redeem' (resolved, payout not yet observed at the venue) — still valued.
    Reporter callers use _load_value_positions(strict=True) instead.
    """
    try:
        return _load_value_positions(strict=False)
    except NavReadError as e:   # pragma: no cover - strict=False never raises
        log(f"❌ Error loading positions: {e}")
        return []


def _load_value_positions(strict: bool = True) -> list[ArbPosition]:
    """Positions whose value is not yet in any cash balance.

    strict=True: a DB error raises NavReadError (and, in robinhood mode, so does a missing
    DATABASE_URL — positions cannot be assumed to be zero). strict=False keeps the legacy
    "log and return []" behaviour.
    """
    rh = is_robinhood_funding()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        if strict and rh:
            raise NavReadError("positions", "DATABASE_URL not set — open positions unknown")
        log("⚠️ DATABASE_URL not set — returning empty position list")
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        try:
            cur = conn.cursor()
            if rh:
                cur.execute("""
                    SELECT pair_id, poly_yes_token, kalshi_ticker, shares, cost_basis_usdc,
                           expiry_ts, status, COALESCE(settled_pnl_usdc, 0),
                           COALESCE(kalshi_side, 'YES'), COALESCE(resolved_venue, '')
                    FROM arb_positions
                    WHERE status IN ('open', 'pending_redeem')
                """)
            else:
                cur.execute("""
                    SELECT pair_id, poly_yes_token, kalshi_ticker, shares, cost_basis_usdc,
                           expiry_ts, status, COALESCE(settled_pnl_usdc, 0),
                           COALESCE(kalshi_side, 'YES')
                    FROM arb_positions
                    WHERE status = 'open'
                """)
            rows = cur.fetchall()
            cur.close()
        finally:
            conn.close()
        positions = []
        for row in rows:
            positions.append(ArbPosition(
                pair_id=row[0],
                poly_yes_token=row[1],
                kalshi_ticker=row[2],
                shares=float(row[3]),
                cost_basis_usdc=float(row[4]),
                expiry_ts=int(row[5] or 0),
                status=row[6],
                settled_pnl_usdc=float(row[7]),
                kalshi_side=row[8] if row[8] in ("YES", "NO") else "YES",
                resolved_venue=(row[9] if rh and len(row) > 9 else "") or "",
            ))
        log(f"Loaded {len(positions)} value-bearing positions from DB")
        return positions
    except Exception as e:
        if strict:
            raise NavReadError("positions", f"DB read failed: {e}") from e
        log(f"❌ Error loading positions: {e}")
        return []


def positions_value_raw(positions: list) -> int:
    """Sum of cost basis in raw 6-decimal units. Open AND pending_redeem positions are valued
    at cost basis until their cash is observed at a venue (no unrealised gain is booked)."""
    return sum(usdc_to_raw(p.cost_basis_usdc) for p in positions)


def _get_opinion_cash() -> float:
    """Get uninvested USDT balance from Opinion Labs via the official CLOB SDK.

    OpenAPI (/openapi/*) endpoints do not support balance queries — only the
    CLOB SDK (opinion_clob_sdk) can read account balances.
    Returns balance as float (USDT, which Opinion uses as quote token on BSC).
    Returns 0.0 on any error.
    """
    try:
        from ..adapters.opinion_clob import get_balance as opinion_sdk_balance
        bal = opinion_sdk_balance()
        if bal > 0:
            log(f"✅ Opinion cash (CLOB SDK): {bal:.4f} USDT")
        else:
            log("ℹ️ Opinion cash (CLOB SDK): 0.0000 — SDK returned 0 or credentials missing")
        return bal
    except Exception as e:
        log(f"⚠️ Opinion cash could not be fetched from any endpoint — opinion_cash = 0 ({e})")
        return 0.0


def _get_opinion_cash_raw() -> int:
    """Strict Opinion balance (only called when Opinion is enabled). Raises NavReadError."""
    try:
        from ..adapters.opinion_clob import get_balance as opinion_sdk_balance
        return usdc_to_raw(opinion_sdk_balance())
    except Exception as e:
        raise NavReadError("opinion_cash", str(e)) from e


def _get_servicer_wallet_usdc_on_base_raw() -> int:
    """Strict base-mode servicer USDC on Base, raw units. Unset wallet -> 0 (legacy:
    nothing is tracked). HTTP / RPC error -> NavReadError."""
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    if not servicer_wallet:
        log("ℹ️ ARB_SERVICER_WALLET not set — servicer_on_base = 0")
        return 0
    rpc_url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    usdc_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    try:
        import requests as _req
        padded = "000000000000000000000000" + servicer_wallet.lower().replace("0x", "")
        resp = _req.post(rpc_url, json={
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": usdc_address, "data": "0x70a08231" + padded}, "latest"], "id": 1,
        }, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        body = resp.json()
        if "error" in body or "result" not in body:
            raise RuntimeError(f"RPC error {body.get('error')}")
        raw = body["result"]
        if not raw or raw == "0x":
            raise RuntimeError("empty balanceOf result")
        return int(raw, 16)
    except Exception as e:
        raise NavReadError("servicer_usdc_base", str(e)) from e


def _get_servicer_wallet_usdc_on_base() -> float:
    """Get USDC balance of the servicer wallet sitting on Base mainnet.

    This captures USDC that has been forwarded from the vault but not yet
    distributed to any trading platform. Without this, fresh deposits cause
    a temporary NAV dip until funds reach Polymarket/Kalshi/Opinion.

    Uses a direct eth_call (no web3 dependency — requests only).
    Returns balance in USDC (float, 6-decimal normalised). Returns 0.0 on error.
    """
    if is_robinhood_funding():
        # Name kept for callers (arb_reporter): robinhood mode returns all servicer cash
        # incl. USDG on 4663, USDC on Base and in-transit amounts.
        return _get_servicer_cash_breakdown()["total"]

    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    if not servicer_wallet:
        log("ℹ️ ARB_SERVICER_WALLET not set — servicer_on_base = 0")
        return 0.0

    rpc_url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    usdc_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    try:
        import requests as _req
        padded = "000000000000000000000000" + servicer_wallet.lower().replace("0x", "")
        data = "0x70a08231" + padded
        resp = _req.post(rpc_url, json={
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_address, "data": data}, "latest"],
            "id": 1,
        }, timeout=10)
        if resp.status_code == 200:
            raw = resp.json().get("result", "0x0")
            balance_raw = int(raw, 16) if raw and raw != "0x" else 0
            balance_usdc = balance_raw / 1_000_000
            log(f"Servicer wallet USDC on Base: {balance_usdc:.4f} USDC ({servicer_wallet})")
            return balance_usdc
        else:
            log(f"⚠️ Servicer Base balance HTTP {resp.status_code} — servicer_on_base = 0")
    except Exception as e:
        log(f"⚠️ Error fetching servicer wallet balance on Base: {e}")
    return 0.0


def _peg_factor(price_source=None) -> float:
    """USDG/USDC peg haircut. 1.0 unless ARB_PEG_MAX_DEVIATION_BPS > 0 and the peg is off by
    more than that, then min(price, 1). Unknown price with a threshold set -> worst tolerated."""
    max_dev = peg_max_deviation_bps()
    if max_dev <= 0:
        return 1.0
    src = price_source or PEG_PRICE_SOURCE
    try:
        if src is None:
            raise RuntimeError("no USDG/USDC price source injected")
        price = float(src())
    except Exception as e:
        factor = 1.0 - max_dev / 10_000
        log(f"⚠️ USDG peg price unavailable ({e}) — applying worst tolerated factor {factor:.4f}")
        return factor
    if abs(1.0 - price) * 10_000 > max_dev:
        factor = min(price, 1.0)
        log(f"⚠️ USDG peg off: price={price:.6f} > {max_dev} bps — haircut factor {factor:.6f}")
        return factor
    return 1.0


def _assert_ledger_readable(path: str, name: str) -> None:
    """arb_withdrawals._load_ledger() turns a corrupt file into [] (= nothing in transit).
    Strict callers check the file themselves so a corrupt ledger aborts instead."""
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise NavReadError(name, f"ledger {path} unreadable: {e}") from e
    if not isinstance(data, list):
        raise NavReadError(name, f"ledger {path} is not a list")


def assert_ledgers_readable() -> None:
    """Strict check of every funding ledger NAV depends on (withdrawals, bridges and — when the
    funding side provides them — venue deposits and write-ahead intents). Raises NavReadError."""
    from . import arb_withdrawals as _w
    paths = [(getattr(_w, "WITHDRAWAL_LEDGER_PATH", ""), "withdrawal_ledger"),
             (getattr(_w, "BRIDGE_LEDGER_PATH", ""), "bridge_ledger")]
    try:
        from . import arb_ledger as _L
        paths += [(_L.venue_deposit_path(), "venue_deposit_ledger"), (_L.intents_path(), "intent_ledger")]
    except ImportError:
        pass
    for path, name in paths:
        _assert_ledger_readable(path, name)


def _get_servicer_cash_breakdown(peg_price_source=None, strict: bool = False,
                                 usdg_raw: Optional[int] = None, apply_peg: bool = True) -> dict:
    """Robinhood mode servicer cash incl. in-transit money (§4 of ARBITRAGE_FUNDING_READINESS):

      servicer USDG on 4663 (× peg factor) + servicer USDC still on Base
      + pending withdrawals (ledger) + bridge transfers sent but not yet arrived.

    Pending Kalshi recalls land as USDC on Base first; the part already visible in the Base
    balance is not counted twice (pending_base − base_usdc, floored at 0).

    strict=True (reporter): every read error raises NavReadError instead of becoming 0, and
    a missing ARB_SERVICER_WALLET is an error. usdg_raw lets the reporter pass the servicer
    USDG balance it read at its pinned 4663 block. apply_peg=False: the V3 vault's unit of
    account IS USDG, so USDG held by the servicer is never haircut against itself (a haircut
    here would turn every vault→servicer tend into an artificial loss).
    """
    from .arb_funder import get_servicer_cash_usdc, _get_usdc_balance
    from . import arb_withdrawals as _w
    from .arb_withdrawals import (
        mark_withdrawals_arrived,
        get_pending_withdrawal_usdc_by_dest,
        reconcile_bridge_transfers,
        get_inflight_bridge_usdc,
    )

    out = {"servicer_usdg_rh": 0.0, "servicer_usdc_base": 0.0, "pending_withdrawals": 0.0,
           "bridges_in_transit": 0.0, "peg_factor": 1.0, "total": 0.0}
    wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    if not wallet:
        if strict:
            raise NavReadError("servicer_wallet", "ARB_SERVICER_WALLET not set")
        log("ℹ️ ARB_SERVICER_WALLET not set — servicer cash = 0")
        return out
    try:
        out["servicer_usdg_rh"] = (usdg_raw / 1_000_000) if usdg_raw is not None else get_servicer_cash_usdc(wallet)
    except Exception as e:
        if strict:
            raise NavReadError("servicer_usdg_rh", str(e)) from e
        log(f"⚠️ Error reading servicer USDG on Robinhood Chain: {e}")
    try:
        out["servicer_usdc_base"] = _get_usdc_balance(wallet)
    except Exception as e:
        if strict:
            raise NavReadError("servicer_usdc_base", str(e)) from e
        log(f"⚠️ Error reading servicer USDC on Base: {e}")
    try:
        if strict:
            assert_ledgers_readable()
        reconcile_bridge_transfers()
        mark_withdrawals_arrived(out["servicer_usdg_rh"], out["servicer_usdc_base"])
        pending = get_pending_withdrawal_usdc_by_dest()
        # Base-landing recalls WITH a balance snapshot are settled by balance delta in
        # mark_withdrawals_arrived (just called), so a still-pending one has not landed: count
        # it in full. Only legacy snapshot-less rows keep the "already visible on Base" offset
        # (which under-counts when unrelated USDC sits on Base).
        split_fn = getattr(_w, "get_pending_withdrawals_split", None)
        if split_fn is not None:
            split = split_fn()
            servicer_p, base_snap, base_legacy = (split["servicer"], split["base_usdc_tracked"],
                                                  split["base_usdc_legacy"])
        else:   # older arb_withdrawals without the split reader
            servicer_p, base_snap, base_legacy = pending["servicer"], 0.0, pending["base_usdc"]
        out["pending_withdrawals"] = (servicer_p + base_snap
                                      + max(0.0, base_legacy - out["servicer_usdc_base"]))
        out["bridges_in_transit"] = get_inflight_bridge_usdc()
    except NavReadError:
        raise
    except Exception as e:
        if strict:
            raise NavReadError("in_transit_ledgers", str(e)) from e
        log(f"⚠️ Error reading in-transit ledgers: {e}")
    out["peg_factor"] = _peg_factor(peg_price_source) if apply_peg else 1.0
    out["total"] = (
        out["servicer_usdg_rh"] * out["peg_factor"] + out["servicer_usdc_base"]
        + out["pending_withdrawals"] + out["bridges_in_transit"]
    )
    # Exact raw total (reporter). With peg_factor 1.0 every term is a 6-decimal amount.
    out["total_raw"] = (
        usdc_to_raw(out["servicer_usdg_rh"] * out["peg_factor"]) if out["peg_factor"] != 1.0
        else (usdg_raw if usdg_raw is not None else usdc_to_raw(out["servicer_usdg_rh"]))
    ) + usdc_to_raw(out["servicer_usdc_base"]) + usdc_to_raw(out["pending_withdrawals"]) \
        + usdc_to_raw(out["bridges_in_transit"])
    log(
        f"Servicer (robinhood): usdg={out['servicer_usdg_rh']:.4f} ×{out['peg_factor']:.4f} "
        f"base_usdc={out['servicer_usdc_base']:.4f} pending_withdrawals={out['pending_withdrawals']:.4f} "
        f"bridges_in_transit={out['bridges_in_transit']:.4f} total={out['total']:.4f}"
    )
    return out


def _get_poly_cash_raw(require_configured: bool = False) -> int:
    """Strict Polymarket collateral balance in raw 6-decimal units.

    Not configured (no POLY_API_KEY) -> 0, or NavReadError when require_configured.
    Proxy (sig_type=2) read error -> NavReadError. The EOA (sig_type=0) read is diagnostic:
    it is only used when the proxy balance is 0, and its failure is not fatal (legacy).
    """
    poly_api_key        = os.environ.get("POLY_API_KEY", "")
    poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
    poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
    if not poly_api_key:
        if require_configured:
            raise NavReadError("poly_cash", "POLY_API_KEY not set — Polymarket cash unknown")
        log("ℹ️ POLY_API_KEY not set — poly_cash = 0")
        return 0
    try:
        from .clob_compat import (
            make_client, ApiCreds, BalanceAllowanceParams, AssetType, SDK_VERSION,
        )
        clob_url           = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
        private_key        = os.environ.get("POLY_PRIVATE_KEY", "")
        poly_proxy_address = os.environ.get("POLY_PROXY_ADDRESS", "") or None
        creds = ApiCreds(
            api_key=poly_api_key.strip(),
            api_secret=poly_api_secret.strip(),
            api_passphrase=poly_api_passphrase.strip(),
        )
        client = make_client(
            host=clob_url,
            key=private_key,
            chain_id=137,
            creds=creds,
            signature_type=2,
            funder=poly_proxy_address,
        )
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
        log(f"🔍 [NAV] sig_type=2 (GNOSIS_SAFE) response: {result}")
        if not isinstance(result, dict) or "balance" not in result:
            raise RuntimeError(f"no balance field in response {result!r}")
        raw = result.get("balance", "0")
        poly_raw = int(Decimal(str(raw)))
        log(f"Poly servicer cash (proxy): {poly_raw / 1_000_000:.6f} USDC (raw={raw})")
    except Exception as e:
        raise NavReadError("poly_cash", str(e)) from e

    # Diagnostic: also query sig_type=0 (EOA) to locate funds
    try:
        client_eoa = make_client(host=clob_url, key=private_key, chain_id=137, creds=creds, signature_type=0)
        result_eoa = client_eoa.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
        )
        log(f"🔍 [NAV] sig_type=0 (EOA) response: {result_eoa}")
        raw_eoa = result_eoa.get("balance", "0")
        eoa_raw = int(Decimal(str(raw_eoa)))
        log(f"Poly servicer cash (EOA): {eoa_raw / 1_000_000:.6f} USDC (raw={raw_eoa})")
        if poly_raw == 0 and eoa_raw > 0:
            log(f"ℹ️ [NAV] Funds found in EOA account — using EOA balance for NAV")
            poly_raw = eoa_raw
    except Exception as eoa_err:
        log(f"⚠️ [NAV] EOA balance check failed: {eoa_err}")
    return poly_raw


def _get_kalshi_cash_raw(require_configured: bool = False) -> int:
    """Strict Kalshi cash (API returns cents) in raw 6-decimal units (cents × 10_000)."""
    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        if require_configured:
            raise NavReadError("kalshi_cash", "Kalshi credentials not configured — Kalshi cash unknown")
        log("ℹ️ Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH) — kalshi_cash = 0")
        return 0
    try:
        import requests
        from ..config import KALSHI_BASE_URL
        url = f"{KALSHI_BASE_URL}/portfolio/balance"
        headers = get_kalshi_headers("GET", url)
        if not headers:
            raise RuntimeError("Kalshi RSA signing failed")
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:100]}")
        data = resp.json()
        if "balance" not in data:
            raise RuntimeError(f"no balance field in response {data!r}"[:200])
        cents = Decimal(str(data["balance"]))
        kalshi_raw = int(cents * 10_000)
        log(f"Kalshi servicer cash: {kalshi_raw / 1_000_000} USDC")
        return kalshi_raw
    except Exception as e:
        raise NavReadError("kalshi_cash", str(e)) from e


def get_venue_cash_raw(strict: bool = True) -> dict:
    """{"polymarket", "kalshi", "opinion"} cash in raw 6-decimal units.

    strict=True: any read error raises NavReadError (component named). In robinhood mode a
    venue that is not configured is also an error (its cash cannot be assumed 0); Opinion is
    only read when enabled (disabled -> 0 by design).
    """
    req = strict and is_robinhood_funding()
    out = {"polymarket": 0, "kalshi": 0, "opinion": 0}
    out["polymarket"] = _get_poly_cash_raw(require_configured=req)
    out["kalshi"] = _get_kalshi_cash_raw(require_configured=req)
    if opinion_enabled():
        out["opinion"] = _get_opinion_cash_raw() if strict else usdc_to_raw(_get_opinion_cash())
    else:
        log("ℹ️ Opinion disabled (ARB_OPINION_ENABLED=false) — opinion_cash = 0")
    return out


def _get_servicer_balances(strict: bool = False) -> tuple[float, float, float]:
    """Get servicer wallet cash balances on Polymarket, Kalshi, and Opinion Labs.

    Returns (poly_cash_usdc, kalshi_cash_usdc, opinion_cash_usdc).
    These are queried from the respective APIs and represent uninvested USDC.

    strict=False (legacy, display NAV): a failed read logs and counts 0.
    strict=True: a failed read raises NavReadError.
    """
    if strict:
        raw = get_venue_cash_raw(strict=True)
        return raw["polymarket"] / 1_000_000, raw["kalshi"] / 1_000_000, raw["opinion"] / 1_000_000

    poly_cash = 0.0
    kalshi_cash = 0.0
    try:
        poly_cash = _get_poly_cash_raw() / 1_000_000
    except NavReadError as e:
        log(f"⚠️ Error fetching poly servicer balance: {e.detail}")
    try:
        kalshi_cash = _get_kalshi_cash_raw() / 1_000_000
    except NavReadError as e:
        log(f"⚠️ Error fetching kalshi servicer balance: {e.detail}")

    if opinion_enabled():
        opinion_cash = _get_opinion_cash()
    else:
        log("ℹ️ Opinion disabled (ARB_OPINION_ENABLED=false) — opinion_cash = 0")
        opinion_cash = 0.0

    return poly_cash, kalshi_cash, opinion_cash


def _get_settled_pnl() -> float:
    """Get sum of all settled PnL from closed positions."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return 0.0
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(settled_pnl_usdc), 0)
            FROM arb_positions
            WHERE status = 'settled'
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as e:
        log(f"⚠️ Error fetching settled PnL: {e}")
        return 0.0


def _sign_nav_abi_encoded(
    total_assets_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    open_positions_value: float,
    settled_pnl: float,
    round_id: int,
    timestamp: int,
    deadline: int,
) -> Optional[str]:
    """Sign NAV payload using ECDSA over ABI-encoded ArbNavDataV1 struct hash.

    Matches the encoding verified on-chain in PMFIArbVaultV1._verifyAndApplyNav:
      keccak256(abi.encode(NAV_TYPEHASH, totalAssets, polyCash, kalshiCash,
                           openPositionsValue, settledPnl, timestamp, deadline,
                           roundId, vault, chainId, domainSalt))
      .toEthSignedMessageHash()

    Returns hex-encoded signature string or None if signing key not available.
    """
    nav_signer_key = os.environ.get("ARB_NAV_SIGNER_PRIVATE_KEY", "")
    if not nav_signer_key:
        log("⚠️ ARB_NAV_SIGNER_PRIVATE_KEY not set — NAV will be unsigned")
        return None

    vault_address = os.environ.get("ARB_VAULT_V1_ADDRESS", "0x0000000000000000000000000000000000000000")
    chain_id = int(os.environ.get("ARB_CHAIN_ID", "8453"))  # Base mainnet

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import eth_utils
        from eth_abi import encode

        NAV_TYPEHASH = eth_utils.keccak(text=(
            "ArbNavDataV1("
            "uint256 totalAssets,"
            "uint256 polyCash,"
            "uint256 kalshiCash,"
            "uint256 openPositionsValue,"
            "uint256 settledPnl,"
            "uint256 timestamp,"
            "uint256 deadline,"
            "uint256 roundId,"
            "address vault,"
            "uint256 chainId,"
            "bytes32 domainSalt"
            ")"
        ))

        DOMAIN_SALT_BYTES32 = eth_utils.keccak(text=ARB_VAULT_DOMAIN_SALT)

        def to_usdc6(v: float) -> int:
            return int(round(v * 1_000_000))

        total_int = to_usdc6(total_assets_usdc)
        poly_int = to_usdc6(poly_cash)
        kalshi_int = to_usdc6(kalshi_cash)
        open_int = to_usdc6(open_positions_value)
        settled_int = to_usdc6(settled_pnl)

        encoded = encode(
            ["bytes32", "uint256", "uint256", "uint256", "uint256", "uint256",
             "uint256", "uint256", "uint256", "address", "uint256", "bytes32"],
            [NAV_TYPEHASH,
             total_int, poly_int, kalshi_int, open_int, settled_int,
             timestamp, deadline, round_id,
             vault_address, chain_id, DOMAIN_SALT_BYTES32]
        )

        struct_hash = eth_utils.keccak(encoded)
        msg = encode_defunct(primitive=struct_hash)
        signed = Account.sign_message(msg, private_key=nav_signer_key)
        sig = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        log(f"✅ NAV signed: roundId={round_id} sig={sig[:20]}...")
        return sig

    except ImportError as ie:
        log(f"⚠️ Missing dependency for NAV signing ({ie}) — NAV will be unsigned")
        return None
    except Exception as e:
        log(f"❌ NAV signing error: {e}")
        return None


_round_id_counter: int = 0
_round_id_lock = None


def _get_round_id_lock():
    global _round_id_lock
    if _round_id_lock is None:
        import threading
        _round_id_lock = threading.Lock()
    return _round_id_lock


def _read_on_chain_last_round_id() -> int:
    """Read the contract's lastRoundId from the blockchain.

    contract.claim() increments lastRoundId on-chain, so we must read it
    before signing a NAV payload to ensure our round_id strictly exceeds
    what the contract has already seen.
    Returns 0 if contract address not set or chain is unreachable.
    """
    vault_address = os.environ.get("ARB_VAULT_V1_ADDRESS", "")
    rpc_url = os.environ.get("RPC_URL", os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"))
    if not vault_address or vault_address == "0x0000000000000000000000000000000000000000":
        log("ℹ️ ARB_VAULT_V1_ADDRESS not set — on-chain lastRoundId = 0")
        return 0
    try:
        import requests as _req
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": vault_address,
                    # lastRoundId() selector = keccak256("lastRoundId()")[:4] = 0x388ca80f
                    "data": "0x388ca80f",
                },
                "latest",
            ],
            "id": 1,
        }
        resp = _req.post(rpc_url, json=payload, timeout=5)
        if resp.status_code == 200:
            result = resp.json().get("result", "0x0")
            on_chain_id = int(result, 16) if result and result != "0x" else 0
            log(f"✅ On-chain lastRoundId={on_chain_id}")
            return on_chain_id
    except Exception as e:
        log(f"⚠️ Could not read on-chain lastRoundId: {e}")
    return 0


def _get_next_round_id() -> int:
    """Get a strictly monotonically increasing round ID for the NAV oracle.

    Sources:
    1. On-chain contract.lastRoundId (which claim() can increment)
    2. Off-chain DB max (highest previously issued NAV round_id)
    3. In-memory counter (for within-process monotonicity)

    Takes max of all three + 1, so the returned ID is always > everything the
    contract has seen and > any previously signed payload.
    """
    global _round_id_counter
    lock = _get_round_id_lock()

    with lock:
        # Read on-chain value on every call to stay ahead of claim() increments
        on_chain = _read_on_chain_last_round_id()

        # Seed/refresh from DB
        db_max = 0
        database_url = os.environ.get("DATABASE_URL", "")
        if database_url:
            try:
                import psycopg2
                conn = psycopg2.connect(database_url)
                cur = conn.cursor()
                cur.execute("SELECT COALESCE(MAX(round_id), 0) FROM arb_vault_nav")
                row = cur.fetchone()
                cur.close()
                conn.close()
                db_max = int(row[0]) if row else 0
            except Exception as e:
                log(f"⚠️ Could not read DB max round_id: {e}")

        # Take max of all sources and increment to exceed all of them
        new_counter = max(on_chain, db_max, _round_id_counter) + 1
        _round_id_counter = new_counter
        log(f"🔢 round_id={new_counter} (on_chain={on_chain}, db_max={db_max})")
        return new_counter


def compute_nav(peg_price_source=None) -> dict:
    """Compute current vault NAV and return signed payload.

    NAV = poly_cash + kalshi_cash + opinion_cash + servicer_on_base
          + open_positions_value  (@ $0.99/share, no live API calls)

    Open position valuation: each arb pair = N contracts where one side resolves
    to $1 and the other to $0, so floor value = $1/pair. $0.99 is conservative.

    settled_pnl intentionally excluded: when a position settles and the winning
    side is claimed, the full proceeds land in platform cash (poly_cash / kalshi_cash
    / opinion_cash). Adding settled_pnl on top double-counts the profit portion.

    Returns a payload dict that the frontend can pass directly into deposit()/requestWithdraw()
    after restructuring into the ArbNavDataV1 tuple.
    """
    log("Computing arb vault NAV...")
    start = time.time()

    poly_cash, kalshi_cash, opinion_cash = _get_servicer_balances()

    # USDC in servicer wallet on Base, awaiting distribution to platforms.
    # Folded into poly_cash so it's visible in NAV immediately after deposit.
    # Robinhood mode: USDG on 4663 (peg-adjusted) + USDC on Base + in-transit.
    servicer_breakdown = None
    if is_robinhood_funding():
        servicer_breakdown = _get_servicer_cash_breakdown(peg_price_source)
        servicer_on_base = servicer_breakdown["total"]
    else:
        servicer_on_base = _get_servicer_wallet_usdc_on_base()

    positions = _load_open_positions()
    # Value each open position at $0.99/share — deterministic, no live API calls.
    # One leg resolves to $1, the other to $0; $0.99 shaves 1¢ for conservatism.
    # NOTE: the code below actually sums cost_basis_usdc, not shares × $0.99. Left unchanged
    # on purpose (see ARBITRAGE_FUNDING_READINESS.md §3); confirm which is intended.
    open_positions_value = sum(pos.cost_basis_usdc for pos in positions)

    total_assets = (
        poly_cash + kalshi_cash + opinion_cash
        + servicer_on_base + open_positions_value
    )

    log(
        f"NAV breakdown: poly_cash={poly_cash:.4f}, kalshi_cash={kalshi_cash:.4f}, "
        f"opinion_cash={opinion_cash:.4f}, servicer_on_base={servicer_on_base:.4f}, "
        f"open_positions={open_positions_value:.4f} ({len(positions)} @ $0.99/share), "
        f"TOTAL={total_assets:.4f} USDC"
    )

    timestamp = int(time.time())
    round_id = _get_next_round_id()
    deadline = timestamp + NAV_VALIDITY_WINDOW

    # The contract struct has no opinionCash field — fold opinion_cash and
    # servicer_on_base into polyCash for ABI encoding. These are informational
    # only on-chain; totalAssets is what drives the share price math.
    poly_cash_for_struct = poly_cash + opinion_cash + servicer_on_base

    signature = _sign_nav_abi_encoded(
        total_assets_usdc=total_assets,
        poly_cash=poly_cash_for_struct,
        kalshi_cash=kalshi_cash,
        open_positions_value=open_positions_value,
        settled_pnl=0.0,
        round_id=round_id,
        timestamp=timestamp,
        deadline=deadline,
    )

    elapsed_ms = int((time.time() - start) * 1000)

    _save_nav_snapshot(
        total_assets_usdc=total_assets,
        poly_cash=poly_cash_for_struct,
        kalshi_cash=kalshi_cash,
        open_positions_value=open_positions_value,
        settled_pnl=0.0,
        round_id=round_id,
        signature=signature or "",
        opinion_cash=opinion_cash,
        servicer_on_base=servicer_on_base,
    )

    payload = {
        "total_assets_usdc": round(total_assets, 6),
        # poly_cash in the signed struct = poly_cash + opinion_cash + servicer_on_base
        # (no separate slots in the contract for those). The frontend passes this
        # directly into the ABI struct, so it must match the signature.
        "poly_cash": round(poly_cash_for_struct, 6),
        "poly_cash_polymarket_only": round(poly_cash, 6),      # display breakdown only
        "servicer_on_base": round(servicer_on_base, 6),        # display breakdown only
        "kalshi_cash": round(kalshi_cash, 6),
        "opinion_cash": round(opinion_cash, 6),
        "open_positions_value": round(open_positions_value, 6),
        "open_position_count": len(positions),
        "settled_pnl": 0.0,
        "round_id": round_id,
        "timestamp": timestamp,
        "deadline": deadline,
        "domain_salt": ARB_VAULT_DOMAIN_SALT,
        "signature": signature,
        "computed_in_ms": elapsed_ms,
        "open_positions": [
            {
                "pair_id": pos.pair_id,
                "shares": pos.shares,
                "value_per_share": 0.99,
                "total_value": round(pos.shares * 0.99, 6),
                "cost_basis_usdc": pos.cost_basis_usdc,
            }
            for pos in positions
        ],
    }

    if servicer_breakdown is not None:
        # Robinhood mode only (display breakdown; base-mode payload is unchanged).
        payload["funding_chain"] = "robinhood"
        payload["servicer_breakdown"] = {k: round(v, 6) for k, v in servicer_breakdown.items()}

    log(f"NAV computed in {elapsed_ms}ms: total={total_assets:.4f} USDC, roundId={round_id}")
    return payload


def _save_nav_snapshot(
    total_assets_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    open_positions_value: float,
    settled_pnl: float,
    round_id: int,
    signature: str,
    opinion_cash: float = 0.0,
    servicer_on_base: float = 0.0,
):
    """Persist NAV snapshot to arb_vault_nav table."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute("""
            ALTER TABLE arb_vault_nav
                ADD COLUMN IF NOT EXISTS opinion_cash NUMERIC(20,6) NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS servicer_on_base NUMERIC(20,6) NOT NULL DEFAULT 0
        """)
        cur.execute("""
            UPDATE arb_vault_nav
            SET total_assets_usdc=%s, poly_cash=%s, kalshi_cash=%s,
                open_positions_value=%s, settled_pnl=%s, signature=%s,
                opinion_cash=%s, servicer_on_base=%s
            WHERE round_id=%s
        """, (total_assets_usdc, poly_cash, kalshi_cash, open_positions_value,
              settled_pnl, signature, opinion_cash, servicer_on_base, round_id))
        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO arb_vault_nav
                    (computed_at, total_assets_usdc, poly_cash, kalshi_cash,
                     open_positions_value, settled_pnl, round_id, signature,
                     opinion_cash, servicer_on_base)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (total_assets_usdc, poly_cash, kalshi_cash, open_positions_value,
                  settled_pnl, round_id, signature, opinion_cash, servicer_on_base))
        conn.commit()
        cur.close()
        conn.close()
        log(f"✅ NAV snapshot saved: total={total_assets_usdc:.4f} poly={poly_cash:.4f} "
            f"kalshi={kalshi_cash:.4f} opinion={opinion_cash:.4f} "
            f"servicer={servicer_on_base:.4f} positions={open_positions_value:.4f}")
    except Exception as e:
        log(f"⚠️ Error saving NAV snapshot: {e}")
