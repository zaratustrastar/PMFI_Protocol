"""arb_withdrawals.py — Platform cash withdrawal functions for pARB waterfall.

Called by run_withdrawal_waterfall() in arb_reporter.py when the vault needs
to pull cash back from trading platforms to cover pending redemptions.

Three withdrawal paths (called in priority order — fastest/cheapest first):

  1. withdraw_from_polymarket(amount_usdc)
       CLOB API /withdraw → Polygon → Relay bridge → Base servicer wallet
       Settlement: 5–20 minutes

  2. withdraw_from_kalshi(amount_usdc, dest_address)
       POST /portfolio/balance/withdrawals (crypto) → direct to dest_address on Base
       Settlement: 30 minutes (via international crypto provider for non-US)

  3. withdraw_from_opinion(amount_usdc, dest_address)
       Opinion API withdrawal → BSC USDC → LI.FI bridge BSC → Base servicer wallet
       Settlement: 15–30 minutes (bridge dependent)
       Requires: servicer wallet has BNB on BSC for bridge gas

All functions return:
    (success: bool, identifier: str, amount_usdc: float)

On failure they return (False, error_msg, 0.0) — never raise.
"""

import os
import time
import json
import requests

from ..config import KALSHI_BASE_URL, OPINION_BASE_URL


def log(msg: str):
    print(f"💸 [ArbWithdrawals] {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Polymarket withdrawal (CLOB API → Relay bridge → Base)
# ─────────────────────────────────────────────────────────────────────────────

POLY_CLOB_URL = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
RELAY_API_URL = "https://api.relay.link"
BASE_CHAIN_ID = 8453


def _poly_clob_headers(method: str = "GET") -> dict:
    """Build Polymarket CLOB API auth headers using Bearer token."""
    api_key = os.environ.get("POLY_API_KEY", "")
    if not api_key:
        raise ValueError("POLY_API_KEY not set")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _poly_get_balance() -> float:
    """Return uninvested USDC balance on Polymarket."""
    resp = requests.get(
        f"{POLY_CLOB_URL}/balance",
        headers=_poly_clob_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return float(data.get("balance", data.get("usdc", 0)))


def _relay_bridge_polygon_to_base(
    amount_usdc: float,
    private_key: str,
    dest_address: str,
) -> tuple[bool, str]:
    """Bridge USDC from Polygon to Base via Relay.link.

    Returns (success, tx_hash_or_error).
    Re-uses the same Relay bridge pattern as pSNIPER.
    """
    try:
        from eth_account import Account
        from eth_hash.auto import keccak

        account = Account.from_key(private_key)
        amount_raw = int(amount_usdc * 1_000_000)

        USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        POLYGON_CHAIN_ID = 137

        log(f"🌉 Getting Relay bridge quote: {amount_usdc:.4f} USDC Polygon → Base → {dest_address}")

        quote_resp = requests.post(
            f"{RELAY_API_URL}/quote",
            json={
                "user": account.address,
                "originChainId": POLYGON_CHAIN_ID,
                "destinationChainId": BASE_CHAIN_ID,
                "originCurrency": USDC_POLYGON,
                "destinationCurrency": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amount": str(amount_raw),
                "recipient": dest_address,
                "tradeType": "EXACT_INPUT",
            },
            timeout=20,
        )
        quote_resp.raise_for_status()
        quote = quote_resp.json()

        steps = quote.get("steps", [])
        if not steps:
            return False, f"Relay quote returned no steps: {list(quote.keys())}"

        for step in steps:
            for item in step.get("items", []):
                tx_data = item.get("data", {})
                if not tx_data:
                    continue

                import requests as _req

                POLYGON_RPC = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

                nonce_raw = _rpc_call(POLYGON_RPC, "eth_getTransactionCount",
                                      [account.address, "pending"])
                nonce = int(nonce_raw, 16)

                gas_price_raw = _rpc_call(POLYGON_RPC, "eth_gasPrice", [])
                gas_price = int(int(gas_price_raw, 16) * 1.3)

                tx = {
                    "to":       tx_data.get("to", ""),
                    "data":     tx_data.get("data", "0x"),
                    "value":    int(tx_data.get("value", "0x0"), 16),
                    "gas":      int(tx_data.get("gas", "0x30d40"), 16),
                    "gasPrice": gas_price,
                    "nonce":    nonce,
                    "chainId":  POLYGON_CHAIN_ID,
                }
                signed = account.sign_transaction(tx)
                tx_hash = _rpc_call(
                    POLYGON_RPC,
                    "eth_sendRawTransaction",
                    ["0x" + signed.rawTransaction.hex()],
                )
                log(f"✅ Relay bridge tx submitted: {tx_hash}")
                return True, tx_hash

        return False, "No executable tx found in Relay quote steps"

    except Exception as e:
        return False, str(e)


def _rpc_call(rpc_url: str, method: str, params: list) -> str:
    """Minimal JSON-RPC call."""
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]


def withdraw_from_polymarket(
    amount_usdc: float,
    min_amount: float = 10.0,
) -> tuple[bool, str, float]:
    """Withdraw USDC from Polymarket → Polygon → Relay bridge → Base servicer wallet.

    Args:
        amount_usdc: USDC amount to withdraw (will be capped at 90% of available balance)
        min_amount:  Minimum worthwhile withdrawal (default $10)

    Returns:
        (success, tx_hash_or_error, amount_withdrawn_usdc)
    """
    poly_api_key = os.environ.get("POLY_API_KEY", "")
    private_key  = os.environ.get("POLY_PRIVATE_KEY", "")
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")

    if not poly_api_key:
        return False, "POLY_API_KEY not set", 0.0
    if not private_key:
        return False, "POLY_PRIVATE_KEY not set", 0.0
    if not servicer_wallet:
        return False, "ARB_SERVICER_WALLET not set", 0.0

    try:
        balance = _poly_get_balance()
        log(f"Polymarket available: {balance:.4f} USDC")

        withdraw_amount = min(amount_usdc, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Poly balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        log(f"📤 Requesting Polymarket withdrawal: {withdraw_amount:.4f} USDC → Polygon")

        # Initiate withdrawal via CLOB API
        # Amount in USDC units (6 decimals for Polygon USDC)
        amount_raw = str(int(withdraw_amount * 1_000_000))
        resp = requests.post(
            f"{POLY_CLOB_URL}/withdraw",
            headers=_poly_clob_headers("POST"),
            json={"amount": amount_raw},
            timeout=15,
        )

        if resp.status_code not in (200, 201):
            return False, f"Poly CLOB /withdraw HTTP {resp.status_code}: {resp.text[:200]}", 0.0

        withdraw_data = resp.json()
        log(f"✅ Polymarket withdrawal initiated: {withdraw_data}")

        # Now bridge from Polygon to Base via Relay
        # Give Polymarket 60s to process withdrawal to Polygon wallet before bridging
        log(f"⏳ Waiting 60s for Polymarket withdrawal to settle on Polygon…")
        time.sleep(60)

        success, tx_or_err = _relay_bridge_polygon_to_base(
            amount_usdc=withdraw_amount,
            private_key=private_key,
            dest_address=servicer_wallet,
        )
        if success:
            log(f"✅ Relay bridge submitted: {tx_or_err} ({withdraw_amount:.4f} USDC → Base)")
            return True, tx_or_err, withdraw_amount
        else:
            log(f"⚠️ Relay bridge failed after Poly withdrawal: {tx_or_err}")
            return False, f"Bridge failed: {tx_or_err}", 0.0

    except Exception as e:
        log(f"❌ withdraw_from_polymarket error: {e}")
        return False, str(e), 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Kalshi withdrawal (crypto via API → direct to wallet on Base)
# ─────────────────────────────────────────────────────────────────────────────

def _kalshi_get_balance() -> float:
    """Return available USDC balance on Kalshi (in USDC, not cents)."""
    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
    if not kalshi_auth_available():
        raise ValueError("Kalshi credentials not configured")

    url = f"{KALSHI_BASE_URL}/portfolio/balance"
    headers = get_kalshi_headers("GET", url)
    if not headers:
        raise ValueError("Kalshi RSA signing failed")

    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # Kalshi reports balance in cents
    return float(data.get("balance", 0)) / 100.0


def withdraw_from_kalshi(
    amount_usdc: float,
    dest_address: str,
    min_amount: float = 10.0,
) -> tuple[bool, str, float]:
    """Withdraw USDC from Kalshi via crypto withdrawal to dest_address.

    Uses POST /portfolio/balance/withdrawals with method=crypto.
    For international (non-US) accounts this routes through Kalshi's
    international crypto provider. Funds typically arrive within 30 minutes.

    Args:
        amount_usdc:  USDC amount to withdraw
        dest_address: Destination wallet address (Base-compatible, servicer wallet)
        min_amount:   Minimum worthwhile withdrawal (default $10)

    Returns:
        (success, withdrawal_id_or_error, amount_withdrawn_usdc)
    """
    from .kalshi_auth import get_kalshi_headers, kalshi_auth_available

    if not kalshi_auth_available():
        return False, "Kalshi credentials not configured", 0.0
    if not dest_address:
        return False, "dest_address not set", 0.0

    try:
        balance = _kalshi_get_balance()
        log(f"Kalshi available: {balance:.4f} USDC")

        withdraw_amount = min(amount_usdc, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Kalshi balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        log(f"📤 Requesting Kalshi crypto withdrawal: {withdraw_amount:.4f} USDC → {dest_address}")

        url = f"{KALSHI_BASE_URL}/portfolio/balance/withdrawals"
        headers = get_kalshi_headers("POST", url)
        if not headers:
            return False, "Kalshi RSA signing failed for withdrawal", 0.0

        # Kalshi amounts are in cents
        amount_cents = int(withdraw_amount * 100)

        payload = {
            "amount":  amount_cents,
            "method":  "crypto",
            "address": dest_address,
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=15)

        if resp.status_code not in (200, 201):
            return False, f"Kalshi /withdrawals HTTP {resp.status_code}: {resp.text[:200]}", 0.0

        data = resp.json()
        withdrawal_id = data.get("id") or data.get("withdrawal_id") or str(data)
        log(f"✅ Kalshi withdrawal initiated: id={withdrawal_id} amount={withdraw_amount:.4f} USDC → {dest_address}")
        return True, withdrawal_id, withdraw_amount

    except Exception as e:
        log(f"❌ withdraw_from_kalshi error: {e}")
        return False, str(e), 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Opinion Labs withdrawal (API → BSC → LI.FI bridge → Base)
# ─────────────────────────────────────────────────────────────────────────────

BSC_CHAIN_ID = 56
USDC_BSC  = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
LIFI_API  = "https://li.quest/v1"
BSC_RPC   = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.binance.org")


def _opinion_headers() -> dict:
    api_key = os.environ.get("OPINION_API_KEY", "")
    if not api_key:
        raise ValueError("OPINION_API_KEY not set")
    return {"X-API-KEY": api_key, "Content-Type": "application/json"}


def _opinion_get_balance() -> float:
    """Return uninvested USDC balance on Opinion Labs."""
    for path in ("/account/balance", "/balance", "/account"):
        try:
            resp = requests.get(
                f"{OPINION_BASE_URL}{path}",
                headers=_opinion_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                for field in ("balance", "usdc", "usdcBalance", "availableBalance", "available"):
                    if field in data:
                        return float(data[field])
        except Exception:
            continue
    return 0.0


def _bridge_bsc_to_base(
    amount_usdc: float,
    private_key: str,
    dest_address: str,
) -> tuple[bool, str]:
    """Bridge USDC from BSC to Base via LI.FI.

    The servicer wallet uses the same EVM private key on BSC.
    Requires BNB in servicer wallet on BSC for gas.

    Returns (success, tx_hash_or_error).
    """
    try:
        from eth_account import Account

        account = Account.from_key(private_key)
        amount_raw = int(amount_usdc * 1_000_000)

        log(f"🌉 Getting LI.FI quote: {amount_usdc:.4f} USDC BSC → Base → {dest_address}")

        quote_resp = requests.get(
            f"{LIFI_API}/quote",
            params={
                "fromChain":   BSC_CHAIN_ID,
                "toChain":     BASE_CHAIN_ID,
                "fromToken":   USDC_BSC,
                "toToken":     USDC_BASE,
                "fromAmount":  str(amount_raw),
                "fromAddress": account.address,
                "toAddress":   dest_address,
                "slippage":    "0.005",
            },
            timeout=20,
        )
        quote_resp.raise_for_status()
        quote = quote_resp.json()

        tx_req = quote.get("transactionRequest")
        if not tx_req:
            return False, f"LI.FI quote missing transactionRequest: {list(quote.keys())}"

        lifi_router = tx_req.get("to", "")
        if not lifi_router:
            return False, "LI.FI transactionRequest missing 'to'"

        # Check USDC allowance on BSC
        allowance_data = (
            "0xdd62ed3e"
            + "000000000000000000000000" + account.address.lower().replace("0x", "")
            + "000000000000000000000000" + lifi_router.lower().replace("0x", "")
        )
        raw_allowance = _rpc_call(BSC_RPC, "eth_call",
                                  [{"to": USDC_BSC, "data": allowance_data}, "latest"])
        current_allowance = int(raw_allowance, 16) / 1e6

        nonce_raw = _rpc_call(BSC_RPC, "eth_getTransactionCount",
                              [account.address, "pending"])
        nonce = int(nonce_raw, 16)

        gas_price_raw = _rpc_call(BSC_RPC, "eth_gasPrice", [])
        gas_price = int(int(gas_price_raw, 16) * 1.2)

        if current_allowance < amount_usdc:
            log(f"🔐 Approving LI.FI router on BSC for {amount_usdc:.4f} USDC")
            amount_hex = amount_raw.to_bytes(32, "big").hex()
            padded_router = "000000000000000000000000" + lifi_router.lower().replace("0x", "")
            approve_data = "0x095ea7b3" + padded_router + amount_hex

            approve_tx = {
                "to": USDC_BSC, "data": approve_data,
                "gas": 80_000, "gasPrice": gas_price,
                "nonce": nonce, "chainId": BSC_CHAIN_ID, "value": 0,
            }
            signed_approve = account.sign_transaction(approve_tx)
            _rpc_call(BSC_RPC, "eth_sendRawTransaction",
                      ["0x" + signed_approve.rawTransaction.hex()])
            nonce += 1
            log(f"✅ BSC USDC approval sent")

        tx_data  = tx_req.get("data", "0x")
        tx_value = int(tx_req.get("value", "0x0"), 16) if tx_req.get("value") else 0
        tx_gas   = int(tx_req.get("gasLimit", "0x30d40"), 16) if tx_req.get("gasLimit") else 250_000
        tx_gas   = int(tx_gas * 1.2)

        bridge_tx = {
            "to": lifi_router, "data": tx_data, "value": tx_value,
            "gas": tx_gas, "gasPrice": gas_price,
            "nonce": nonce, "chainId": BSC_CHAIN_ID,
        }
        signed_bridge = account.sign_transaction(bridge_tx)
        tx_hash = _rpc_call(BSC_RPC, "eth_sendRawTransaction",
                            ["0x" + signed_bridge.rawTransaction.hex()])
        log(f"✅ LI.FI BSC→Base bridge tx: {tx_hash}")
        return True, tx_hash

    except Exception as e:
        return False, str(e)


def withdraw_from_opinion(
    amount_usdc: float,
    dest_address: str,
    min_amount: float = 10.0,
) -> tuple[bool, str, float]:
    """Withdraw USDC from Opinion Labs → BSC → LI.FI bridge → Base.

    Flow:
      1. Call Opinion API to withdraw BSC USDC to servicer wallet's BSC address
         (same address as Base — EVM key is chain-agnostic)
      2. Wait for BSC USDC to arrive (~5 min)
      3. Bridge BSC USDC → Base USDC via LI.FI

    Prerequisite: servicer wallet needs BNB on BSC for gas.
    Check with: BNB_BALANCE_WARNING in logs — bot will warn if BNB is too low.

    Args:
        amount_usdc:  USDC amount to withdraw
        dest_address: Destination address on Base (servicer wallet)
        min_amount:   Minimum worthwhile withdrawal (default $10)

    Returns:
        (success, bridge_tx_or_error, amount_withdrawn_usdc)
    """
    opinion_api_key = os.environ.get("OPINION_API_KEY", "")
    private_key     = os.environ.get("POLY_PRIVATE_KEY", "")

    if not opinion_api_key:
        return False, "OPINION_API_KEY not set", 0.0
    if not private_key:
        return False, "POLY_PRIVATE_KEY not set", 0.0
    if not dest_address:
        return False, "dest_address not set", 0.0

    try:
        from eth_account import Account
        servicer_bsc_address = Account.from_key(private_key).address

        # Check BNB balance for gas (warn if low)
        try:
            bnb_raw = _rpc_call(BSC_RPC, "eth_getBalance", [servicer_bsc_address, "latest"])
            bnb_balance = int(bnb_raw, 16) / 1e18
            if bnb_balance < 0.002:
                log(
                    f"⚠️ BNB_BALANCE_WARNING: servicer has {bnb_balance:.6f} BNB on BSC — "
                    f"need at least 0.002 BNB for bridge gas. Opinion withdrawal may fail."
                )
        except Exception as e:
            log(f"⚠️ Could not check BNB balance: {e}")

        balance = _opinion_get_balance()
        log(f"Opinion available: {balance:.4f} USDC")

        withdraw_amount = min(amount_usdc, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Opinion balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        log(f"📤 Requesting Opinion withdrawal: {withdraw_amount:.4f} USDC → BSC {servicer_bsc_address}")

        # Try Opinion API withdrawal endpoints (probe pattern like balance check)
        withdraw_result = None
        for path in ("/account/withdraw", "/withdraw", "/account/withdrawals"):
            try:
                resp = requests.post(
                    f"{OPINION_BASE_URL}{path}",
                    headers=_opinion_headers(),
                    json={
                        "amount":  withdraw_amount,
                        "address": servicer_bsc_address,
                        "chain":   "BSC",
                        "token":   "USDC",
                    },
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    withdraw_result = resp.json()
                    log(f"✅ Opinion withdrawal accepted via {path}: {withdraw_result}")
                    break
                elif resp.status_code == 404:
                    continue
                else:
                    log(f"⚠️ Opinion {path} HTTP {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                log(f"⚠️ Opinion {path} error: {e}")

        if withdraw_result is None:
            return False, "Opinion withdrawal API returned no success response", 0.0

        withdrawal_id = (
            withdraw_result.get("id")
            or withdraw_result.get("withdrawal_id")
            or withdraw_result.get("txHash")
            or "pending"
        )
        log(f"⏳ Waiting 5 min for Opinion withdrawal to settle on BSC (id={withdrawal_id})…")
        time.sleep(300)

        # Bridge BSC USDC → Base
        success, tx_or_err = _bridge_bsc_to_base(
            amount_usdc=withdraw_amount,
            private_key=private_key,
            dest_address=dest_address,
        )

        if success:
            log(f"✅ Opinion→Base bridge submitted: {tx_or_err} ({withdraw_amount:.4f} USDC)")
            return True, tx_or_err, withdraw_amount
        else:
            log(f"⚠️ Bridge failed after Opinion withdrawal: {tx_or_err}")
            return False, f"Bridge failed: {tx_or_err}", 0.0

    except Exception as e:
        log(f"❌ withdraw_from_opinion error: {e}")
        return False, str(e), 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Public: orchestrated platform withdrawals (used by waterfall)
# ─────────────────────────────────────────────────────────────────────────────

def withdraw_from_platforms(
    shortfall_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    opinion_cash: float,
    servicer_wallet: str,
) -> float:
    """Orchestrate platform withdrawals to cover a redemption shortfall.

    Calls platforms in priority order (fastest → slowest):
      1. Polymarket (5–20 min via Relay bridge)
      2. Kalshi     (30 min via crypto provider)
      3. Opinion    (15–30 min via LI.FI bridge)

    Each platform is called for min(its_cash * 90%, remaining_shortfall).
    Stops once shortfall is covered.

    Returns total USDC withdrawal amount initiated (may not yet have arrived).
    """
    if shortfall_usdc <= 0:
        return 0.0

    remaining = shortfall_usdc
    total_initiated = 0.0

    log(
        f"🔄 Platform withdrawal orchestration: shortfall={shortfall_usdc:.4f} USDC "
        f"poly_avail={poly_cash:.4f} kalshi_avail={kalshi_cash:.4f} opinion_avail={opinion_cash:.4f}"
    )

    # ── 1. Polymarket ─────────────────────────────────────────────────────
    if remaining > 0 and poly_cash > 10.0:
        target = min(poly_cash * 0.9, remaining)
        log(f"📤 Polymarket withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_polymarket(target)
        if ok:
            log(f"✅ Polymarket withdrawal initiated: {amount:.4f} USDC (ref={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            log(f"⚠️ Polymarket withdrawal failed: {ref}")

    # ── 2. Kalshi ─────────────────────────────────────────────────────────
    if remaining > 0 and kalshi_cash > 10.0:
        target = min(kalshi_cash * 0.9, remaining)
        log(f"📤 Kalshi withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_kalshi(target, servicer_wallet)
        if ok:
            log(f"✅ Kalshi withdrawal initiated: {amount:.4f} USDC (id={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            log(f"⚠️ Kalshi withdrawal failed: {ref}")

    # ── 3. Opinion Labs ───────────────────────────────────────────────────
    if remaining > 0 and opinion_cash > 10.0:
        target = min(opinion_cash * 0.9, remaining)
        log(f"📤 Opinion withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_opinion(target, servicer_wallet)
        if ok:
            log(f"✅ Opinion withdrawal initiated: {amount:.4f} USDC (bridge={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            log(f"⚠️ Opinion withdrawal failed: {ref}")

    if remaining > 0:
        log(
            f"⚠️ Platform withdrawals initiated {total_initiated:.4f} USDC but "
            f"{remaining:.4f} USDC shortfall remains — position unwind may be needed"
        )
    else:
        log(f"✅ Platform withdrawals cover full shortfall ({total_initiated:.4f} USDC initiated)")

    return total_initiated
