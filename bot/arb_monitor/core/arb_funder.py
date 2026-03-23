"""pARB Auto-Funder: distributes USDC from servicer wallet to trading platforms.

On each tick:
1. Reads servicer wallet USDC balance on Base
2. If balance >= ARB_MIN_FUND_AMOUNT, distributes proportionally:
   - Polymarket share → ERC-20 transfer on Base → POLY_BASE_DEPOSIT_ADDR
   - Kalshi share → ERC-20 transfer on Base → KALSHI_BASE_DEPOSIT_ADDR
   - Opinion share → LI.FI bridge (Base → BSC) → OPINION_BSC_DEPOSIT_ADDR
3. Logs each distribution with timestamp, amount, destination, and tx hash

All amounts are in USDC (6-decimal precision on-chain).
Transactions are signed with POLY_PRIVATE_KEY (= servicer wallet private key).

Nonce management: a _NonceTracker is initialised once per tick from the pending
nonce (eth_getTransactionCount + "pending"), then incremented client-side for each
subsequent transaction in the same tick. This prevents nonce collisions when
multiple transfers are sent back-to-back before any confirms.

Errors are logged and suppressed so a funder failure never crashes the arb loop.
"""

import os
import time
from ..config import (
    ARB_MIN_FUND_AMOUNT,
    ARB_FUND_POLY_PCT,
    ARB_FUND_KALSHI_PCT,
    ARB_FUND_OPINION_PCT,
    ARB_SERVICER_GAS_RESERVE_ETH,
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


def run_funder_tick() -> None:
    """Check servicer wallet balance and distribute USDC to platforms if ready.

    Called from the arb execution loop at the top of each cycle.
    Liquidity-aware: only deploys capital beyond requiredIdle (pending redeems + target buffer).
    All errors are logged and suppressed — never raises.
    """
    private_key     = os.environ.get("POLY_PRIVATE_KEY", "")
    servicer_wallet = os.environ.get("ARB_SERVICER_WALLET", "")

    if not private_key or not servicer_wallet:
        log("ℹ️ POLY_PRIVATE_KEY or ARB_SERVICER_WALLET not set — funder skipped")
        return

    _verify_wallet_key_match(private_key, servicer_wallet)

    try:
        _run_funder_tick_inner(private_key, servicer_wallet)
    except Exception as e:
        log(f"❌ Funder tick error (non-fatal): {e}")


def _run_funder_tick_inner(private_key: str, servicer_wallet: str) -> None:
    """Inner implementation — all exceptions propagate to run_funder_tick for logging."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── 1. Liquidity state: compute deployable capital ─────────────────────
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    deploy_amount: float  # resolved below

    if vault_address:
        try:
            from .arb_liquidity import compute_liquidity_state
            liq = compute_liquidity_state(vault_address, servicer_wallet)
            if liq.ok:
                log(
                    f"[{ts}] Liquidity: idle={liq.idle_available:.2f} "
                    f"required_idle={liq.required_idle:.2f} "
                    f"pending_redeem={liq.pending_redeem_value:.2f} "
                    f"free_cash={liq.free_cash:.2f} "
                    f"deployable={liq.deployable_capital:.2f} "
                    f"under_pressure={liq.under_pressure}"
                )

                if liq.under_pressure and liq.deployable_capital <= 0:
                    log(
                        f"🛑 Funder paused: redemption pressure active "
                        f"(shortfall={liq.shortfall:.2f} USDC) and deployable_capital=0 — "
                        f"all servicer cash reserved for pending redeems"
                    )
                    return

                deploy_amount = liq.deployable_capital
                if deploy_amount < ARB_MIN_FUND_AMOUNT:
                    log(
                        f"ℹ️ Deployable capital {deploy_amount:.4f} USDC < "
                        f"min threshold {ARB_MIN_FUND_AMOUNT:.2f} — nothing to distribute"
                    )
                    return
            else:
                log("⚠️ Liquidity state unavailable — falling back to raw balance check")
                deploy_amount = _get_usdc_balance(servicer_wallet)
        except Exception as e:
            log(f"⚠️ Liquidity check failed ({e}) — falling back to raw balance")
            deploy_amount = _get_usdc_balance(servicer_wallet)
    else:
        # V2 not deployed — use raw balance (legacy / pre-deploy mode)
        try:
            deploy_amount = _get_usdc_balance(servicer_wallet)
        except Exception as e:
            log(f"⚠️ Could not read servicer USDC balance: {e}")
            return

    log(f"[{ts}] Servicer wallet USDC on Base: {deploy_amount:.4f} (deployable)")

    if deploy_amount < ARB_MIN_FUND_AMOUNT:
        log(
            f"ℹ️ Deployable {deploy_amount:.4f} < threshold {ARB_MIN_FUND_AMOUNT:.2f} USDC — "
            "nothing to distribute"
        )
        return

    # ── 2. Check ETH gas reserve ───────────────────────────────────────────
    try:
        eth_balance = _get_eth_balance(servicer_wallet)
        log(f"Servicer wallet ETH on Base: {eth_balance:.6f} ETH")
        if eth_balance < ARB_SERVICER_GAS_RESERVE_ETH:
            log(
                f"⚠️ ETH balance {eth_balance:.6f} < gas reserve "
                f"{ARB_SERVICER_GAS_RESERVE_ETH:.4f} ETH — "
                "skipping distribution to avoid stranding gas"
            )
            return
    except Exception as e:
        log(f"⚠️ Could not read ETH balance — proceeding cautiously: {e}")

    # ── 3. Calculate proportional split of deployable capital ─────────────
    usdc_balance = deploy_amount   # rename for clarity in distribution logic below
    total_pct = ARB_FUND_POLY_PCT + ARB_FUND_KALSHI_PCT + ARB_FUND_OPINION_PCT
    if total_pct <= 0:
        log("⚠️ All fund percentages are 0 — nothing to distribute")
        return

    poly_amt   = round(usdc_balance * ARB_FUND_POLY_PCT   / total_pct, 6)
    kalshi_amt = round(usdc_balance * ARB_FUND_KALSHI_PCT / total_pct, 6)
    opinion_amt = round(usdc_balance - poly_amt - kalshi_amt, 6)  # remainder avoids drift

    log(
        f"📊 Split (of {usdc_balance:.4f} deployable): "
        f"poly={poly_amt:.4f} ({ARB_FUND_POLY_PCT}%) "
        f"kalshi={kalshi_amt:.4f} ({ARB_FUND_KALSHI_PCT}%) "
        f"opinion={opinion_amt:.4f} ({ARB_FUND_OPINION_PCT}%)"
    )

    # ── 4. Initialise nonce tracker (pending nonce, single sender) ─────────
    # All txs in this tick share one tracker so each subsequent tx automatically
    # gets the next sequential nonce, preventing RPC-pending-state race conditions.
    try:
        nonce_tracker = _NonceTracker(servicer_wallet)
    except Exception as e:
        log(f"❌ Could not initialise nonce tracker: {e}")
        return

    # ── 5. Send Polymarket share ───────────────────────────────────────────
    if poly_amt > 0 and POLY_BASE_DEPOSIT_ADDR:
        try:
            tx = _send_erc20_transfer(
                private_key=private_key,
                to_addr=POLY_BASE_DEPOSIT_ADDR,
                amount_usdc=poly_amt,
                nonce_tracker=nonce_tracker,
            )
            log(
                f"✅ [{ts}] Polymarket: sent {poly_amt:.4f} USDC → "
                f"{POLY_BASE_DEPOSIT_ADDR} tx={tx}"
            )
        except Exception as e:
            log(f"❌ Polymarket transfer failed ({poly_amt:.4f} USDC): {e}")
    elif not POLY_BASE_DEPOSIT_ADDR:
        log(
            f"⚠️ POLY_BASE_DEPOSIT_ADDR not set — skipping Polymarket share "
            f"({poly_amt:.4f} USDC stays in servicer wallet; will be redistributed "
            "per configured split on next funded tick)"
        )

    # ── 6. Send Kalshi share ───────────────────────────────────────────────
    if kalshi_amt > 0 and KALSHI_BASE_DEPOSIT_ADDR:
        try:
            tx = _send_erc20_transfer(
                private_key=private_key,
                to_addr=KALSHI_BASE_DEPOSIT_ADDR,
                amount_usdc=kalshi_amt,
                nonce_tracker=nonce_tracker,
            )
            log(
                f"✅ [{ts}] Kalshi: sent {kalshi_amt:.4f} USDC → "
                f"{KALSHI_BASE_DEPOSIT_ADDR} tx={tx}"
            )
        except Exception as e:
            log(f"❌ Kalshi transfer failed ({kalshi_amt:.4f} USDC): {e}")
    elif not KALSHI_BASE_DEPOSIT_ADDR:
        log(
            f"⚠️ KALSHI_BASE_DEPOSIT_ADDR not set — skipping Kalshi share "
            f"({kalshi_amt:.4f} USDC stays in servicer wallet; will be redistributed "
            "per configured split on next funded tick)"
        )

    # ── 7. Bridge Opinion share (Base → BSC via LI.FI) ────────────────────
    if opinion_amt > 0 and OPINION_BSC_DEPOSIT_ADDR:
        try:
            tx = _bridge_usdc_base_to_bsc(
                private_key=private_key,
                from_addr=servicer_wallet,
                amount_usdc=opinion_amt,
                to_bsc_addr=OPINION_BSC_DEPOSIT_ADDR,
                nonce_tracker=nonce_tracker,
            )
            log(
                f"✅ [{ts}] Opinion (bridge): {opinion_amt:.4f} USDC "
                f"Base → BSC {OPINION_BSC_DEPOSIT_ADDR} tx={tx}"
            )
        except Exception as e:
            log(f"❌ Opinion bridge failed ({opinion_amt:.4f} USDC): {e}")
    elif not OPINION_BSC_DEPOSIT_ADDR:
        log(
            f"⚠️ OPINION_BSC_DEPOSIT_ADDR not set — skipping Opinion bridge "
            f"({opinion_amt:.4f} USDC stays in servicer wallet; will be redistributed "
            "per configured split on next funded tick)"
        )
