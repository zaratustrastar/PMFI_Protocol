"""arb_reporter.py — V2 report signer and submitter for PMFIArbVaultV2.

Responsibilities:
  1. Compute conservative reportedAssets (cash + settled proceeds; NO open position marks)
  2. Sign a ReportDataV2 payload for the contract's report() function
  3. Submit the report() transaction to Base mainnet when cooldown allows
  4. Optionally sweep servicer cash back to vault when redemption demand exists

reportedAssets formula (conservative — only what is clearly owned and withdrawable):
    vault_idle_usdc + servicer_on_base + poly_cash + kalshi_cash + opinion_cash + settled_pnl

Intentionally excluded:
    open_positions_liquid_value (unrealised, mark-to-market — monitoring only)

Domain salt: "PMFIArbVaultV2.v1"  (isolated from V1 sigs and pSNIPER sigs)

Env vars required:
    ARB_VAULT_V2_ADDRESS       — deployed PMFIArbVaultV2 address
    ARB_NAV_SIGNER_PRIVATE_KEY — private key of the reportSigner address
    BASE_RPC_URL               — (optional) Base RPC, defaults to mainnet.base.org
"""

import os
import time
import struct as _struct

DOMAIN_SALT_TEXT = "PMFIArbVaultV2.v1"
BASE_CHAIN_ID = 8453
BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# report() signature: report((uint256,uint256,uint256,uint256),bytes,uint256,uint256)
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

SEL_LAST_REPORT_NONCE      = "6e8d8fb5"
SEL_LAST_REPORT_TIMESTAMP  = "e44f62e3"
SEL_REPORT_COOLDOWN        = "1b6a4e72"
SEL_OFFICIAL_PPS           = "90f89f65"
SEL_IDLE_BALANCE           = "1be05289"
SEL_PENDING_REDEEM_SHARES  = "3a1c4fe3"


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

def build_report_payload(vault_address: str, signer_key: str) -> dict | None:
    """Build and sign a ReportDataV2 payload for the contract's report() function.

    Returns the payload dict or None if cooldown hasn't elapsed or key/vault missing.
    """
    if not vault_address:
        log("⚠️ ARB_VAULT_V2_ADDRESS not set — cannot build report")
        return None
    if not signer_key:
        log("⚠️ ARB_NAV_SIGNER_PRIVATE_KEY not set — cannot sign report")
        return None

    # Check cooldown
    vault_state = _read_vault_state(vault_address)
    if not vault_state["ok"]:
        log("⚠️ Could not read vault state — skipping report")
        return None

    last_ts  = vault_state["last_report_timestamp"]
    cooldown = vault_state["report_cooldown"]
    now      = int(time.time())

    if last_ts > 0 and (now - last_ts) < cooldown:
        remaining = cooldown - (now - last_ts)
        log(f"⏳ Report cooldown: {remaining}s remaining — skip")
        return None

    log(f"✅ Report cooldown elapsed (last={last_ts}, cooldown={cooldown}s) — proceeding")

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

    Signature: report((uint256,uint256,uint256,uint256),bytes,uint256,uint256)
    Selector:  first 4 bytes of keccak256 of the signature string
    """
    from eth_abi import encode as abi_encode

    selector = _keccak256_text(REPORT_SELECTOR)[:4]

    sig_bytes = bytes.fromhex(payload["signature"].replace("0x", ""))
    reported_assets_wei = int(payload["reported_assets_usdc"] * 1e6)

    # Tuple: (reportedAssets, timestamp, deadline, nonce)
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
# Vault buffer sweeper
# ─────────────────────────────────────────────────────────────────────────────

def sweep_servicer_to_vault(vault_address: str, servicer_key: str, min_sweep_usdc: float = 10.0) -> str | None:
    """Call refillBuffer() on the vault to return servicer cash when redemptions are pending.

    The vault's tend() moves capital OUT to the servicer.
    This function moves capital BACK IN when needed to satisfy pending redemptions.
    Anyone can call refillBuffer() — it only adds USDC to the vault.

    Returns tx hash or None.
    """
    from eth_account import Account

    if not vault_address or not servicer_key:
        return None

    vault_state = _read_vault_state(vault_address)
    if not vault_state["ok"]:
        return None

    pending_redeem_shares = vault_state["pending_redeem_shares"]
    official_pps = vault_state["official_pps"]
    idle_balance  = vault_state["idle_balance_usdc"]

    if pending_redeem_shares == 0:
        log("No pending redemptions — sweep not needed")
        return None

    # Estimate USDC needed for pending redemptions
    needed_usdc = (pending_redeem_shares * official_pps) / 1e18 / 1e6
    shortfall    = max(0.0, needed_usdc - idle_balance)

    if shortfall < min_sweep_usdc:
        log(f"Redemption shortfall {shortfall:.4f} USDC < min_sweep={min_sweep_usdc:.2f} — skip")
        return None

    # Read servicer USDC on Base
    account = Account.from_key(servicer_key)
    servicer_balance = _read_usdc_balance(account.address)
    sweep_amount     = min(shortfall, servicer_balance * 0.9)  # keep 10% for gas reserve

    if sweep_amount < min_sweep_usdc:
        log(f"Servicer balance {servicer_balance:.4f} too low to sweep {sweep_amount:.4f} — skip")
        return None

    log(
        f"💧 Sweeping {sweep_amount:.4f} USDC → vault to cover "
        f"{needed_usdc:.4f} USDC in pending redemptions (idle={idle_balance:.4f})"
    )

    sweep_wei = int(sweep_amount * 1e6)

    # 1. Approve USDC for vault
    padded_vault = "000000000000000000000000" + vault_address.lower().replace("0x", "")
    amount_hex   = sweep_wei.to_bytes(32, "big").hex()
    approve_data = "0x095ea7b3" + padded_vault + amount_hex

    gas_price = _get_gas_price()
    gas_price = int(gas_price * 1.2)
    nonce     = _get_nonce(account.address)

    approve_tx = {
        "to":       USDC_BASE,
        "data":     approve_data,
        "gas":      80_000,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  BASE_CHAIN_ID,
        "value":    0,
    }
    signed_approve = account.sign_transaction(approve_tx)
    _rpc("eth_sendRawTransaction", ["0x" + signed_approve.rawTransaction.hex()])

    nonce += 1

    # 2. Call refillBuffer(uint256 amount)
    #    selector: keccak256("refillBuffer(uint256)")[:4]
    REFILL_SELECTOR = _keccak256_text("refillBuffer(uint256)")[:4].hex()
    refill_data = "0x" + REFILL_SELECTOR + amount_hex

    refill_tx = {
        "to":       vault_address,
        "data":     refill_data,
        "gas":      150_000,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  BASE_CHAIN_ID,
        "value":    0,
    }
    signed_refill = account.sign_transaction(refill_tx)
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed_refill.rawTransaction.hex()])
    log(f"✅ refillBuffer({sweep_amount:.4f} USDC) → vault: {tx_hash}")
    return tx_hash


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point (called by arb_execution_loop.py)
# ─────────────────────────────────────────────────────────────────────────────

def run_reporter_tick():
    """Called once per execution cycle. Tries to report and sweep if needed.

    Silently no-ops if cooldown hasn't elapsed or env vars missing.
    Errors are caught so a reporter failure never crashes the arb loop.
    """
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    signer_key    = os.environ.get("ARB_NAV_SIGNER_PRIVATE_KEY", "")
    servicer_key  = os.environ.get("POLY_PRIVATE_KEY", "")

    if not vault_address:
        return  # V2 not deployed yet — silent skip

    try:
        # 1. Sweep servicer cash back to vault if redemptions are pending
        sweep_servicer_to_vault(vault_address, servicer_key)
    except Exception as e:
        log(f"⚠️ Sweep error (non-fatal): {e}")

    try:
        # 2. Build and submit report if cooldown has elapsed
        payload = build_report_payload(vault_address, signer_key)
        if payload:
            submit_report(payload, signer_key)
    except Exception as e:
        log(f"⚠️ Report error (non-fatal): {e}")
