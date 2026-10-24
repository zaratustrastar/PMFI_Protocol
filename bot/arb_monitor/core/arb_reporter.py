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

reportedAssets formula:
    vault_idle_usdc + servicer_on_base + poly_cash + kalshi_cash + opinion_cash
    + open_position_value  (open arb positions @ $0.99/share, DB read only)

Intentionally excluded:
    settled_pnl — claimed proceeds already land in platform cash; adding separately
                  would double-count the profit portion of settled positions.
    haircut     — positions already priced conservatively at $0.99 (below $1 floor);
                  cash balances are exact on-chain / API reads.

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

from .arb_withdrawals import withdraw_from_platforms, get_pending_withdrawals_total_usdc
from ..config import is_robinhood_funding, usdg_address, robinhood_chain_id

# Defaults = the live Base V2 vault. A MILORD V3 vault on Robinhood Chain sets:
#   ARB_VAULT_DOMAIN_SALT=PMFIArbVaultV3.milord.v1  ARB_VAULT_CHAIN_ID=4663
#   ARB_VAULT_RPC_URL=<Robinhood RPC>  ARB_VAULT_ASSET=<USDG address, 6 decimals>
# (names below are kept so the rest of this module is unchanged)
DOMAIN_SALT_TEXT = os.environ.get("ARB_VAULT_DOMAIN_SALT", "PMFIArbVaultV2.v1")
BASE_CHAIN_ID = int(os.environ.get("ARB_VAULT_CHAIN_ID", "8453"))
BASE_RPC = os.environ.get("ARB_VAULT_RPC_URL", os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"))
USDC_BASE = os.environ.get("ARB_VAULT_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")


class VaultConfigError(RuntimeError):
    """Robinhood mode with an incomplete/inconsistent vault configuration. Raised instead of
    silently falling back to the Base V2 defaults (which would sign for the wrong chain/asset)."""


def load_vault_config() -> dict:
    """(Re)read the vault chain settings from the environment into the module globals.

    Base mode: legacy defaults, unchanged. Robinhood mode (ARB_FUNDING_CHAIN=robinhood):
    ARB_VAULT_ASSET, ARB_VAULT_RPC_URL, ARB_VAULT_CHAIN_ID and ARB_VAULT_DOMAIN_SALT must all
    be set; the chain id must not be Base's and must equal ROBINHOOD_CHAIN_ID; the vault asset
    must equal ARB_USDG_ADDRESS (the token the funder moves). Any violation raises.
    """
    global DOMAIN_SALT_TEXT, BASE_CHAIN_ID, BASE_RPC, USDC_BASE
    env = os.environ
    if is_robinhood_funding():
        need = ("ARB_VAULT_ASSET", "ARB_VAULT_RPC_URL", "ARB_VAULT_CHAIN_ID", "ARB_VAULT_DOMAIN_SALT")
        missing = [k for k in need if not env.get(k, "").strip()]
        if missing:
            raise VaultConfigError(
                f"ARB_FUNDING_CHAIN=robinhood but {', '.join(missing)} unset — refusing to fall back to Base V2"
            )
        try:
            chain = int(env["ARB_VAULT_CHAIN_ID"])
        except ValueError as e:
            raise VaultConfigError(f"ARB_VAULT_CHAIN_ID not an integer: {e}") from e
        if chain == 8453:
            raise VaultConfigError("robinhood mode with ARB_VAULT_CHAIN_ID=8453 (Base)")
        if chain != robinhood_chain_id():
            raise VaultConfigError(
                f"ARB_VAULT_CHAIN_ID={chain} != ROBINHOOD_CHAIN_ID={robinhood_chain_id()} (funder chain)"
            )
        asset = env["ARB_VAULT_ASSET"].strip()
        if asset.lower() != usdg_address().lower():
            raise VaultConfigError(f"ARB_VAULT_ASSET={asset} != ARB_USDG_ADDRESS={usdg_address()}")
        DOMAIN_SALT_TEXT = env["ARB_VAULT_DOMAIN_SALT"].strip()
        BASE_CHAIN_ID = chain
        BASE_RPC = env["ARB_VAULT_RPC_URL"].strip()
        USDC_BASE = asset
    else:
        DOMAIN_SALT_TEXT = env.get("ARB_VAULT_DOMAIN_SALT", "PMFIArbVaultV2.v1")
        BASE_CHAIN_ID = int(env.get("ARB_VAULT_CHAIN_ID", "8453"))
        BASE_RPC = env.get("ARB_VAULT_RPC_URL", env.get("BASE_RPC_URL", "https://mainnet.base.org"))
        USDC_BASE = env.get("ARB_VAULT_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    return {"salt": DOMAIN_SALT_TEXT, "chain_id": BASE_CHAIN_ID, "rpc": BASE_RPC, "asset": USDC_BASE}


# Robinhood mode validates lazily: run_reporter_tick() raises VaultConfigError, and
# _read_vault_state() / build_report_payload() fail closed, on an incomplete configuration.
# (Not at import time: arb_funder / tests import this module before the vault env is set.)

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

# Local cooldown guard — prevents hammering report() when on-chain reads return 0.
# Set only after a report() tx is MINED with status 1 (M3).
_last_local_report_ts: float = 0.0
_LOCAL_REPORT_COOLDOWN = int(os.environ.get("ARB_LOCAL_REPORT_COOLDOWN", str(6 * 3600)))
# A broadcast report whose receipt did not arrive in time: retry no sooner than this.
_pending_report_until: float = 0.0


def _report_gas_limit() -> int:
    return int(os.environ.get("ARB_REPORT_GAS_LIMIT", "500000"))


def _report_receipt_timeout() -> int:
    return int(os.environ.get("ARB_REPORT_RECEIPT_TIMEOUT_SECS", "120"))


def _report_pending_guard_secs() -> int:
    return int(os.environ.get("ARB_REPORT_PENDING_GUARD_SECS", "900"))


# Why the last build_report_payload() returned None (for logs, monitoring and tests).
LAST_ABORT_REASON: str = ""


def _abort(reason: str):
    global LAST_ABORT_REASON
    LAST_ABORT_REASON = reason
    log(f"🛑 Report aborted: {reason}")
    return None


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
    reported_assets_raw: int | None = None,
) -> str:
    """Return hex signature for ReportDataV2.

    reported_assets_raw (int, 6-decimal raw units) is what gets signed when given. The float
    path is kept for callers/fixtures; it converts exactly (H4: int(x*1e6) truncated 249 raw
    to 248).
    """
    from eth_account import Account
    from .arb_nav import usdc_to_raw

    reported_assets_wei = (int(reported_assets_raw) if reported_assets_raw is not None
                           else usdc_to_raw(reported_assets_usdc))
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
    pk = Account.from_key(private_key)
    sig = pk.unsafe_sign_hash(digest)
    return "0x" + sig.signature.hex()


# ─────────────────────────────────────────────────────────────────────────────
# Vault state reader (minimal RPC — no web3 dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _rpc(method: str, params: list) -> object:
    from .shadow_guard import assert_rpc_allowed
    assert_rpc_allowed(method)
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


# 4-byte selectors for the actual deployed PMFIArbVaultV2 contract

SEL_LAST_REPORT_NONCE            = _keccak256_text("lastReportNonce()")[:4].hex()
SEL_LAST_REPORT_TIMESTAMP        = _keccak256_text("lastReportTimestamp()")[:4].hex()
SEL_REPORT_COOLDOWN             = _keccak256_text("reportCooldown()")[:4].hex()
SEL_OFFICIAL_PPS                = _keccak256_text("officialPPS()")[:4].hex()
SEL_LAST_REPORTED_BACKING       = _keccak256_text("lastReportedBackingAssets()")[:4].hex()
SEL_TOTAL_PENDING_REDEEM_SHARES = _keccak256_text("totalPendingRedeemShares()")[:4].hex()
SEL_PAUSED                      = _keccak256_text("paused()")[:4].hex()
SEL_SHUTDOWN                    = _keccak256_text("shutdown()")[:4].hex()
SEL_TOTAL_SUPPLY                = _keccak256_text("totalSupply()")[:4].hex()
SEL_USDC_BALANCE_VAULT          = "70a08231"  # balanceOf(address) on USDC, used with address(this)

LOCAL_REPORT_COOLDOWN = int(os.environ.get("ARB_REPORT_COOLDOWN_SECS", "1800"))


def _call_view_bool(to: str, selector_hex: str) -> bool:
    raw = _call_view(to, selector_hex)
    return bool(int(raw, 16)) if raw and raw != "0x" else False


def _call_view_tuple_uints_and_bools(to: str, selector_hex: str) -> tuple:
    from eth_abi import decode as abi_decode
    raw = _call_view(to, selector_hex)
    if not raw or raw == "0x":
        raise RuntimeError(f"empty tuple response for selector {selector_hex}")
    data = bytes.fromhex(raw[2:])
    return abi_decode(
        [
            "uint256", "uint256", "uint256", "uint256", "uint256",
            "uint256", "uint256", "uint256", "bool", "bool"
        ],
        data,
    )


def _eth_call(to: str, data_hex: str, block: str = "latest") -> str:
    """eth_call at an explicit block tag (H6: one pinned block per report computation)."""
    if not data_hex.startswith("0x"):
        data_hex = "0x" + data_hex
    return _rpc("eth_call", [{"to": to, "data": data_hex}, block])


def _decode_word(raw, what: str) -> int:
    """First 32-byte word of an eth_call result. Empty / short results raise (fail closed:
    a missing getter must never read as 0)."""
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) <= 2:
        raise RuntimeError(f"{what}: unusable eth_call result {raw!r}")
    return int(raw[2:66], 16)


def _pin_block() -> str:
    """Robinhood mode: verify the vault RPC is on ARB_VAULT_CHAIN_ID and return the current
    block number (hex tag) used for every chain-4663 read of this report."""
    chain = int(_rpc("eth_chainId", []), 16)
    if chain != BASE_CHAIN_ID:
        raise RuntimeError(f"vault RPC is on chain {chain}, expected {BASE_CHAIN_ID}")
    bn = _rpc("eth_blockNumber", [])
    if not isinstance(bn, str) or not bn.startswith("0x") or int(bn, 16) <= 0:
        raise RuntimeError(f"bad eth_blockNumber {bn!r}")
    return bn


def _read_token_balance_raw(wallet: str, block: str = "latest", token: str | None = None) -> int:
    """balanceOf(wallet) of the vault asset (USDC on Base / USDG on 4663), raw units."""
    padded = "000000000000000000000000" + wallet.lower().replace("0x", "")
    return _decode_word(_eth_call(token or USDC_BASE, "0x70a08231" + padded, block), f"balanceOf({wallet})")


# PMFIArbVaultV3 has no getVaultState(): read its public getters one by one (same block).
_V3_UINT_GETTERS = {
    "official_pps":                "officialPPS()",
    "last_report_nonce":           "lastReportNonce()",
    "last_report_timestamp":       "lastReportTimestamp()",
    "report_cooldown":             "reportCooldown()",
    "pending_redeem_shares":       "totalPendingRedeemShares()",
    "pending_deposit_assets_raw":  "totalPendingDepositAssets()",
    "claimable_redeem_assets_raw": "totalClaimableRedeemAssets()",
    "idle_balance_raw":            "idleBalance()",
    "max_pps_move_bps":            "maxPpsMoveBps()",
    "last_reported_backing_raw":   "lastReportedBackingAssets()",
    "total_supply":                "totalSupply()",
}
_V3_BOOL_GETTERS = {"paused": "paused()", "shutdown": "shutdown()"}
_V3_ADDR_GETTERS = {"asset": "usdc()", "report_signer": "reportSigner()"}


def _read_vault_state_v3(vault_address: str, block: str) -> dict:
    out = {}
    for key, sig in _V3_UINT_GETTERS.items():
        out[key] = _decode_word(_eth_call(vault_address, _keccak256_text(sig)[:4].hex(), block), sig)
    for key, sig in _V3_BOOL_GETTERS.items():
        out[key] = bool(_decode_word(_eth_call(vault_address, _keccak256_text(sig)[:4].hex(), block), sig))
    for key, sig in _V3_ADDR_GETTERS.items():
        word = _decode_word(_eth_call(vault_address, _keccak256_text(sig)[:4].hex(), block), sig)
        out[key] = "0x" + f"{word:064x}"[-40:]
    if out["asset"].lower() != USDC_BASE.lower():
        raise RuntimeError(f"vault.usdc()={out['asset']} != ARB_VAULT_ASSET={USDC_BASE}")
    out["vault_balance_raw"] = _read_token_balance_raw(vault_address, block)
    out["idle_balance_usdc"] = out["idle_balance_raw"] / 1e6
    out["block"] = block
    out["ok"] = True
    log(
        f"Vault state (V3 @ {block}): pps={out['official_pps']} idle={out['idle_balance_usdc']:.6f} "
        f"balance={out['vault_balance_raw']} pending_redeem={out['pending_redeem_shares']} "
        f"last_ts={out['last_report_timestamp']} nonce={out['last_report_nonce']} "
        f"cooldown={out['report_cooldown']}s paused={out['paused']} shutdown={out['shutdown']}"
    )
    return out


def _read_vault_state(vault_address: str, block: str | None = None) -> dict:
    """Read vault state.

    Base mode (V2): getVaultState() — one call using the real contract ABI.
    Robinhood mode (V3): individual public getters, all at one block (pinned when not given).

    getVaultState() returns 12-element tuple per ABI:
      [0] officialPPS  [1] circulatingSupply  [2] idleBal
      [3] lastReportedBacking  [4] highWaterMarkAssets  [5] pendingDepositAssets
      [6] claimableRedeemAssets  [7] pendingRedeemShares
      [8] lastReportTimestamp  [9] lastReportNonce  [10] paused  [11] shutdown

    Fails closed: {"ok": False, "error": ...} when anything cannot be read.
    """
    if is_robinhood_funding():
        try:
            load_vault_config()
            return _read_vault_state_v3(vault_address, block or _pin_block())
        except Exception as e:
            log(f"WARNING Could not read V3 vault state: {e}")
            return {"ok": False, "error": str(e)}
    try:
        from eth_abi import decode as abi_decode

        sel = _keccak256_text("getVaultState()")[:4].hex()
        raw = _call_view(vault_address, sel)

        if not raw or raw == "0x":
            raise ValueError(f"getVaultState() empty — wrong address? sel={sel}")

        result_bytes = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        decoded = abi_decode(
            ["uint256","uint256","uint256","uint256","uint256",
             "uint256","uint256","uint256","uint256","uint256",
             "bool","bool"],
            result_bytes,
        )
        official_pps   = decoded[0]
        idle_balance   = decoded[2]
        pending_redeem = decoded[7]
        last_ts        = decoded[8]
        last_nonce     = decoded[9]

        # reportCooldown is not in getVaultState, read separately
        cooldown = _call_view_uint(vault_address, _keccak256_text("reportCooldown()")[:4].hex())
        if cooldown == 0:
            log(f"WARNING reportCooldown() returned 0, using fallback {_LOCAL_REPORT_COOLDOWN}s")
            cooldown = _LOCAL_REPORT_COOLDOWN

        # If chain shows last_ts=0 but we recently submitted, use local record
        if last_ts == 0 and _last_local_report_ts > 0:
            log(f"WARNING lastReportTimestamp=0 on-chain, using local ts={int(_last_local_report_ts)}")
            last_ts = int(_last_local_report_ts)

        log(
            f"Vault state: pps={official_pps} idle={idle_balance/1e6:.4f} "
            f"pending_redeem={pending_redeem} last_ts={last_ts} "
            f"nonce={last_nonce} cooldown={cooldown}s"
        )
        return {
            "last_report_nonce":     last_nonce,
            "last_report_timestamp": last_ts,
            "report_cooldown":       cooldown,
            "official_pps":          official_pps,
            "idle_balance_usdc":     idle_balance / 1e6,
            "idle_balance_raw":      idle_balance,
            "pending_redeem_shares": pending_redeem,
            "pending_deposit_assets_raw":  decoded[5],
            "claimable_redeem_assets_raw": decoded[6],
            # M3: exposed so run_reporter_tick's paused check actually works (V2 report()
            # is whenNotPaused, so a paused vault would only burn gas on a revert).
            "paused":   bool(decoded[10]),
            "shutdown": bool(decoded[11]),
            "ok": True,
        }
    except Exception as e:
        log(f"WARNING Could not read vault state: {e}")
        return {"ok": False, "error": str(e)}


def _read_usdc_balance(wallet: str) -> float:
    """Read USDC balance of wallet on Base."""
    padded = "000000000000000000000000" + wallet.lower().replace("0x", "")
    data = "0x" + "70a08231" + padded
    raw = _rpc("eth_call", [{"to": USDC_BASE, "data": data}, "latest"])
    return int(raw, 16) / 1e6


# ─────────────────────────────────────────────────────────────────────────────
# reportedAssets computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_reported_assets(vault_address: str, vault_state: dict | None = None,
                            block: str | None = None) -> dict:
    from . import arb_ctf_redemption as ctf
    if ctf.enabled():
        with ctf.nav_lock():
            return _compute_reported_assets(vault_address, vault_state, block)
    return _compute_reported_assets(vault_address, vault_state, block)


def _compute_reported_assets(vault_address: str, vault_state: dict | None = None,
                            block: str | None = None) -> dict:
    """Compute reportedAssets for report() — GROSS, in exact raw units.

    The contract requires reportedAssets >= asset.balanceOf(vault) and itself subtracts
    totalPendingDepositAssets + totalClaimableRedeemAssets, so reportedAssets must be the
    vault's ENTIRE asset balance plus every off-vault asset, each exactly once:

        vault balance (incl. pending deposits / claimable redeems — contract deducts them)
      + servicer cash   base mode: USDC on Base
                        robinhood: USDG on 4663 + USDC on Base + pending recalls + bridges
      + venue cash      Polymarket + Kalshi (+ Opinion only when enabled)
      + positions       open (+ pending_redeem in robinhood mode) at cost basis

    Robinhood mode: all chain-4663 reads (vault balance, servicer USDG) use one pinned block.
    Every component is read strictly: on ANY failure the result has ok=False, the failed
    components listed in "failures", and reported_assets_raw=None — never a silent 0 (C3).

    settled_pnl intentionally excluded: realised proceeds are in venue cash already.
    No USDG/USDC peg haircut in robinhood mode: USDG is the vault's unit of account.
    """
    from . import arb_nav as nav

    rh = is_robinhood_funding()
    failures: list[str] = []
    comp: dict[str, int] = {}
    breakdown = None
    positions: list = []

    def _try(name, fn):
        try:
            return fn()
        except nav.NavReadError as e:
            failures.append(f"{e.component}: {e.detail}")
        except Exception as e:
            failures.append(f"{name}: {e}")
        return None

    log("Computing reportedAssets (gross: vault balance + servicer + venues + positions)...")

    if rh and block is None:
        block = _try("block", _pin_block)
    blk = block or "latest"

    # Vault balance (gross — the contract deducts pending deposits / claimable redeems)
    if vault_state and vault_state.get("ok") and "vault_balance_raw" in vault_state \
            and vault_state.get("block") == blk:
        comp["vault"] = int(vault_state["vault_balance_raw"])
    elif not (rh and block is None):
        v = _try("vault_balance", lambda: _read_token_balance_raw(vault_address, blk))
        if v is not None:
            comp["vault"] = v

    # Servicer cash (+ in-transit in robinhood mode)
    if rh:
        wallet = os.environ.get("ARB_SERVICER_WALLET", "")
        if not wallet:
            failures.append("servicer_wallet: ARB_SERVICER_WALLET not set")
        elif block is not None:
            usdg = _try("servicer_usdg_rh", lambda: _read_token_balance_raw(wallet, blk))
            if usdg is not None:
                breakdown = _try("servicer", lambda: nav._get_servicer_cash_breakdown(
                    strict=True, usdg_raw=usdg, apply_peg=False))
                if breakdown is not None:
                    comp["servicer"] = int(breakdown["total_raw"])
    else:
        v = _try("servicer_usdc_base", nav._get_servicer_wallet_usdc_on_base_raw)
        if v is not None:
            comp["servicer"] = v

    venues = _try("venue_cash", lambda: nav.get_venue_cash_raw(strict=True))
    if venues is not None:
        comp["poly"], comp["kalshi"], comp["opinion"] = venues["polymarket"], venues["kalshi"], venues["opinion"]

    pos = _try("positions", lambda: nav._load_value_positions(strict=True))
    if pos is not None:
        positions = pos
        comp["positions"] = nav.positions_value_raw(positions)

    ok = not failures
    raw_total = sum(comp.values()) if ok else None
    f6 = lambda k: round(comp.get(k, 0) / 1e6, 6)
    out = {
        "ok":                    ok,
        "failures":              failures,
        "block":                 blk,
        "reported_assets_raw":   raw_total,
        "components_raw":        dict(comp),
        "reported_assets_usdc":  round(raw_total / 1e6, 6) if ok else None,
        "vault_idle_usdc":       f6("vault"),
        "servicer_on_base":      f6("servicer"),
        "poly_cash":             f6("poly"),
        "kalshi_cash":           f6("kalshi"),
        "opinion_cash":          f6("opinion"),
        "open_position_value":   f6("positions"),
        "open_position_count":   len(positions),
        "raw_total_usdc":        round(raw_total / 1e6, 6) if ok else None,
        "positions":             positions,
    }
    if breakdown is not None:
        out["servicer_breakdown"] = {k: v for k, v in breakdown.items()}
    if ok:
        log(
            f"reportedAssets @ {blk}: vault={comp['vault']} + servicer={comp['servicer']} "
            f"+ poly={comp['poly']} + kalshi={comp['kalshi']} + opinion={comp['opinion']} "
            f"+ positions={comp['positions']} = {raw_total} raw ({raw_total / 1e6:.6f})"
        )
    else:
        log(f"❌ reportedAssets NOT computed — failed components: {failures}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Robinhood-mode pre-report guards
# ─────────────────────────────────────────────────────────────────────────────

def _expiry_grace_secs() -> int:
    return int(os.environ.get("ARB_REPORT_EXPIRY_GRACE_SECS", str(6 * 3600)))


def _robinhood_report_guards(assets: dict) -> str | None:
    """Conditions under which reportedAssets is known to be inexact. Returns a reason or None.

    1. A Polymarket recall recorded as pending whose ETA has not passed: the withdraw script
       only returns once the USDG landed, so it is in the servicer balance AND the ledger.
    2. A pending_redeem position whose winning leg is on Kalshi: Kalshi credits payouts to
       cash automatically, so cash and the position would both be counted until the position
       is marked redeemed (mark_position_redeemed) — there is no verified Kalshi settlement
       reader yet (see arb_settlement.observe_venue_payout).
    3. An 'open' position past expiry + grace: same double-count risk if Kalshi settled it.
    """
    from . import arb_withdrawals as _w
    from . import arb_ctf_redemption as ctf
    now = int(time.time())
    try:
        rows = _w._load_ledger()
    except Exception as e:
        return f"withdrawal ledger unreadable: {e}"
    # Only legacy heuristic rows (no destination-balance snapshot). Rows carrying
    # dest_balance_before are settled by balance delta, so they are never double counted.
    settling = [r for r in rows if r.get("status") == "pending" and r.get("dest") == "rh_usdg"
                and r.get("dest_balance_before") is None and int(r.get("eta_at") or 0) > now]
    if settling:
        return (f"{len(settling)} Polymarket recall(s) landing (ETA not passed: "
                f"{[r.get('reference') for r in settling]}) — would double count")
    for p in assets.get("positions") or []:
        if ctf.enabled() and p.status == "open":
            # Cash was read before this check: an already credited settlement must
            # never coexist with the open position's cost basis in a signed NAV.
            try:
                from dataclasses import asdict
                if ctf.kalshi_payout(asdict(p)) is not None:
                    return f"position {p.pair_id}: Kalshi settlement awaiting CTF reconciliation"
            except Exception:
                return f"position {p.pair_id}: settlement evidence unavailable"
        if p.status == "pending_redeem" and (p.resolved_venue == "kalshi" or ctf.enabled()):
            venue = "Kalshi" if p.resolved_venue == "kalshi" else p.resolved_venue
            return f"position {p.pair_id} resolved on {venue}; payout not reconciled (mark_position_redeemed)"
        if p.status == "open" and p.expiry_ts and now > p.expiry_ts + _expiry_grace_secs():
            return f"position {p.pair_id} still open {now - p.expiry_ts}s past expiry — settlement not reconciled"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Report payload builder
# ─────────────────────────────────────────────────────────────────────────────

def build_report_payload(vault_address: str, signer_key: str, force: bool = False) -> dict | None:
    """Build and sign a ReportDataV2 payload for the contract's report() function.

    Args:
        vault_address: deployed V2 (base mode) / V3 (robinhood mode) vault address
        signer_key:    ARB_NAV_SIGNER_PRIVATE_KEY
        force:         if True, bypasses the internal cooldown check. Use when
                       run_reporter_tick has already decided to fire early due to
                       redemption pressure. The contract-level cooldown is still
                       enforced on-chain — this only skips the local guard.

    Returns the payload dict, or None (reason in LAST_ABORT_REASON) if vault/key missing,
    cooldown not elapsed, vault paused, or ANY reportedAssets component failed to read.
    """
    global LAST_ABORT_REASON
    LAST_ABORT_REASON = ""
    if not vault_address:
        log("⚠️ ARB_VAULT_V2_ADDRESS not set — cannot build report")
        return _abort("ARB_VAULT_V2_ADDRESS not set")
    if not signer_key:
        log("⚠️ ARB_NAV_SIGNER_PRIVATE_KEY not set — cannot sign report")
        return _abort("ARB_NAV_SIGNER_PRIVATE_KEY not set")

    rh = is_robinhood_funding()
    block = "latest"
    if rh:
        try:
            load_vault_config()
            block = _pin_block()
        except Exception as e:
            return _abort(f"vault chain/config check failed: {e}")

    # Read vault state (needed for nonce regardless of cooldown check)
    vault_state = _read_vault_state(vault_address, block) if rh else _read_vault_state(vault_address)
    if not vault_state["ok"]:
        log("⚠️ Could not read vault state — skipping report")
        return _abort(f"vault state unreadable: {vault_state.get('error', '')}")

    if vault_state.get("paused") and not (rh and vault_state.get("shutdown")):
        return _abort("vault is paused")

    if rh:
        from eth_account import Account
        signer_addr = Account.from_key(signer_key).address
        if vault_state.get("report_signer", "").lower() != signer_addr.lower():
            return _abort(f"signer key {signer_addr} != vault.reportSigner() {vault_state.get('report_signer')}")

    last_ts  = vault_state["last_report_timestamp"]
    cooldown = vault_state["report_cooldown"]
    now      = int(time.time())

    if not force and last_ts > 0 and (now - last_ts) < cooldown:
        remaining = cooldown - (now - last_ts)
        log(f"⏳ Report cooldown: {remaining}s remaining — skip")
        return _abort(f"cooldown {remaining}s remaining")

    reason = "forced (early trigger)" if force else "cooldown elapsed"
    log(f"✅ Building report — reason: {reason} (last={last_ts}, cooldown={cooldown}s)")

    # Compute assets — any failed component aborts the report (C3)
    assets_data = compute_reported_assets(vault_address, vault_state=vault_state,
                                          block=block if rh else None)
    if not assets_data["ok"]:
        return _abort("reportedAssets component read failed: " + "; ".join(assets_data["failures"]))
    reported_raw = assets_data["reported_assets_raw"]
    reported_assets = reported_raw / 1e6

    if rh:
        guard = _robinhood_report_guards(assets_data)
        if guard:
            return _abort(guard)
        if reported_raw < vault_state["vault_balance_raw"]:   # contract would revert
            return _abort(f"reportedAssets {reported_raw} < vault balance {vault_state['vault_balance_raw']}")

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
        reported_assets_raw=reported_raw,
    )

    breakdown = {k: v for k, v in assets_data.items() if k != "positions"}
    payload = {
        "reported_assets_usdc": reported_assets,
        "reported_assets_raw":  reported_raw,
        "timestamp":            timestamp,
        "deadline":             deadline,
        "nonce":                nonce,
        "vault_address":        vault_address,
        "chain_id":             BASE_CHAIN_ID,
        "domain_salt":          DOMAIN_SALT_TEXT,
        "signature":            signature,
        "block":                block,
        "breakdown":            breakdown,
    }

    log(
        f"✅ Report payload built: reportedAssets={reported_raw} raw ({reported_assets:.6f}) "
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

    from .arb_nav import usdc_to_raw

    sig_bytes = bytes.fromhex(payload["signature"].replace("0x", ""))
    # H4: the exact raw amount that was signed (int(x * 1e6) truncated, e.g. 249 -> 248).
    reported_assets_wei = (int(payload["reported_assets_raw"]) if payload.get("reported_assets_raw") is not None
                           else usdc_to_raw(payload["reported_assets_usdc"]))

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


def _get_receipt_status(tx_hash: str, timeout: int, poll_interval: int = 6) -> int | None:
    """1 = mined OK, 0 = mined but reverted, None = not mined within timeout."""
    deadline = time.time() + timeout
    while True:
        try:
            receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                return int(receipt.get("status", "0x0"), 16)
        except Exception as e:
            log(f"⚠️ Receipt poll error: {e}")
        if time.time() >= deadline:
            return None
        time.sleep(poll_interval)


def submit_report(
    payload: dict,
    signer_key: str,
    max_deposits: int = DEFAULT_MAX_DEPOSITS,
    max_redeems: int  = DEFAULT_MAX_REDEEMS,
) -> str | None:
    """Simulate, sign and broadcast the report() transaction, then wait for its receipt.

    M3: the call is first simulated with eth_call from the signer; a revert means nothing is
    broadcast and no cooldown is set. The local 6h cooldown is set ONLY when the receipt shows
    status 1. A reverted receipt returns None (no cooldown). A receipt that does not arrive
    within ARB_REPORT_RECEIPT_TIMEOUT_SECS sets a short ARB_REPORT_PENDING_GUARD_SECS guard
    and returns the tx hash. Gas limit: ARB_REPORT_GAS_LIMIT (default 500000).

    Returns tx hash string or None on failure.
    """
    global _last_local_report_ts, _pending_report_until
    from eth_account import Account

    vault_address = payload["vault_address"]
    account       = Account.from_key(signer_key)

    try:
        call_data = _abi_encode_report_call(payload, max_deposits, max_redeems)
    except Exception as e:
        log(f"❌ ABI encode error: {e}")
        return None
    data_hex = "0x" + call_data.hex()

    try:
        _rpc("eth_call", [{"from": account.address, "to": vault_address, "data": data_hex}, "latest"])
    except Exception as e:
        log(f"❌ report() simulation reverted — not broadcasting, no cooldown set: {e}")
        return None

    gas_price = _get_gas_price()
    # Bump 20% to ensure inclusion
    gas_price = int(gas_price * 1.2)
    nonce = _get_nonce(account.address)

    tx = {
        "to":       vault_address,
        "data":     data_hex,
        "gas":      _report_gas_limit(),
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  BASE_CHAIN_ID,
        "value":    0,
    }

    signed = account.sign_transaction(tx)

    try:
        raw_hex = "0x" + signed.raw_transaction.hex()
        tx_hash = _rpc("eth_sendRawTransaction", [raw_hex])
        log(f"✅ report() tx sent: {tx_hash}")
    except Exception as e:
        log(f"❌ report() tx failed: {e}")
        return None

    status = _get_receipt_status(tx_hash, _report_receipt_timeout())
    if status == 1:
        _save_report_snapshot(payload, tx_hash)
        _last_local_report_ts = time.time()
        log(f"Local report guard set: cooldown {_LOCAL_REPORT_COOLDOWN}s started")
        return tx_hash
    if status == 0:
        log(f"❌ report() tx {tx_hash} REVERTED on-chain — no cooldown set")
        return None
    _pending_report_until = time.time() + _report_pending_guard_secs()
    log(f"⏰ report() tx {tx_hash} not mined in {_report_receipt_timeout()}s — "
        f"retry guard {_report_pending_guard_secs()}s (no 6h cooldown)")
    return tx_hash


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
                id                   SERIAL PRIMARY KEY,
                reported_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                nonce                BIGINT NOT NULL,
                reported_assets      NUMERIC(18,6) NOT NULL,
                vault_idle_usdc      NUMERIC(18,6),
                servicer_on_base     NUMERIC(18,6),
                poly_cash            NUMERIC(18,6),
                kalshi_cash          NUMERIC(18,6),
                opinion_cash         NUMERIC(18,6),
                open_position_value  NUMERIC(18,6),
                open_position_count  INTEGER,
                signature            TEXT,
                tx_hash              TEXT
            )
        """)
        cur.execute("""
            ALTER TABLE arb_vault_reports
                ADD COLUMN IF NOT EXISTS open_position_value NUMERIC(18,6),
                ADD COLUMN IF NOT EXISTS open_position_count INTEGER
        """)
        bd = payload.get("breakdown", {})
        cur.execute("""
            INSERT INTO arb_vault_reports
                (nonce, reported_assets, vault_idle_usdc, servicer_on_base,
                 poly_cash, kalshi_cash, opinion_cash,
                 open_position_value, open_position_count,
                 signature, tx_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            payload["nonce"],
            payload["reported_assets_usdc"],
            bd.get("vault_idle_usdc"),
            bd.get("servicer_on_base"),
            bd.get("poly_cash"),
            bd.get("kalshi_cash"),
            bd.get("opinion_cash"),
            bd.get("open_position_value"),
            bd.get("open_position_count"),
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
         ["0x" + account.sign_transaction(approve_tx).raw_transaction.hex()])

    # Step B: refillBuffer(amount)
    REFILL_SELECTOR = _keccak256_text("refillBuffer(uint256)")[:4].hex()
    refill_data = "0x" + REFILL_SELECTOR + amount_hex
    refill_tx = {
        "to": vault_address, "data": refill_data,
        "gas": 150_000, "gasPrice": gas_price,
        "nonce": nonce + 1, "chainId": BASE_CHAIN_ID, "value": 0,
    }
    tx_hash = _rpc("eth_sendRawTransaction",
                   ["0x" + account.sign_transaction(refill_tx).raw_transaction.hex()])
    return tx_hash


# Per-venue recall cooldown (C4). In memory; the ledger check below survives restarts.
_last_recall_ts: dict = {}


def _recall_cooldown_secs() -> int:
    return int(os.environ.get("ARB_RECALL_COOLDOWN_SECS", "1800"))


def _in_flight_home_usdc() -> float:
    """Money already on its way back to the servicer: pending venue recalls + Base→home
    bridges (robinhood mode). Raises if a ledger cannot be read."""
    from . import arb_withdrawals as _w
    from .arb_nav import assert_ledgers_readable
    assert_ledgers_readable()
    counted = getattr(_w, "COUNTED_STATUSES", ("pending",))
    total = _w.get_pending_withdrawal_usdc()
    for row in _w._load_ledger(_w.BRIDGE_LEDGER_PATH):
        if row.get("status") in counted and row.get("direction") == "base_to_rh":
            total += float(row.get("amount_usdc") or 0.0)
    return round(total, 6)


def _venues_with_pending_recall() -> set:
    from . import arb_withdrawals as _w
    counted = getattr(_w, "COUNTED_STATUSES", ("pending",))
    return {str(r.get("platform")) for r in _w._load_ledger() if r.get("status") in counted}


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

    idle_balance = vault_state["idle_balance_usdc"]
    pending_redeem_shares = vault_state["pending_redeem_shares"]

    # ── Step 1: Vault idle covers redemptions ─────────────────────────────────
    if pending_redeem_shares == 0:
        log("✅ Waterfall: no pending redemptions")
        return

    official_pps = int(vault_state.get("official_pps") or 0)

    if pending_redeem_shares > 0 and official_pps > 0:
        pending_redeem_value = (pending_redeem_shares / 1e18) * (official_pps / 1e6)
    else:
        pending_redeem_value = 0.0

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
            if is_robinhood_funding():
                # journal the servicer outflow so delta-based arrival detection does not mistake it
                # for (or let it hide) a recall landing in the same window
                try:
                    from . import arb_ledger
                    arb_ledger.record_cashflow(BASE_CHAIN_ID, USDC_BASE, -sweep_amount, "vault_refill", tx_hash)
                except Exception as e:
                    log(f"⚠️ Waterfall: refill not journaled ({e}); recall arrivals may go stale until reconciled")
            shortfall -= sweep_amount
        except Exception as e:
            log(f"❌ Waterfall Step 2/3 sweep failed: {e}")
    else:
        log(
            f"⚠️ Waterfall Step 2/3: servicer has only {servicer_balance:.4f} USDC "
            f"(available={available_to_sweep:.4f}) — cannot cover {shortfall:.4f} shortfall"
        )

    # ── Step 3.5: Pull cash from trading platforms if servicer sweep insufficient ─
    # C4: recalls already in flight (pending withdrawals + Base→home bridges) are subtracted,
    # and a venue with a pending recall or one issued within ARB_RECALL_COOLDOWN_SECS is not
    # asked again — previously the full shortfall was re-requested on every tick.
    if shortfall >= ARB_WATERFALL_MIN_SHORTFALL:
        log(
            f"🔄 Waterfall Step 3.5: shortfall {shortfall:.4f} remains after servicer sweep — "
            f"pulling from trading platforms"
        )
        try:
            from .arb_nav import _get_servicer_balances

            try:
                in_flight = _in_flight_home_usdc()
                busy = _venues_with_pending_recall()
            except Exception as e:
                raise RuntimeError(f"recall ledger unreadable ({e}) — no new recall (avoids duplicates)")
            uncovered = shortfall - in_flight
            log(f"ℹ️ Waterfall Step 3.5: in-flight recalls/bridges={in_flight:.4f} → uncovered={uncovered:.4f}")

            if uncovered < ARB_WATERFALL_MIN_SHORTFALL:
                log("✅ Waterfall Step 3.5: in-flight recalls cover the shortfall — no new recall")
            else:
                poly_cash, kalshi_cash, opinion_cash = _get_servicer_balances()   # 0 = do not recall from it
                total_platform_cash = poly_cash + kalshi_cash + opinion_cash
                log(
                    f"💧 Platform cash: poly={poly_cash:.4f} kalshi={kalshi_cash:.4f} "
                    f"opinion={opinion_cash:.4f} total={total_platform_cash:.4f} USDC"
                )
                now = time.time()
                cash = {"polymarket": poly_cash, "kalshi": kalshi_cash, "opinion": opinion_cash}
                for venue in cash:
                    if venue in busy or now - _last_recall_ts.get(venue, 0.0) < _recall_cooldown_secs():
                        if cash[venue] > 0:
                            log(f"⏳ Waterfall Step 3.5: {venue} recall pending/cooling down — skipped")
                        cash[venue] = 0.0

                if not any(v > 1.0 for v in cash.values()):
                    log("ℹ️ Waterfall Step 3.5: no eligible venue this tick")
                else:
                    for venue, v in cash.items():
                        if v > 1.0:
                            _last_recall_ts[venue] = now
                    initiated = withdraw_from_platforms(
                        shortfall_usdc=uncovered,
                        poly_cash=cash["polymarket"],
                        kalshi_cash=cash["kalshi"],
                        opinion_cash=cash["opinion"],
                        servicer_wallet=account.address,
                    )
                    log(
                        f"✅ Waterfall Step 3.5: initiated {initiated:.4f} USDC of platform withdrawals "
                        f"(funds in transit — will arrive in 5–30 min depending on platform)"
                    )
                    if initiated >= uncovered * 0.9:
                        log(f"ℹ️ Step 3.5 expected to cover shortfall — Step 4 unwind deferred")

            pending_inflight = get_pending_withdrawals_total_usdc()
            log(
                f"ℹ️ Waterfall Step 3.5: pending in-flight platform withdrawals="
                f"{pending_inflight:.4f} USDC"
            )
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

    pending_redeem_shares = int(vault_state.get("pending_redeem_shares", 0) or 0)
    if pending_redeem_shares == 0:
        return False

    idle_balance = float(vault_state.get("idle_balance_usdc", 0.0) or 0.0)
    last_ts = int(vault_state.get("last_report_timestamp", 0) or 0)
    official_pps = int(vault_state.get("official_pps", 0) or 0)
    now = int(time.time())

    if pending_redeem_shares > 0 and official_pps > 0:
        pending_redeem_value = (pending_redeem_shares / 1e18) * (official_pps / 1e6)
    else:
        pending_redeem_value = 0.0

    # Must have enough time elapsed since last report
    elapsed = now - last_ts if last_ts > 0 else ARB_EARLY_REPORT_MIN_ELAPSED + 1
    if elapsed < ARB_EARLY_REPORT_MIN_ELAPSED:
        log(
            f"⏳ Early report check: {elapsed}s elapsed < min {ARB_EARLY_REPORT_MIN_ELAPSED}s — skip"
        )
        return False

    # Pressure ratio check
    if idle_balance <= 0:
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
    """Best-effort request count by probing public array getter until it reverts."""
    from eth_abi import encode as abi_encode

    fn_sig = "depositRequests(uint256)" if is_deposit else "redeemRequests(uint256)"
    selector = _keccak256_text(fn_sig)[:4]

    def exists(idx: int) -> bool:
        try:
            encoded = abi_encode(["uint256"], [idx])
            raw = _rpc("eth_call", [
                {"to": vault_address, "data": "0x" + (selector + encoded).hex()},
                "latest",
            ])
            return bool(raw and raw != "0x")
        except Exception:
            return False

    if not exists(0):
        return 0

    lo, hi = 0, 1
    while exists(hi):
        lo = hi
        hi *= 2
        if hi > 1_000_000:
            break

    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if exists(mid):
            lo = mid
        else:
            hi = mid

    return lo + 1


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
    raw_hex = "0x" + signed.raw_transaction.hex()
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


def sweep_claimable_requests(vault_address: str, signer_key: str) -> None:
    """Scan ALL deposit and redeem requests and auto-claim any in CLAIMABLE state.

    This is the core claiming engine. It is called:
      a) by auto_claim_after_report() immediately after a report() tx confirms, AND
      b) at the start of every run_reporter_tick() as a catch-up sweep to handle
         requests that went CLAIMABLE but were missed (e.g. report tx timed-out
         last cycle, or a previous sweep batch partially failed).

    Safe to call when there are zero CLAIMABLE requests — it no-ops silently.
    All errors are swallowed; never crashes the reporter loop.
    """
    log("🔄 [AutoClaim] Scanning all requests for CLAIMABLE status…")

    try:
        from eth_account import Account
        account   = Account.from_key(signer_key)
        nonce     = _get_nonce(account.address)
        gas_price = int(_get_gas_price() * 1.2)
    except Exception as e:
        log(f"⚠️ [AutoClaim] Cannot initialise signer for sweep: {e}")
        return

    # ── Deposit requests ──────────────────────────────────────────────────────
    try:
        dep_count = _get_request_count(vault_address, is_deposit=True)
        log(f"📋 [AutoClaim] Total deposit requests: {dep_count}")
        dep_claimable = []      # {"id", "receiver", "assets", "processed_pps"}
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
            log(
                f"🏦 [AutoClaim] {len(dep_claimable)} claimable deposit(s)"
                f" — batching in chunks of {MAX_CLAIMS_PER_TX}"
            )
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
        log(f"📋 [AutoClaim] Total redeem requests: {rdm_count}")
        rdm_claimable = []      # {"id", "receiver", "claimable_assets"}
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
            log(
                f"💸 [AutoClaim] {len(rdm_claimable)} claimable redeem(s)"
                f" — batching in chunks of {MAX_CLAIMS_PER_TX}"
            )
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


def auto_claim_after_report(vault_address: str, signer_key: str, report_tx_hash: str) -> None:
    """Trigger an auto-claim sweep after a report() tx is submitted.

    Waits up to 90s for the report tx to confirm before sweeping (requests only
    become CLAIMABLE once the report mines).  If the tx is not confirmed within
    90s, a best-effort sweep runs anyway — this is a no-op if the tx hasn't
    mined yet, but guarantees that already-CLAIMABLE requests (from a previous
    report cycle that timed out) are claimed without further delay.

    The definitive catch-up path for missed claims is the sweep that runs at
    the start of every run_reporter_tick() call.

    All errors are swallowed; never crashes the reporter loop.
    """
    log("⏳ [AutoClaim] Waiting for report tx to confirm before sweeping…")
    confirmed = _wait_for_tx_confirm(report_tx_hash, timeout=90)
    if confirmed:
        log("✅ [AutoClaim] Report tx confirmed — running claim sweep")
    else:
        log(
            "⚠️ [AutoClaim] Report tx not confirmed within 90s"
            " — running best-effort sweep (may find 0 claimable if tx still pending)"
        )

    # Always run the sweep, whether the report tx confirmed or timed out.
    # If the tx is still pending, this will find 0 CLAIMABLE and no-op.
    # If it just barely mined after the 90s poll, this will catch those claims.
    # Either way, the next run_reporter_tick() catch-up sweep is the safety net.
    sweep_claimable_requests(vault_address, signer_key)


def run_reporter_tick():
    """Called once per execution cycle.

    Order of operations:
      1.  Waterfall: sweep servicer cash → vault if redemption shortfall exists
      1b. Catch-up claim sweep: auto-claim any CLAIMABLE requests (runs every tick
          to handle missed claims from previous timed-out cycles)
      2.  Check for early report trigger (redemption pressure bypasses cooldown)
      3.  Build and submit report() if cooldown elapsed OR early trigger fired
      4.  Auto-claim sweep immediately after report() confirms on-chain
      5.  Silently no-ops if ARB_VAULT_V2_ADDRESS not set

    All errors are caught so a reporter failure never crashes the arb loop.
    """
    from .shadow_guard import assert_not_shadow
    assert_not_shadow('vault NAV report')
    vault_address = os.environ.get("ARB_VAULT_V2_ADDRESS", "")
    signer_key    = os.environ.get("ARB_NAV_SIGNER_PRIVATE_KEY", "")
    servicer_key  = os.environ.get("POLY_PRIVATE_KEY", "")

    if not vault_address:
        return  # V2 not deployed yet — silent skip

    # M5: robinhood mode refuses to run on a missing/inconsistent vault config (raises
    # VaultConfigError) instead of silently using the Base V2 defaults.
    if is_robinhood_funding():
        load_vault_config()

    # ── 1. Withdrawal funding waterfall ───────────────────────────────────────
    try:
        run_withdrawal_waterfall(vault_address, servicer_key)
    except Exception as e:
        log(f"⚠️ Waterfall error (non-fatal): {e}")

    # Settlement checker
    try:
        from .arb_settlement import check_and_settle_positions
        settled = check_and_settle_positions()
        if settled > 0:
            log(f"Settlement checker: {settled} position(s) settled this cycle")
    except Exception as e:
        log(f"Settlement checker error (non-fatal): {e}")

    # ── 1b. Catch-up claim sweep ───────────────────────────────────────────────
    # Always sweep for CLAIMABLE requests on every tick.  This handles:
    #   - Requests that became CLAIMABLE in a previous cycle but weren't claimed
    #     because auto_claim_after_report timed out (90s) or partially failed.
    #   - Reports processed before this bot version was deployed (legacy backlog).
    # Safe no-op when no CLAIMABLE requests exist.
    if signer_key:
        try:
            sweep_claimable_requests(vault_address, signer_key)
        except Exception as e:
            log(f"⚠️ Catch-up sweep error (non-fatal): {e}")

    # ── 2 + 3. Report: cooldown OR early trigger ───────────────────────────────
    try:
        # Local cooldown guard - fast path, no RPC needed
        _local_elapsed = time.time() - _last_local_report_ts
        if _last_local_report_ts > 0 and _local_elapsed < _LOCAL_REPORT_COOLDOWN:
            log(f"Local cooldown guard: {int(_LOCAL_REPORT_COOLDOWN - _local_elapsed)}s remaining — skip")
            return
        if time.time() < _pending_report_until:
            log(f"Previous report tx unconfirmed — retry guard {int(_pending_report_until - time.time())}s remaining — skip")
            return

        vault_state = _read_vault_state(vault_address)
        if not vault_state["ok"]:
            log("⚠️ Cannot read vault state for report check — skip")
            return

        last_ts   = vault_state["last_report_timestamp"]
        cooldown  = vault_state["report_cooldown"]
        now       = int(time.time())
        elapsed   = now - last_ts if last_ts > 0 else cooldown + 1

        # V2 report() is whenNotPaused; V3 allows report() while paused only in shutdown.
        if vault_state.get("paused") and not (is_robinhood_funding() and vault_state.get("shutdown")):
            log("⚠️ Report: vault is paused — skip")
            return

        cooldown_elapsed = elapsed >= cooldown
        early_trigger    = False  # TEMP: disable early-triggered report attempts for PMFIArbVaultV2

        if not cooldown_elapsed:
            remaining = cooldown - elapsed
            log(f"⏳ Report: cooldown {remaining}s remaining — skip")
            return

        reason = "cooldown elapsed"
        log(f"📋 Report firing — reason: {reason}")

        payload = build_report_payload(vault_address, signer_key, force=False)
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
