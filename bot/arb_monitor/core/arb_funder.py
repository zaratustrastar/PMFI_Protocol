"""pARB Auto-Funder: capital-efficient on-demand funding for arb trades.

Architecture (on-demand model):
  Capital stays in the servicer wallet until a specific trade needs it.
  The executor calls ensure_funded_for_trade() just before placing orders,
  which reads the platform's current balance and tops up only the exact gap.
  This eliminates fixed-ratio pre-allocation (e.g. 40/40/20) which wasted
  capital on platforms with no current opportunities.

Funder tick (run each cycle):
  1. Call tend() — moves excess vault idle USDC to the servicer wallet
  2. Log servicer status (USDC + ETH balances)
  3. Minimum float maintenance — keeps a small standing reserve (default $3)
     on each platform so the executor isn't always starting from zero

On-demand top-up (called from executor before each trade):
  ensure_funded_for_trade(venue, needed_usdc, servicer_wallet, private_key)
  → reads platform balance → if gap exists, transfers from servicer → waits
    ARB_DEPOSIT_WAIT_SECS for the deposit to register → returns bool

Nonce management: _NonceTracker is initialised from pending nonce and
incremented client-side per transfer to prevent nonce collisions.
All errors are logged and suppressed — never raises.
"""

import os
import time
from ..config import (
    ARB_MIN_FLOAT_POLY,
    ARB_MIN_FLOAT_KALSHI,
    ARB_MIN_FLOAT_OPINION,
    ARB_DEPOSIT_WAIT_SECS,
    ARB_SERVICER_GAS_RESERVE_ETH,
    ARB_SAFETY_BUFFER_USDC,
    POLY_BASE_DEPOSIT_ADDR,
    KALSHI_BASE_DEPOSIT_ADDR,
    OPINION_BSC_DEPOSIT_ADDR,
)

BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
BASE_CHAIN_ID = 8453
BSC_CHAIN_ID = 56

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

LIFI_API = "https://li.quest/v1"

# ERC-20 function selectors (keccak256 first 4 bytes)
SELECTOR_TRANSFER = "a9059cbb"   # transfer(address,uint256)
SELECTOR_APPROVE = "095ea7b3"    # approve(address,uint256)
SELECTOR_ALLOWANCE = "dd62ed3e"  # allowance(address,address)
SELECTOR_BALANCE_OF = "70a08231" # balanceOf(address)


def log(msg: str):
    print(f"💸 [ArbFunder] {msg}")


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list, rpc_url: str = BASE_RPC) -> str:
    import requests as _req
    resp = _req.post(rpc_url, json={
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error ({method}): {data['error']}")
    return data["result"]


def _checksum(addr: str) -> str:
    import eth_utils
    return eth_utils.to_checksum_address(addr)


def _get_usdc_balance(wallet: str, rpc_url: str = BASE_RPC) -> float:
    """Return USDC balance of wallet (normalised to float USDC)."""
    padded = "000000000000000000000000" + wallet.lower().replace("0x", "")
    data = "0x" + SELECTOR_BALANCE_OF + padded
    raw = _rpc("eth_call", [{"to": USDC_BASE, "data": data}, "latest"], rpc_url)
    balance_raw = int(raw, 16) if raw and raw != "0x" else 0
    return balance_raw / 1_000_000


def _get_eth_balance(wallet: str, rpc_url: str = BASE_RPC) -> float:
    """Return ETH balance of wallet (in ETH, normalised from wei)."""
    raw = _rpc("eth_getBalance", [wallet, "latest"], rpc_url)
    return int(raw, 16) / 1e18


def _get_allowance(owner: str, spender: str, token: str = USDC_BASE, rpc_url: str = BASE_RPC) -> float:
    """Return ERC-20 allowance of spender for owner (in USDC float)."""
    padded_owner = "000000000000000000000000" + owner.lower().replace("0x", "")
    padded_spender = "000000000000000000000000" + spender.lower().replace("0x", "")
    data = "0x" + SELECTOR_ALLOWANCE + padded_owner + padded_spender
    raw = _rpc("eth_call", [{"to": token, "data": data}, "latest"], rpc_url)
    raw_int = int(raw, 16) if raw and raw != "0x" else 0
    return raw_int / 1_000_000


# ---------------------------------------------------------------------------
# Nonce tracker — prevents collisions across back-to-back txs in one tick
# ---------------------------------------------------------------------------

class _NonceTracker:
    """Per-tick nonce tracker initialised from the pending nonce.

    eth_getTransactionCount with "pending" returns the next nonce including
    already-queued-but-unconfirmed transactions. After that, each tx in the
    same tick increments the cursor client-side so later txs never reuse a
    nonce that a prior tx in the same tick is already occupying.
    """

    def __init__(self, sender: str, rpc_url: str = BASE_RPC):
        raw = _rpc("eth_getTransactionCount", [sender, "pending"], rpc_url)
        self._next = int(raw, 16)
        self.sender = sender
        log(f"🔢 NonceTracker: sender={sender} pending_nonce={self._next}")

    def consume(self) -> int:
        """Return the next nonce and advance the cursor."""
        nonce = self._next
        self._next += 1
        return nonce


# ---------------------------------------------------------------------------
# Transaction builder/signer
# ---------------------------------------------------------------------------

def _build_and_send_tx(
    private_key: str,
    to: str,
    data: str,
    nonce_tracker: _NonceTracker,
    value_wei: int = 0,
    chain_id: int = BASE_CHAIN_ID,
    rpc_url: str = BASE_RPC,
    gas_override: int | None = None,
) -> str:
    """Build, sign and broadcast a raw transaction. Returns tx hash string.

    Uses nonce_tracker.consume() to get the next nonce — caller must pass the
    same tracker for all txs in one tick to guarantee nonce ordering.
    """
    from eth_account import Account

    account = Account.from_key(private_key)
    to_cs = _checksum(to)

    nonce = nonce_tracker.consume()

    gas_price_hex = _rpc("eth_gasPrice", [], rpc_url)
    gas_price = int(int(gas_price_hex, 16) * 1.2)

    if gas_override:
        gas_limit = gas_override
    else:
        try:
            gas_est_hex = _rpc("eth_estimateGas", [{
                "from": account.address, "to": to_cs,
                "data": data, "value": hex(value_wei)
            }], rpc_url)
            gas_limit = int(int(gas_est_hex, 16) * 1.35)
        except Exception as e:
            log(f"⚠️ Gas estimation failed ({e}) — using 150,000")
            gas_limit = 150_000

    tx = {
        "chainId": chain_id,
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": gas_limit,
        "to": to_cs,
        "value": value_wei,
        "data": data,
    }

    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + raw.hex()], rpc_url)
    log(f"📤 TX nonce={nonce} → {tx_hash}")
    return tx_hash


# ---------------------------------------------------------------------------
# ERC-20 transfer
# ---------------------------------------------------------------------------

def _send_erc20_transfer(
    private_key: str,
    to_addr: str,
    amount_usdc: float,
    nonce_tracker: _NonceTracker,
    token: str = USDC_BASE,
    chain_id: int = BASE_CHAIN_ID,
    rpc_url: str = BASE_RPC,
) -> str:
    """Send `amount_usdc` USDC to `to_addr`. Returns tx hash."""
    amount_raw = int(amount_usdc * 1_000_000)
    padded_to = "000000000000000000000000" + to_addr.lower().replace("0x", "")
    padded_amount = hex(amount_raw)[2:].zfill(64)
    data = "0x" + SELECTOR_TRANSFER + padded_to + padded_amount

    return _build_and_send_tx(
        private_key=private_key,
        to=token,
        data=data,
        nonce_tracker=nonce_tracker,
        value_wei=0,
        chain_id=chain_id,
        rpc_url=rpc_url,
    )


# ---------------------------------------------------------------------------
# ERC-20 approval
# ---------------------------------------------------------------------------

def _send_erc20_approve(
    private_key: str,
    spender: str,
    amount_usdc: float,
    nonce_tracker: _NonceTracker,
    token: str = USDC_BASE,
    chain_id: int = BASE_CHAIN_ID,
    rpc_url: str = BASE_RPC,
) -> str:
    """Approve `spender` to spend `amount_usdc` USDC. Returns tx hash."""
    amount_raw = int(amount_usdc * 1_000_000)
    padded_spender = "000000000000000000000000" + spender.lower().replace("0x", "")
    padded_amount = hex(amount_raw)[2:].zfill(64)
    data = "0x" + SELECTOR_APPROVE + padded_spender + padded_amount

    return _build_and_send_tx(
        private_key=private_key,
        to=token,
        data=data,
        nonce_tracker=nonce_tracker,
        value_wei=0,
        chain_id=chain_id,
        rpc_url=rpc_url,
        gas_override=80_000,
    )


# ---------------------------------------------------------------------------
# LI.FI bridge (Base → BSC)
# ---------------------------------------------------------------------------

def _bridge_usdc_base_to_bsc(
    private_key: str,
    from_addr: str,
    amount_usdc: float,
    to_bsc_addr: str,
    nonce_tracker: _NonceTracker,
) -> str:
    """Bridge `amount_usdc` USDC from Base to BSC via LI.FI.

    Flow:
      1. GET /v1/quote for the route
      2. Approve the LI.FI spender if needed (uses nonce_tracker for sequencing)
      3. Submit the transactionRequest on Base (uses nonce_tracker for sequencing)

    Returns the Base bridge tx hash (fire-and-forget — does not wait for BSC receipt).
    """
    import requests as _req

    amount_raw = int(amount_usdc * 1_000_000)

    log(f"🌉 Fetching LI.FI quote: {amount_usdc:.4f} USDC Base → BSC to {to_bsc_addr}")

    try:
        quote_resp = _req.get(f"{LIFI_API}/quote", params={
            "fromChain": BASE_CHAIN_ID,
            "toChain": BSC_CHAIN_ID,
            "fromToken": USDC_BASE,
            "toToken": USDC_BSC,
            "fromAmount": str(amount_raw),
            "fromAddress": from_addr,
            "toAddress": to_bsc_addr,
            "slippage": "0.005",    # 0.5% slippage tolerance
        }, timeout=20)
        quote_resp.raise_for_status()
        quote = quote_resp.json()
    except Exception as e:
        raise RuntimeError(f"LI.FI quote failed: {e}")

    tx_req = quote.get("transactionRequest")
    if not tx_req:
        raise RuntimeError(f"LI.FI quote missing transactionRequest: {list(quote.keys())}")

    lifi_router = tx_req.get("to", "")
    if not lifi_router:
        raise RuntimeError("LI.FI transactionRequest missing 'to' field")

    # Check and grant USDC allowance to LI.FI router.
    # The approval tx is sent with the current nonce_tracker cursor, then the
    # bridge tx with the next cursor — guaranteeing ordered execution.
    try:
        current_allowance = _get_allowance(from_addr, lifi_router)
        if current_allowance < amount_usdc:
            log(f"🔐 Approving LI.FI router {lifi_router} for {amount_usdc:.4f} USDC")
            approve_hash = _send_erc20_approve(
                private_key=private_key,
                spender=lifi_router,
                amount_usdc=amount_usdc,
                nonce_tracker=nonce_tracker,
            )
            log(f"✅ Approval tx: {approve_hash}")
        else:
            log(f"ℹ️ LI.FI allowance already sufficient ({current_allowance:.4f} USDC)")
    except Exception as e:
        raise RuntimeError(f"LI.FI approval step failed: {e}")

    # Submit the bridge transaction (nonce is automatically next after approval)
    tx_data = tx_req.get("data", "0x")
    tx_value = int(tx_req.get("value", "0x0"), 16) if tx_req.get("value") else 0
    tx_gas_raw = tx_req.get("gasLimit", "")
    tx_gas = int(tx_gas_raw, 16) if tx_gas_raw else None
    if tx_gas:
        tx_gas = int(tx_gas * 1.2)  # 20% buffer over LI.FI's estimate

    log(f"📤 Submitting LI.FI bridge tx to {lifi_router} (value={tx_value} wei, gas={tx_gas})")

    tx_hash = _build_and_send_tx(
        private_key=private_key,
        to=lifi_router,
        data=tx_data,
        nonce_tracker=nonce_tracker,
        value_wei=tx_value,
        chain_id=BASE_CHAIN_ID,
        rpc_url=BASE_RPC,
        gas_override=tx_gas,
    )

    log(f"🌉 Bridge tx submitted: {tx_hash} ({amount_usdc:.4f} USDC Base → BSC {to_bsc_addr})")
    return tx_hash


# ---------------------------------------------------------------------------
# Main funder tick
# ---------------------------------------------------------------------------

def _verify_wallet_key_match(private_key: str, servicer_wallet: str) -> bool:
    """Warn if ARB_SERVICER_WALLET doesn't match the address derived from POLY_PRIVATE_KEY.

    This catches the common operator misconfiguration where the private key and the
    declared wallet address are out of sync (e.g. copied the wrong key). Returns True
    if they match (or if verification fails due to missing eth_account).
    """
    try:
        from eth_account import Account
        derived = Account.from_key(private_key).address
        if derived.lower() != servicer_wallet.lower():
            log(
                f"⚠️ WALLET MISMATCH: ARB_SERVICER_WALLET={servicer_wallet} but "
                f"POLY_PRIVATE_KEY derives {derived}. Funder will sign from {derived}, "
                "not from the declared servicer wallet. Check your env config!"
            )
            return False
        return True
    except Exception as e:
        log(f"⚠️ Could not verify wallet/key match: {e}")
        return True  # soft failure — don't block if eth_account unavailable


def _call_tend_if_ready(vault_address: str, private_key: str, servicer_wallet: str) -> None:
    """Call tend() on the vault contract if the cooldown has elapsed.

    This is the production mechanism by which excess vault idle USDC flows to the
    servicer wallet for off-chain deployment to trading platforms.

    tend() is permissionless — anyone can call it. The bot calls it proactively so
    the production flow is:
        deposit processed by report() → idle accumulates in vault
        → _call_tend_if_ready() sends tend() tx → idle moves to servicer wallet
        → run_funder_tick distributes deployable_capital to platforms

    tend() invariants (enforced on-chain):
        • Does NOT change officialPPS
        • Does NOT touch USDC reserved for pending redemptions
        • Does NOT process deposit/redeem queues
    """
    from eth_hash.auto import keccak
    from eth_account import Account

    def _sel(sig: str) -> str:
        return keccak(sig.encode())[:4].hex()

    def _read_uint256(fn_sig: str) -> int:
        sel = _sel(fn_sig)
        raw = _rpc("eth_call", [{"to": vault_address, "data": "0x" + sel}, "latest"])
        if not raw or raw == "0x":
            return 0
        return int(raw, 16)

    try:
        last_tend_ts  = _read_uint256("lastTendTimestamp()")
        tend_cooldown = _read_uint256("tendCooldown()")
        now = int(time.time())

        if now < last_tend_ts + tend_cooldown:
            remaining = (last_tend_ts + tend_cooldown) - now
            log(f"⏳ tend() cooldown: {remaining}s remaining — skip")
            return

        log(f"🔄 tend() cooldown elapsed (last={last_tend_ts}, cooldown={tend_cooldown}s) — calling")

        tend_data    = "0x" + _sel("tend()")
        nonce_tracker = _NonceTracker(servicer_wallet)
        tx_hash = _build_and_send_tx(
            private_key=private_key,
            to=vault_address,
            data=tend_data,
            nonce_tracker=nonce_tracker,
            gas_override=120_000,
        )
        log(f"✅ tend() sent: tx={tx_hash}")

    except Exception as e:
        log(f"⚠️ _call_tend_if_ready error (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Platform balance reading
# ---------------------------------------------------------------------------

def _get_platform_balance(venue: str) -> float:
    """Read the current USDC balance available for trading on a platform.

    Args:
        venue: "polymarket" or "kalshi"

    Returns USDC float. Returns 0.0 on any error.
    """
    if venue == "polymarket":
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        if not poly_api_key:
            log("⚠️ POLY_API_KEY not set — Poly balance unknown (returning 0)")
            return 0.0
        try:
            import requests, hmac as _hmac, hashlib, base64, time as _time
            clob_url  = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            timestamp = str(int(_time.time()))
            message   = timestamp + "GET" + "/balance"
            if poly_api_secret:
                sig = base64.b64encode(
                    _hmac.new(
                        poly_api_secret.encode("utf-8"),
                        message.encode("utf-8"),
                        hashlib.sha256,
                    ).digest()
                ).decode("utf-8")
            else:
                sig = ""
            headers = {
                "POLY-API-KEY":    poly_api_key,
                "POLY-SIGNATURE":  sig,
                "POLY-TIMESTAMP":  timestamp,
                "POLY-PASSPHRASE": poly_api_passphrase,
                "Content-Type":    "application/json",
            }
            resp = requests.get(f"{clob_url}/balance", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                bal = float(data.get("balance", data.get("usdc", 0)))
                log(f"💰 Poly balance: {bal:.4f} USDC")
                return bal
            log(f"⚠️ Poly balance HTTP {resp.status_code}: {resp.text[:120]}")
            return 0.0
        except Exception as e:
            log(f"⚠️ Poly balance read error: {e}")
            return 0.0

    elif venue == "kalshi":
        try:
            from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
            if not kalshi_auth_available():
                log("⚠️ Kalshi auth not configured — Kalshi balance unknown (returning 0)")
                return 0.0
            import requests
            from ..config import KALSHI_BASE_URL
            url = f"{KALSHI_BASE_URL}/portfolio/balance"
            headers = get_kalshi_headers("GET", url)
            if not headers:
                log("⚠️ Kalshi RSA signing failed — Kalshi balance unknown (returning 0)")
                return 0.0
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                bal = float(data.get("balance", 0)) / 100.0
                log(f"💰 Kalshi balance: {bal:.4f} USDC")
                return bal
            log(f"⚠️ Kalshi balance HTTP {resp.status_code}: {resp.text[:80]}")
            return 0.0
        except Exception as e:
            log(f"⚠️ Kalshi balance read error: {e}")
            return 0.0

    elif venue == "opinion":
        try:
            from ..config import OPINION_BASE_URL, OPINION_API_KEY
            if not OPINION_API_KEY:
                log("⚠️ OPINION_API_KEY not set — Opinion balance unknown (returning 0)")
                return 0.0
            import requests
            headers = {"apikey": OPINION_API_KEY, "Content-Type": "application/json"}
            for path in ("/account/balance", "/account", "/balance"):
                try:
                    resp = requests.get(f"{OPINION_BASE_URL}{path}", headers=headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        for field in ("balance", "usdc", "usdcBalance", "availableBalance", "available"):
                            if field in data:
                                bal = float(data[field])
                                log(f"💰 Opinion balance: {bal:.4f} USDC (via {path})")
                                return bal
                except Exception:
                    continue
            log("⚠️ Opinion balance: all endpoints failed — returning 0")
            return 0.0
        except Exception as e:
            log(f"⚠️ Opinion balance read error: {e}")
            return 0.0

    log(f"⚠️ Unknown venue '{venue}' — balance unknown")
    return 0.0


# ---------------------------------------------------------------------------
# On-demand top-up (called from executor before each trade)
# ---------------------------------------------------------------------------

def ensure_funded_for_trade(
    venue: str,
    needed_usdc: float,
    servicer_wallet: str,
    private_key: str,
    buffer_usdc: float = 1.0,
    nonce_tracker=None,
) -> bool:
    """Ensure a trading platform has enough USDC for an upcoming trade.

    Reads the platform's current balance and tops it up from the servicer
    wallet if the balance is below (needed_usdc + buffer_usdc). This is
    called by the executor just before placing orders so capital is deployed
    exactly when and where it is needed rather than pre-allocated in fixed ratios.

    Args:
        venue:           "polymarket" or "kalshi"
        needed_usdc:     exact USDC required for this leg (contracts × live_ask)
        servicer_wallet: address of the servicer wallet holding the USDC
        private_key:     servicer wallet signing key
        buffer_usdc:     extra cushion above needed_usdc (default $1)
        nonce_tracker:   optional shared nonce tracker; a new one is created if None

    Returns:
        True  — platform has (or now has) sufficient funds; safe to trade
        False — servicer wallet cannot cover the gap; trade should be aborted
    """
    deposit_addr = POLY_BASE_DEPOSIT_ADDR if venue == "polymarket" else KALSHI_BASE_DEPOSIT_ADDR
    if not deposit_addr:
        log(
            f"⚠️ No deposit address configured for {venue} — "
            "skipping funding check (trading with whatever balance exists on-platform)"
        )
        return True  # fail-open: try to trade with existing balance

    current = _get_platform_balance(venue)
    target = needed_usdc + buffer_usdc
    gap = target - current

    if gap <= 0:
        log(
            f"✅ [{venue}] Balance sufficient: {current:.4f} USDC "
            f">= {target:.4f} needed ({needed_usdc:.4f} + {buffer_usdc:.2f} buffer)"
        )
        return True

    log(
        f"💸 [{venue}] Balance {current:.4f} < target {target:.4f} USDC — "
        f"topping up {gap:.4f} USDC from servicer"
    )

    # Gate: servicer must keep ARB_SAFETY_BUFFER_USDC after the top-up
    servicer_usdc = _get_usdc_balance(servicer_wallet)
    servicer_eth  = _get_eth_balance(servicer_wallet)

    if servicer_eth < ARB_SERVICER_GAS_RESERVE_ETH:
        log(
            f"❌ [{venue}] Servicer ETH too low ({servicer_eth:.6f} < "
            f"{ARB_SERVICER_GAS_RESERVE_ETH} ETH reserve) — cannot fund"
        )
        return False

    required_servicer = gap + ARB_SAFETY_BUFFER_USDC
    if servicer_usdc < required_servicer:
        log(
            f"❌ [{venue}] Servicer has {servicer_usdc:.4f} USDC — "
            f"insufficient to top up {gap:.4f} while keeping "
            f"{ARB_SAFETY_BUFFER_USDC:.2f} safety buffer "
            f"(needs {required_servicer:.4f})"
        )
        return False

    try:
        nt = nonce_tracker or _NonceTracker(servicer_wallet)
        tx = _send_erc20_transfer(
            private_key=private_key,
            to_addr=deposit_addr,
            amount_usdc=round(gap, 6),
            nonce_tracker=nt,
        )
        log(f"📤 [{venue}] Deposited {gap:.4f} USDC → {deposit_addr} tx={tx}")

        if ARB_DEPOSIT_WAIT_SECS > 0:
            log(f"⏱ Waiting {ARB_DEPOSIT_WAIT_SECS}s for {venue} deposit to register...")
            time.sleep(ARB_DEPOSIT_WAIT_SECS)

        return True

    except Exception as e:
        log(f"❌ [{venue}] Top-up transfer failed: {e}")
        return False


def run_funder_tick() -> None:
    """Maintain vault tend() cadence and minimum platform floats each cycle.

    Called from the arb execution loop at the top of each cycle.
    All errors are logged and suppressed — never raises.
    """
    # ── Heartbeat: always log at the very top so we can confirm the funder is
    # being called even if it exits early due to missing env vars or guards.
    ts_hb = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log(f"💓 [funder tick] starting — {ts_hb}")

    # ── Env-var check: log each missing variable explicitly so the operator knows
    # exactly what to add to .env rather than getting a generic "skipped" message.
    private_key     = os.environ.get("POLY_PRIVATE_KEY", "")
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    vault_address   = os.environ.get("ARB_VAULT_V2_ADDRESS", "")

    missing_vars = []
    if not private_key:
        missing_vars.append("POLY_PRIVATE_KEY (servicer wallet signing key)")
    if not servicer_wallet:
        missing_vars.append("ARB_SERVICER_WALLET (servicer wallet address, e.g. 0xba32aa4c...)")

    if missing_vars:
        for var in missing_vars:
            log(f"❌ Missing required env var: {var}")
        log(
            "⛔ Funder cannot run without the above variables. "
            "Add them to .env and restart the bot."
        )
        return

    if not vault_address:
        log(
            "⚠️ ARB_VAULT_V2_ADDRESS not set — tend() will NOT be called. "
            "Set ARB_VAULT_V2_ADDRESS=0x1182054b82f96c110698eAe6Af73FdA013599F9d to enable "
            "the vault→servicer USDC flow. The funder will still distribute any "
            "USDC already in the servicer wallet."
        )

    _verify_wallet_key_match(private_key, servicer_wallet)

    try:
        _run_funder_tick_inner(private_key, servicer_wallet)
    except Exception as e:
        log(f"❌ Funder tick error (non-fatal): {e}")


def _run_funder_tick_inner(private_key: str, servicer_wallet: str) -> None:
    """Inner implementation — all exceptions propagate to run_funder_tick for logging."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── 0. Call tend() on-chain if cooldown elapsed ────────────────────────
    # This is the mechanism that moves excess vault idle USDC → servicer wallet.
    # tend() respects pending-redeem reservations and the idle target buffer on-chain.
    # Must run BEFORE liquidity state so the state reflects the post-tend balance.
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    if vault_address:
        log(
            f"🔄 tend() check — vault={vault_address} servicer={servicer_wallet}"
        )
        _call_tend_if_ready(vault_address, private_key, servicer_wallet)
    else:
        log(
            f"ℹ️ ARB_VAULT_V2_ADDRESS not set — skipping tend() "
            f"(servicer wallet: {servicer_wallet})"
        )

    # ── 0b. Servicer wallet status: USDC + ETH + threshold ─────────────────
    # Logged immediately after tend() so the operator can see the wallet state
    # before any liquidity-state computation.
    try:
        raw_usdc = _get_usdc_balance(servicer_wallet)
        raw_eth  = _get_eth_balance(servicer_wallet)
        log(
            f"💼 Servicer status [{ts}]: "
            f"address={servicer_wallet} "
            f"USDC={raw_usdc:.4f} "
            f"ETH={raw_eth:.6f} "
            f"poly_float_min={ARB_MIN_FLOAT_POLY} "
            f"kalshi_float_min={ARB_MIN_FLOAT_KALSHI} "
            f"gas_reserve={ARB_SERVICER_GAS_RESERVE_ETH:.4f} ETH"
        )
        if raw_eth < ARB_SERVICER_GAS_RESERVE_ETH:
            log(
                f"❌ Servicer ETH too low: {raw_eth:.6f} ETH < gas_reserve "
                f"{ARB_SERVICER_GAS_RESERVE_ETH:.4f} ETH. "
                f"Send at least {ARB_SERVICER_GAS_RESERVE_ETH - raw_eth:.6f} ETH to "
                f"{servicer_wallet} on Base to restore gas capacity."
            )
    except Exception as _be:
        log(f"⚠️ Could not read servicer wallet balances: {_be}")

    # ── 1. Liquidity health snapshot (observability only) ─────────────────
    # Log the vault liquidity state so operators can monitor vault health.
    # No longer used to gate distribution — on-demand funding in the executor
    # handles trade-specific capital deployment.
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    if vault_address:
        try:
            from .arb_liquidity import compute_liquidity_state
            liq = compute_liquidity_state(vault_address, servicer_wallet)
            if liq.ok:
                log(
                    f"[{ts}] Vault liquidity: idle={liq.idle_available:.2f} "
                    f"required_idle={liq.required_idle:.2f} "
                    f"pending_redeem={liq.pending_redeem_value:.2f} "
                    f"servicer={liq.free_cash:.2f} "
                    f"deployable={liq.deployable_capital:.2f} "
                    f"under_pressure={liq.under_pressure}"
                )
                if liq.under_pressure:
                    log(
                        f"⚠️ Redemption pressure: shortfall={liq.shortfall:.2f} USDC — "
                        f"executor will prioritise liquidity over new trades"
                    )
        except Exception as e:
            log(f"⚠️ Liquidity snapshot failed (non-fatal): {e}")

    # ── 2. Minimum float maintenance ───────────────────────────────────────
    # Keep a small standing balance on each platform so the executor always
    # has a reserve immediately available for small trades without waiting
    # for a Base transaction to confirm. Larger trades are funded on-demand
    # by ensure_funded_for_trade() in the executor just before order placement.
    #
    # ETH guard: both the float top-up here and the on-demand top-ups in the
    # executor require ETH for gas. Log a warning but continue — we may still
    # be able to call tend() without sending any USDC transfers.
    try:
        eth_balance = _get_eth_balance(servicer_wallet)
        if eth_balance < ARB_SERVICER_GAS_RESERVE_ETH:
            log(
                f"⚠️ Servicer ETH low ({eth_balance:.6f} < "
                f"{ARB_SERVICER_GAS_RESERVE_ETH} ETH) — "
                f"skipping float top-ups (send ETH to {servicer_wallet} on Base)"
            )
            return
    except Exception as e:
        log(f"⚠️ Could not read ETH balance — skipping float maintenance: {e}")
        return

    servicer_usdc = _get_usdc_balance(servicer_wallet)
    log(f"[{ts}] Servicer USDC available for float maintenance: {servicer_usdc:.4f}")

    # Shared nonce tracker across both top-ups to prevent nonce collisions.
    try:
        nonce_tracker = _NonceTracker(servicer_wallet)
    except Exception as e:
        log(f"❌ Could not initialise nonce tracker: {e}")
        return

    # ── Polymarket float ───────────────────────────────────────────────────
    if POLY_BASE_DEPOSIT_ADDR:
        try:
            poly_bal = _get_platform_balance("polymarket")
            if poly_bal < ARB_MIN_FLOAT_POLY:
                top_up = round(ARB_MIN_FLOAT_POLY - poly_bal, 6)
                if servicer_usdc >= top_up + ARB_SAFETY_BUFFER_USDC:
                    tx = _send_erc20_transfer(private_key, POLY_BASE_DEPOSIT_ADDR, top_up, nonce_tracker)
                    log(f"✅ [{ts}] Poly float topped up: {top_up:.4f} USDC → {POLY_BASE_DEPOSIT_ADDR} tx={tx}")
                    servicer_usdc -= top_up
                else:
                    log(
                        f"ℹ️ Poly float low ({poly_bal:.4f} < {ARB_MIN_FLOAT_POLY}) "
                        f"but servicer only has {servicer_usdc:.4f} USDC — skipping"
                    )
            else:
                log(f"✅ Poly float OK: {poly_bal:.4f} USDC (min={ARB_MIN_FLOAT_POLY})")
        except Exception as e:
            log(f"⚠️ Poly float maintenance error: {e}")
    else:
        log("ℹ️ POLY_BASE_DEPOSIT_ADDR not set — skipping Poly float maintenance")

    # ── Kalshi float ───────────────────────────────────────────────────────
    if KALSHI_BASE_DEPOSIT_ADDR:
        try:
            kalshi_bal = _get_platform_balance("kalshi")
            if kalshi_bal < ARB_MIN_FLOAT_KALSHI:
                top_up = round(ARB_MIN_FLOAT_KALSHI - kalshi_bal, 6)
                if servicer_usdc >= top_up + ARB_SAFETY_BUFFER_USDC:
                    tx = _send_erc20_transfer(private_key, KALSHI_BASE_DEPOSIT_ADDR, top_up, nonce_tracker)
                    log(f"✅ [{ts}] Kalshi float topped up: {top_up:.4f} USDC → {KALSHI_BASE_DEPOSIT_ADDR} tx={tx}")
                else:
                    log(
                        f"ℹ️ Kalshi float low ({kalshi_bal:.4f} < {ARB_MIN_FLOAT_KALSHI}) "
                        f"but servicer only has {servicer_usdc:.4f} USDC — skipping"
                    )
            else:
                log(f"✅ Kalshi float OK: {kalshi_bal:.4f} USDC (min={ARB_MIN_FLOAT_KALSHI})")
        except Exception as e:
            log(f"⚠️ Kalshi float maintenance error: {e}")
    else:
        log("ℹ️ KALSHI_BASE_DEPOSIT_ADDR not set — skipping Kalshi float maintenance")

    # ── Opinion float (BSC bridge) ──────────────────────────────────────────
    # Opinion runs on BSC; capital must be pre-positioned because the LI.FI
    # bridge takes 3-10 minutes. We trigger a top-up here when the Opinion
    # balance falls below ARB_MIN_FLOAT_OPINION. The bridge is fire-and-forget
    # (tx hash logged; we don't wait for BSC receipt).
    if OPINION_BSC_DEPOSIT_ADDR:
        try:
            opinion_bal = _get_platform_balance("opinion")
            if opinion_bal < ARB_MIN_FLOAT_OPINION:
                top_up = round(ARB_MIN_FLOAT_OPINION - opinion_bal + 1.0, 6)  # +$1 buffer
                if servicer_usdc >= top_up + ARB_SAFETY_BUFFER_USDC:
                    log(
                        f"🌉 [{ts}] Opinion float low ({opinion_bal:.4f} < {ARB_MIN_FLOAT_OPINION}) — "
                        f"bridging {top_up:.4f} USDC Base→BSC to {OPINION_BSC_DEPOSIT_ADDR}"
                    )
                    try:
                        bridge_tx = _bridge_usdc_base_to_bsc(
                            private_key=private_key,
                            from_addr=servicer_wallet,
                            amount_usdc=top_up,
                            to_bsc_addr=OPINION_BSC_DEPOSIT_ADDR,
                            nonce_tracker=nonce_tracker,
                        )
                        log(f"✅ [{ts}] Opinion bridge initiated: {top_up:.4f} USDC → BSC tx={bridge_tx}")
                        servicer_usdc -= top_up
                    except Exception as bridge_err:
                        log(f"⚠️ Opinion bridge failed (non-fatal): {bridge_err}")
                else:
                    log(
                        f"ℹ️ Opinion float low ({opinion_bal:.4f} < {ARB_MIN_FLOAT_OPINION}) "
                        f"but servicer only has {servicer_usdc:.4f} USDC — skipping bridge"
                    )
            else:
                log(f"✅ Opinion float OK: {opinion_bal:.4f} USDC (min={ARB_MIN_FLOAT_OPINION})")
        except Exception as e:
            log(f"⚠️ Opinion float maintenance error: {e}")
    else:
        log("ℹ️ OPINION_BSC_DEPOSIT_ADDR not set — skipping Opinion float maintenance")
