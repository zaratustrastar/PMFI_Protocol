"""arb_withdrawals.py — Platform cash withdrawal functions for pARB waterfall.

Called by run_withdrawal_waterfall() in arb_reporter.py when the vault needs
to pull cash back from trading platforms to cover pending redemptions.

This module also persists withdrawal intents so liquidity logic can count
"in-flight" recalls before funds land on Base.
"""

import json
import os
import time
from typing import Any

import requests

from ..config import KALSHI_BASE_URL, OPINION_BASE_URL

POLY_CLOB_URL = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
RELAY_API_URL = "https://api.relay.link"
BASE_CHAIN_ID = 8453

BSC_CHAIN_ID = 56
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
LIFI_API = "https://li.quest/v1"
BSC_RPC = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.binance.org")

WITHDRAWAL_LEDGER_PATH = os.environ.get(
    "ARB_WITHDRAWAL_LEDGER_PATH",
    os.path.expanduser("~/PolyNotifyBot/data/arb_withdrawals_ledger.json"),
)

POLY_ESTIMATED_SETTLEMENT_SECS = int(os.environ.get("ARB_POLY_WITHDRAW_SETTLEMENT_SECS", "1800"))
KALSHI_ESTIMATED_SETTLEMENT_SECS = int(os.environ.get("ARB_KALSHI_WITHDRAW_SETTLEMENT_SECS", "3600"))
OPINION_ESTIMATED_SETTLEMENT_SECS = int(os.environ.get("ARB_OPINION_WITHDRAW_SETTLEMENT_SECS", "2700"))


def log(msg: str):
    print(f"💸 [ArbWithdrawals] {msg}")


def _ensure_ledger_dir() -> None:
    os.makedirs(os.path.dirname(WITHDRAWAL_LEDGER_PATH), exist_ok=True)


def _load_ledger() -> list[dict[str, Any]]:
    try:
        if not os.path.exists(WITHDRAWAL_LEDGER_PATH):
            return []
        with open(WITHDRAWAL_LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"⚠️ Could not load withdrawal ledger: {e}")
        return []


def _save_ledger(rows: list[dict[str, Any]]) -> None:
    try:
        _ensure_ledger_dir()
        tmp = WITHDRAWAL_LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, sort_keys=True)
        os.replace(tmp, WITHDRAWAL_LEDGER_PATH)
    except Exception as e:
        log(f"⚠️ Could not save withdrawal ledger: {e}")


def record_withdrawal_intent(
    platform: str,
    amount_usdc: float,
    reference: str,
    eta_seconds: int,
) -> None:
    rows = _load_ledger()
    now = int(time.time())
    rows.append({
        "platform": platform,
        "amount_usdc": round(float(amount_usdc), 6),
        "reference": str(reference),
        "status": "pending",
        "created_at": now,
        "eta_at": now + max(60, int(eta_seconds)),
        "completed_at": None,
        "failed_at": None,
    })
    _save_ledger(rows)
    log(
        f"📝 Recorded withdrawal intent: platform={platform} amount={amount_usdc:.4f} "
        f"ref={reference} eta={eta_seconds}s"
    )


def mark_withdrawal_failed(reference: str) -> None:
    rows = _load_ledger()
    now = int(time.time())
    changed = False
    for row in rows:
        if row.get("reference") == reference and row.get("status") == "pending":
            row["status"] = "failed"
            row["failed_at"] = now
            changed = True
    if changed:
        _save_ledger(rows)


def mark_withdrawals_arrived(servicer_balance_usdc: float) -> float:
    """
    Best-effort completion logic:
    if ETA has passed, mark pending recalls as completed up to current servicer balance.
    Conservative but much better than leaving recalls pending forever.
    """
    rows = _load_ledger()
    if not rows:
        return 0.0

    now = int(time.time())
    changed = False
    completed_total = 0.0

    for row in rows:
        if row.get("status") != "pending":
            continue
        if now < int(row.get("eta_at", 0)):
            continue
        amt = float(row.get("amount_usdc", 0.0))
        if servicer_balance_usdc + 1e-9 >= amt:
            row["status"] = "completed"
            row["completed_at"] = now
            completed_total += amt
            changed = True

    if changed:
        _save_ledger(rows)
    return completed_total


def get_pending_withdrawal_usdc() -> float:
    rows = _load_ledger()
    now = int(time.time())
    total = 0.0
    changed = False

    for row in rows:
        if row.get("status") != "pending":
            continue
        eta_at = int(row.get("eta_at", 0))
        created_at = int(row.get("created_at", 0))
        # Expire stale pending recalls after 24h so they don't block forever
        if now > created_at + 86400:
            row["status"] = "failed"
            row["failed_at"] = now
            changed = True
            continue
        total += float(row.get("amount_usdc", 0.0))

    if changed:
        _save_ledger(rows)

    return round(total, 6)


def _poly_clob_headers(method: str = "GET") -> dict:
    api_key = os.environ.get("POLY_API_KEY", "")
    if not api_key:
        raise ValueError("POLY_API_KEY not set")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _poly_get_balance() -> float:
    poly_api_key        = os.environ.get("POLY_API_KEY", "")
    poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
    poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
    private_key         = os.environ.get("POLY_PRIVATE_KEY", "")
    poly_proxy_address  = os.environ.get("POLY_PROXY_ADDRESS", "") or None

    if not poly_api_key:
        raise ValueError("POLY_API_KEY not set")

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    creds = ApiCreds(
        api_key=poly_api_key.strip(),
        api_secret=poly_api_secret.strip(),
        api_passphrase=poly_api_passphrase.strip(),
    )

    client = ClobClient(
        POLY_CLOB_URL,
        key=private_key,
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=poly_proxy_address,
    )

    try:
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        )
    except Exception as upd_err:
        log(f"⚠️ [POLY] update_balance_allowance failed (non-fatal): {upd_err}")

    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
    )
    log(f"🔍 [POLY] sig_type=2 response: {result}")

    raw = result.get("balance", "0")
    bal = float(raw) / 1_000_000

    try:
        client_eoa = ClobClient(
            POLY_CLOB_URL,
            key=private_key,
            chain_id=137,
            creds=creds,
            signature_type=0,
        )
        result_eoa = client_eoa.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
        )
        log(f"🔍 [POLY] sig_type=0 response: {result_eoa}")
        raw_eoa = result_eoa.get("balance", "0")
        bal_eoa = float(raw_eoa) / 1_000_000
        if bal == 0 and bal_eoa > 0:
            log(f"ℹ️ [POLY] Funds found in EOA account — using EOA balance {bal_eoa:.6f} USDC")
            return bal_eoa
    except Exception as eoa_err:
        log(f"⚠️ [POLY] EOA balance check failed: {eoa_err}")

    return bal

def _rpc_call(rpc_url: str, method: str, params: list) -> str:
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


def _relay_bridge_polygon_to_base(
    amount_usdc: float,
    private_key: str,
    dest_address: str,
) -> tuple[bool, str]:
    try:
        from eth_account import Account

        account = Account.from_key(private_key)
        amount_raw = int(amount_usdc * 1_000_000)

        usdc_polygon = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        polygon_chain_id = 137

        log(f"🌉 Getting Relay quote: {amount_usdc:.4f} USDC Polygon → Base → {dest_address}")

        quote_resp = requests.post(
            f"{RELAY_API_URL}/quote",
            json={
                "user": account.address,
                "originChainId": polygon_chain_id,
                "destinationChainId": BASE_CHAIN_ID,
                "originCurrency": usdc_polygon,
                "destinationCurrency": USDC_BASE,
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

        polygon_rpc = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

        for step in steps:
            for item in step.get("items", []):
                tx_data = item.get("data", {})
                if not tx_data:
                    continue

                nonce_raw = _rpc_call(polygon_rpc, "eth_getTransactionCount", [account.address, "pending"])
                nonce = int(nonce_raw, 16)

                gas_price_raw = _rpc_call(polygon_rpc, "eth_gasPrice", [])
                gas_price = int(int(gas_price_raw, 16) * 1.3)

                tx = {
                    "to": tx_data.get("to", ""),
                    "data": tx_data.get("data", "0x"),
                    "value": int(tx_data.get("value", "0x0"), 16),
                    "gas": int(tx_data.get("gas", "0x30d40"), 16),
                    "gasPrice": gas_price,
                    "nonce": nonce,
                    "chainId": polygon_chain_id,
                }
                signed = account.sign_transaction(tx)
                tx_hash = _rpc_call(
                    polygon_rpc,
                    "eth_sendRawTransaction",
                    ["0x" + signed.raw_transaction.hex()],
                )
                log(f"✅ Relay bridge tx submitted: {tx_hash}")
                return True, tx_hash

        return False, "No executable tx found in Relay quote steps"

    except Exception as e:
        return False, str(e)



def get_pending_withdrawals_total_usdc() -> float:
    """
    Sum USDC for all pending withdrawal intents still considered in-flight.
    """
    rows = _load_ledger()
    if not rows:
        return 0.0

    now = int(time.time())
    total = 0.0

    for row in rows:
        if row.get("status") != "pending":
            continue

        try:
            amount = float(row.get("amount_usdc") or 0.0)
        except Exception:
            amount = 0.0

        eta_at = int(row.get("eta_at") or 0)

        # Count pending recalls as in-flight while ETA is still in the future.
        # If ETA is missing, count them conservatively.
        if eta_at <= 0 or eta_at >= now:
            total += amount

    return round(total, 6)


def withdraw_from_polymarket(
    amount_usdc: float,
    min_amount: float = 1.0,
) -> tuple[bool, str, float]:
    import subprocess
    import re

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    poly_proxy = os.environ.get("POLY_PROXY_ADDRESS", "")

    if not private_key:
        return False, "POLY_PRIVATE_KEY not set", 0.0
    if not poly_proxy:
        return False, "POLY_PROXY_ADDRESS not set", 0.0

    try:
        balance = _poly_get_balance()
        log(f"Polymarket available: {balance:.4f} USDC")

        # The TS Safe/bridge script has a hard MIN_WITHDRAWAL_USDC = 5.
        # Redeem shortfall can be slightly below 5 because PPS < 1, e.g. 4.98252.
        # In that case request exactly 5.00 so the recall is allowed.
        requested = float(amount_usdc)
        if requested < 5.0 and requested >= 4.90:
            requested = 5.0

        withdraw_amount = min(requested, balance * 0.9)
        if withdraw_amount < min_amount:
            msg = f"Poly balance {balance:.4f} too low — {withdraw_amount:.4f} < min {min_amount:.2f}"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        if withdraw_amount < 5.0:
            msg = f"Poly withdrawal {withdraw_amount:.6f} below bridge minimum 5.00"
            log(f"ℹ️ {msg}")
            return False, msg, 0.0

        cmd = [
            "npx",
            "tsx",
            "trading_bot/safe_proxy_withdraw_parb.ts",
            "withdraw",
            f"{withdraw_amount:.6f}",
        ]

        log(f"📤 Full Polymarket recall: {withdraw_amount:.4f} USDC.e {poly_proxy} -> EOA -> Base vault")

        result = subprocess.run(
            cmd,
            cwd="/root/PolyNotifyBot",
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ},
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        log(f"Direct Safe withdraw output:\n{output[:3000]}")

        if result.returncode != 0:
            return False, f"direct_safe_withdraw failed: {output[:300]}", 0.0

        tx_match = (
            re.search(r"Step 2 \(EOA → Base\):\s*(0x[a-fA-F0-9]+)", output)
            or re.search(r"TX:\s*(0x[a-fA-F0-9]+)", output)
            or re.search(r"Bridge tx sent:\s*(0x[a-fA-F0-9]+)", output)
            or re.search(r"TX_HASH:\s*(0x[a-fA-F0-9]+)", output)
        )

        bridge_ok = (
            "✅ BRIDGE COMPLETE" in output
            or "✅ FULL WITHDRAWAL COMPLETE" in output
            or "Bridge complete!" in output
        )

        tx_hash = tx_match.group(1) if tx_match else "unknown"

        if not bridge_ok:
            return False, f"bridge script did not report success: {output[:500]}", 0.0

        log(f"✅ Full Polymarket recall complete: tx={tx_hash} amount={withdraw_amount:.4f}")
        return True, tx_hash, withdraw_amount

    except Exception as e:
        log(f"❌ withdraw_from_polymarket error: {e}")
        return False, str(e), 0.0


def _kalshi_get_balance() -> float:
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
    return float(data.get("balance", 0)) / 100.0


def withdraw_from_kalshi(
    amount_usdc: float,
    dest_address: str,
    min_amount: float = 1.0,
) -> tuple[bool, str, float]:
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

        amount_cents = int(withdraw_amount * 100)
        payload = {
            "amount": amount_cents,
            "method": "crypto",
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


def _opinion_headers() -> dict:
    api_key = os.environ.get("OPINION_API_KEY", "")
    if not api_key:
        raise ValueError("OPINION_API_KEY not set")
    return {"X-API-KEY": api_key, "Content-Type": "application/json"}


def _opinion_get_balance() -> float:
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
    try:
        from eth_account import Account

        account = Account.from_key(private_key)
        amount_raw = int(amount_usdc * 1_000_000)

        log(f"🌉 Getting LI.FI quote: {amount_usdc:.4f} USDC BSC → Base → {dest_address}")

        quote_resp = requests.get(
            f"{LIFI_API}/quote",
            params={
                "fromChain": BSC_CHAIN_ID,
                "toChain": BASE_CHAIN_ID,
                "fromToken": USDC_BSC,
                "toToken": USDC_BASE,
                "fromAmount": str(amount_raw),
                "fromAddress": account.address,
                "toAddress": dest_address,
                "slippage": "0.005",
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

        allowance_data = (
            "0xdd62ed3e"
            + "000000000000000000000000" + account.address.lower().replace("0x", "")
            + "000000000000000000000000" + lifi_router.lower().replace("0x", "")
        )
        raw_allowance = _rpc_call(BSC_RPC, "eth_call", [{"to": USDC_BSC, "data": allowance_data}, "latest"])
        current_allowance = int(raw_allowance, 16) / 1e6

        nonce_raw = _rpc_call(BSC_RPC, "eth_getTransactionCount", [account.address, "pending"])
        nonce = int(nonce_raw, 16)

        gas_price_raw = _rpc_call(BSC_RPC, "eth_gasPrice", [])
        gas_price = int(int(gas_price_raw, 16) * 1.2)

        if current_allowance < amount_usdc:
            log(f"🔐 Approving LI.FI router on BSC for {amount_usdc:.4f} USDC")
            amount_hex = amount_raw.to_bytes(32, "big").hex()
            padded_router = "000000000000000000000000" + lifi_router.lower().replace("0x", "")
            approve_data = "0x095ea7b3" + padded_router + amount_hex

            approve_tx = {
                "to": USDC_BSC,
                "data": approve_data,
                "gas": 80_000,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": BSC_CHAIN_ID,
                "value": 0,
            }
            signed_approve = account.sign_transaction(approve_tx)
            _rpc_call(BSC_RPC, "eth_sendRawTransaction", ["0x" + signed_approve.raw_transaction.hex()])
            nonce += 1
            log("✅ BSC USDC approval sent")

        tx_data = tx_req.get("data", "0x")
        tx_value = int(tx_req.get("value", "0x0"), 16) if tx_req.get("value") else 0
        tx_gas = int(tx_req.get("gasLimit", "0x30d40"), 16) if tx_req.get("gasLimit") else 250_000
        tx_gas = int(tx_gas * 1.2)

        bridge_tx = {
            "to": lifi_router,
            "data": tx_data,
            "value": tx_value,
            "gas": tx_gas,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": BSC_CHAIN_ID,
        }
        signed_bridge = account.sign_transaction(bridge_tx)
        tx_hash = _rpc_call(BSC_RPC, "eth_sendRawTransaction", ["0x" + signed_bridge.raw_transaction.hex()])
        log(f"✅ LI.FI BSC→Base bridge tx: {tx_hash}")
        return True, tx_hash

    except Exception as e:
        return False, str(e)


def withdraw_from_opinion(
    amount_usdc: float,
    dest_address: str,
    min_amount: float = 1.0,
) -> tuple[bool, str, float]:
    opinion_api_key = os.environ.get("OPINION_API_KEY", "")
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")

    if not opinion_api_key:
        return False, "OPINION_API_KEY not set", 0.0
    if not private_key:
        return False, "POLY_PRIVATE_KEY not set", 0.0
    if not dest_address:
        return False, "dest_address not set", 0.0

    try:
        from eth_account import Account
        servicer_bsc_address = Account.from_key(private_key).address

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

        withdraw_result = None
        for path in ("/account/withdraw", "/withdraw", "/account/withdrawals"):
            try:
                resp = requests.post(
                    f"{OPINION_BASE_URL}{path}",
                    headers=_opinion_headers(),
                    json={
                        "amount": withdraw_amount,
                        "address": servicer_bsc_address,
                        "chain": "BSC",
                        "token": "USDC",
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


def withdraw_from_platforms(
    shortfall_usdc: float,
    poly_cash: float,
    kalshi_cash: float,
    opinion_cash: float,
    servicer_wallet: str,
) -> float:
    if shortfall_usdc <= 0:
        return 0.0

    remaining = shortfall_usdc
    total_initiated = 0.0

    log(
        f"🔄 Platform withdrawal orchestration: shortfall={shortfall_usdc:.4f} USDC "
        f"poly_avail={poly_cash:.4f} kalshi_avail={kalshi_cash:.4f} opinion_avail={opinion_cash:.4f}"
    )

    if remaining > 0 and poly_cash > 1.0:
        target = min(poly_cash * 0.9, remaining)
        log(f"📤 Polymarket withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_polymarket(target)
        if ok:
            record_withdrawal_intent("polymarket", amount, ref, 600)
            log(f"✅ Polymarket withdrawal initiated: {amount:.4f} USDC (tx={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            mark_withdrawal_failed(ref)
            log(f"⚠️ Polymarket withdrawal failed: {ref}")

    if remaining > 0 and kalshi_cash > 1.0:
        target = min(kalshi_cash * 0.9, remaining)
        log(f"📤 Kalshi withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_kalshi(target, servicer_wallet)
        if ok:
            record_withdrawal_intent("kalshi", amount, ref, KALSHI_ESTIMATED_SETTLEMENT_SECS)
            log(f"✅ Kalshi withdrawal initiated: {amount:.4f} USDC (id={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            mark_withdrawal_failed(ref)
            log(f"⚠️ Kalshi withdrawal failed: {ref}")

    if remaining > 0 and opinion_cash > 1.0:
        target = min(opinion_cash * 0.9, remaining)
        log(f"📤 Opinion withdrawal target: {target:.4f} USDC")
        ok, ref, amount = withdraw_from_opinion(target, servicer_wallet)
        if ok:
            record_withdrawal_intent("opinion", amount, ref, OPINION_ESTIMATED_SETTLEMENT_SECS)
            log(f"✅ Opinion withdrawal initiated: {amount:.4f} USDC (bridge={ref})")
            total_initiated += amount
            remaining -= amount
        else:
            mark_withdrawal_failed(ref)
            log(f"⚠️ Opinion withdrawal failed: {ref}")

    if remaining > 0:
        log(
            f"⚠️ Platform withdrawals initiated {total_initiated:.4f} USDC but "
            f"{remaining:.4f} USDC shortfall remains — position unwind may be needed"
        )
    else:
        log(f"✅ Platform withdrawals cover full shortfall ({total_initiated:.4f} USDC initiated)")

    return round(total_initiated, 6)
