"""pARB Auto-Funder: trade-driven capital allocation for arb trades.

Architecture (trade-driven model):
  Capital stays in the servicer wallet until a specific, ranked opportunity is
  selected. The executor calls fund_both_legs_for_trade() BEFORE placing any
  orders. That function:
    1. Reads servicer USDC balance ONCE
    2. Reads current platform balances for both legs
    3. Computes gaps (may be 0 if platforms already hold enough)
    4. Checks servicer has (poly_gap + venue2_gap + safety_buffer) combined —
       a single gate for the whole trade, not per-leg
    5. Sends BOTH deposit TXs in one nonce sequence (no sequential wait)
    6. Polls BOTH platform balances concurrently until both reach targets or
       the wait timeout expires
    7. Returns (ok: bool, error: str)

  Opinion (BSC via LI.FI bridge): takes 3-10 minutes — always checked first;
  if Opinion balance is insufficient, bridge is triggered fire-and-forget and
  the call returns False immediately. The engine retries on the next cycle.

Tend tick (run_tend_tick — called from reporter each cycle):
  1. Call tend() — moves excess vault idle USDC to the servicer wallet
  2. Log servicer status (USDC + ETH balances)
  NOTE: No float maintenance — there are no standing reserves. Capital is
  deposited exactly when and in exactly the amount that a specific trade needs.

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
    OPINION_BASE_DEPOSIT_ADDR,
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
        → fund_both_legs_for_trade() deposits exact amounts when a trade is selected

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
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
            clob_url    = os.environ.get("POLY_CLOB_URL", "https://clob.polymarket.com")
            private_key = os.environ.get("POLY_PRIVATE_KEY", "")
            creds = ApiCreds(
                api_key=poly_api_key.strip(),
                api_secret=poly_api_secret.strip(),
                api_passphrase=poly_api_passphrase.strip(),
            )
            client = ClobClient(
                clob_url,
                key=private_key,
                chain_id=137,
                creds=creds,
                signature_type=0,
            )
            result = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
            )
            raw = result.get("balance", "0")
            bal_raw = float(raw)
            bal = bal_raw / 1_000_000 if bal_raw > 1_000 else bal_raw
            log(f"💰 Poly balance: {bal:.4f} USDC (raw={raw})")
            return bal
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
            from ..adapters.opinion_clob import get_balance as opinion_get_balance
            return opinion_get_balance()
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


def get_servicer_deployable_usdc() -> float:
    """Return servicer wallet USDC minus the safety buffer (deployable capital).

    This is the total amount that can be safely committed to a trade right now.
    Returns 0.0 on any error.
    """
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    if not servicer_wallet:
        log("⚠️ ARB_SERVICER_WALLET not set — cannot determine deployable USDC")
        return 0.0
    try:
        usdc = _get_usdc_balance(servicer_wallet)
        deployable = max(0.0, usdc - ARB_SAFETY_BUFFER_USDC)
        log(
            f"💼 Servicer deployable: {usdc:.4f} USDC total − "
            f"{ARB_SAFETY_BUFFER_USDC:.2f} safety_buffer = {deployable:.4f} deployable"
        )
        return deployable
    except Exception as e:
        log(f"⚠️ get_servicer_deployable_usdc error: {e}")
        return 0.0


def fund_both_legs_for_trade(
    poly_usdc: float,
    venue2: str,
    venue2_usdc: float,
    wait_timeout: int = 180,
    poll_interval: int = 10,
) -> tuple[bool, str]:
    """Deposit exact capital to BOTH legs of a trade simultaneously, then wait.

    This is the ONLY function that moves capital from the servicer wallet to
    trading platforms. It implements the trade-driven funding algorithm:

      1. Read servicer USDC balance once
      2. Read current Poly and venue2 balances
      3. Compute funding gaps for each leg (may be 0 if already funded)
      4. Single capital gate: check servicer has (poly_gap + venue2_gap + safety_buffer)
         — one check for the whole trade, never per-leg
      5a. Poly + Kalshi: send BOTH deposit TXs in one nonce sequence
          (no sequential wait between them — they land in parallel)
      5b. Opinion: if OPINION_BASE_DEPOSIT_ADDR is set, send direct Base USDC
          transfer (instant, same path as Kalshi). Otherwise falls back to
          LI.FI bridge to BSC (fire-and-forget, retries on next cycle).
      6. Poll BOTH platform balances every poll_interval seconds until both
         reach their respective targets or wait_timeout expires
      7. Return (ok, error_message)

    Args:
        poly_usdc:    exact USDC required for the Polymarket leg
        venue2:       "kalshi" or "opinion"
        venue2_usdc:  exact USDC required for the venue2 leg
        wait_timeout: seconds to wait for balances to arrive (default 180s)
        poll_interval: seconds between balance polls (default 10s)

    Returns:
        (True, "")           — both platforms funded; safe to execute
        (False, error_str)   — cannot fund; error_str explains why
    """
    private_key     = os.environ.get("POLY_PRIVATE_KEY", "")
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")

    if not private_key or not servicer_wallet:
        msg = "fund_both_legs: POLY_PRIVATE_KEY or ARB_SERVICER_WALLET not set — cannot fund"
        log(f"❌ {msg}")
        return False, msg

    log(
        f"💰 fund_both_legs: poly={poly_usdc:.4f} USDC | "
        f"{venue2}={venue2_usdc:.4f} USDC (total={poly_usdc + venue2_usdc:.4f})"
    )

    # ── Step 1: Servicer balance ──────────────────────────────────────────
    try:
        servicer_eth  = _get_eth_balance(servicer_wallet)
        servicer_usdc = _get_usdc_balance(servicer_wallet)
    except Exception as e:
        msg = f"fund_both_legs: cannot read servicer wallet: {e}"
        log(f"❌ {msg}")
        return False, msg

    if servicer_eth < ARB_SERVICER_GAS_RESERVE_ETH:
        msg = (
            f"fund_both_legs: servicer ETH too low "
            f"({servicer_eth:.6f} < {ARB_SERVICER_GAS_RESERVE_ETH} ETH gas reserve)"
        )
        log(f"❌ {msg}")
        return False, msg

    # ── Step 2 & 3: Read current platform balances and compute gaps ───────
    # Minimum deposit thresholds enforced by each venue.
    # Sending below the minimum results in a rejected/lost deposit.
    _VENUE_MIN_DEPOSIT = {
        "opinion":    3.0,   # Opinion Labs: $3 minimum deposit
        "polymarket": 1.0,   # Polymarket CLOB: $1 effective minimum
        "kalshi":     1.0,   # Kalshi: $1 effective minimum
    }

    log("📊 Reading current platform balances...")
    poly_before   = _get_platform_balance("polymarket")
    poly_target   = poly_usdc

    venue2_before = _get_platform_balance(venue2)
    venue2_target = venue2_usdc

    # Gap-based trade funding:
    # If the platform already has enough balance for this trade → no deposit.
    # If it has a partial balance → send only the delta needed to reach the target,
    # subject to the venue's minimum deposit floor.
    # This maximises capital efficiency: platforms accumulate balance across trades
    # and the servicer only ever covers the shortfall, not the full leg amount.
    poly_min  = _VENUE_MIN_DEPOSIT.get("polymarket", 1.0)
    v2_min    = _VENUE_MIN_DEPOSIT.get(venue2, 1.0)

    if poly_before >= poly_target:
        poly_deposit = 0.0
        log(f"✅ Poly already has {poly_before:.4f} >= {poly_target:.4f} — no deposit needed")
    else:
        # Gap-based: only send what's needed to reach the target, enforcing venue minimum.
        # This allows servicer to top up even when the platform has partial balance,
        # rather than requiring the full leg amount from scratch every time.
        poly_gap_raw = poly_target - poly_before
        poly_deposit = max(poly_gap_raw, poly_min)
        if poly_deposit > poly_gap_raw:
            log(
                f"📌 Poly gap={poly_gap_raw:.4f} < min_deposit={poly_min} "
                f"— bumping to minimum"
            )
        log(f"📤 Poly needs {poly_deposit:.4f} USDC (has={poly_before:.4f} target={poly_target:.4f} gap={poly_gap_raw:.4f})")

    if venue2_before >= venue2_target:
        venue2_deposit = 0.0
        log(f"✅ {venue2} already has {venue2_before:.4f} >= {venue2_target:.4f} — no deposit needed")
    else:
        # Gap-based: only send what's needed to reach the target, enforcing venue minimum.
        venue2_gap_raw = venue2_target - venue2_before
        venue2_deposit = max(venue2_gap_raw, v2_min)
        if venue2_deposit > venue2_gap_raw:
            log(
                f"📌 {venue2} gap={venue2_gap_raw:.4f} < min_deposit={v2_min} "
                f"— bumping to minimum"
            )
        log(f"📤 {venue2} needs {venue2_deposit:.4f} USDC (has={venue2_before:.4f} target={venue2_target:.4f} gap={venue2_gap_raw:.4f})")

    log(
        f"📊 Balances: poly={poly_before:.4f} (required={poly_target:.4f} deposit={poly_deposit:.4f}) | "
        f"{venue2}={venue2_before:.4f} (required={venue2_target:.4f} deposit={venue2_deposit:.4f})"
    )

    # Alias for the rest of the function (capital gate, TX sending, wait logic)
    poly_gap   = poly_deposit
    venue2_gap = venue2_deposit
    total_gap  = poly_gap + venue2_gap

    # ── Step 4: Single combined capital gate ─────────────────────────────
    if poly_before >= poly_target and venue2_before >= venue2_target:
        log("✅ Both platforms already have sufficient balance — no deposit needed")
        return True, ""

    required = total_gap + ARB_SAFETY_BUFFER_USDC
    if servicer_usdc < required:
        msg = (
            f"fund_both_legs: servicer has {servicer_usdc:.4f} USDC — "
            f"insufficient for gaps ({poly_gap:.4f} + {venue2_gap:.4f}) + "
            f"safety_buffer ({ARB_SAFETY_BUFFER_USDC:.2f}) = {required:.4f} needed"
        )
        log(f"❌ {msg}")
        return False, msg

    log(
        f"✅ Capital check passed: servicer={servicer_usdc:.4f} >= "
        f"required={required:.4f} (gaps={total_gap:.4f} + buffer={ARB_SAFETY_BUFFER_USDC:.2f})"
    )

    # ── Step 5a: Opinion ─────────────────────────────────────────────────────
    if venue2 == "opinion":
        if venue2_before >= venue2_target:
            log(f"✅ Opinion already has {venue2_before:.4f} USDC >= {venue2_target:.4f} needed")
        elif OPINION_BASE_DEPOSIT_ADDR:
            # Direct Base deposit — instant, same path as Poly/Kalshi
            log(
                f"📤 Opinion direct Base deposit: {venue2_gap:.4f} USDC → "
                f"{OPINION_BASE_DEPOSIT_ADDR}"
            )
            try:
                nt = _NonceTracker(servicer_wallet)
                opinion_tx = _send_erc20_transfer(
                    private_key, OPINION_BASE_DEPOSIT_ADDR, round(venue2_gap, 6), nt
                )
                log(f"📤 Opinion deposit TX: {venue2_gap:.4f} USDC → tx={opinion_tx}")
            except Exception as e:
                msg = f"fund_both_legs: Opinion Base deposit failed: {e}"
                log(f"❌ {msg}")
                return False, msg
        else:
            msg = "fund_both_legs: OPINION_BASE_DEPOSIT_ADDR not set — BSC bridge path disabled; set OPINION_BASE_DEPOSIT_ADDR in .env"
            log(f"❌ {msg}")
            return False, msg

        # Opinion is funded (already had enough, or direct Base deposit sent).
        # Handle Poly top-up if needed, then wait for both to settle.
        if poly_gap > 0:
            if not POLY_BASE_DEPOSIT_ADDR:
                msg = "fund_both_legs: POLY_BASE_DEPOSIT_ADDR not set"
                log(f"❌ {msg}")
                return False, msg
            try:
                nt = _NonceTracker(servicer_wallet)
                poly_tx = _send_erc20_transfer(
                    private_key, POLY_BASE_DEPOSIT_ADDR, round(poly_gap, 6), nt
                )
                log(f"📤 Poly deposit: {poly_gap:.4f} USDC → tx={poly_tx}")
            except Exception as e:
                msg = f"fund_both_legs: Poly deposit failed: {e}"
                log(f"❌ {msg}")
                return False, msg

        # Wait for both platforms to confirm balance arrival
        if poly_gap > 0 and venue2_gap > 0:
            return _wait_for_both_balances(
                "polymarket", poly_before + poly_gap,
                "opinion", venue2_before + venue2_gap,
                wait_timeout, poll_interval,
            )
        elif poly_gap > 0:
            return _wait_for_balance("polymarket", poly_before + poly_gap, wait_timeout, poll_interval)
        elif venue2_gap > 0:
            return _wait_for_balance("opinion", venue2_before + venue2_gap, wait_timeout, poll_interval)

        return True, ""

    # ── Step 5b: Poly + Kalshi — send BOTH TXs in one nonce sequence ─────
    try:
        nt = _NonceTracker(servicer_wallet)
    except Exception as e:
        msg = f"fund_both_legs: cannot init nonce tracker: {e}"
        log(f"❌ {msg}")
        return False, msg

    if poly_gap > 0:
        if not POLY_BASE_DEPOSIT_ADDR:
            msg = "fund_both_legs: POLY_BASE_DEPOSIT_ADDR not set"
            log(f"❌ {msg}")
            return False, msg
        try:
            poly_tx = _send_erc20_transfer(
                private_key, POLY_BASE_DEPOSIT_ADDR, round(poly_gap, 6), nt
            )
            log(f"📤 Poly deposit TX: {poly_gap:.4f} USDC → {POLY_BASE_DEPOSIT_ADDR} tx={poly_tx}")
        except Exception as e:
            msg = f"fund_both_legs: Poly deposit failed: {e}"
            log(f"❌ {msg}")
            return False, msg
    else:
        log(f"✅ Poly already funded ({poly_before:.4f} >= {poly_target:.4f}) — no TX needed")

    if venue2_gap > 0:
        if not KALSHI_BASE_DEPOSIT_ADDR:
            msg = "fund_both_legs: KALSHI_BASE_DEPOSIT_ADDR not set — cannot fund Kalshi"
            log(f"❌ {msg}")
            return False, msg
        try:
            # Uses NEXT nonce from the same tracker — guaranteed to be sequenced
            # correctly with the Poly TX above.
            kalshi_tx = _send_erc20_transfer(
                private_key, KALSHI_BASE_DEPOSIT_ADDR, round(venue2_gap, 6), nt
            )
            log(f"📤 Kalshi deposit TX: {venue2_gap:.4f} USDC → {KALSHI_BASE_DEPOSIT_ADDR} tx={kalshi_tx}")
        except Exception as e:
            msg = f"fund_both_legs: Kalshi deposit failed: {e}"
            log(f"❌ {msg}")
            return False, msg
    else:
        log(f"✅ Kalshi already funded ({venue2_before:.4f} >= {venue2_target:.4f}) — no TX needed")

    # ── Step 6: Poll BOTH platform balances until both reach targets ──────
    poly_needed_target   = poly_before + poly_gap
    kalshi_needed_target = venue2_before + venue2_gap

    log(
        f"⏳ Waiting for deposits to land: "
        f"poly_target={poly_needed_target:.4f} USDC | "
        f"kalshi_target={kalshi_needed_target:.4f} USDC "
        f"(timeout={wait_timeout}s, poll={poll_interval}s)"
    )

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        poly_now   = _get_platform_balance("polymarket")
        kalshi_now = _get_platform_balance("kalshi")
        log(
            f"⏳ Balance poll: poly={poly_now:.4f}/{poly_needed_target:.4f} | "
            f"kalshi={kalshi_now:.4f}/{kalshi_needed_target:.4f}"
        )

        poly_ok   = poly_now   >= poly_needed_target   - 0.01  # 1-cent tolerance
        kalshi_ok = kalshi_now >= kalshi_needed_target - 0.01

        if poly_ok and kalshi_ok:
            log(
                f"✅ Both legs funded: poly={poly_now:.4f} kalshi={kalshi_now:.4f} "
                f"— deposits confirmed, proceeding to execution"
            )
            return True, ""

        remaining = deadline - time.time()
        log(
            f"⏳ Waiting for deposits... "
            f"poly={'✅' if poly_ok else f'{poly_now:.4f}/{poly_needed_target:.4f}'} "
            f"kalshi={'✅' if kalshi_ok else f'{kalshi_now:.4f}/{kalshi_needed_target:.4f}'} "
            f"({remaining:.0f}s remaining)"
        )

    # Timeout — report which legs didn't arrive
    poly_final   = _get_platform_balance("polymarket")
    kalshi_final = _get_platform_balance("kalshi")
    msg = (
        f"fund_both_legs: timeout after {wait_timeout}s — "
        f"poly={poly_final:.4f}/{poly_needed_target:.4f} "
        f"kalshi={kalshi_final:.4f}/{kalshi_needed_target:.4f}"
    )
    log(f"❌ {msg}")
    return False, msg


def _wait_for_balance(
    venue: str,
    target: float,
    wait_timeout: int,
    poll_interval: int,
) -> tuple[bool, str]:
    """Poll a single platform balance until it reaches `target` or timeout."""
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        current = _get_platform_balance(venue)
        if current >= target - 0.01:
            log(f"✅ {venue} balance {current:.4f} >= {target:.4f} — funded")
            return True, ""
        remaining = deadline - time.time()
        log(f"⏳ {venue}: {current:.4f}/{target:.4f} ({remaining:.0f}s remaining)")

    final = _get_platform_balance(venue)
    msg = f"fund_both_legs: {venue} deposit timeout after {wait_timeout}s — {final:.4f}/{target:.4f}"
    log(f"❌ {msg}")
    return False, msg


def _wait_for_both_balances(
    venue1: str,
    target1: float,
    venue2: str,
    target2: float,
    wait_timeout: int,
    poll_interval: int,
) -> tuple[bool, str]:
    """Poll two platform balances simultaneously until both reach their targets or timeout."""
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        bal1 = _get_platform_balance(venue1)
        bal2 = _get_platform_balance(venue2)
        ok1 = bal1 >= target1 - 0.01
        ok2 = bal2 >= target2 - 0.01
        remaining = deadline - time.time()
        log(
            f"⏳ Balance poll: {venue1}={bal1:.4f}/{target1:.4f} {'✅' if ok1 else '⏳'} | "
            f"{venue2}={bal2:.4f}/{target2:.4f} {'✅' if ok2 else '⏳'} "
            f"({remaining:.0f}s remaining)"
        )
        if ok1 and ok2:
            log(f"✅ Both legs funded — {venue1}={bal1:.4f} | {venue2}={bal2:.4f}")
            return True, ""

    final1 = _get_platform_balance(venue1)
    final2 = _get_platform_balance(venue2)
    msg = (
        f"fund_both_legs: deposit timeout after {wait_timeout}s — "
        f"{venue1}={final1:.4f}/{target1:.4f} | {venue2}={final2:.4f}/{target2:.4f}"
    )
    log(f"❌ {msg}")
    return False, msg


def run_tend_tick() -> None:
    """Call tend() on the vault and log servicer wallet status each cycle.

    This is the ONLY per-cycle funder action. There is NO float maintenance.
    Capital is deployed exactly when a specific trade is selected, in the exact
    amounts both legs need, via fund_both_legs_for_trade().

    All errors are logged and suppressed — never raises.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log(f"💓 [tend tick] starting — {ts}")

    private_key     = os.environ.get("POLY_PRIVATE_KEY", "")
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")
    vault_address   = os.environ.get("ARB_VAULT_V2_ADDRESS", "")

    missing_vars = []
    if not private_key:
        missing_vars.append("POLY_PRIVATE_KEY (servicer wallet signing key)")
    if not servicer_wallet:
        missing_vars.append("ARB_SERVICER_WALLET (servicer wallet address)")

    if missing_vars:
        for var in missing_vars:
            log(f"❌ Missing required env var: {var}")
        return

    _verify_wallet_key_match(private_key, servicer_wallet)

    try:
        # ── Tend: move vault idle USDC → servicer wallet ──────────────────
        if vault_address:
            log(f"🔄 tend() check — vault={vault_address} servicer={servicer_wallet}")
            _call_tend_if_ready(vault_address, private_key, servicer_wallet)
        else:
            log("ℹ️ ARB_VAULT_V2_ADDRESS not set — skipping tend()")

        # ── Servicer status ────────────────────────────────────────────────
        try:
            raw_usdc = _get_usdc_balance(servicer_wallet)
            raw_eth  = _get_eth_balance(servicer_wallet)
            log(
                f"💼 Servicer [{ts}]: address={servicer_wallet} "
                f"USDC={raw_usdc:.4f} ETH={raw_eth:.6f} "
                f"safety_buffer={ARB_SAFETY_BUFFER_USDC} "
                f"gas_reserve={ARB_SERVICER_GAS_RESERVE_ETH:.4f} ETH"
            )
            if raw_eth < ARB_SERVICER_GAS_RESERVE_ETH:
                log(
                    f"❌ Servicer ETH too low: {raw_eth:.6f} < {ARB_SERVICER_GAS_RESERVE_ETH} ETH — "
                    f"send ETH to {servicer_wallet} on Base"
                )
        except Exception as _be:
            log(f"⚠️ Could not read servicer wallet balances: {_be}")

        # ── Liquidity health snapshot (observability only) ─────────────────
        if vault_address:
            try:
                from .arb_liquidity import compute_liquidity_state
                liq = compute_liquidity_state(vault_address, servicer_wallet)
                if liq.ok:
                    log(
                        f"📊 Vault liquidity: idle={liq.idle_available:.2f} "
                        f"required_idle={liq.required_idle:.2f} "
                        f"pending_redeem={liq.pending_redeem_value:.2f} "
                        f"servicer={liq.free_cash:.2f} "
                        f"deployable={liq.deployable_capital:.2f} "
                        f"under_pressure={liq.under_pressure}"
                    )
            except Exception as e:
                log(f"⚠️ Liquidity snapshot failed (non-fatal): {e}")

    except Exception as e:
        log(f"❌ Tend tick error (non-fatal): {e}")


# Keep legacy name as alias so any external callers don't break immediately.
run_funder_tick = run_tend_tick
