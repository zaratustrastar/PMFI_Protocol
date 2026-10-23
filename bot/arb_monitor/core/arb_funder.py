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

Funding chain (ARB_FUNDING_CHAIN, default "base" = unchanged legacy V2 path):
  robinhood — servicer cash is USDG on Robinhood Chain 4663; tend() hits the V3 vault there;
  Polymarket is funded by a USDG transfer to POLY_RH_DEPOSIT_ADDR; Kalshi by an Across
  (Relay fallback) USDG→Base USDC bridge followed by the existing Base USDC transfer.
  Every value-moving tx is write-ahead (arb_ledger intent + idempotency key), receipt-checked,
  validated against arb_allowlist (tokens, bridge contracts, venue deposit addresses), and only
  then written to the in-transit ledgers in arb_withdrawals (bridge / venue-deposit) so NAV counts
  it exactly once. reconcile_funding_intents() resolves intents after a crash or RPC outage.
  Opinion has no Robinhood route and is fully disabled in robinhood mode (V1).

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
    is_robinhood_funding,
    opinion_enabled,
    robinhood_chain_id,
    robinhood_rpc_url,
    usdg_address,
    poly_rh_deposit_addr,
    across_api_url,
    relay_api_url,
    bridge_max_fee_bps,
    rh_sweep_min_usdc,
    tx_receipt_timeout_secs,
    tx_receipt_poll_secs,
)
from . import arb_allowlist as AL
from . import arb_ledger as _L
from .arb_http import request_json

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

# Minimum deposit enforced by each trading venue.
# Sending below this amount results in a rejected/lost deposit.
# Must stay in sync with fund_both_legs_for_trade() logic.
VENUE_MIN_DEPOSIT: dict = {
    "opinion":    3.0,   # Opinion Labs: $3 minimum deposit
    "polymarket": 3.0,   # Polymarket CLOB: $1 effective minimum
    "kalshi":     3.0,   # Kalshi: $1 effective minimum
}


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


def _get_usdc_balance(wallet: str, rpc_url: str = BASE_RPC, token: str = USDC_BASE) -> float:
    """Return USDC (or any 6-decimal stablecoin, e.g. USDG) balance of wallet as float."""
    padded = "000000000000000000000000" + wallet.lower().replace("0x", "")
    data = "0x" + SELECTOR_BALANCE_OF + padded
    raw = _rpc("eth_call", [{"to": token, "data": data}, "latest"], rpc_url)
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
# Funding chain (ARB_FUNDING_CHAIN=base|robinhood)
# ---------------------------------------------------------------------------

def _funding_rpc_chain() -> tuple[str, int]:
    """(rpc_url, chain_id) of the chain holding servicer cash and the vault."""
    if is_robinhood_funding():
        rpc = robinhood_rpc_url()
        if not rpc:
            raise RuntimeError("ARB_FUNDING_CHAIN=robinhood but ROBINHOOD_RPC_URL is not set")
        return rpc, robinhood_chain_id()
    return BASE_RPC, BASE_CHAIN_ID


def rpc_for_chain(chain_id: int) -> str:
    """RPC URL for a chain the funder sends on (4663 / Base / Polygon)."""
    chain_id = int(chain_id)
    if chain_id == BASE_CHAIN_ID:
        return BASE_RPC
    if chain_id == robinhood_chain_id():
        rpc = robinhood_rpc_url()
        if not rpc:
            raise RuntimeError("ROBINHOOD_RPC_URL is not set")
        return rpc
    if chain_id == 137:
        return os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
    raise RuntimeError(f"no RPC configured for chain {chain_id}")


def get_servicer_cash_usdc(wallet: str) -> float:
    """Servicer cash on the funding chain: USDG on 4663 (robinhood) or USDC on Base (base)."""
    if is_robinhood_funding():
        rpc, _ = _funding_rpc_chain()
        return _get_usdc_balance(wallet, rpc, token=usdg_address())
    return _get_usdc_balance(wallet)


def _get_servicer_gas_eth(wallet: str) -> float:
    """Gas balance on the funding chain."""
    rpc, _ = _funding_rpc_chain()
    return _get_eth_balance(wallet, rpc)


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

def _prepare_signed_tx(
    private_key: str,
    to: str,
    data: str,
    nonce_tracker: _NonceTracker,
    value_wei: int = 0,
    chain_id: int = BASE_CHAIN_ID,
    rpc_url: str = BASE_RPC,
    gas_override: int | None = None,
) -> tuple[str, str, int]:
    """Build and sign (no broadcast). Returns (raw_tx_hex, local_tx_hash, nonce)."""
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
    local_hash = signed.hash.hex() if hasattr(signed, "hash") else ""
    if local_hash and not local_hash.startswith("0x"):
        local_hash = "0x" + local_hash
    return "0x" + raw.hex(), local_hash, nonce


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
    (Base-mode behaviour unchanged: fire-and-forget, no receipt wait.)
    """
    raw, _, nonce = _prepare_signed_tx(private_key, to, data, nonce_tracker, value_wei,
                                       chain_id, rpc_url, gas_override)
    tx_hash = _rpc("eth_sendRawTransaction", [raw], rpc_url)
    log(f"📤 TX nonce={nonce} → {tx_hash}")
    return tx_hash


def _wait_for_receipt(tx_hash: str, rpc_url: str, timeout: float | None = None,
                      poll: float | None = None) -> str:
    """Poll for a receipt: "success" (status 1) | "failed" (status 0) | "unconfirmed" (timeout).

    Bounded by both wall-clock (ARB_TX_RECEIPT_TIMEOUT_SECS) and attempt count. RPC errors while
    polling are treated as "no receipt yet".
    """
    import math
    timeout = tx_receipt_timeout_secs() if timeout is None else timeout
    poll = tx_receipt_poll_secs() if poll is None else poll
    attempts = max(1, int(math.ceil(timeout / max(poll, 0.001)))) + 1
    deadline = time.time() + timeout
    warned = False
    for i in range(attempts):
        try:
            rec = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_url)
        except Exception as e:
            rec = None
            if not warned:
                log(f"⚠️ receipt poll error for {tx_hash}: {e}")
                warned = True
        if rec:
            ok = _to_int(rec.get("status")) == 1
            if not ok:
                log(f"❌ TX {tx_hash} REVERTED (receipt status 0)")
            return "success" if ok else "failed"
        if time.time() >= deadline or i == attempts - 1:
            break
        time.sleep(poll)
    log(f"⏳ TX {tx_hash}: no receipt within {timeout}s — UNCONFIRMED (counted in transit, flagged)")
    return "unconfirmed"


class FundingBlocked(RuntimeError):
    """Refused before any tx was signed (no capital moved)."""


def _execute_rh_op(
    *,
    key: str,
    kind: str,
    chain_id: int,
    rpc_url: str,
    token: str,
    amount_usdc: float,
    txs: list,
    nonce_tracker: "_NonceTracker",
    private_key: str,
    sender: str,
    meta: dict,
) -> dict:
    """Write-ahead execution of a value-moving operation (approvals first, value tx last).

    1. begin_intent(key) BEFORE signing (refuses a live duplicate key);
    2. each tx is signed, its precomputed hash + nonce stored in the intent, then broadcast;
    3. approvals: a reverted approval aborts; an unconfirmed one proceeds (nonce ordering);
    4. value tx: reverted → intent "reverted", nothing recorded in transit, raises;
       status 1 → "confirmed"; timeout → "unconfirmed" (still counted, flagged);
    5. our outflow is journaled and the in-transit row is written from the intent's meta
       (_ledger_intent), the same code startup reconciliation uses after a crash.
    """
    _L.begin_intent(key, kind, chain_id, token, amount_usdc, sender, txs[-1]["to"], meta=meta)
    final_hash = None
    for i, tx in enumerate(txs):
        final = i == len(txs) - 1
        gas = _to_int(tx.get("gas") or tx.get("gasLimit"))
        try:
            raw, local_hash, nonce = _prepare_signed_tx(
                private_key, tx["to"], tx.get("data", "0x"), nonce_tracker, 0, chain_id, rpc_url,
                int(gas * 1.2) if gas else tx.get("gas_override"),
            )
        except Exception as e:
            _L.update_intent(key, status="failed", error=f"sign tx {i}: {e}")
            raise
        if final:
            _L.update_intent(key, add_tx=local_hash or None, status="broadcast", final_nonce=nonce)
        else:
            _L.update_intent(key, add_tx=local_hash or None)
        try:
            tx_hash = _rpc("eth_sendRawTransaction", [raw], rpc_url)
        except Exception as e:
            if final:
                # Outcome unknown: the intent stays "broadcast" so nothing re-sends it until
                # reconcile_funding_intents() resolves it by receipt / nonce.
                raise RuntimeError(f"{kind}: broadcast outcome unknown ({e}); intent {key} left for reconciliation")
            _L.update_intent(key, status="failed", error=f"approval broadcast: {e}")
            raise
        log(f"📤 TX nonce={nonce} → {tx_hash}")
        _L.update_intent(key, add_tx=tx_hash)
        st = _wait_for_receipt(tx_hash, rpc_url)
        if not final:
            if st == "failed":
                _L.update_intent(key, status="failed", error=f"approval {tx_hash} reverted")
                raise RuntimeError(f"{kind}: approval {tx_hash} reverted")
            continue
        final_hash = tx_hash
        if st == "failed":
            _L.update_intent(key, status="reverted", final_tx=tx_hash)
            raise RuntimeError(f"{kind}: tx {tx_hash} reverted — nothing recorded in transit")
        _L.update_intent(key, status="confirmed" if st == "success" else "unconfirmed", final_tx=tx_hash)
        _L.record_cashflow(chain_id, token, -float(amount_usdc), kind, key)
        _ledger_intent(key)
        return {"tx_hash": final_hash, "receipt": st, "key": key}
    raise RuntimeError(f"{kind}: no transactions")


def _ledger_intent(key: str) -> None:
    """Write the in-transit row described by an intent's meta (idempotent) and mark it ledgered."""
    row = _L.get_intent(key)
    if not row or row.get("ledgered"):
        return
    from .arb_withdrawals import record_bridge_transfer, record_venue_deposit
    meta = row.get("meta") or {}
    tx_hash = row.get("final_tx") or (row.get("tx_hashes") or [None])[-1]
    receipt = "success" if row.get("status") == "confirmed" else "unconfirmed"
    if meta.get("ledger") == "bridge":
        record_bridge_transfer(
            meta["direction"], meta["provider"], meta.get("reference") or tx_hash,
            row["amount_usdc"], row["chain_id"], tx_hash=tx_hash, receipt=receipt,
            dest_balance_before=meta.get("dest_balance_before"),
            expected_out_usdc=meta.get("expected_out_usdc"), idem_key=key,
        )
    elif meta.get("ledger") == "venue":
        record_venue_deposit(
            meta["venue"], tx_hash, row["amount_usdc"], row["chain_id"],
            dest_balance_before=meta.get("dest_balance_before"), receipt=receipt,
            tx_hash=tx_hash, idem_key=key,
        )
    _L.update_intent(key, ledgered=True)


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
# Across (primary) / Relay (fallback) bridge — Robinhood USDG <-> Base USDC
# ---------------------------------------------------------------------------

def _to_int(v, default: int = 0) -> int:
    if v is None or v == "":
        return default
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _tok_fields(t) -> tuple:
    """(chainId, address) from a token descriptor that may be a dict or a bare address."""
    if isinstance(t, dict):
        return t.get("chainId"), t.get("address")
    return None, t if isinstance(t, str) else None


def _across_route_fields(q: dict) -> dict:
    in_chain, in_tok = _tok_fields(q.get("inputToken"))
    out_chain, out_tok = _tok_fields(q.get("outputToken"))
    return {"recipient": q.get("recipient"), "origin_chain": in_chain, "origin_token": in_tok,
            "dest_chain": out_chain or q.get("destinationChainId"), "dest_token": out_tok}


def _relay_route_fields(q: dict) -> dict:
    d = q.get("details") or {}
    cin = (d.get("currencyIn") or {}).get("currency") or {}
    cout = (d.get("currencyOut") or {}).get("currency") or {}
    return {"recipient": d.get("recipient"), "origin_chain": cin.get("chainId"),
            "origin_token": cin.get("address"), "dest_chain": cout.get("chainId"),
            "dest_token": cout.get("address")}


def _across_quote(amount_raw, trade_type, from_addr, recipient,
                  origin_chain, origin_token, dest_chain, dest_token):
    """Across swap API. Returns (txs, input_raw, min_output_raw, request_id, route_fields)."""
    q = request_json("GET", f"{across_api_url()}/swap/approval", params={
        "tradeType": trade_type,  # exactInput | exactOutput
        "amount": str(amount_raw),
        "inputToken": origin_token,
        "originChainId": origin_chain,
        "outputToken": dest_token,
        "destinationChainId": dest_chain,
        "depositor": from_addr,
        "recipient": recipient,
    }, timeout=20)
    swap_tx = q.get("swapTx") or {}
    if not swap_tx.get("to"):
        raise RuntimeError(f"Across quote missing swapTx: {list(q.keys())}")
    txs = [t for t in (q.get("approvalTxns") or []) if t.get("to")] + [swap_tx]
    if trade_type == "exactInput":
        in_raw = amount_raw
        out_raw = _to_int(q.get("minOutputAmount") or q.get("expectedOutputAmount"))
    else:
        in_raw = _to_int(q.get("maxInputAmount") or q.get("inputAmount"))
        out_raw = amount_raw
    return txs, in_raw, out_raw, None, _across_route_fields(q)


def _relay_quote(amount_raw, trade_type, from_addr, recipient,
                 origin_chain, origin_token, dest_chain, dest_token):
    """Relay quote API. Returns (txs, input_raw, min_output_raw, request_id, route_fields)."""
    q = request_json("POST", f"{relay_api_url()}/quote", json_body={
        "user": from_addr,
        "originChainId": origin_chain,
        "destinationChainId": dest_chain,
        "originCurrency": origin_token,
        "destinationCurrency": dest_token,
        "amount": str(amount_raw),
        "recipient": recipient,
        "tradeType": "EXACT_INPUT" if trade_type == "exactInput" else "EXACT_OUTPUT",
    }, timeout=20)
    txs, request_id = [], None
    for step in q.get("steps", []):
        request_id = request_id or step.get("requestId")
        for item in step.get("items", []):
            data = item.get("data") or {}
            if data.get("to"):
                txs.append(data)
    if not txs:
        raise RuntimeError(f"Relay quote returned no executable steps: {list(q.keys())}")
    details = q.get("details") or {}
    if trade_type == "exactInput":
        in_raw = amount_raw
        cur_out = details.get("currencyOut") or {}
        out_raw = _to_int(cur_out.get("minimumAmount") or cur_out.get("amount"))
    else:
        in_raw = _to_int((details.get("currencyIn") or {}).get("amount"))
        out_raw = amount_raw
    return txs, in_raw, out_raw, request_id, _relay_route_fields(q)


def _validate_quote(provider, txs, in_raw, out_raw, route, amount_raw, trade_type, recipient,
                    origin_chain, origin_token, dest_chain, dest_token) -> None:
    max_fee = bridge_max_fee_bps()
    if in_raw <= 0 or out_raw <= 0 or out_raw * 10_000 < in_raw * (10_000 - max_fee):
        raise RuntimeError(f"fee unverifiable or above {max_fee} bps (in={in_raw} out={out_raw})")
    if trade_type == "exactInput" and in_raw != amount_raw:
        raise RuntimeError(f"exactInput quote input {in_raw} != requested {amount_raw}")
    if trade_type == "exactOutput" and out_raw < amount_raw:
        raise RuntimeError(f"exactOutput quote output {out_raw} < requested {amount_raw}")
    AL.validate_bridge_txs(txs, origin_chain, origin_token, in_raw)
    AL.validate_route(route, recipient=recipient, origin_chain=origin_chain, origin_token=origin_token,
                      dest_chain=dest_chain, dest_token=dest_token)


def _bridge_stable(
    private_key: str,
    from_addr: str,
    recipient: str,
    amount_usdc: float,
    trade_type: str,
    origin_chain: int,
    origin_token: str,
    origin_rpc: str,
    dest_chain: int,
    dest_token: str,
    nonce_tracker: "_NonceTracker",
    *,
    direction: str = "bridge",
    idempotency_key: str | None = None,
    dest_balance_before: float | None = None,
) -> dict:
    """Bridge a 6-decimal stablecoin via Across, falling back to Relay on a bad/failed QUOTE.

    Fail-closed guards (arb_allowlist): allowlisted (chain, token) on both ends; fee and
    slippage <= ARB_BRIDGE_MAX_FEE_BPS (min output for exactInput, max input for exactOutput);
    approvals are exact approve(spender, <= input) on the origin token to an allowlisted
    spender; the deposit `to` is allowlisted; no native value; quoted recipient / destination
    must match when present. Once a tx has been broadcast we never fall back (that could
    double-spend); errors propagate. The op is write-ahead (idempotency key) and the in-transit
    row is written only after the deposit receipt (status 1, or "unconfirmed" on timeout).
    Returns {provider, tx_hash, reference, input_usdc, output_usdc, receipt, key}.
    """
    AL.require_token(origin_chain, origin_token)
    AL.require_token(dest_chain, dest_token)
    key = idempotency_key or _L.new_key(direction)
    _L.assert_key_available(key)
    amount_raw = int(round(amount_usdc * 1_000_000))
    errors = []
    for provider, quote_fn in (("across", _across_quote), ("relay", _relay_quote)):
        try:
            txs, in_raw, out_raw, request_id, route = quote_fn(
                amount_raw, trade_type, from_addr, recipient,
                origin_chain, origin_token, dest_chain, dest_token,
            )
            _validate_quote(provider, txs, in_raw, out_raw, route, amount_raw, trade_type, recipient,
                            origin_chain, origin_token, dest_chain, dest_token)
        except Exception as e:
            errors.append(f"{provider}: {e}")
            log(f"⚠️ {provider} bridge quote rejected: {e}")
            continue

        log(
            f"🌉 {provider}: {in_raw / 1e6:.6f} (chain {origin_chain}) → "
            f">= {out_raw / 1e6:.6f} (chain {dest_chain}) to {recipient} [{len(txs)} tx]"
        )
        res = _execute_rh_op(
            key=key, kind=direction, chain_id=origin_chain, rpc_url=origin_rpc, token=origin_token,
            amount_usdc=in_raw / 1_000_000, txs=txs, nonce_tracker=nonce_tracker,
            private_key=private_key, sender=from_addr,
            meta={"ledger": "bridge", "direction": direction, "provider": provider,
                  "reference": request_id, "dest_balance_before": dest_balance_before,
                  "expected_out_usdc": out_raw / 1_000_000, "dest_chain_id": dest_chain},
        )
        log(f"🌉 {provider} bridge deposit sent: {res['tx_hash']} (receipt={res['receipt']})")
        return {
            "provider": provider,
            "tx_hash": res["tx_hash"],
            "reference": request_id or res["tx_hash"],
            "input_usdc": in_raw / 1_000_000,
            "output_usdc": out_raw / 1_000_000,
            "receipt": res["receipt"],
            "key": key,
        }
    raise RuntimeError("all bridge providers failed: " + "; ".join(errors))


def _bridge_usdg_to_base_usdc(
    private_key: str,
    servicer_wallet: str,
    amount_usdc: float,
    nonce_tracker: "_NonceTracker",
    dest_balance_before: float | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """USDG on Robinhood Chain → exactly `amount_usdc` USDC on Base (servicer wallet)."""
    rpc, chain_id = _funding_rpc_chain()
    return _bridge_stable(
        private_key, servicer_wallet, servicer_wallet, amount_usdc, "exactOutput",
        chain_id, usdg_address(), rpc, BASE_CHAIN_ID, USDC_BASE, nonce_tracker,
        direction="rh_to_base", idempotency_key=idempotency_key, dest_balance_before=dest_balance_before,
    )


def _bridge_base_usdc_to_usdg(
    private_key: str,
    servicer_wallet: str,
    amount_usdc: float,
    nonce_tracker: "_NonceTracker",
    idempotency_key: str | None = None,
) -> dict:
    """All of `amount_usdc` USDC on Base → USDG on Robinhood Chain (servicer wallet)."""
    return _bridge_stable(
        private_key, servicer_wallet, servicer_wallet, amount_usdc, "exactInput",
        BASE_CHAIN_ID, USDC_BASE, BASE_RPC, robinhood_chain_id(), usdg_address(), nonce_tracker,
        direction="base_to_rh", idempotency_key=idempotency_key,
    )


def _erc20_transfer_calldata(to_addr: str, amount_usdc: float) -> str:
    amount_raw = int(round(amount_usdc * 1_000_000))
    return ("0x" + SELECTOR_TRANSFER + "000000000000000000000000" + to_addr.lower().replace("0x", "")
            + hex(amount_raw)[2:].zfill(64))


def _guard_no_inflight(direction: str | None = None, venue: str | None = None, kinds: tuple = ()) -> None:
    """Refuse a new send while an earlier one to the same destination is unresolved or in transit
    (prevents double-funding after a timeout or a crash/restart)."""
    from .arb_withdrawals import inflight_rows_for
    rows = inflight_rows_for(direction=direction, venue=venue)
    if rows:
        raise FundingBlocked(
            f"in_transit_pending: {len(rows)} earlier {direction or venue} transfer(s) still in transit "
            f"(refs={[r.get('reference') for r in rows][:3]})"
        )
    if kinds and _L.unresolved_intents(kinds=kinds):
        raise FundingBlocked(f"unresolved_intent: an earlier {'/'.join(kinds)} operation awaits reconciliation")


def _send_usdg_to_polymarket(private_key: str, amount_usdc: float, nonce_tracker: "_NonceTracker",
                             poly_before: float | None = None, idempotency_key: str | None = None) -> str:
    """Robinhood mode Polymarket deposit: USDG transfer on 4663 to the Polymarket bridge address.

    Write-ahead + receipt-checked; the rh_to_polymarket in-transit row carries a Polymarket
    balance snapshot and is cleared by balance delta. Returns the tx hash.
    """
    deposit_addr = poly_rh_deposit_addr()
    if not deposit_addr:
        raise FundingBlocked("POLY_RH_DEPOSIT_ADDR not set")
    rpc, chain_id = _funding_rpc_chain()
    AL.require_venue_deposit("polymarket", chain_id, usdg_address(), deposit_addr)
    _guard_no_inflight(direction="rh_to_polymarket", kinds=("rh_to_polymarket",))
    servicer = os.environ.get("ARB_SERVICER_WALLET", "")
    res = _execute_rh_op(
        key=idempotency_key or _L.new_key("rh_to_polymarket"), kind="rh_to_polymarket",
        chain_id=chain_id, rpc_url=rpc, token=usdg_address(), amount_usdc=round(amount_usdc, 6),
        txs=[{"to": usdg_address(), "data": _erc20_transfer_calldata(deposit_addr, amount_usdc)}],
        nonce_tracker=nonce_tracker, private_key=private_key, sender=servicer,
        meta={"ledger": "bridge", "direction": "rh_to_polymarket", "provider": "polymarket_bridge",
              "reference": None, "dest_balance_before": poly_before},
    )
    return res["tx_hash"]


def _fund_kalshi_from_robinhood(
    private_key: str,
    servicer_wallet: str,
    amount_usdc: float,
    nonce_tracker: "_NonceTracker",
    wait_timeout: int,
    poll_interval: int,
    kalshi_before: float | None = None,
) -> tuple[bool, str]:
    """Robinhood mode Kalshi deposit: bridge USDG (4663) → USDC (Base), wait for arrival,
    then the Base USDC transfer to KALSHI_BASE_DEPOSIT_ADDR (ledgered in transit until the
    Kalshi balance moves). Returns (ok, kalshi_deposit_tx | error).

    Base USDC already sitting in the servicer wallet (e.g. from an earlier timed-out bridge)
    is used first, so a stranded bridge is never paid for twice. A new bridge / deposit is
    refused while an earlier one is still in transit or unresolved.
    """
    from .arb_withdrawals import mark_bridge_arrived

    if not KALSHI_BASE_DEPOSIT_ADDR:
        return False, "fund_both_legs: KALSHI_BASE_DEPOSIT_ADDR not set — cannot fund Kalshi"
    try:
        AL.require_venue_deposit("kalshi", BASE_CHAIN_ID, USDC_BASE, KALSHI_BASE_DEPOSIT_ADDR)
        _guard_no_inflight(direction="rh_to_base", kinds=("rh_to_base", "base_to_rh"))
        _guard_no_inflight(venue="kalshi", kinds=("base_to_kalshi",))
    except (AL.AllowlistError, FundingBlocked) as e:
        return False, f"funding_insufficient: {e}"
    base_eth = _get_eth_balance(servicer_wallet)
    if base_eth < ARB_SERVICER_GAS_RESERVE_ETH:
        return False, (
            f"fund_both_legs: servicer Base ETH too low for the Kalshi transfer "
            f"({base_eth:.6f} < {ARB_SERVICER_GAS_RESERVE_ETH})"
        )

    base_before = _get_usdc_balance(servicer_wallet)
    need = round(amount_usdc - base_before, 6)
    if need > 0:
        try:
            res = _bridge_usdg_to_base_usdc(private_key, servicer_wallet, need, nonce_tracker,
                                            dest_balance_before=base_before)
        except Exception as e:
            return False, f"fund_both_legs: USDG→Base bridge failed: {e}"
        ok, err = _wait_for_balance(
            "servicer_base_usdc", base_before + need, wait_timeout, poll_interval,
            reader=lambda: _get_usdc_balance(servicer_wallet),
        )
        if not ok:
            return False, f"{err} (bridge {res['reference']} still recorded as in-transit)"
        mark_bridge_arrived(res["reference"])
    else:
        log(f"ℹ️ Servicer already holds {base_before:.4f} USDC on Base — no bridge needed")

    if kalshi_before is None:
        # Snapshot for delta-based clearing of the in-transit row (callers normally pass it).
        try:
            kalshi_before = _get_platform_balance("kalshi", raise_on_error=True)
        except Exception as e:
            log(f"⚠️ Kalshi balance unreadable before deposit ({e}) — row will need manual/funder clearing")
    try:
        res = _execute_rh_op(
            key=_L.new_key("base_to_kalshi"), kind="base_to_kalshi", chain_id=BASE_CHAIN_ID,
            rpc_url=BASE_RPC, token=USDC_BASE, amount_usdc=round(amount_usdc, 6),
            txs=[{"to": USDC_BASE, "data": _erc20_transfer_calldata(KALSHI_BASE_DEPOSIT_ADDR, amount_usdc)}],
            nonce_tracker=_NonceTracker(servicer_wallet), private_key=private_key, sender=servicer_wallet,
            meta={"ledger": "venue", "venue": "kalshi", "dest_balance_before": kalshi_before},
        )
    except Exception as e:
        return False, f"fund_both_legs: Kalshi deposit failed: {e}"
    kalshi_tx = res["tx_hash"]
    log(f"📤 Kalshi deposit TX: {amount_usdc:.4f} USDC → {KALSHI_BASE_DEPOSIT_ADDR} tx={kalshi_tx} (receipt={res['receipt']})")
    return True, kalshi_tx


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
        raw = _rpc("eth_call", [{"to": vault_address, "data": "0x" + sel}, "latest"], rpc_url)
        if not raw or raw == "0x":
            return 0
        return int(raw, 16)

    try:
        # Base V2 vault (default) or the V3 vault on Robinhood Chain 4663 — same tend() ABI.
        rpc_url, chain_id = _funding_rpc_chain()
        last_tend_ts  = _read_uint256("lastTendTimestamp()")
        tend_cooldown = _read_uint256("tendCooldown()")
        now = int(time.time())

        if now < last_tend_ts + tend_cooldown:
            remaining = (last_tend_ts + tend_cooldown) - now
            log(f"⏳ tend() cooldown: {remaining}s remaining — skip")
            return

        log(f"🔄 tend() cooldown elapsed (last={last_tend_ts}, cooldown={tend_cooldown}s) — calling")

        tend_data    = "0x" + _sel("tend()")
        nonce_tracker = _NonceTracker(servicer_wallet, rpc_url)
        rh = is_robinhood_funding()
        usdg_before = None
        if rh:
            try:
                usdg_before = get_servicer_cash_usdc(servicer_wallet)
            except Exception as e:
                log(f"⚠️ tend(): cannot snapshot servicer USDG ({e}) — inflow will not be journaled")
        tx_hash = _build_and_send_tx(
            private_key=private_key,
            to=vault_address,
            data=tend_data,
            nonce_tracker=nonce_tracker,
            chain_id=chain_id,
            rpc_url=rpc_url,
            gas_override=120_000,
        )
        log(f"✅ tend() sent: tx={tx_hash}")
        if rh:
            # Journal the vault → servicer inflow so delta-based recall arrival detection does not
            # mistake tend() proceeds for a landed recall.
            st = _wait_for_receipt(tx_hash, rpc_url)
            if st == "success" and usdg_before is not None:
                delta = get_servicer_cash_usdc(servicer_wallet) - usdg_before
                if delta > 0:
                    _L.record_cashflow(chain_id, usdg_address(), delta, "tend", tx_hash)
            elif st != "success":
                log(f"⚠️ tend() receipt {st} — inflow (if any) not journaled")

    except Exception as e:
        log(f"⚠️ _call_tend_if_ready error (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Platform balance reading
# ---------------------------------------------------------------------------

def _get_platform_balance(venue: str, raise_on_error: bool = False) -> float:
    """Read the current USDC balance available for trading on a platform.

    Args:
        venue:          "polymarket", "kalshi", or "opinion"
        raise_on_error: When True, re-raises exceptions instead of returning 0.0.
                        Use this in callers that must distinguish true $0 from
                        an unreadable balance (e.g. for balance-fit sizing).

    Returns USDC float. Returns 0.0 on any error (unless raise_on_error=True).
    """
    def _handle_err(msg: str, exc: Exception = None):
        log(msg)
        if raise_on_error:
            raise RuntimeError(msg) from exc
        return 0.0

    if venue == "polymarket":
        poly_api_key        = os.environ.get("POLY_API_KEY", "")
        poly_api_secret     = os.environ.get("POLY_API_SECRET", "")
        poly_api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        if not poly_api_key:
            return _handle_err("⚠️ POLY_API_KEY not set — Poly balance unknown (returning 0)")
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
            try:
                client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
                )
            except Exception as upd_err:
                log(f"⚠️ [POLY] update_balance_allowance failed (non-fatal): {upd_err}")
            result = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
            )
            log(f"🔍 [POLY] sig_type=2 (GNOSIS_SAFE) response: {result}")
            raw = result.get("balance", "0")
            bal_raw = float(raw)
            bal = bal_raw / 1_000_000
            log(f"💰 Poly balance (proxy): {bal:.6f} USDC (raw={raw})")

            # Diagnostic: also query sig_type=0 (EOA) to see which account has funds
            try:
                client_eoa = make_client(host=clob_url, key=private_key, chain_id=137, creds=creds, signature_type=0)
                result_eoa = client_eoa.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
                )
                raw_eoa = result_eoa.get("balance", "0")
                bal_eoa = float(raw_eoa) / 1_000_000
                log(f"🔍 sig_type=0 (EOA) response: {result_eoa}")
                log(f"💰 Poly balance (EOA): {bal_eoa:.6f} USDC (raw={raw_eoa})")
                if bal == 0 and bal_eoa > 0:
                    log(f"ℹ️ [POLY] Funds found in EOA account — returning EOA balance")
                    return bal_eoa
            except Exception as eoa_err:
                log(f"⚠️ [POLY] EOA balance check failed: {eoa_err}")

            return bal
        except Exception as e:
            return _handle_err(f"⚠️ Poly balance read error: {e}", e)

    elif venue == "kalshi":
        try:
            from .kalshi_auth import get_kalshi_headers, kalshi_auth_available
            if not kalshi_auth_available():
                return _handle_err("⚠️ Kalshi auth not configured — Kalshi balance unknown (returning 0)")
            import requests
            from ..config import KALSHI_BASE_URL
            url = f"{KALSHI_BASE_URL}/portfolio/balance"
            headers = get_kalshi_headers("GET", url)
            if not headers:
                return _handle_err("⚠️ Kalshi RSA signing failed — Kalshi balance unknown (returning 0)")
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                bal = float(data.get("balance", 0)) / 100.0
                log(f"💰 Kalshi balance: {bal:.4f} USDC")
                return bal
            return _handle_err(f"⚠️ Kalshi balance HTTP {resp.status_code}: {resp.text[:80]}")
        except Exception as e:
            return _handle_err(f"⚠️ Kalshi balance read error: {e}", e)

    elif venue == "opinion":
        if not opinion_enabled():
            log("ℹ️ Opinion disabled (ARB_OPINION_ENABLED=false) — Opinion balance treated as 0")
            return 0.0
        try:
            from ..adapters.opinion_clob import get_balance as opinion_get_balance
            return opinion_get_balance()
        except Exception as e:
            return _handle_err(f"⚠️ Opinion balance read error: {e}", e)

    log(f"⚠️ Unknown venue '{venue}' — balance unknown")
    return 0.0


def get_platform_spot_balances(venue2: str) -> tuple:
    """Read current USDC balances on Polymarket and venue2.

    Raises on any read failure so callers can distinguish true $0 from an
    unreadable balance. The executor wraps this in try/except to fail-open
    (skip balance-fit sizing) while the funder's capital gate stays authoritative.

    Args:
        venue2: "kalshi" or "opinion"

    Returns:
        (poly_usdc: float, venue2_usdc: float)

    Raises:
        RuntimeError: if either platform balance cannot be read.
    """
    poly_bal   = _get_platform_balance("polymarket", raise_on_error=True)
    venue2_bal = _get_platform_balance(venue2, raise_on_error=True)
    log(
        f"📊 [spot_balances] poly={poly_bal:.4f} USDC "
        f"| {venue2}={venue2_bal:.4f} USDC"
    )
    return poly_bal, venue2_bal


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
    rh_mode = is_robinhood_funding()
    if rh_mode and venue == "polymarket":
        deposit_addr = poly_rh_deposit_addr()
    else:
        deposit_addr = POLY_BASE_DEPOSIT_ADDR if venue == "polymarket" else KALSHI_BASE_DEPOSIT_ADDR
    if not deposit_addr:
        if rh_mode:
            log(f"❌ No deposit address configured for {venue} (robinhood mode fails closed)")
            return False
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
    servicer_usdc = get_servicer_cash_usdc(servicer_wallet)
    servicer_eth  = _get_servicer_gas_eth(servicer_wallet)

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

    if rh_mode:
        try:
            nt = nonce_tracker or _NonceTracker(servicer_wallet, _funding_rpc_chain()[0])
            if venue == "polymarket":
                tx = _send_usdg_to_polymarket(private_key, gap, nt, poly_before=current)
                log(f"📤 [{venue}] Deposited {gap:.4f} USDG → {deposit_addr} tx={tx}")
                return True
            ok, ref = _fund_kalshi_from_robinhood(private_key, servicer_wallet, gap, nt, 180, 10,
                                                  kalshi_before=current)
            if not ok:
                log(f"❌ [{venue}] {ref}")
            return ok
        except Exception as e:
            log(f"❌ [{venue}] Top-up transfer failed: {e}")
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
        usdc = get_servicer_cash_usdc(servicer_wallet)
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
    if venue2 == "opinion" and not opinion_enabled():
        # "funding_insufficient" prefix = no capital moved, so the execution loop skips to the next pair.
        msg = "funding_insufficient: opinion_disabled (ARB_OPINION_ENABLED=false) — Opinion leg not funded"
        log(f"❌ {msg}")
        return False, msg
    if venue2 == "opinion" and is_robinhood_funding():
        # Opinion code is kept intact but has no Robinhood Chain funding route in V1.
        msg = "funding_insufficient: Opinion funding from Robinhood Chain is not implemented (V1 keeps Opinion off)"
        log(f"❌ {msg}")
        return False, msg

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
        # Base mode: USDC/ETH on Base. Robinhood mode: USDG/ETH on 4663.
        servicer_eth  = _get_servicer_gas_eth(servicer_wallet)
        servicer_usdc = get_servicer_cash_usdc(servicer_wallet)
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
    poly_min  = VENUE_MIN_DEPOSIT.get("polymarket", 1.0)
    v2_min    = VENUE_MIN_DEPOSIT.get(venue2, 1.0)

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
        if ARB_DEPOSIT_WAIT_SECS > 0:
            log(f"⏱ Post-funding settlement delay: {ARB_DEPOSIT_WAIT_SECS}s (ARB_DEPOSIT_WAIT_SECS)")
            time.sleep(ARB_DEPOSIT_WAIT_SECS)
        return True, ""

    # ── Withdrawal reservation: earmark pending redemptions before checking ──
    # Read the vault's pending_redeem_value so funds owed to redeemers are
    # never consumed by a new trade. Without this, a race exists between the
    # upstream liquidity check (which does account for pending_redeem_value)
    # and this TX gate — if the servicer balance dropped between those two
    # reads, redeemer funds could be sent to a trading platform.
    pending_reservation = 0.0
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    if vault_address:
        try:
            from .arb_reporter import _read_vault_state
            vs = _read_vault_state(vault_address)
            if vs["ok"] and vs["pending_redeem_shares"] > 0:
                # pending_redeem_value = shares × officialPPS / 1e18 / 1e6
                # official_pps is in USDC×1e6 per 1e18 shares (raw uint from contract)
                pending_reservation = (
                    vs["pending_redeem_shares"] * vs["official_pps"]
                ) / 1e18 / 1e6
                log(
                    f"🔒 Withdrawal reservation: {pending_reservation:.4f} USDC earmarked "
                    f"for {vs['pending_redeem_shares']} pending redeem shares "
                    f"(PPS={vs['official_pps']/1e6:.6f})"
                )
        except Exception as _e:
            log(f"⚠️ Could not read pending_redeem_value for reservation (non-fatal): {_e}")
            # fail-open: proceed without reservation rather than blocking all trades

    # Effective servicer USDC = total on-chain balance minus what's reserved for redeemers
    effective_servicer = max(0.0, servicer_usdc - pending_reservation)

    required = total_gap + ARB_SAFETY_BUFFER_USDC
    if effective_servicer < required:
        # Use "funding_insufficient" prefix (not "funding_failed") so the caller
        # can distinguish: this gate fires BEFORE any TX is sent, so no capital
        # was moved and the execution cycle can safely continue to the next pair.
        msg = (
            f"funding_insufficient: servicer has {servicer_usdc:.4f} USDC "
            f"but {pending_reservation:.4f} reserved for pending redeems → "
            f"effective={effective_servicer:.4f} < "
            f"required={required:.4f} "
            f"(gaps={poly_gap:.4f}+{venue2_gap:.4f} + buffer={ARB_SAFETY_BUFFER_USDC:.2f})"
        )
        log(f"❌ {msg}")
        return False, msg

    log(
        f"✅ Capital check passed: servicer={servicer_usdc:.4f} "
        f"− reservation={pending_reservation:.4f} → effective={effective_servicer:.4f} >= "
        f"required={required:.4f} (gaps={total_gap:.4f} + buffer={ARB_SAFETY_BUFFER_USDC:.2f})"
    )

    # ── Step 5r: Robinhood mode — Poly via USDG on 4663, Kalshi via bridge to Base ──
    if is_robinhood_funding():
        return _fund_legs_robinhood(
            private_key, servicer_wallet,
            poly_before, poly_gap, venue2_before, venue2_gap,
            wait_timeout, poll_interval,
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

        # Wait for both platforms to confirm balance arrival, then apply settlement delay.
        # The delay is applied after confirmation so the exchange engine has fully
        # credited the funds before orders are placed.
        def _settle_and_return(ok: bool, err: str) -> tuple[bool, str]:
            if ok and ARB_DEPOSIT_WAIT_SECS > 0:
                log(f"⏱ Post-funding settlement delay: {ARB_DEPOSIT_WAIT_SECS}s (ARB_DEPOSIT_WAIT_SECS)")
                time.sleep(ARB_DEPOSIT_WAIT_SECS)
            return ok, err

        if poly_gap > 0 and venue2_gap > 0:
            return _settle_and_return(*_wait_for_both_balances(
                "polymarket", poly_before + poly_gap,
                "opinion", venue2_before + venue2_gap,
                wait_timeout, poll_interval,
            ))
        elif poly_gap > 0:
            return _settle_and_return(*_wait_for_balance("polymarket", poly_before + poly_gap, wait_timeout, poll_interval))
        elif venue2_gap > 0:
            return _settle_and_return(*_wait_for_balance("opinion", venue2_before + venue2_gap, wait_timeout, poll_interval))

        # Both gaps were 0 and opinion already had sufficient balance
        if ARB_DEPOSIT_WAIT_SECS > 0:
            log(f"⏱ Post-funding settlement delay: {ARB_DEPOSIT_WAIT_SECS}s (ARB_DEPOSIT_WAIT_SECS)")
            time.sleep(ARB_DEPOSIT_WAIT_SECS)
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
            if ARB_DEPOSIT_WAIT_SECS > 0:
                log(f"⏱ Post-funding settlement delay: {ARB_DEPOSIT_WAIT_SECS}s (ARB_DEPOSIT_WAIT_SECS)")
                time.sleep(ARB_DEPOSIT_WAIT_SECS)
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


def _fund_legs_robinhood(
    private_key: str,
    servicer_wallet: str,
    poly_before: float,
    poly_gap: float,
    kalshi_before: float,
    kalshi_gap: float,
    wait_timeout: int,
    poll_interval: int,
) -> tuple[bool, str]:
    """Robinhood-mode deposits for fund_both_legs_for_trade (Poly + Kalshi only).

    All refusals that happen before any tx is signed use the "funding_insufficient" prefix so the
    execution loop moves on without treating it as committed capital.
    """
    from .arb_withdrawals import mark_bridge_arrived, mark_venue_deposit_arrived

    # Pre-flight (no tx yet): allowlists and in-transit guards for BOTH legs, so we never fund
    # one leg and then refuse the other.
    try:
        if poly_gap > 0:
            AL.require_venue_deposit("polymarket", _funding_rpc_chain()[1], usdg_address(), poly_rh_deposit_addr())
            _guard_no_inflight(direction="rh_to_polymarket", kinds=("rh_to_polymarket",))
        if kalshi_gap > 0:
            AL.require_venue_deposit("kalshi", BASE_CHAIN_ID, USDC_BASE, KALSHI_BASE_DEPOSIT_ADDR)
            _guard_no_inflight(direction="rh_to_base", kinds=("rh_to_base", "base_to_rh"))
            _guard_no_inflight(venue="kalshi", kinds=("base_to_kalshi",))
    except (AL.AllowlistError, FundingBlocked) as e:
        msg = f"funding_insufficient: {e}"
        log(f"❌ {msg}")
        return False, msg

    try:
        nt = _NonceTracker(servicer_wallet, _funding_rpc_chain()[0])
    except Exception as e:
        msg = f"fund_both_legs: cannot init nonce tracker: {e}"
        log(f"❌ {msg}")
        return False, msg

    poly_tx = None
    if poly_gap > 0:
        try:
            poly_tx = _send_usdg_to_polymarket(private_key, poly_gap, nt, poly_before=poly_before)
            log(f"📤 Poly deposit TX: {poly_gap:.4f} USDG (4663) → {poly_rh_deposit_addr()} tx={poly_tx}")
        except (AL.AllowlistError, FundingBlocked, _L.DuplicateOperation) as e:
            msg = f"funding_insufficient: {e}"
            log(f"❌ {msg}")
            return False, msg
        except Exception as e:
            msg = f"fund_both_legs: Poly deposit failed: {e}"
            log(f"❌ {msg}")
            return False, msg

    kalshi_tx = None
    if kalshi_gap > 0:
        ok, ref = _fund_kalshi_from_robinhood(
            private_key, servicer_wallet, kalshi_gap, nt, wait_timeout, poll_interval,
            kalshi_before=kalshi_before,
        )
        if not ok:
            log(f"❌ {ref}")
            if poly_tx and ref.startswith("funding_insufficient"):
                ref = "fund_both_legs: " + ref   # the Poly leg already moved capital
            return False, ref
        kalshi_tx = ref

    if poly_gap > 0 and kalshi_gap > 0:
        ok, err = _wait_for_both_balances(
            "polymarket", poly_before + poly_gap, "kalshi", kalshi_before + kalshi_gap,
            wait_timeout, poll_interval,
        )
    elif poly_gap > 0:
        ok, err = _wait_for_balance("polymarket", poly_before + poly_gap, wait_timeout, poll_interval)
    elif kalshi_gap > 0:
        ok, err = _wait_for_balance("kalshi", kalshi_before + kalshi_gap, wait_timeout, poll_interval)
    else:
        ok, err = True, ""

    # Arrival is balance-delta based (venue balance reached before + deposit); on timeout the
    # rows stay in transit (counted) and are settled later by reconcile_bridge_transfers().
    if ok and poly_tx:
        mark_bridge_arrived(poly_tx)
    if ok and kalshi_tx:
        mark_venue_deposit_arrived(kalshi_tx)
    if ok and ARB_DEPOSIT_WAIT_SECS > 0:
        log(f"⏱ Post-funding settlement delay: {ARB_DEPOSIT_WAIT_SECS}s (ARB_DEPOSIT_WAIT_SECS)")
        time.sleep(ARB_DEPOSIT_WAIT_SECS)
    return ok, err


def _wait_for_balance(
    venue: str,
    target: float,
    wait_timeout: int,
    poll_interval: int,
    reader=None,
) -> tuple[bool, str]:
    """Poll a single platform balance (or `reader()`) until it reaches `target` or timeout."""
    read = reader or (lambda: _get_platform_balance(venue))
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        current = read()
        if current >= target - 0.01:
            log(f"✅ {venue} balance {current:.4f} >= {target:.4f} — funded")
            return True, ""
        remaining = deadline - time.time()
        log(f"⏳ {venue}: {current:.4f}/{target:.4f} ({remaining:.0f}s remaining)")

    final = read()
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
            raw_usdc = get_servicer_cash_usdc(servicer_wallet)
            raw_eth  = _get_servicer_gas_eth(servicer_wallet)
            log(
                f"💼 Servicer [{ts}]: chain={'robinhood' if is_robinhood_funding() else 'base'} address={servicer_wallet} "
                f"USDC={raw_usdc:.4f} ETH={raw_eth:.6f} "
                f"safety_buffer={ARB_SAFETY_BUFFER_USDC} "
                f"gas_reserve={ARB_SERVICER_GAS_RESERVE_ETH:.4f} ETH"
            )
            if raw_eth < ARB_SERVICER_GAS_RESERVE_ETH:
                log(
                    f"❌ Servicer ETH too low: {raw_eth:.6f} < {ARB_SERVICER_GAS_RESERVE_ETH} ETH — "
                    f"send ETH to {servicer_wallet} on {'Robinhood Chain' if is_robinhood_funding() else 'Base'}"
                )
        except Exception as _be:
            log(f"⚠️ Could not read servicer wallet balances: {_be}")

        # ── Robinhood mode: bring Base USDC (Kalshi withdrawals, stranded bridges) home as USDG ──
        if is_robinhood_funding():
            try:
                reconcile_funding_intents()
            except Exception as e:
                log(f"⚠️ Intent reconciliation failed (non-fatal): {e}")
            try:
                from .arb_withdrawals import sweep_base_usdc_to_robinhood
                sweep_base_usdc_to_robinhood(servicer_wallet, private_key)
            except Exception as e:
                log(f"⚠️ Base→USDG sweep failed (non-fatal): {e}")

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


# ---------------------------------------------------------------------------
# Startup / crash recovery
# ---------------------------------------------------------------------------

def _nonce_state(sender: str, rpc_url: str) -> tuple[int, int]:
    latest = int(_rpc("eth_getTransactionCount", [sender, "latest"], rpc_url), 16)
    pending = int(_rpc("eth_getTransactionCount", [sender, "pending"], rpc_url), 16)
    return latest, pending


def reconcile_funding_intents() -> dict:
    """Resolve write-ahead intents left by a crash / restart / RPC outage. Idempotent.

    * "intent"     — the value tx was never signed → "abandoned" (nothing moved).
    * "broadcast"  — check receipts of every stored hash: status 1 → confirmed + in-transit row;
                     status 0 → reverted. No receipt: if the account nonce has not reached the
                     value tx's nonce (latest and pending) it was never broadcast → "abandoned";
                     if it is only in the mempool it stays "broadcast" (blocks duplicates);
                     if the nonce was consumed without our receipt → "unconfirmed" (counted, alert).
    * confirmed/unconfirmed without an in-transit row → the row is written now.
    * "unconfirmed" — receipt re-checked; a revert marks the intent and its row "reverted".
    Returns counts per outcome. A live intent always blocks a new op for the same key/destination.
    """
    from .arb_withdrawals import mark_bridge_arrived, mark_venue_deposit_arrived
    counts = {"abandoned": 0, "confirmed": 0, "reverted": 0, "unconfirmed": 0, "still_pending": 0, "ledgered": 0}
    for row in _L.list_intents():
        key, st = row.get("key"), row.get("status")
        try:
            if st == "intent":
                _L.update_intent(key, status="abandoned", error="value tx never signed (reconciled)")
                counts["abandoned"] += 1
                continue
            if st not in ("broadcast", "confirmed", "unconfirmed"):
                continue
            rpc_url = rpc_for_chain(row["chain_id"])
            if st in ("broadcast", "unconfirmed"):
                receipt = None
                for h in reversed(row.get("tx_hashes") or []):
                    try:
                        rec = _rpc("eth_getTransactionReceipt", [h], rpc_url)
                    except Exception:
                        rec = None
                    if rec:
                        receipt = ("success" if _to_int(rec.get("status")) == 1 else "failed", h)
                        break
                if receipt and receipt[0] == "failed" and receipt[1] in (row.get("final_tx"), (row.get("tx_hashes") or [None])[-1]):
                    _L.update_intent(key, status="reverted", final_tx=receipt[1])
                    if row.get("ledgered"):
                        ref = (row.get("meta") or {}).get("reference") or receipt[1]
                        if (row.get("meta") or {}).get("ledger") == "venue":
                            mark_venue_deposit_arrived(receipt[1], status="reverted")
                        else:
                            mark_bridge_arrived(ref, status="reverted")
                    elif st != "broadcast":
                        _L.record_cashflow(row["chain_id"], row["token"], row["amount_usdc"], "reverted", key)
                    counts["reverted"] += 1
                    continue
                if receipt and receipt[0] == "success":
                    _L.update_intent(key, status="confirmed", final_tx=receipt[1])
                    if st == "broadcast":
                        _L.record_cashflow(row["chain_id"], row["token"], -float(row["amount_usdc"]), row["kind"], key)
                    counts["confirmed"] += 1
                elif st == "broadcast":
                    nonce = row.get("final_nonce")
                    latest, pending = _nonce_state(row["sender"], rpc_url) if nonce is not None else (None, None)
                    if nonce is not None and latest <= nonce and pending <= nonce:
                        _L.update_intent(key, status="abandoned", error="nonce never used (reconciled)")
                        counts["abandoned"] += 1
                        continue
                    if nonce is not None and latest > nonce:
                        _L.update_intent(key, status="unconfirmed", error="nonce consumed, no receipt for our hash")
                        _L.record_cashflow(row["chain_id"], row["token"], -float(row["amount_usdc"]), row["kind"], key)
                        log(f"🚨 Intent {key}: nonce {nonce} consumed but no receipt — counted in transit, manual review")
                        counts["unconfirmed"] += 1
                    else:
                        counts["still_pending"] += 1
                        log(f"⏳ Intent {key}: tx still in mempool — kept unresolved (blocks duplicates)")
                        continue
            fresh = _L.get_intent(key) or {}
            if fresh.get("status") in ("confirmed", "unconfirmed") and not fresh.get("ledgered"):
                _ledger_intent(key)
                counts["ledgered"] += 1
        except Exception as e:
            log(f"⚠️ Could not reconcile intent {key}: {e} (left unresolved — blocks duplicates)")
    if any(counts.values()):
        log(f"🔁 Intent reconciliation: {counts}")
    return counts


def run_startup_checks() -> list[str]:
    """Robinhood mode: validate funding config (allowlists, venue deposit chain/token ties) and
    reconcile write-ahead intents. Returns config problems; non-empty must block trading."""
    if not is_robinhood_funding():
        return []
    errs = AL.validate_funding_config(kalshi_deposit_addr=KALSHI_BASE_DEPOSIT_ADDR)
    for e in errs:
        log(f"🛑 Funding config: {e}")
    try:
        reconcile_funding_intents()
    except Exception as e:
        errs.append(f"intent reconciliation failed: {e}")
        log(f"🛑 Intent reconciliation failed: {e}")
    return errs


# Keep legacy name as alias so any external callers don't break immediately.
run_funder_tick = run_tend_tick
