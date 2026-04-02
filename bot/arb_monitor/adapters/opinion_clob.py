"""Opinion Labs CLOB trading client.

Handles balance queries and order placement using Opinion's trading API.
Market discovery / orderbook data stays in opinion.py (OpenAPI).

Required env vars for trading:
  OPINION_API_KEY           - API key (market data + trading auth header)
  OPINION_PRIVATE_KEY       - signer wallet private key (signs orders)
  OPINION_PORTFOLIO_ADDRESS - multi-sig / portfolio wallet address (holds funds)
  OPINION_CLOB_URL          - trading API base (defaults to OpenAPI base URL)

Balance is read in three steps:
  1. Opinion CLOB /account/balance with portfolio address
  2. Opinion CLOB /portfolio/{address} style endpoint
  3. On-chain USDC balance of portfolio address (BSC USDC, 18 dec fallback)

Order placement requires OPINION_PRIVATE_KEY and OPINION_PORTFOLIO_ADDRESS.
Until those are configured, _place_order returns (False, "", "OPINION_PRIVATE_KEY not set").
"""

import os
import time
import hashlib
import hmac
import json
import requests
from typing import Optional

from ..config import (
    OPINION_CLOB_URL,
    OPINION_BASE_URL,
    OPINION_API_KEY,
    OPINION_PRIVATE_KEY,
    OPINION_PORTFOLIO_ADDRESS,
)

_BSC_RPC = os.environ.get("OPINION_RPC_URL", "https://bsc-dataseed.binance.org/")
_BSC_USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"  # USDC on BSC (18 dec)
_BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base (6 dec)

_ERC20_BALANCE_OF_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def log(msg: str) -> None:
    print(f"💬 [Arb/OpinionCLOB] {msg}")


def _api_headers() -> dict:
    return {
        "apikey": OPINION_API_KEY,
        "Content-Type": "application/json",
    }


# ── Balance ──────────────────────────────────────────────────────────────────


def _balance_via_clob_api() -> Optional[float]:
    """Try Opinion CLOB API endpoints for portfolio balance."""
    if not OPINION_API_KEY:
        return None

    portfolio = OPINION_PORTFOLIO_ADDRESS or ""
    headers = _api_headers()
    if portfolio:
        headers["x-portfolio-address"] = portfolio
        headers["x-wallet-address"] = portfolio

    candidate_urls = [
        f"{OPINION_CLOB_URL}/account/balance",
        f"{OPINION_CLOB_URL}/account",
        f"{OPINION_CLOB_URL}/portfolio",
        f"{OPINION_CLOB_URL}/balance",
    ]
    if portfolio:
        candidate_urls += [
            f"{OPINION_CLOB_URL}/account/balance?address={portfolio}",
            f"{OPINION_CLOB_URL}/portfolio/{portfolio}",
            f"{OPINION_CLOB_URL}/account/{portfolio}",
            f"{OPINION_CLOB_URL}/account/{portfolio}/balance",
        ]

    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                log(f"⚠️  CLOB balance {url} → HTTP {resp.status_code}")
                continue
            data = resp.json()
            result = data.get("result") or data.get("data") or data
            if isinstance(result, list) and result:
                result = result[0]
            for field in ("balance", "usdc", "usdcBalance", "availableBalance",
                          "available", "cashBalance", "portfolioValue", "value"):
                if isinstance(result, dict) and field in result:
                    bal = float(result[field])
                    log(f"💰 CLOB balance {bal:.4f} USDC (via {url} field={field})")
                    return bal
        except Exception as exc:
            log(f"⚠️  CLOB balance endpoint {url} error: {exc}")
            continue

    return None


def _balance_via_onchain(portfolio_address: str) -> Optional[float]:
    """Read USDC balance of the portfolio address on-chain (BSC + Base fallback)."""
    try:
        from web3 import Web3

        chains = [
            ("Base", "https://mainnet.base.org", _BASE_USDC, 6),
            ("BSC", _BSC_RPC, _BSC_USDC, None),
        ]
        for chain_name, rpc, usdc_addr, default_dec in chains:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
                if not w3.is_connected():
                    log(f"⚠️  On-chain balance: {chain_name} RPC not reachable")
                    continue
                chk_addr = Web3.to_checksum_address(portfolio_address)
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(usdc_addr),
                    abi=_ERC20_BALANCE_OF_ABI,
                )
                raw = contract.functions.balanceOf(chk_addr).call()
                if default_dec is None:
                    default_dec = contract.functions.decimals().call()
                bal = raw / (10 ** default_dec)
                if bal > 0:
                    log(f"💰 On-chain {chain_name} USDC balance: {bal:.4f} "
                        f"(address={portfolio_address[:10]}...)")
                    return bal
            except Exception as exc:
                log(f"⚠️  On-chain {chain_name} balance error: {exc}")
                continue
    except ImportError:
        log("⚠️  web3 not installed — skipping on-chain balance fallback")
    return None


def get_balance() -> float:
    """Return Opinion Labs portfolio balance in USDC.

    Tries:
      1. CLOB API endpoints (requires OPINION_API_KEY + OPINION_PORTFOLIO_ADDRESS)
      2. On-chain USDC read of portfolio address (BSC / Base)

    Returns 0.0 if all methods fail.
    """
    if not OPINION_API_KEY:
        log("⚠️  OPINION_API_KEY not set — returning 0")
        return 0.0

    bal = _balance_via_clob_api()
    if bal is not None:
        return bal

    if OPINION_PORTFOLIO_ADDRESS:
        log("⚠️  CLOB API balance failed — trying on-chain USDC read")
        bal = _balance_via_onchain(OPINION_PORTFOLIO_ADDRESS)
        if bal is not None:
            return bal

    log("⚠️  All Opinion balance methods failed — returning 0 "
        "(set OPINION_PORTFOLIO_ADDRESS for on-chain fallback)")
    return 0.0


# ── Order placement ───────────────────────────────────────────────────────────


def _sign_order(payload: dict) -> Optional[str]:
    """Sign an order payload with the signer wallet private key.

    Opinion's CLOB uses an API-key + HMAC-SHA256 signature scheme where the
    signature covers the JSON-serialised payload sorted by key.

    NOTE: Update this function once the exact Opinion SDK signing spec is
    confirmed. The structure below implements a common HMAC pattern; swap in
    the EIP-712 typed-data signing if Opinion uses that instead.
    """
    if not OPINION_PRIVATE_KEY:
        return None
    try:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        sig = hmac.new(
            OPINION_PRIVATE_KEY.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        return sig
    except Exception as exc:
        log(f"⚠️  Order signing error: {exc}")
        return None


def place_order(
    market_id: str,
    side: str,
    price: float,
    size_usdc: float,
    contract_count: int,
) -> tuple[bool, str, str]:
    """Place a limit buy order on Opinion Labs CLOB.

    Args:
        market_id:      Opinion market ID (numeric string, e.g. "371")
        side:           "YES" or "NO"
        price:          fractional price (0.0–1.0)
        size_usdc:      total USDC value of the order (informational)
        contract_count: integer number of contracts to buy

    Returns (ok: bool, order_id: str, error_msg: str).
    """
    if not OPINION_API_KEY:
        return False, "", "OPINION_API_KEY not set"

    if not OPINION_PRIVATE_KEY:
        return False, "", (
            "OPINION_PRIVATE_KEY not set — Opinion CLOB orders require a "
            "signer wallet. Add OPINION_PRIVATE_KEY to your VPS .env"
        )

    if not OPINION_PORTFOLIO_ADDRESS:
        log("⚠️  OPINION_PORTFOLIO_ADDRESS not set — order may be rejected by server")

    log(f"📤 [OPINION] Placing {side} BUY: market={market_id} "
        f"contracts={contract_count} @ {price:.4f} (~${size_usdc:.2f})")

    price_cents = int(round(price * 100))
    payload = {
        "marketId": str(market_id),
        "side": side.lower(),
        "action": "buy",
        "amount": contract_count,
        "price": price_cents,
        "type": "limit",
        "portfolioAddress": OPINION_PORTFOLIO_ADDRESS,
        "clientOrderId": f"arb_{int(time.time())}_{market_id}",
    }

    headers = _api_headers()
    if OPINION_PORTFOLIO_ADDRESS:
        headers["x-portfolio-address"] = OPINION_PORTFOLIO_ADDRESS
        headers["x-wallet-address"] = OPINION_PORTFOLIO_ADDRESS

    sig = _sign_order(payload)
    if sig:
        headers["x-signature"] = sig

    url = f"{OPINION_CLOB_URL}/orders"
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        log(f"📡 [OPINION] POST /orders → HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code in (200, 201):
            data = resp.json()
            result = data.get("result") or data.get("data") or data
            order_id = str(
                result.get("orderId") or result.get("order_id") or
                result.get("id") or data.get("orderId") or ""
            )
            log(f"✅ [OPINION] Order placed: orderId={order_id}")
            return True, order_id, ""
        else:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            log(f"❌ [OPINION] Order rejected: {err}")
            return False, "", err
    except Exception as exc:
        err = str(exc)
        log(f"❌ [OPINION] Order exception: {err}")
        return False, "", err
