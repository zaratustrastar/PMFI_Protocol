"""Opinion Labs CLOB trading client.

Uses the official `opinion_clob_sdk` PyPI package for all balance and order
operations. Market discovery / orderbook data stays in opinion.py (OpenAPI).

SDK docs: https://docs.opinion.trade/developer-guide/opinion-clob-sdk
Package:  pip install opinion_clob_sdk

Required env vars:
  OPINION_API_KEY           - API key (contact opinion.trade for access)
  OPINION_PRIVATE_KEY       - secp256k1 private key of the signer wallet
  OPINION_PORTFOLIO_ADDRESS - multi-sig wallet address that holds USDT collateral (BSC)
  OPINION_RPC_URL           - BSC JSON-RPC endpoint (default: bsc-dataseed.binance.org)
  OPINION_CLOB_HOST         - CLOB host (default: https://proxy.opinion.trade:8443)

NOTE: Opinion uses USDT on BSC (chain_id=56) as the quote / collateral token.
      Deposits go from Base USDC → bridged internally → BSC USDT in multi-sig.
"""

import os
import time
from typing import Optional

from ..config import (
    OPINION_CLOB_HOST,
    OPINION_API_KEY,
    OPINION_PRIVATE_KEY,
    OPINION_PORTFOLIO_ADDRESS,
    OPINION_RPC_URL,
)

_OPINION_CHAIN_ID = 56  # BNB Chain Mainnet
_CONDITIONAL_TOKENS_ADDR = "0xAD1a38cEc043e70E83a3eC30443dB285ED10D774"
_MULTISEND_ADDR = "0x998739BFdAAdde7C933B942a68053933098f9EDa"

# Module-level singleton client — created lazily, reused across calls.
_client = None
_client_error: Optional[str] = None  # cached init failure message


def log(msg: str) -> None:
    print(f"💬 [Arb/OpinionCLOB] {msg}")


# ── Client singleton ──────────────────────────────────────────────────────────


def _get_client():
    """Return the singleton SDK Client, initialising it on first call.

    Returns None and logs the reason if required credentials are missing or
    the SDK is not installed.
    """
    global _client, _client_error

    if _client is not None:
        return _client
    if _client_error is not None:
        log(f"⚠️  Client unavailable (cached error): {_client_error}")
        return None

    if not OPINION_API_KEY:
        _client_error = "OPINION_API_KEY not set"
        log(f"⚠️  {_client_error}")
        return None
    if not OPINION_PRIVATE_KEY:
        _client_error = "OPINION_PRIVATE_KEY not set — cannot sign orders or authenticate"
        log(f"⚠️  {_client_error}")
        return None
    if not OPINION_PORTFOLIO_ADDRESS:
        _client_error = "OPINION_PORTFOLIO_ADDRESS not set — multi-sig wallet required"
        log(f"⚠️  {_client_error}")
        return None

    try:
        from opinion_clob_sdk import Client as OpinionClient
        log(f"🔧 Initialising Opinion SDK client: host={OPINION_CLOB_HOST} "
            f"chain_id={_OPINION_CHAIN_ID} multi_sig={OPINION_PORTFOLIO_ADDRESS[:10]}...")
        _client = OpinionClient(
            host=OPINION_CLOB_HOST,
            apikey=OPINION_API_KEY,
            chain_id=_OPINION_CHAIN_ID,
            rpc_url=OPINION_RPC_URL,
            private_key=OPINION_PRIVATE_KEY,
            multi_sig_addr=OPINION_PORTFOLIO_ADDRESS,
            conditional_tokens_addr=_CONDITIONAL_TOKENS_ADDR,
            multisend_addr=_MULTISEND_ADDR,
        )
        log("✅ Opinion SDK client initialised")
        return _client
    except ImportError:
        _client_error = (
            "opinion_clob_sdk not installed — run: pip install opinion_clob_sdk"
        )
        log(f"❌ {_client_error}")
        return None
    except Exception as exc:
        _client_error = f"SDK Client init failed: {exc}"
        log(f"❌ {_client_error}")
        return None


def reset_client() -> None:
    """Force re-initialisation of the SDK client on next call (e.g. after env change)."""
    global _client, _client_error
    _client = None
    _client_error = None


# ── Balance ───────────────────────────────────────────────────────────────────


def get_balance() -> float:
    """Return Opinion Labs portfolio USDT balance.

    Uses client.get_my_balances() from the official SDK.
    Returns 0.0 on any failure with detailed logging.
    """
    client = _get_client()
    if client is None:
        return 0.0

    try:
        log("📡 Calling client.get_my_balances()...")
        response = client.get_my_balances()
        log(f"📡 get_my_balances() → errno={response.errno} "
            f"errmsg={getattr(response, 'errmsg', '')} "
            f"result={str(getattr(response, 'result', ''))[:400]}")

        if response.errno != 0:
            log(f"❌ get_my_balances() API error errno={response.errno}: "
                f"{getattr(response, 'errmsg', 'unknown')}")
            return 0.0

        result = getattr(response, "result", None)
        if result is None:
            log("⚠️  get_my_balances() returned errno=0 but result is None")
            return 0.0

        # Result may be a list of balance objects or a single object.
        # Try common field names for USDT / total balance.
        if isinstance(result, list):
            items = result
        elif hasattr(result, "list"):
            items = result.list or []
        elif hasattr(result, "data"):
            items = [result.data] if result.data else []
        else:
            items = [result]

        total = 0.0
        for item in items:
            for field in ("balance", "usdt", "usdtBalance", "availableBalance",
                          "available", "cashBalance", "total", "value",
                          "usdc", "usdcBalance"):
                val = (
                    getattr(item, field, None)
                    if not isinstance(item, dict)
                    else item.get(field)
                )
                if val is not None:
                    try:
                        total += float(val)
                        log(f"💰 Balance item field={field!r} value={val}")
                        break
                    except (ValueError, TypeError):
                        continue

        log(f"💰 Opinion USDT balance: {total:.4f}")
        return total

    except Exception as exc:
        log(f"❌ get_my_balances() exception: {exc}")
        return 0.0


# ── Order placement ───────────────────────────────────────────────────────────


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
        size_usdc:      total USDT to spend (Opinion quote token is USDT)
        contract_count: integer number of contracts (used as fallback sizing)

    Returns (ok: bool, order_id: str, error_msg: str).
    """
    client = _get_client()
    if client is None:
        return False, "", (_client_error or "Opinion SDK client not available")

    # Resolve token IDs for the market (YES or NO token).
    from ..adapters.opinion import lookup_token_ids_by_market_id
    token_pair = lookup_token_ids_by_market_id(market_id)
    if not token_pair:
        err = f"Cannot resolve token IDs for Opinion market_id={market_id!r}"
        log(f"❌ {err}")
        return False, "", err

    yes_token_id, no_token_id = token_pair
    token_id = yes_token_id if side.upper() == "YES" else no_token_id
    log(f"📤 Placing {side} BUY: market={market_id} token={token_id[:16]}... "
        f"price={price:.4f} size_usdt={size_usdc:.2f} contracts={contract_count}")

    try:
        from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
        from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
        from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER

        order = PlaceOrderDataInput(
            marketId=int(market_id),
            tokenId=token_id,
            side=OrderSide.BUY,
            orderType=LIMIT_ORDER,
            price=str(round(price, 4)),
            makerAmountInQuoteToken=round(size_usdc, 6),
        )

        log(f"📡 Calling client.place_order()...")
        result = client.place_order(order, check_approval=True)
        log(f"📡 place_order() → errno={result.errno} "
            f"errmsg={getattr(result, 'errmsg', '')} "
            f"result={str(getattr(result, 'result', ''))[:300]}")

        if result.errno != 0:
            err = f"Opinion order rejected errno={result.errno}: {getattr(result, 'errmsg', 'unknown')}"
            log(f"❌ {err}")
            return False, "", err

        order_data = getattr(result, "result", None)
        order_id = ""
        if order_data:
            if hasattr(order_data, "data") and order_data.data:
                order_id = str(getattr(order_data.data, "order_id", "")
                               or getattr(order_data.data, "orderId", "")
                               or getattr(order_data.data, "id", ""))
            elif isinstance(order_data, dict):
                inner = order_data.get("data", order_data)
                order_id = str(inner.get("order_id") or inner.get("orderId") or inner.get("id") or "")

        log(f"✅ Opinion order placed: orderId={order_id}")
        return True, order_id, ""

    except Exception as exc:
        err = str(exc)
        log(f"❌ place_order() exception: {err}")
        return False, "", err
