"""Venue taker fees at the actual trade size.

Polymarket (CLOB V2, docs.polymarket.com "Fees"; py-clob-client-v2 fees.py):
    fee (USDC) = shares × rate × (p × (1 − p)) ** exponent
    rate / exponent come per market from GET {CLOB}/clob-markets/{condition_id} → fd.r / fd.e.
    Makers pay nothing; the engine only takes liquidity (FOK), so every fill pays the taker fee.
    When the market's fee data cannot be read, the most expensive published category is used
    (ARB_POLY_FEE_FALLBACK_RATE, default 0.07 = crypto, exponent 1): never under-estimated.

Kalshi (kalshi.com fee schedule; GET /series/{ticker} → fee_type, fee_multiplier):
    fee (USD) = ceil_to_cent(0.07 × contracts × p × (1 − p)) for the standard quadratic taker fee.
    The executor keeps its existing _kalshi_taker_fee_pad (same formula); kalshi_taker_fee() here
    is the shared implementation used for opportunity scoring.

All prices are dollars in (0, 1). Results are USD totals unless the name says per contract.
"""

import os
import threading
import time
from decimal import Decimal, ROUND_CEILING
from typing import Optional

import requests

from ..config import POLY_CLOB_URL


def log(msg: str):
    print(f"💸 [Arb/Fees] {msg}")


KALSHI_TAKER_RATE = 0.07


def poly_fee_fallback() -> tuple[float, float]:
    return (
        float(os.environ.get("ARB_POLY_FEE_FALLBACK_RATE", "0.07")),
        float(os.environ.get("ARB_POLY_FEE_FALLBACK_EXPONENT", "1")),
    )


def poly_exact_fees_enabled() -> bool:
    """Polymarket taker fees in the execution gate. Default on in robinhood mode (the relaunch);
    base mode keeps its previous behaviour unless ARB_POLY_EXACT_FEES=true."""
    from ..config import is_robinhood_funding

    default = "true" if is_robinhood_funding() else "false"
    return os.environ.get("ARB_POLY_EXACT_FEES", default).strip().lower() == "true"


_FEE_CACHE: dict[str, tuple[float, float, str, float]] = {}
_FEE_LOCK = threading.Lock()
_FEE_TTL = 3600


def poly_fee_info(condition_id: Optional[str], session=None) -> tuple[float, float, str]:
    """(rate, exponent, source) for a Polymarket market. source is "clob" or "fallback"."""
    if not condition_id:
        rate, exp = poly_fee_fallback()
        return rate, exp, "fallback"
    now = time.time()
    with _FEE_LOCK:
        hit = _FEE_CACHE.get(condition_id)
        if hit and now - hit[3] < _FEE_TTL:
            return hit[0], hit[1], hit[2]
    try:
        resp = (session or requests).get(f"{POLY_CLOB_URL}/clob-markets/{condition_id}", timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        body = resp.json()
        fd = body.get("fd")
        if not isinstance(fd, dict) or "r" not in fd:
            # Fail safe: without explicit fee data assume the most expensive category.
            raise RuntimeError("no fd.r in /clob-markets response")
        rate, exp = float(fd.get("r") or 0.0), float(fd.get("e") or 0.0)
        if rate < 0 or exp < 0:
            raise RuntimeError(f"bad fee data r={rate} e={exp}")
        with _FEE_LOCK:
            _FEE_CACHE[condition_id] = (rate, exp, "clob", now)
        return rate, exp, "clob"
    except Exception as e:
        rate, exp = poly_fee_fallback()
        log(f"⚠️ fee data for {condition_id[:12]}… unavailable ({e}); using fallback rate={rate} exp={exp}")
        return rate, exp, "fallback"


def poly_taker_fee_per_contract(price: float, rate: float, exponent: float) -> float:
    p = float(price or 0.0)
    if p <= 0 or p >= 1 or rate <= 0:
        return 0.0
    return rate * (p * (1.0 - p)) ** exponent


def poly_taker_fee(price: float, contracts: float, rate: float, exponent: float) -> float:
    """USDC fee for `contracts` shares bought at `price`, rounded up to 1e-6 (USDC units)."""
    raw = Decimal(str(poly_taker_fee_per_contract(price, rate, exponent))) * Decimal(str(contracts or 0))
    return float(raw.quantize(Decimal("0.000001"), rounding=ROUND_CEILING))


def kalshi_taker_fee(price: float, contracts: int, multiplier: float = KALSHI_TAKER_RATE) -> float:
    """USD fee for `contracts` bought at `price`: ceil to the cent of multiplier × C × P × (1 − P)."""
    n = int(contracts or 0)
    p = float(price or 0.0)
    if n <= 0 or p <= 0 or p >= 1:
        return 0.0
    cents = Decimal(str(multiplier)) * n * Decimal(str(p)) * (1 - Decimal(str(p))) * 100
    return float(cents.to_integral_value(rounding=ROUND_CEILING)) / 100.0


def pair_net_edge_cents(
    poly_price: float,
    venue2_price: float,
    contracts: int,
    poly_rate: float,
    poly_exponent: float,
    venue2: str = "kalshi",
) -> float:
    """Net profit per matched contract pair, in cents per $1 payout, after both legs' taker fees at
    this size. Same unit as the executor's gate: (1 − poly − venue2 − fees/contract) × 100."""
    n = max(int(contracts or 0), 1)
    fees = poly_taker_fee(poly_price, n, poly_rate, poly_exponent)
    if str(venue2).lower() == "kalshi":
        fees += kalshi_taker_fee(venue2_price, n)
    return (1.0 - poly_price - venue2_price - fees / n) * 100.0


__all__ = [
    "poly_fee_info", "poly_taker_fee", "poly_taker_fee_per_contract", "kalshi_taker_fee",
    "pair_net_edge_cents", "poly_exact_fees_enabled", "poly_fee_fallback",
]
