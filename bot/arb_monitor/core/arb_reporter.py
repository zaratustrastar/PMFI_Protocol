"""arb_reporter.py — V2 report signer, waterfall sweeper, and early trigger.

Responsibilities:
  1. Compute conservative reportedAssets (cash + settled proceeds; NO open position marks)
  2. Sign a ReportDataV2 payload for the contract's report() function
  3. Submit the report() transaction to Base mainnet when cooldown OR early trigger fires
  4. Run the withdrawal funding waterfall when redemption shortfall exists:
       Step 1 — vault idle cash (already there, just counted)
       Step 2 — sweep free servicer cash back via refillBuffer()
       Step 3 — settled/claimable proceeds (included in servicer cash or logged)
       Step 4 — log unwind recommendation (position unwind handled by trading bot)
       Step 5 — broader unwind alert if still undershooting

reportedAssets formula (conservative — only clearly owned and withdrawable):
    vault_idle_usdc + servicer_on_base + poly_cash + kalshi_cash + opinion_cash + settled_pnl

Intentionally excluded:
    open_positions_liquid_value (unrealised, mark-to-market — monitoring only)

Domain salt: "PMFIArbVaultV2.v1"  (isolated from V1 sigs and pSNIPER sigs)

Env vars:
    ARB_VAULT_V2_ADDRESS            — deployed PMFIArbVaultV2 address
    ARB_NAV_SIGNER_PRIVATE_KEY      — private key of the reportSigner address
    ARB_SERVICER_WALLET             — servicer wallet address (derived from POLY_PRIVATE_KEY)
    BASE_RPC_URL                    — (optional) Base RPC, defaults to mainnet.base.org
    ARB_EARLY_REPORT_PRESSURE_RATIO — early report fires when pending_redeems/idle >= this
    ARB_EARLY_REPORT_MIN_ELAPSED    — min seconds since last report before early trigger
    ARB_WATERFALL_MIN_SHORTFALL     — min USDC shortfall to activate waterfall sweep
"""

import os
import time
import struct as _struct

DOMAIN_SALT_TEXT = "PMFIArbVaultV2.v1"
BASE_CHAIN_ID = 8453
BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# report() external ABI — 4-field struct only.
# vault/chainId/domainSalt are hardcoded inside the contract; the caller omits them.
# Verified by scanning deployed bytecode: selector 8c1d9244 matches this signature.
REPORT_SELECTOR = "report((uint256,uint256,uint256,uint256),bytes,uint256,uint256)"

# Conservative haircut applied to vault cash balances (95%) to account for
# gas costs, bridge fees, and minor API latency errors.
REPORTED_ASSETS_HAIRCUT = 0.95

# How long before the report deadline (seconds) — gives the bot time to broadcast
REPORT_DEADLINE_BUFFER = 3600  # 1 hour

# Default: process up to 50 deposits and 50 redeems per report call
DEFAULT_MAX_DEPOSITS = 50
DEFAULT_MAX_REDEEMS = 50


def log(msg: str):
    print(f"📋 [ArbReporter] {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Crypto helpers (pure Python — no web3 dependency for signing)
# ─────────────────────────────────────────────────────────────────────────────

def _keccak256(data: bytes) -> bytes:
    from eth_hash.auto import keccak
    return keccak(data)


def _keccak256_text(text: str) -> bytes:
    return _keccak256(text.encode("utf-8"))


def _abi_encode_report_struct(
    reported_assets_wei: int,
    timestamp: int,
    deadline: int,
    nonce: int,
    vault_address: str,
    chain_id: int,
    domain_salt_bytes: bytes,
) -> bytes:
    """ABI-encode the report struct for the EIP-712 structHash."""
    # Matches: keccak256(abi.encode(REPORT_TYPEHASH, reportedAssets, timestamp,
    #                                deadline, nonce, vault, chainId, domainSalt))
    TYPEHASH = _keccak256_text(
        "ReportDataV2(uint256 reportedAssets,uint256 timestamp,uint256 deadline,"
        "uint256 nonce,address vault,uint256 chainId,bytes32 domainSalt)"
    )

    def _pad32(n: int) -> bytes:
        return n.to_bytes(32, "big")

    def _addr32(a: str) -> bytes:
        addr = a.replace("0x", "").lower()
        return bytes.fromhex("0" * 24 + addr)

    def _bytes32(b: bytes) -> bytes:
        return b.ljust(32, b"\x00")[:32]

    encoded = (
        TYPEHASH                        # bytes32
        + _pad32(reported_assets_wei)   # uint256
        + _pad32(timestamp)             # uint256
        + _pad32(deadline)              # uint256
        + _pad32(nonce)                 # uint256
        + _addr32(vault_address)        # address (padded to 32)
        + _pad32(chain_id)              # uint256
        + _bytes32(domain_salt_bytes)   # bytes32
    )
    return _keccak256(encoded)


def _eth_signed_message_hash(struct_hash: bytes) -> bytes:
    """Apply \x19Ethereum Signed Message:\n32 prefix (matches toEthSignedMessageHash)."""
    prefix = b"\x19Ethereum Signed Message:\n32"
    return _keccak256(prefix + struct_hash)


def _sign_report(
    reported_assets_usdc: float,
    timestamp: int,
    deadline: int,
    nonce: int,
    vault_address: str,
    private_key: str,
) -> str:
    """Return hex signature for ReportDataV2."""
    from eth_account import Account

    reported_assets_wei = int(reported_assets_usdc * 1e6)
    domain_salt = _keccak256_text(DOMAIN_SALT_TEXT)

    struct_hash = _abi_encode_report_struct(
        reported_assets_wei=reported_assets_wei,
        timestamp=timestamp,
        deadline=deadline,
        nonce=nonce,
        vault_address=vault_address,
        chain_id=BASE_CHAIN_ID,
        domain_salt_bytes=domain_salt,
    )

    digest = _eth_signed_message_hash(struct_hash)
    from eth_account._utils.signing import sign_message_hash
    pk = Account.from_key(private_key)
    sig = pk.signHash(digest)
    return "0x" + sig.signature.hex()


# ─────────────────────────────────────────────────────────────────────────────
# Vault state reader (minimal RPC — no web3 dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _rpc(method: str, params: list) -> object:
    import requests as _req
    resp = _req.post(BASE_RPC, json={
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error ({method}): {data['error']}")
    return data["result"]


def _call_view(to: str, selector_hex: str) -> str:
    """eth_call with 4-byte selector, no args."""
    result = _rpc("eth_call", [{"to": to, "data": "0x" + selector_hex}, "latest"])
    return result


def _call_view_uint(to: str, selector_hex: str) -> int:
    raw = _call_view(to, selector_hex)
    return int(raw, 16) if raw and raw != "0x" else 0


# 4-byte selectors (keccak256 first 4 bytes)
# lastReportNonce()   → 0x6e8d8fb5  (computed offline: keccak256("lastReportNonce()"))
# lastReportTimestamp() → 0x ... computed from ABI
# reportCooldown()
# officialPPS()
# idleBalance()
# totalPendingRedeemShares()

SEL_LAST_REPORT_NONCE      = "247afd64"   # keccak256("lastReportNonce()")[:4]
SEL_LAST_REPORT_TIMESTAMP  = "57db845a"   # keccak256("lastReportTimestamp()")[:4]
SEL_REPORT_COOLDOWN        = "54b81a71"   # keccak256("reportCooldown()")[:4]
SEL_OFFICIAL_PPS           = "bc0a7f5d"   # keccak256("officialPPS()")[:4]
SEL_IDLE_BALANCE           = "b1bbb310"   # keccak256("idleBalance()")[:4]
SEL_PENDING_REDEEM_SHARES  = "8eff0106"   # keccak256("totalPendingRedeemShares()")[:4]


def _read_vault_state(vault_address: str) -> dict:
    """Read key vault state variables via eth_call."""
    try:
        last_nonce     = _call_view_uint(vault_address, SEL_LAST_REPORT_NONCE)
        last_ts        = _call_view_uint(vault_address, SEL_LAST_REPORT_TIMESTAMP)
        cooldown       = _call_view_uint(vault_address, SEL_REPORT_COOLDOWN)
        official_pps   = _call_view_uint(vault_address, SEL_OFFICIAL_PPS)
        idle_balance   = _call_view_uint(vault_address, SEL_IDLE_BALANCE)
        pending_redeem = _call_view_uint(vault_address, SEL_PENDING_REDEEM_SHARES)
        return {
            "last_report_nonce":      last_nonce,
            "last_report_timestamp":  last_ts,
            "report_cooldown":        cooldown,
            "official_pps":           official_pps,
            "idle_balance_usdc":      idle_balance / 1e6,
            "pending_redeem_shares":  pending_redeem,
            "ok": True,
        }
    except Exception as e:
        log(f"⚠️ Could not read vault state: {e}")
        return {"ok": False}


def _read_usdc_balance(wallet: str) -> float:
    """Read USDC balance of wallet on Base."""
    padded = "000000000000000000000000" + wallet.lower().replace("0x", "")
    data = "0x" + "70a08231" + padded
    raw = _rpc("eth_call", [{"to": USDC_BASE, "data": data}, "latest"])
    return int(raw, 16) / 1e6


# ─────────────────────────────────────────────────────────────────────────────
# reportedAssets computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_reported_assets(vault_address: str) -> dict:
    """Compute conservative reportedAssets for report().

    Conservative = cash that is clearly owned and withdrawable.
    Does NOT include open position mark-to-market values.

    Returns:
        {
            "reported_assets_usdc": float,
            "vault_idle_usdc": float,
            "servicer_on_base": float,
            "poly_cash": float,
            "kalshi_cash": float,
            "opinion_cash": float,
            "settled_pnl": float,
            "breakdown": {...},
        }
    """
    from .arb_nav import (
        _get_servicer_balances,
        _get_servicer_wallet_usdc_on_base,
        _get_settled_pnl,
    )

    log("Computing conservative reportedAssets...")

    # Vault idle USDC (in-contract, not yet deployed)
    vault_idle = 0.0
    try:
        vault_state = _read_vault_state(vault_address)
        if vault_state["ok"]:
            vault_idle = vault_state["idle_balance_usdc"]
            log(f"  vault idle USDC: {vault_idle:.4f}")
    except Exception as e:
        log(f"  ⚠️ Could not read vault idle: {e}")

    # Servicer wallet free USDC on Base
    servicer_on_base = _get_servicer_wallet_usdc_on_base()
    log(f"  servicer on Base: {servicer_on_base:.4f}")

    # Platform cash balances
    poly_cash, kalshi_cash, opinion_cash = _get_servicer_balances()
    log(f"  poly_cash={poly_cash:.4f} kalshi_cash={kalshi_cash:.4f} opinion_cash={opinion_cash:.4f}")

    # Settled (realized) PnL only — not open position estimates
    settled_pnl = _get_settled_pnl()
    log(f"  settled_pnl={settled_pnl:.4f}")

    raw_total = (
        vault_idle
        + servicer_on_base
        + poly_cash
        + kalshi_cash
        + opinion_cash
        + settled_pnl
    )

    # Apply conservative haircut to account for fees, latency, minor API errors
    reported_assets = max(0.0, raw_total * REPORTED_ASSETS_HAIRCUT)

    log(
        f"reportedAssets: raw={raw_total:.4f} → haircut({REPORTED_ASSETS_HAIRCUT:.0%})={reported_assets:.4f} USDC"
    )

    return {
        "reported_assets_usdc": round(reported_assets, 6),
        "vault_idle_usdc":      round(vault_idle, 6),
        "servicer_on_base":     round(servicer_on_base, 6),
        "poly_cash":            round(poly_cash, 6),
        "kalshi_cash":          round(kalshi_cash, 6),
        "opinion_cash":         round(opinion_cash, 6),
        "settled_pnl":          round(settled_pnl, 6),
        "raw_total_usdc":       round(raw_total, 6),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report payload builder
# ─────────────────────────────────────────────────────────────────────────────

def build_report_payload(vault_address: str, signer_key: str, force: bool = False) -> dict | None:
    """Build and sign a ReportDataV2 payload for the contract's report() function.

    Args:
        vault_address: deployed V2 vault address
        signer_key:    ARB_NAV_SIGNER_PRIVATE_KEY
        force:         if True, bypasses the internal cooldown check. Use when
                       run_reporter_tick has already decided to fire early due to
                       redemption pressure. The contract-level cooldown is still
                       enforced on-chain — this only skips the local guard.

    Returns the payload dict or None if vault/key missing or cooldown not elapsed.
    """
    if not vault_address:
        log("⚠️ ARB_VAULT_V2_ADDRESS not set — cannot build report")
        return None
    if not signer_key:
        log("⚠️ ARB_NAV_SIGNER_PRIVATE_KEY not set — cannot sign report")
        return None

    # Read vault state (needed for nonce regardless of cooldown check)
    vault_state = _read_vault_state(vault_address)
    if not vault_state["ok"]:
        log("⚠️ Could not read vault state — skipping report")
        return None

    last_ts  = vault_state["last_report_timestamp"]
    cooldown = vault_state["report_cooldown"]
    now      = int(time.time())

    if not force and last_ts > 0 and (now - last_ts) < cooldown:
        remaining = cooldown - (now - last_ts)
        log(f"⏳ Report cooldown: {remaining}s remaining — skip")
        return None

    reason = "forced (early trigger)" if force else "cooldown elapsed"
    log(f"✅ Building report — reason: {reason} (last={last_ts}, cooldown={cooldown}s)")

    # Compute conservative assets
    assets_data = compute_reported_assets(vault_address)
    reported_assets = assets_data["reported_assets_usdc"]

    # Build nonce (strictly > last)
    nonce = vault_state["last_report_nonce"] + 1
    timestamp = now
    deadline  = now + REPORT_DEADLINE_BUFFER

    # Sign
    signature = _sign_report(
        reported_assets_usdc=reported_assets,
        timestamp=timestamp,
        deadline=deadline,
        nonce=nonce,
        vault_address=vault_address,
        private_key=signer_key,
    )

    payload = {
        "reported_assets_usdc": reported_assets,
        "timestamp":            timestamp,
        "deadline":             deadline,
        "nonce":                nonce,
        "vault_address":        vault_address,
        "chain_id":             BASE_CHAIN_ID,
        "domain_salt":          DOMAIN_SALT_TEXT,
        "signature":            signature,
        "breakdown":            assets_data,
    }

    log(
        f"✅ Report payload built: reportedAssets={reported_assets:.4f} USDC "
        f"nonce={nonce} deadline={deadline}"
    )
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Transaction broadcaster
# ─────────────────────────────────────────────────────────────────────────────

def _get_gas_price() -> int:
    raw = _rpc("eth_gasPrice", [])
    return int(raw, 16)


def _get_nonce(address: str) -> int:
    raw = _rpc("eth_getTransactionCount", [address, "pending"])
    return int(raw, 16)


def _abi_encode_report_call(payload: dict, max_deposits: int, max_redeems: int) -> bytes:
    """Encode the report() call data.

    External signature: report((uint256,uint256,uint256,uint256),bytes,uint256,uint256)
    Selector: 8c1d9244 (keccak256 of above, verified against deployed bytecode)

    The contract appends address(this), block.chainid, DOMAIN_SALT internally
    for signature verification — the caller only provides the 4 data fields.
    """
    from eth_abi import encode as abi_encode

    selector = _keccak256_text(REPORT_SELECTOR)[:4]

    sig_bytes = bytes.fromhex(payload["signature"].replace("0x", ""))
    reported_assets_wei = int(payload["reported_assets_usdc"] * 1e6)

    # 4-field tuple — matches deployed ReportData struct exactly
    report_tuple = (
        reported_assets_wei,
        payload["timestamp"],
        payload["deadline"],
        payload["nonce"],
    )

    encoded_args = abi_encode(
        ["(uint256,uint256,uint256,uint256)", "bytes", "uint256", "uint256"],
        [report_tuple, sig_bytes, max_deposits, max_redeems],
    )

    return selector + encoded_args


def submit_report(
    payload: dict,
    signer_key: str,
    max_deposits: int = DEFAULT_MAX_DEPOSITS,
    max_redeems: int  = DEFAULT_MAX_REDEEMS,
) -> str | None:
    """Sign and broadcast the report() transaction to Base mainnet.

    Returns tx hash string or None on failure.
    """
    from eth_account import Account

    vault_address = payload["vault_address"]
    account       = Account.from_key(signer_key)

    try:
        call_data = _abi_encode_report_call(payload, max_deposits, max_redeems)
    except Exception as e:
        log(f"❌ ABI encode error: {e}")
        return None

    gas_price = _get_gas_price()
    # Bump 20% to ensure inclusion
    gas_price = int(gas_price * 1.2)
    nonce = _get_nonce(account.address)

    tx = {
        "to":       vault_address,
        "data":     "0x" + call_data.hex(),
        "gas":      500_000,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  BASE_CHAIN_ID,
        "value":    0,
    }

    signed = account.sign_transaction(tx)

    try:
        raw_hex = "0x" + signed.rawTransaction.hex()
        tx_hash = _rpc("eth_sendRawTransaction", [raw_hex])
        log(f"✅ report() tx sent: {tx_hash}")
        _save_report_snapshot(payload, tx_hash)
        return tx_hash
    except Exception as e:
        log(f"❌ report() tx failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DB persistence
# ─────────────────────────────────────────────────────────────────────────────

def _save_report_snapshot(payload: dict, tx_hash: str):
    """Persist report snapshot to arb_vault_reports table."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arb_vault_reports (
                id               SERIAL PRIMARY KEY,
                reported_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                nonce            BIGINT NOT NULL,
                reported_assets  NUMERIC(18,6) NOT NULL,
                vault_idle_usdc  NUMERIC(18,6),
                servicer_on_base NUMERIC(18,6),
                poly_cash        NUMERIC(18,6),
                kalshi_cash      NUMERIC(18,6),
                opinion_cash     NUMERIC(18,6),
                settled_pnl      NUMERIC(18,6),
                signature        TEXT,
                tx_hash          TEXT
            )
        """)
        bd = payload.get("breakdown", {})
        cur.execute("""
            INSERT INTO arb_vault_reports
                (nonce, reported_assets, vault_idle_usdc, servicer_on_base,
                 poly_cash, kalshi_cash, opinion_cash, settled_pnl, signature, tx_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            payload["nonce"],
            payload["reported_assets_usdc"],
            bd.get("vault_idle_usdc"),
            bd.get("servicer_on_base"),
            bd.get("poly_cash"),
            bd.get("kalshi_cash"),
            bd.get("opinion_cash"),
            bd.get("settled_pnl"),
            payload["signature"],
            tx_hash,
        ))
        conn.commit()
        cur.close()
        conn.close()
        log(f"📝 Report snapshot saved (nonce={payload['nonce']})")
    except Exception as e:
        log(f"⚠️ Error saving report snapshot: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Withdrawal funding waterfall
# ─────────────────────────────────────────────────────────────────────────────

def _refill_buffer_tx(
    vault_address: str,
    account,                # eth_account.Account instance
    sweep_amount_usdc: float,
    gas_price: int,
    nonce: int,
) -> str:
    """Send approve + refillBuffer(amount) to move USDC from servicer → vault.

    Returns the refillBuffer tx hash.
    """
    sweep_wei  = int(sweep_amount_usdc * 1e6)
    amount_hex = sweep_wei.to_bytes(32, "big").hex()
    padded_vault = "000000000000000000000000" + vault_address.lower().replace("0x", "")

    # Step A: approve USDC for vault
    approve_data = "0x095ea7b3" + padded_vault + amount_hex
    approve_tx = {
        "to": USDC_BASE, "data": approve_data,
        "gas": 80_000, "gasPrice": gas_price,
        "nonce": nonce, "chainId": BASE_CHAIN_ID, "value": 0,
    }
    _rpc("eth_sendRawTransaction",
         ["0x" + account.sign_transaction(approve_tx).rawTransaction.hex()])

    # Step B: refillBuffer(amount)
    REFILL_SELECTOR = _keccak256_text("refillBuffer(uint256)")[:4].hex()
    refill_data = "0x" + REFILL_SELECTOR + amount_hex
    refill_tx = {
        "to": vault_address, "data": refill_data,
        "gas": 150_000, "gasPrice": gas_price,
        "nonce": nonce + 1, "chainId": BASE_CHAIN_ID, "value": 0,
    }
    tx_hash = _rpc("eth_sendRawTransaction",
                   ["0x" + account.sign_transaction(refill_tx).rawTransaction.hex()])
    return tx_hash


def run_withdrawal_waterfall(vault_address: str, servicer_key: str) -> None:
    """Satisfy pending redemption shortfall via ordered funding layers.

    Waterfall:
      1. Vault idle cash                — already there; counted, no action needed
      2. Free servicer cash on Base     — swept in via refillBuffer()
      3. Settled proceeds               — folded into servicer cash (same wallet); no extra tx
      4. Unwind cheapest/nearest positions — LOGGED and flagged; execution left to trade bot
      5. Broader unwind                 — ALERTED if step 4 still insufficient

    This function only executes steps 1-3 automatically.
    Steps 4-5 produce log warnings that the execution loop uses to block new trades.
    """
    from ..config import ARB_WATERFALL_MIN_SHORTFALL

    if not vault_address or not servicer_key:
        return

    # Read current vault state fresh
    vault_state = _read_vault_state(vault_address)
    if not vault_state["ok"]:
        log("⚠️ Waterfall: cannot read vault state — skip")
        return

    official_pps          = vault_state["official_pps"]
    idle_balance          = vault_state["idle_balance_usdc"]
    pending_redeem_shares = vault_state["pending_redeem_shares"]

    # ── Step 1: Vault idle covers redemptions ─────────────────────────────────
    if pending_redeem_shares == 0:
        log("✅ Waterfall: no pending redemptions")
        return

    pending_redeem_value = (pending_redeem_shares * official_pps) / 1e18 / 1e6
    shortfall = max(0.0, pending_redeem_value - idle_balance)

    log(
        f"📊 Waterfall: pending_redeem={pending_redeem_value:.4f} "
        f"idle={idle_balance:.4f} shortfall={shortfall:.4f}"
    )

    if shortfall < ARB_WATERFALL_MIN_SHORTFALL:
        log(f"✅ Waterfall: shortfall {shortfall:.4f} < min {ARB_WATERFALL_MIN_SHORTFALL:.2f} — idle covers")
        return

    # ── Step 2 + 3: Sweep free servicer cash (includes settled proceeds) ──────
    from eth_account import Account
    account = Account.from_key(servicer_key)
    servicer_balance = _read_usdc_balance(account.address)

    # Keep 10% of servicer cash as gas reserve (not swept)
    available_to_sweep = servicer_balance * 0.9
    sweep_amount = min(shortfall, available_to_sweep)

    if sweep_amount >= ARB_WATERFALL_MIN_SHORTFALL:
        log(
            f"💧 Waterfall Step 2/3: sweeping {sweep_amount:.4f} USDC "
            f"(servicer={servicer_balance:.4f}) → vault (shortfall={shortfall:.4f})"
        )
        try:
            gas_price = int(_get_gas_price() * 1.2)
            nonce     = _get_nonce(account.address)
            tx_hash   = _refill_buffer_tx(vault_address, account, sweep_amount, gas_price, nonce)
            log(f"✅ Waterfall Step 2/3: refillBuffer tx={tx_hash}")
            shortfall -= sweep_amount
        except Exception as e:
            log(f"❌ Waterfall Step 2/3 sweep failed: {e}")
    else:
        log(
            f"⚠️ Waterfall Step 2/3: servicer has only {servicer_balance:.4f} USDC "
            f"(available={available_to_sweep:.4f}) — cannot cover {shortfall:.4f} shortfall"
        )

    # ── Step 3.5: Pull cash from trading platforms if servicer sweep insufficient ─
    if shortfall >= ARB_WATERFALL_MIN_SHORTFALL:
        log(
            f"🔄 Waterfall Step 3.5: shortfall {shortfall:.4f} remains after servicer sweep — "
            f"pulling from trading platforms"
        )
        try:
            from .arb_nav import _get_servicer_balances
            from .arb_withdrawals import withdraw_from_platforms

            poly_cash, kalshi_cash, opinion_cash = _get_servicer_balances()
            total_platform_cash = poly_cash + kalshi_cash + opinion_cash
            log(
                f"💧 Platform cash: poly={poly_cash:.4f} kalshi={kalshi_cash:.4f} "
                f"opinion={opinion_cash:.4f} total={total_platform_cash:.4f} USDC"
            )

            initiated = withdraw_from_platforms(
                shortfall_usdc=shortfall,
                poly_cash=poly_cash,
                kalshi_cash=kalshi_cash,
                opinion_cash=opinion_cash,
                servicer_wallet=account.address,
            )
            log(
                f"✅ Waterfall Step 3.5: initiated {initiated:.4f} USDC of platform withdrawals "
                f"(funds in transit — will arrive in 5–30 min depending on platform)"
            )
            # We don't immediately reduce the shortfall (funds haven't arrived yet),
            # but we log the expectation so Step 4 can be informational rather than critical
            if initiated >= shortfall * 0.9:
                log(f"ℹ️ Step 3.5 expected to cover shortfall — Step 4 unwind deferred")
        except Exception as e:
            log(f"⚠️ Waterfall Step 3.5 platform withdrawals error: {e}")

    # ── Step 4: Unwind cheapest/nearest-expiry positions ─────────────────────
    if shortfall >= ARB_WATERFALL_MIN_SHORTFALL:
        try:
            from .arb_positions_db import get_open_positions
            open_positions = get_open_positions()
            # Sort by (expiry_ts asc, cost_basis asc) to unwind nearest-expiry / cheapest first
            candidates = sorted(
                [p for p in open_positions if p.get("status") == "open"],
                key=lambda p: (p.get("expiry_ts", 0), p.get("cost_basis_usdc", 0))
            )
            if candidates:
                log(
                    f"⚠️ Waterfall Step 4: remaining shortfall={shortfall:.4f} USDC. "
                    f"Recommend unwinding {len(candidates)} open position(s) starting with "
                    f"pair_id={candidates[0].get('pair_id')} "
                    f"(cost={candidates[0].get('cost_basis_usdc', 0):.4f} USDC, "
                    f"expiry={candidates[0].get('expiry_ts', 0)}). "
                    f"Execution loop will throttle new deployments."
                )
            else:
                log(f"⚠️ Waterfall Step 4: shortfall={shortfall:.4f} but no open positions to unwind")
        except Exception as e:
            log(f"⚠️ Waterfall Step 4 lookup failed: {e}")

    # ── Step 5: Broader unwind alert ─────────────────────────────────────────
    if shortfall > pending_redeem_value * 0.5:
        log(
            f"🚨 Waterfall Step 5: CRITICAL — shortfall={shortfall:.4f} USDC covers "
            f">{shortfall/pending_redeem_value:.0%} of pending redeems. "
            f"Manual intervention may be required if position unwind is insufficient."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Early report trigger
# ─────────────────────────────────────────────────────────────────────────────

def should_report_early(vault_state: dict) -> bool:
    """Return True if redemption pressure warrants an early report.

    Conditions (all must hold):
      1. pending_redeem_value / idle_available >= ARB_EARLY_REPORT_PRESSURE_RATIO
      2. At least ARB_EARLY_REPORT_MIN_ELAPSED seconds since last report
      3. There are actually pending redemptions (> 0 shares)
    """
    from ..config import ARB_EARLY_REPORT_PRESSURE_RATIO, ARB_EARLY_REPORT_MIN_ELAPSED

    pending_redeem_shares = vault_state.get("pending_redeem_shares", 0)
    if pending_redeem_shares == 0:
        return False

    official_pps = vault_state.get("official_pps", 0)
    idle_balance = vault_state.get("idle_balance_usdc", 0.0)
    last_ts      = vault_state.get("last_report_timestamp", 0)
    now          = int(time.time())

    pending_redeem_value = (pending_redeem_shares * official_pps) / 1e18 / 1e6

    # Must have enough time elapsed since last report
    elapsed = now - last_ts if last_ts > 0 else ARB_EARLY_REPORT_MIN_ELAPSED + 1
    if elapsed < ARB_EARLY_REPORT_MIN_ELAPSED:
        log(
            f"⏳ Early report check: {elapsed}s elapsed < min {ARB_EARLY_REPORT_MIN_ELAPSED}s — skip"
        )
        return False

    # Pressure ratio check
    if idle_balance <= 0:
        # Any pending redeems with zero idle is full pressure
        log(f"🚨 Early report: idle=0, pending_redeem={pending_redeem_value:.4f} — TRIGGER")
        return True

    pressure_ratio = pending_redeem_value / idle_balance
    if pressure_ratio >= ARB_EARLY_REPORT_PRESSURE_RATIO:
        log(
            f"🚨 Early report triggered: pressure_ratio={pressure_ratio:.2f} "
            f">= threshold={ARB_EARLY_REPORT_PRESSURE_RATIO:.2f} "
            f"(pending={pending_redeem_value:.4f} idle={idle_balance:.4f} elapsed={elapsed}s)"
        )
        return True

    log(
        f"ℹ️ Early report check: pressure_ratio={pressure_ratio:.2f} "
        f"< {ARB_EARLY_REPORT_PRESSURE_RATIO:.2f} — no early trigger"
    )
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Auto-claim: push shares/USDC to users after report() confirms
# ─────────────────────────────────────────────────────────────────────────────

# Safe upper-bound per auto-claim batch: 100k base + 25 * 80k ≈ 2.1 M gas.
# This stays well under the ~30 M Base block gas limit and leaves room for
# other activity.  Larger backlogs are processed over multiple sequential txs.
MAX_CLAIMS_PER_TX = 25


def _wait_for_tx_confirm(tx_hash: str, timeout: int = 90, poll_interval: int = 6) -> bool:
    """Poll eth_getTransactionReceipt until mined or timeout.
    Returns True if tx was mined with status=1 (success)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                status = int(receipt.get("status", "0x0"), 16)
                confirmed = status == 1
                log(f"📦 Tx {tx_hash[:18]}… mined — status={'✅ success' if confirmed else '❌ reverted'}")
                return confirmed
        except Exception as e:
            log(f"⚠️ Receipt poll error: {e}")
        time.sleep(poll_interval)
    log(f"⏰ Tx {tx_hash[:18]}… not mined within {timeout}s timeout")
    return False


def _get_request_count(vault_address: str, is_deposit: bool) -> int:
    """Return total number of deposit or redeem requests (all statuses combined)."""
    fn_sig = "depositRequestCount()" if is_deposit else "redeemRequestCount()"
    selector = _keccak256_text(fn_sig)[:4]
    raw = _rpc("eth_call", [{"to": vault_address, "data": "0x" + selector.hex()}, "latest"])
    return int(raw, 16)


def _get_deposit_request(vault_address: str, idx: int) -> dict:
    """Read one deposit request struct from the public array getter."""
    from eth_abi import encode as abi_encode, decode as abi_decode
    selector = _keccak256_text("depositRequests(uint256)")[:4]
    encoded  = abi_encode(["uint256"], [idx])
    raw = _rpc("eth_call", [
        {"to": vault_address, "data": "0x" + (selector + encoded).hex()},
        "latest",
    ])
    result_bytes = bytes.fromhex(raw[2:])
    owner, receiver, assets, submitted_at, status, processed_pps = abi_decode(
        ["address", "address", "uint256", "uint256", "uint8", "uint256"],
        result_bytes,
    )
    return {
        "owner": owner, "receiver": receiver, "assets": assets,
        "submitted_at": submitted_at, "status": status, "processed_pps": processed_pps,
    }


def _get_redeem_request(vault_address: str, idx: int) -> dict:
    """Read one redeem request struct from the public array getter."""
    from eth_abi import encode as abi_encode, decode as abi_decode
    selector = _keccak256_text("redeemRequests(uint256)")[:4]
    encoded  = abi_encode(["uint256"], [idx])
    raw = _rpc("eth_call", [
        {"to": vault_address, "data": "0x" + (selector + encoded).hex()},
        "latest",
    ])
    result_bytes = bytes.fromhex(raw[2:])
    owner, receiver, shares, submitted_at, status, claimable_assets = abi_decode(
        ["address", "address", "uint256", "uint256", "uint8", "uint256"],
        result_bytes,
    )
    return {
        "owner": owner, "receiver": receiver, "shares": shares,
        "submitted_at": submitted_at, "status": status, "claimable_assets": claimable_assets,
    }


def _broadcast_claim_tx(
    vault_address: str,
    signer_key: str,
    is_deposit: bool,
    batch_ids: list,
    nonce: int,
    gas_price: int,
) -> str | None:
    """Encode and broadcast one auto-claim batch. Returns tx hash or None on error.

    Caller must supply `nonce` and `gas_price` to enable sequential batching
    without extra RPC round-trips.  Gas budget: 100k base + 80k per request.
    """
    from eth_abi import encode as abi_encode
    from eth_account import Account

    fn_sig   = "autoClaimDeposits(uint256[])" if is_deposit else "autoClaimRedeems(uint256[])"
    label    = "autoClaimDeposits" if is_deposit else "autoClaimRedeems"
    selector = _keccak256_text(fn_sig)[:4]
    call_data = "0x" + (selector + abi_encode(["uint256[]"], [batch_ids])).hex()

    gas_limit = 100_000 + len(batch_ids) * 80_000
    account   = Account.from_key(signer_key)

    tx = {
        "to":       vault_address,
        "data":     call_data,
        "gas":      gas_limit,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  BASE_CHAIN_ID,
        "value":    0,
    }
    log(f"🔧 [{label}] batch ids={batch_ids} gas={gas_limit:,} nonce={nonce}")
    signed  = account.sign_transaction(tx)
    raw_hex = "0x" + signed.rawTransaction.hex()
    try:
        tx_hash = _rpc("eth_sendRawTransaction", [raw_hex])
        log(f"📤 [{label}] tx broadcast: {tx_hash}")
        return tx_hash
    except Exception as e:
        log(f"❌ [{label}] broadcast failed: {e}")
        return None


def _send_claim_batch_with_retry(
    vault_address: str,
    signer_key: str,
    is_deposit: bool,
    batch_ids: list,
    batch_details: list,
    nonce: int,
    gas_price: int,
) -> int:
    """Send one chunk, wait for confirmation, retry with half-size on failure.

    On success: emits confirmed per-claim log lines (receiver + amount).
    On final failure after halving once: logs each failed ID and moves on.
    Returns the next nonce to use (nonce + 1 on success, unchanged on failure).
    """
    label = "Deposit" if is_deposit else "Redeem"

    def attempt(ids: list, n: int) -> tuple:
        """Returns (success: bool, nonce_consumed: bool).

        nonce_consumed is True whenever the tx was broadcast (even if it reverted on-chain),
        because on EVM a reverted tx still uses up the sender's nonce.
        """
        tx_hash = _broadcast_claim_tx(vault_address, signer_key, is_deposit, ids, n, gas_price)
        if tx_hash is None:
            return False, False   # broadcast rejected entirely — nonce not consumed
        confirmed = _wait_for_tx_confirm(tx_hash, timeout=90)
        if confirmed:
            ids_in_batch = set(ids)
            for rid, detail in zip(ids, [d for d in batch_details if d["id"] in ids_in_batch]):
                if is_deposit:
                    pps          = detail.get("processed_pps", 1) or 1
                    shares_est   = detail["assets"] * (10**18) // pps if pps else 0
                    shares_human = shares_est / 1e18
                    log(
                        f"  ✅ Auto-claimed deposit #{rid} → {detail['receiver']}"
                        f" ({detail['assets']/1e6:.4f} USDC → ~{shares_human:.4f} pARB)"
                        f" tx={tx_hash[:18]}…"
                    )
                else:
                    log(
                        f"  ✅ Auto-claimed redeem  #{rid} → {detail['receiver']}"
                        f" (~${detail['claimable_assets']/1e6:.4f} USDC)"
                        f" tx={tx_hash[:18]}…"
                    )
        return confirmed, True    # tx was mined (success or revert) — nonce IS consumed

    # ── Primary attempt with full batch ──────────────────────────────────────
    success, consumed = attempt(batch_ids, nonce)
    if success:
        return nonce + 1

    # ── One retry: split in half, accounting for nonce consumption ────────────
    log(f"⚠️ [{label}] batch failed — retrying in two halves")
    mid        = max(1, len(batch_ids) // 2)
    left       = batch_ids[:mid]
    right      = batch_ids[mid:]
    next_nonce = nonce + (1 if consumed else 0)

    if left:
        ok, cons = attempt(left, next_nonce)
        if ok:
            next_nonce += 1
        else:
            next_nonce += 1 if cons else 0
            log(f"❌ [{label}] left-half retry failed — ids={left} not claimed")

    if right:
        ok, cons = attempt(right, next_nonce)
        if ok:
            next_nonce += 1
        else:
            next_nonce += 1 if cons else 0
            log(f"❌ [{label}] right-half retry failed — ids={right} not claimed")

    return next_nonce


def auto_claim_after_report(vault_address: str, signer_key: str, report_tx_hash: str) -> None:
    """After report() is confirmed on-chain, sweep all CLAIMABLE requests.

    Flow:
      1. Wait for report() tx receipt (up to 90s)
      2. Read all deposit + redeem request counts
      3. Scan each for CLAIMABLE (status=1) — collect id + detail
      4. Process in batches of MAX_CLAIMS_PER_TX (25) to avoid gas limit issues
      5. Each batch: broadcast → wait for receipt → log confirmed per-claim outcomes
      6. Failed batch: split in half + retry once (each half); log any remaining failures
      7. Nonces are managed sequentially to allow pipelined batches

    The contract's autoClaimDeposits / autoClaimRedeems always deliver to the
    receiver address locked in at request submission time — no redirect possible.
    All errors are swallowed; this never crashes the reporter loop.
    """
    log("⏳ [AutoClaim] Waiting for report tx to confirm before auto-claiming…")
    if not _wait_for_tx_confirm(report_tx_hash, timeout=90):
        log("⚠️ [AutoClaim] Report tx unconfirmed after 90s — skipping auto-claim sweep")
        return

    log("🔄 [AutoClaim] Report confirmed — scanning all requests…")

    from eth_account import Account
    account   = Account.from_key(signer_key)
    nonce     = _get_nonce(account.address)
    gas_price = int(_get_gas_price() * 1.2)

    # ── Deposit requests ──────────────────────────────────────────────────────
    try:
        dep_count = _get_request_count(vault_address, is_deposit=True)
        log(f"📋 [AutoClaim] Scanning {dep_count} deposit request(s)…")
        dep_claimable = []      # list of {"id": int, "receiver": str, "assets": int, "processed_pps": int}
        for i in range(dep_count):
            try:
                req = _get_deposit_request(vault_address, i)
                if req["status"] == 1:  # CLAIMABLE
                    dep_claimable.append({
                        "id":            i,
                        "receiver":      req["receiver"],
                        "assets":        req["assets"],
                        "processed_pps": req["processed_pps"],
                    })
                    log(
                        f"  🔍 Deposit #{i}: ${req['assets']/1e6:.4f} USDC claimable"
                        f" → {req['receiver']}"
                    )
            except Exception as e:
                log(f"  ⚠️ Could not read deposit #{i}: {e}")

        if dep_claimable:
            log(f"🏦 [AutoClaim] {len(dep_claimable)} claimable deposit(s) — processing in batches of {MAX_CLAIMS_PER_TX}")
            ids = [d["id"] for d in dep_claimable]
            for chunk_start in range(0, len(ids), MAX_CLAIMS_PER_TX):
                chunk_ids     = ids[chunk_start : chunk_start + MAX_CLAIMS_PER_TX]
                chunk_details = dep_claimable[chunk_start : chunk_start + MAX_CLAIMS_PER_TX]
                log(f"  📦 Deposit batch [{chunk_start}–{chunk_start+len(chunk_ids)-1}] ids={chunk_ids}")
                nonce = _send_claim_batch_with_retry(
                    vault_address, signer_key, True, chunk_ids, chunk_details, nonce, gas_price
                )
        else:
            log("ℹ️ [AutoClaim] No claimable deposit requests")
    except Exception as e:
        log(f"⚠️ [AutoClaim] Deposit sweep error (non-fatal): {e}")

    # ── Redeem requests ───────────────────────────────────────────────────────
    try:
        rdm_count = _get_request_count(vault_address, is_deposit=False)
        log(f"📋 [AutoClaim] Scanning {rdm_count} redeem request(s)…")
        rdm_claimable = []      # list of {"id": int, "receiver": str, "claimable_assets": int}
        for i in range(rdm_count):
            try:
                req = _get_redeem_request(vault_address, i)
                if req["status"] == 1:  # CLAIMABLE
                    rdm_claimable.append({
                        "id":               i,
                        "receiver":         req["receiver"],
                        "claimable_assets": req["claimable_assets"],
                    })
                    log(
                        f"  🔍 Redeem  #{i}: ${req['claimable_assets']/1e6:.4f} USDC claimable"
                        f" → {req['receiver']}"
                    )
            except Exception as e:
                log(f"  ⚠️ Could not read redeem #{i}: {e}")

        if rdm_claimable:
            log(f"💸 [AutoClaim] {len(rdm_claimable)} claimable redeem(s) — processing in batches of {MAX_CLAIMS_PER_TX}")
            ids = [r["id"] for r in rdm_claimable]
            for chunk_start in range(0, len(ids), MAX_CLAIMS_PER_TX):
                chunk_ids     = ids[chunk_start : chunk_start + MAX_CLAIMS_PER_TX]
                chunk_details = rdm_claimable[chunk_start : chunk_start + MAX_CLAIMS_PER_TX]
                log(f"  📦 Redeem batch [{chunk_start}–{chunk_start+len(chunk_ids)-1}] ids={chunk_ids}")
                nonce = _send_claim_batch_with_retry(
                    vault_address, signer_key, False, chunk_ids, chunk_details, nonce, gas_price
                )
        else:
            log("ℹ️ [AutoClaim] No claimable redeem requests")
    except Exception as e:
        log(f"⚠️ [AutoClaim] Redeem sweep error (non-fatal): {e}")


def run_reporter_tick():
    """Called once per execution cycle.

    Order of operations:
      1. Run withdrawal funding waterfall (sweep servicer cash → vault if shortfall exists)
      2. Check for early report trigger (redemption pressure bypasses cooldown)
      3. Build and submit report() if cooldown elapsed OR early trigger fired
      4. Auto-claim all CLAIMABLE requests once report() confirms on-chain
         (calls autoClaimDeposits / autoClaimRedeems on the vault contract)
      5. Silently no-ops if ARB_VAULT_V2_ADDRESS not set

    All errors are caught so a reporter failure never crashes the arb loop.
    """
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    signer_key    = os.environ.get("ARB_NAV_SIGNER_PRIVATE_KEY", "")
    servicer_key  = os.environ.get("POLY_PRIVATE_KEY", "")

    if not vault_address:
        return  # V2 not deployed yet — silent skip

    # ── 1. Withdrawal funding waterfall ───────────────────────────────────────
    try:
        run_withdrawal_waterfall(vault_address, servicer_key)
    except Exception as e:
        log(f"⚠️ Waterfall error (non-fatal): {e}")

    # ── 2 + 3. Report: cooldown OR early trigger ───────────────────────────────
    try:
        vault_state = _read_vault_state(vault_address)
        if not vault_state["ok"]:
            log("⚠️ Cannot read vault state for report check — skip")
            return

        last_ts   = vault_state["last_report_timestamp"]
        cooldown  = vault_state["report_cooldown"]
        now       = int(time.time())
        elapsed   = now - last_ts if last_ts > 0 else cooldown + 1

        cooldown_elapsed = elapsed >= cooldown
        early_trigger    = (not cooldown_elapsed) and should_report_early(vault_state)

        if not cooldown_elapsed and not early_trigger:
            remaining = cooldown - elapsed
            log(f"⏳ Report: cooldown {remaining}s remaining, no early trigger — skip")
            return

        reason = "cooldown elapsed" if cooldown_elapsed else "early trigger (redemption pressure)"
        log(f"📋 Report firing — reason: {reason}")

        # force=True skips internal cooldown re-check when early trigger decided above
        payload = build_report_payload(vault_address, signer_key, force=early_trigger)
        if payload:
            report_tx = submit_report(payload, signer_key)
            if report_tx:
                # ── 4. Auto-claim all CLAIMABLE requests once report confirms ──────
                try:
                    auto_claim_after_report(vault_address, signer_key, report_tx)
                except Exception as claim_err:
                    log(f"⚠️ Auto-claim error (non-fatal): {claim_err}")
    except Exception as e:
        log(f"⚠️ Report error (non-fatal): {e}")
