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
      Deposits go from Base USDC -> bridged internally -> BSC USDT in multi-sig.

IMPORTANT: All SDK calls (Client init, get_my_balances, place_order) run inside
a ThreadPoolExecutor with a hard timeout so they can never freeze the main
execution loop if the BSC RPC or Opinion endpoint hangs.
"""

import os
import time
import concurrent.futures
from typing import Optional

from ..config import (
    OPINION_CLOB_HOST,
    OPINION_API_KEY,
    OPINION_PRIVATE_KEY,
    OPINION_PORTFOLIO_ADDRESS,
    OPINION_RPC_URL,
)

# ── Route opinion_clob_sdk through Serbian residential proxy ──────────────────
# Patches HTTPAdapter.send with a URL filter so only opinion.trade requests
# get the proxy injected (Kalshi/Poly/BSC-RPC are unaffected).
# Proxy is read at request time (not import time) so startup order does not matter:
# if OPINION_PROXY_URL is missing when the module loads, the patch is still installed
# and will pick up the variable as soon as it appears in os.environ (e.g. after the
# systemd EnvironmentFile is applied and the process restarts).
_active_proxy_url: str = os.environ.get("OPINION_PROXY_URL", "")

try:
    from requests.adapters import HTTPAdapter as _HTTPAdapter

    _orig_http_adapter_send = _HTTPAdapter.send

    def _opinion_proxied_send(self, request, **kwargs):
        """Always inject current OPINION_PROXY_URL for opinion.trade URLs; others unchanged.

        Proxy is read dynamically on every call — no dependency on import-time env state.
        If OPINION_PROXY_URL is empty, the request passes through unmodified (safe fallback).
        Unconditionally overwrites kwargs["proxies"] for opinion.trade so the Serbian proxy
        wins even if the SDK session already has proxies set (empty dict, system proxy, etc).
        """
        if "opinion.trade" in (request.url or ""):
            proxy = os.environ.get("OPINION_PROXY_URL", "")
            if proxy:
                kwargs = dict(kwargs)
                kwargs["proxies"] = {"http": proxy, "https": proxy}
        return _orig_http_adapter_send(self, request, **kwargs)

    _HTTPAdapter.send = _opinion_proxied_send
    _boot_proxy = os.environ.get("OPINION_PROXY_URL", "")
    _boot_proxy_host = _boot_proxy.split("@")[-1] if "@" in _boot_proxy else _boot_proxy
    if _boot_proxy_host:
        print(f"⚡ [Opinion CLOB] proxy ACTIVE (monkey-patched): {_boot_proxy_host}", flush=True)
    else:
        print("⚡ [Opinion CLOB] patch installed — proxy will activate once OPINION_PROXY_URL is in env", flush=True)
except Exception as _patch_err:
    print(f"⚡ [Opinion CLOB] ⚠️ Failed to patch Opinion CLOB proxy: {_patch_err}", flush=True)

_OPINION_CHAIN_ID = 56  # BNB Chain Mainnet
_CONDITIONAL_TOKENS_ADDR = "0xAD1a38cEc043e70E83a3eC30443dB285ED10D774"
_MULTISEND_ADDR = "0x998739BFdAAdde7C933B942a68053933098f9EDa"

_SDK_INIT_TIMEOUT = 15   # seconds — max wait for Client() constructor
_SDK_CALL_TIMEOUT = 10   # seconds — max wait for any SDK API call

# Module-level singleton client — created lazily, reused across calls.
_client = None
_client_error: Optional[str] = None  # cached init failure message


def log(msg: str) -> None:
    print(f"💬 [Arb/OpinionCLOB] {msg}", flush=True)


def _run_with_timeout(fn, timeout: float, label: str):
    """Run fn() in a thread with a hard timeout.

    Returns the result of fn(), or raises TimeoutError if it exceeds `timeout`
    seconds, or re-raises any exception fn() raises.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"{label} timed out after {timeout}s — BSC RPC or Opinion endpoint may be unreachable"
            )


def _inject_sdk_proxy(client) -> None:
    """Best-effort: find requests Session(s) on the SDK client and force Serbian proxy.

    This is a belt-and-suspenders fallback for when the HTTPAdapter.send monkey-patch
    doesn't reach the SDK's HTTP transport (e.g. if the SDK creates sessions before
    the patch, or uses a nested session the patch can't reach at class-dispatch time).
    Probes common attribute names used by SDK clients.  Silently skips unknown layouts.
    """
    proxy_url = os.environ.get("OPINION_PROXY_URL", "")
    if not proxy_url or client is None:
        return
    import requests as _requests
    proxy_dict = {"http": proxy_url, "https": proxy_url}
    injected = []
    for attr in ("session", "_session", "http_session", "_http", "http_client", "_client"):
        obj = getattr(client, attr, None)
        if isinstance(obj, _requests.Session):
            obj.proxies.update(proxy_dict)
            obj.trust_env = False
            injected.append(attr)
    if injected:
        host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        log(f"✅ Serbian proxy injected into SDK sessions: {injected} → {host}")
    else:
        log("ℹ️ No SDK session attrs found for direct proxy injection — HTTPAdapter patch is sole guard")


# ── Client singleton ──────────────────────────────────────────────────────────


def _get_client():
    """Return the singleton SDK Client, initialising it on first call.

    The constructor runs in a thread with _SDK_INIT_TIMEOUT so a hung BSC RPC
    call never blocks the main execution loop.

    Returns None and logs the reason if required credentials are missing,
    the SDK is not installed, or initialisation times out.

    Proxy-change detection: if OPINION_PROXY_URL differs from the value that
    was active when the client was last created, the singleton is reset so the
    next init picks up the new proxy (via the HTTPAdapter patch).
    """
    global _client, _client_error, _active_proxy_url

    # Detect proxy URL changes at runtime and reset the client so it
    # re-initialises with the updated proxy on the next call.
    current_proxy = os.environ.get("OPINION_PROXY_URL", "")
    if _client is not None and current_proxy != _active_proxy_url:
        log(f"🔄 OPINION_PROXY_URL changed ({_active_proxy_url!r} → {current_proxy.split('@')[-1]!r}) — resetting client for re-init")
        _active_proxy_url = current_proxy
        reset_client()

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

    def _init():
        from opinion_clob_sdk import Client as OpinionClient
        return OpinionClient(
            host=OPINION_CLOB_HOST,
            apikey=OPINION_API_KEY,
            chain_id=_OPINION_CHAIN_ID,
            rpc_url=OPINION_RPC_URL,
            private_key=OPINION_PRIVATE_KEY,
            multi_sig_addr=OPINION_PORTFOLIO_ADDRESS,
            conditional_tokens_addr=_CONDITIONAL_TOKENS_ADDR,
            multisend_addr=_MULTISEND_ADDR,
        )

    try:
        log(
            f"🔧 Initialising Opinion SDK client (timeout={_SDK_INIT_TIMEOUT}s): "
            f"host={OPINION_CLOB_HOST} chain_id={_OPINION_CHAIN_ID} "
            f"multi_sig={OPINION_PORTFOLIO_ADDRESS[:10]}..."
        )
        _client = _run_with_timeout(_init, _SDK_INIT_TIMEOUT, "Opinion SDK Client.__init__")
        _inject_sdk_proxy(_client)
        log("✅ Opinion SDK client initialised")
        return _client
    except ImportError:
        _client_error = (
            "opinion_clob_sdk not installed — run: pip install opinion_clob_sdk"
        )
        log(f"❌ {_client_error}")
        return None
    except TimeoutError as exc:
        _client_error = str(exc)
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
    The API call runs with _SDK_CALL_TIMEOUT so a hung endpoint never blocks.
    Returns 0.0 on any failure with detailed logging.
    """
    client = _get_client()
    if client is None:
        return 0.0

    def _call():
        return client.get_my_balances()

    try:
        log(f"📡 Calling client.get_my_balances() (timeout={_SDK_CALL_TIMEOUT}s)...")
        response = _run_with_timeout(_call, _SDK_CALL_TIMEOUT, "get_my_balances")
        log(
            f"📡 get_my_balances() → errno={response.errno} "
            f"errmsg={getattr(response, 'errmsg', '')} "
            f"result={str(getattr(response, 'result', ''))[:400]}"
        )

        if response.errno != 0:
            log(
                f"❌ get_my_balances() API error errno={response.errno}: "
                f"{getattr(response, 'errmsg', 'unknown')}"
            )
            return 0.0

        result = getattr(response, "result", None)
        if result is None:
            log("⚠️  get_my_balances() returned errno=0 but result is None")
            return 0.0

        # Unwrap result structure. SDK v0.7 returns a response object with:
        #   result.balances = [OpenapiQuoteTokenBalance(available_balance=..., total_balance=...)]
        # Fallback: list, result.list, result.data, or bare object.
        if hasattr(result, "balances") and result.balances:
            items = list(result.balances)
        elif isinstance(result, list):
            items = result
        elif hasattr(result, "list"):
            items = result.list or []
        elif hasattr(result, "data"):
            items = [result.data] if result.data else []
        else:
            items = [result]

        total = 0.0
        for item in items:
            for field in (
                # snake_case (SDK v0.7 OpenapiQuoteTokenBalance fields)
                "available_balance", "total_balance", "frozen_balance",
                # camelCase (older SDK versions / alternative response shapes)
                "availableBalance", "totalBalance", "balance",
                "usdt", "usdtBalance", "cashBalance",
                "available", "total", "value", "usdc", "usdcBalance",
            ):
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

    except TimeoutError as exc:
        log(f"⏱️  get_balance() timed out: {exc}")
        return 0.0
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
    outcome_hint: str = "",
    label_hint: str = "",
) -> tuple[bool, str, str]:
    """Place a limit buy order on Opinion Labs CLOB.

    Args:
        market_id:      Opinion market ID (numeric string). May be a categorical
                        parent ID (e.g. "340") — this function resolves it to the
                        correct tradable child market automatically.
        side:           "YES" or "NO"
        price:          fractional price (0.0–1.0)
        size_usdc:      total USDT to spend (Opinion quote token is USDT)
        contract_count: integer number of contracts (used as fallback sizing)
        outcome_hint:   Oddpool outcome_key (e.g. "value_above_120k") — used to
                        select the correct child from a categorical parent market.

    Returns (ok: bool, order_id: str, error_msg: str).
    The SDK call runs with _SDK_CALL_TIMEOUT so a hung endpoint never blocks.
    """
    client = _get_client()
    if client is None:
        return False, "", (_client_error or "Opinion SDK client not available")

    # Resolve to a tradable child market — handles both binary and categorical parents.
    # For a binary market: child_market_id == market_id.
    # For a categorical parent (e.g. 340): picks the correct child via outcome_hint.
    from ..adapters.opinion import resolve_tradable_market
    resolved = resolve_tradable_market(market_id, outcome_hint=outcome_hint, label_hint=label_hint)
    if not resolved:
        err = (
            f"Cannot resolve tradable market for Opinion market_id={market_id!r} "
            f"outcome_hint={outcome_hint!r}"
        )
        log(f"❌ {err}")
        return False, "", err

    child_market_id, yes_token_id, no_token_id = resolved
    token_id = yes_token_id if side.upper() == "YES" else no_token_id
    log(
        f"📤 Placing {side} BUY: parent={market_id} child={child_market_id} "
        f"token={token_id[:16]}... price={price:.4f} size_usdt={size_usdc:.2f} "
        f"contracts={contract_count}"
    )

    try:
        from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
        from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
        from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER

        order = PlaceOrderDataInput(
            marketId=int(child_market_id),   # child market ID — not the categorical parent
            tokenId=token_id,
            side=OrderSide.BUY,
            orderType=LIMIT_ORDER,
            price=str(round(price, 4)),
            makerAmountInQuoteToken=round(size_usdc, 6),
        )

        def _call():
            return client.place_order(order, check_approval=True)

        log(f"📡 Calling client.place_order() (timeout={_SDK_CALL_TIMEOUT}s)...")
        result = _run_with_timeout(_call, _SDK_CALL_TIMEOUT, "place_order")
        log(
            f"📡 place_order() → errno={result.errno} "
            f"errmsg={getattr(result, 'errmsg', '')} "
            f"result={str(getattr(result, 'result', ''))[:300]}"
        )

        if result.errno != 0:
            err = f"Opinion order rejected errno={result.errno}: {getattr(result, 'errmsg', 'unknown')}"
            log(f"❌ {err}")
            return False, "", err

        order_data = getattr(result, "result", None)
        order_id = ""
        if order_data:
            if hasattr(order_data, "data") and order_data.data:
                order_id = str(
                    getattr(order_data.data, "order_id", "")
                    or getattr(order_data.data, "orderId", "")
                    or getattr(order_data.data, "id", "")
                )
            elif isinstance(order_data, dict):
                inner = order_data.get("data", order_data)
                order_id = str(
                    inner.get("order_id") or inner.get("orderId") or inner.get("id") or ""
                )

        log(f"✅ Opinion order placed: orderId={order_id}")
        return True, order_id, ""

    except TimeoutError as exc:
        err = str(exc)
        log(f"⏱️  place_order() timed out: {err}")
        return False, "", err
    except Exception as exc:
        err = str(exc)
        log(f"❌ place_order() exception: {err}")
        return False, "", err
