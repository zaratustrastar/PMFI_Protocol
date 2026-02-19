#!/usr/bin/env python3
"""
PredictFi Sniper Vault V7.5 - NAV Signing Bot with Withdrawal Exclusion

=============================================================================
V7.5 CHANGE: Withdrawal Exclusion from NAV
=============================================================================

When a user calls requestWithdraw(), shares are transferred to the vault and
usdcLocked is recorded. These shares and USDC are "spoken for" and must be
excluded from NAV calculation to prevent distortion for remaining LPs.

V7.5 NAV FORMULA:
    effective_exclusion = max(0, total_usdcLocked - withdrawal_bridge_in_transit)
    effective_assets = totalAssets - effective_exclusion
    effective_supply = totalSupply - totalPendingShares
    NAV = effective_assets / effective_supply

withdrawal_bridge_in_transit = USDC already debited from Polygon cash but
not yet credited on Base vault. This prevents double-subtraction during
bridge transit: once from totalAssets (cash left PM) and once from usdcLocked.

This ensures remaining LP shares are priced correctly regardless of
where the withdrawal funds are in the pipeline (PM cash, positions,
in-flight bridge, vault buffer).

=============================================================================
V7.4 CHANGE (still in effect):
=============================================================================

NAV reflects ACTUAL LIQUID VALUE (cash + positions liquidation value).
Trading losses are shown directly in NAV, not hidden in pendingCredit.

OLD (V7.3 and earlier):
    totalAssets = cash + positions + inFlight + pendingCredit
    pendingCredit = expectedAssets - cash - positions - inFlight
    Result: NAV always equals expectedAssets/shares = initial deposit price

NEW (V7.4+):
    totalAssets = cash + positions + inFlight
    pendingCredit = 0 (for NAV purposes, kept for bridging monitoring only)
    Result: NAV reflects actual market value of positions

=============================================================================
ARCHITECTURE:
=============================================================================

V7.4 tracks asset states for NAV calculation:

1. inFlightOnChain - USDC sitting at Polymarket Base deposit address (usually ~0)
2. creditedAssets - PM cash + positions liquidation value via API
3. vaultBuffer - USDC in vault contract (claimable withdrawals)

pendingCredit is calculated but NOT included in NAV - only for monitoring bridging.

=============================================================================
SAFETY VALVES:
=============================================================================

1. maxPendingAge - If in-flight funds pending > X hours, pause deposits
   (V7.4: Only checks in-flight, not phantom pendingCredit)

=============================================================================
ENDPOINTS:
=============================================================================

1. /health           - Health check
2. /price            - Get cached price (fast, for UI display)
3. /sign-nav         - Get signed NavDataV7 for deposit/withdraw
4. /sign-nav/debug   - Debug endpoint showing full asset breakdown

"""

import os
import sys
import json
import time
import threading
import hmac
import hashlib
import secrets
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from flask import Flask, jsonify, request as flask_request, send_from_directory, render_template, redirect
from flask_cors import CORS

# Cloudflare bypass with curl_cffi (residential proxy support)
try:
    from curl_cffi import requests as curl_requests
    BYPASS_METHOD = "curl_cffi"
    print("🔓 Using curl_cffi for Cloudflare bypass (TLS fingerprint spoofing)")
except ImportError:
    curl_requests = None
    BYPASS_METHOD = "requests"
    print("⚠️  curl_cffi not available, using standard requests (may be blocked by Cloudflare)")

load_dotenv()

# =============================================================================
# Private Key Normalization (catches quotes, whitespace, wrong format early)
# =============================================================================
def normalize_privkey(raw: str, name: str = "key") -> str:
    """
    Normalize and validate a private key.
    Strips quotes, whitespace, validates hex format.
    Returns normalized key with 0x prefix or raises ValueError.
    """
    if not raw:
        return ""  # Allow empty for optional keys
    
    s = raw.strip().strip('"').strip("'").strip()
    
    # Remove 0x prefix for validation
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    
    # Validate length
    if len(s) != 64:
        raise ValueError(f"{name} wrong length: got {len(s)}, expected 64 hex chars. Value: {repr(raw[:20])}...")
    
    # Validate hex
    try:
        int(s, 16)
    except ValueError:
        raise ValueError(f"{name} contains non-hex characters. Value: {repr(raw[:20])}...")
    
    return "0x" + s.lower()


# =============================================================================
# Configuration
# =============================================================================
RPC_URL = os.getenv("RPC_URL") or os.getenv("BASE_RPC_URL") or "https://mainnet.base.org"
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL") or "https://polygon-rpc.com"  # For PM balance queries
VAULT_V7_ADDRESS = os.getenv("VAULT_V7_ADDRESS") or os.getenv("VAULT_V6_ADDRESS") or ""
USDC_ADDRESS = os.getenv("USDC_ADDRESS") or "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_E_POLYGON = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"  # USDC.e on Polygon (6 decimals)
POLYMARKET_BASE_DEPOSIT = "0x2b20920A00D705043260eBFE6561bC96FBd84dBE"

# Normalize private keys at startup (catches bad format immediately)
_raw_oracle_key = os.getenv("ORACLE_PRIVATE_KEY") or os.getenv("KEEPER_PRIVATE_KEY") or os.getenv("PRIVATE_KEY") or ""
try:
    ORACLE_PRIVATE_KEY = normalize_privkey(_raw_oracle_key, "ORACLE_PRIVATE_KEY") if _raw_oracle_key else ""
except ValueError as e:
    print(f"❌ {e}")
    ORACLE_PRIVATE_KEY = ""

POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS")

# Residential proxy for Cloudflare bypass
PROXY_URL = os.getenv("PROXY_URL", "")

HTTP_PORT = int(os.getenv("BOT_HTTP_PORT", 8080))

NAV_VALIDITY_SECONDS = 300  # Match contract MAX_NAV_AGE (5 minutes for MVP)
NAV_CACHE_REFRESH_SECONDS = 45  # Refresh cache every 45 seconds
NAV_PRECISION = 10**18

# Safety valve thresholds
MAX_PENDING_AGE_HOURS = 2  # Pause if any deposit pending > 2 hours

# V7.5: Conservation bound REMOVED from contract
# NAV now reflects actual position values without artificial floors
# Keeping MAX_LOSS_BPS for display/warning purposes only
MAX_LOSS_BPS = 4000  # 40% threshold for warnings (not enforced)

# Rate limiting
REFRESH_RATE_LIMIT_SECONDS = 5
last_refresh_request = {}

# =============================================================================
# Invite Code System Configuration
# =============================================================================
DATABASE_URL = os.getenv("DATABASE_URL", "")
PMFI_ADMIN_TOKEN = os.getenv("PMFI_ADMIN_TOKEN", secrets.token_hex(16))
PMFI_HMAC_SECRET = os.getenv("PMFI_HMAC_SECRET", secrets.token_hex(32))

# In-memory rate limiting for invite codes
invite_rate_limit_store = {}
INVITE_RATE_LIMIT_WINDOW = 60  # 1 minute
INVITE_RATE_LIMIT_MAX = 5

def get_invite_db():
    """Get database connection for invite system"""
    return psycopg2.connect(DATABASE_URL)

def init_invite_tables():
    """Initialize invite code database tables"""
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL not set - invite system disabled")
        return False
    try:
        conn = get_invite_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id SERIAL PRIMARY KEY,
                code_hash VARCHAR(64) NOT NULL UNIQUE,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by VARCHAR(255),
                expires_at TIMESTAMP,
                redeemed_by VARCHAR(42),
                redeemed_at TIMESTAMP,
                note TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_audit_logs (
                id SERIAL PRIMARY KEY,
                action VARCHAR(50) NOT NULL,
                code_hash VARCHAR(64),
                wallet_address VARCHAR(42),
                ip_address VARCHAR(45),
                success BOOLEAN NOT NULL,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whitelisted_wallets (
                wallet_address VARCHAR(42) PRIMARY KEY,
                code_id INTEGER REFERENCES invite_codes(id),
                whitelisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whitelisted_fids (
                fid INTEGER PRIMARY KEY,
                code_id INTEGER REFERENCES invite_codes(id),
                whitelisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Invite code tables initialized (including whitelisted_fids)")
        return True
    except Exception as e:
        print(f"❌ Failed to init invite tables: {e}")
        return False

def init_xp_tables():
    """Initialize XP system database tables"""
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL not set - XP system disabled")
        return False
    try:
        conn = get_invite_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS xp_users (
                fid INTEGER PRIMARY KEY,
                username VARCHAR(255),
                wallet VARCHAR(42),
                referrer_fid INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS xp_events (
                id SERIAL PRIMARY KEY,
                fid INTEGER NOT NULL,
                type VARCHAR(50) NOT NULL,
                xp INTEGER NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'COMPLETED',
                meta JSONB,
                unique_key VARCHAR(255) UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_earnings (
                id SERIAL PRIMARY KEY,
                referrer_fid INTEGER NOT NULL,
                referee_fid INTEGER NOT NULL,
                source_event_id INTEGER NOT NULL,
                xp_share INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(referrer_fid, referee_fid, source_event_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_xp_events_fid ON xp_events(fid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_xp_events_unique_key ON xp_events(unique_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_earnings_referrer ON referral_earnings(referrer_fid)")
        conn.commit()
        cur.close()
        conn.close()
        print("✅ XP system tables initialized")
        return True
    except Exception as e:
        print(f"❌ Failed to init XP tables: {e}")
        return False


TASK_DEFINITIONS = [
    {"id": "follow_fc", "xp": 100, "verified": True},
    {"id": "deposit_10", "xp": 500, "verified": True},
    {"id": "invite", "xp": 250, "verified": True},
    {"id": "follow_x", "xp": 100, "verified": False},
]

REFERRAL_BONUS_RATIO = 0.10
XP_FOLLOW_FC_TARGET_FID = 1550088  # pmfi Farcaster FID
XP_DEPOSIT_MIN_USDC = 10  # Minimum USDC deposit to qualify (in whole units)


def award_xp(fid, xp_type, xp, meta=None, unique_key=None):
    """Award XP to a user idempotently. Returns (event_id, awarded_now).
    unique_key is required for idempotency - will be auto-generated if not provided."""
    if not DATABASE_URL:
        return None, False
    if not unique_key:
        unique_key = f"{xp_type}:{fid}"
    conn = get_invite_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        status = 'PENDING_REVIEW' if xp_type == 'follow_x' else 'COMPLETED'
        cur.execute("""
            INSERT INTO xp_events (fid, type, xp, status, meta, unique_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (unique_key) DO NOTHING
            RETURNING id
        """, (fid, xp_type, xp, status, json.dumps(meta or {}), unique_key))
        row = cur.fetchone()
        if row is None:
            conn.commit()
            print(f"ℹ️ [XP] Duplicate unique_key={unique_key}, skipping")
            cur.close()
            conn.close()
            return None, False
        event_id = row['id']
        if status == 'COMPLETED':
            _award_referral_bonus(cur, fid, event_id, xp)
        conn.commit()
        print(f"✅ [XP] Awarded {xp} XP to fid={fid} type={xp_type} event_id={event_id}")
        cur.close()
        conn.close()
        return event_id, True
    except Exception as e:
        conn.rollback()
        print(f"❌ [XP] award_xp error: {e}")
        cur.close()
        conn.close()
        return None, False


def _award_referral_bonus(cur, referee_fid, source_event_id, base_xp):
    """Award 10% referral bonus to the referrer if one exists.
    Called within award_xp's transaction - does NOT commit or rollback."""
    try:
        cur.execute("SELECT referrer_fid FROM xp_users WHERE fid = %s", (referee_fid,))
        user = cur.fetchone()
        if not user or not user['referrer_fid']:
            return
        referrer_fid = user['referrer_fid']
        bonus_xp = max(1, int(base_xp * REFERRAL_BONUS_RATIO))
        ref_unique = f"ref_bonus:{source_event_id}"
        cur.execute("""
            INSERT INTO xp_events (fid, type, xp, status, meta, unique_key)
            VALUES (%s, 'referral_bonus', %s, 'COMPLETED', %s, %s)
            ON CONFLICT (unique_key) DO NOTHING
            RETURNING id
        """, (referrer_fid, bonus_xp, json.dumps({"from_fid": referee_fid, "source_event_id": source_event_id}), ref_unique))
        bonus_row = cur.fetchone()
        if bonus_row:
            cur.execute("""
                INSERT INTO referral_earnings (referrer_fid, referee_fid, source_event_id, xp_share)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (referrer_fid, referee_fid, source_event_id, bonus_xp))
            print(f"✅ [XP] Referral bonus {bonus_xp} XP to referrer fid={referrer_fid} from fid={referee_fid}")
    except Exception as e:
        print(f"⚠️ [XP] Referral bonus error: {e}")


def hash_invite_code(code: str) -> str:
    """Hash an invite code using HMAC-SHA256"""
    return hmac.new(
        PMFI_HMAC_SECRET.encode(),
        code.lower().strip().encode(),
        hashlib.sha256
    ).hexdigest()

def generate_invite_code() -> str:
    """Generate a random 8-character invite code"""
    return secrets.token_hex(4).upper()

def check_invite_rate_limit(ip: str) -> bool:
    """Check if IP is rate limited for invite operations"""
    now = time.time()
    if ip in invite_rate_limit_store:
        attempts, window_start = invite_rate_limit_store[ip]
        if now - window_start > INVITE_RATE_LIMIT_WINDOW:
            invite_rate_limit_store[ip] = (1, now)
            return True
        if attempts >= INVITE_RATE_LIMIT_MAX:
            return False
        invite_rate_limit_store[ip] = (attempts + 1, window_start)
    else:
        invite_rate_limit_store[ip] = (1, now)
    return True

def log_invite_action(action: str, code_hash: str = None, wallet: str = None, ip: str = None, success: bool = True, error: str = None):
    """Log invite code action to audit table"""
    try:
        conn = get_invite_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO invite_audit_logs (action, code_hash, wallet_address, ip_address, success, error_message)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (action, code_hash, wallet, ip, success, error))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Failed to log invite action: {e}")

# =============================================================================
# Liquidation Reservation System (for withdrawal servicer)
# =============================================================================
import uuid

RESERVATION_TTL_SECONDS = 120  # Reservations expire after 2 minutes
MAX_SLIPPAGE_BPS = 100  # 1% max slippage by default
MIN_LIQUIDATION_USDC = 0.10  # Don't bother with dust

# In-memory reservation store: {reservation_id: {token_id, size, expires_at, limit_price}}
liquidation_reservations = {}
reservation_lock = threading.Lock()

# =============================================================================
# RoundId Cache (prevents fallback to 0 on RPC errors)
# =============================================================================
class RoundIdCache:
    """
    Persists lastRoundId to disk to prevent falling back to 0 on RPC errors.
    
    Rules:
    1. On startup: try chain first, fall back to disk cache
    2. On RPC error during refresh: use cached value + 1 (ONLY ONCE per RPC outage)
    3. If both chain and cache fail: refuse to sign (return error)
    4. Chain value is authoritative - always sync to it on successful reads
    
    Key insight: We track 'fallback_used' to prevent runaway increment during outages.
    Once we've used fallback once, we don't increment again until RPC recovers.
    """
    
    def __init__(self, cache_file: str = "round_id_cache.json"):
        self.cache_file = Path(cache_file)
        self.cached_round_id: Optional[int] = None
        self.last_chain_read: int = 0  # Timestamp of last successful chain read
        self.fallback_used: bool = False  # True if we already incremented during this outage
        self._load_from_disk()
    
    def _load_from_disk(self):
        """Load cached roundId from disk on startup."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                self.cached_round_id = data.get("last_round_id")
                self.last_chain_read = data.get("last_chain_read", 0)
                print(f"📂 Loaded roundId cache from disk: lastRoundId={self.cached_round_id}")
            except Exception as e:
                print(f"⚠️ Error loading roundId cache: {e}")
    
    def _save_to_disk(self):
        """Persist roundId to disk."""
        try:
            data = {
                "last_round_id": self.cached_round_id,
                "last_chain_read": self.last_chain_read,
                "last_updated": int(time.time())
            }
            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"⚠️ Error saving roundId cache: {e}")
    
    def update_from_chain(self, round_id: int):
        """Update cache with value read from chain (authoritative source)."""
        if round_id >= 0:
            # Chain is authoritative - ALWAYS sync to it (fixes drift issue)
            if self.cached_round_id != round_id:
                if self.cached_round_id is not None and self.cached_round_id > round_id:
                    print(f"🔄 Resync: cache was ahead ({self.cached_round_id}) vs chain ({round_id}) - using chain value")
                self.cached_round_id = round_id
            self.last_chain_read = int(time.time())
            self.fallback_used = False  # Reset fallback flag - RPC is working
            self._save_to_disk()
            print(f"📝 RoundId cache synced from chain: {round_id}")
    
    def get_next_round_id(self, chain_round_id: Optional[int] = None) -> Optional[int]:
        """
        Get next roundId to use for signing.
        
        Args:
            chain_round_id: Value read from chain (None if RPC failed)
            
        Returns:
            Next roundId to use, or None if we can't determine a safe value
        """
        if chain_round_id is not None and chain_round_id >= 0:
            # Chain read succeeded - this is authoritative (0 is valid for fresh contracts)
            self.update_from_chain(chain_round_id)
            return chain_round_id + 1
        
        # Chain read failed - use cache if available
        if self.cached_round_id is not None and self.cached_round_id > 0:
            if self.fallback_used:
                # We already incremented once during this outage - return same value
                # This prevents runaway drift during prolonged outages
                next_id = self.cached_round_id + 1
                print(f"⚠️ RPC still failing, reusing fallback roundId: {next_id}")
                return next_id
            else:
                # First fallback during this outage - increment once
                next_id = self.cached_round_id + 1
                self.fallback_used = True
                print(f"⚠️ RPC failed, using cached roundId + 1: {next_id} (fallback mode)")
                return next_id
        
        # No valid source - refuse to sign
        print(f"❌ Cannot determine safe roundId: chain read failed and no cache available")
        return None


# =============================================================================
# Persistent State (for pendingCredit tracking)
# =============================================================================
@dataclass
class DepositRecord:
    amount_usdc: int  # 6 decimals
    timestamp: int
    tx_hash: str

class PendingCreditTracker:
    """
    Tracks forwarded deposits and reconciles with PM cash balance.
    
    pendingCredit = totalForwarded - pmCash - withdrawnBack
    
    Uses cash-only reconciliation to avoid market PnL affecting pending tracking.
    """
    
    def __init__(self, state_file: str = "pending_credit_state.json"):
        self.state_file = Path(state_file)
        self.total_forwarded: int = 0  # In 6 decimals
        self.withdrawn_back: int = 0   # USDC returned from PM to vault
        self.deposit_records: List[DepositRecord] = []
        self.last_known_pm_cash: int = 0
        self.load_state()
    
    def load_state(self):
        """Load persisted state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                self.total_forwarded = data.get("total_forwarded", 0)
                self.withdrawn_back = data.get("withdrawn_back", 0)
                self.deposit_records = [
                    DepositRecord(**r) for r in data.get("deposit_records", [])
                ]
                print(f"📂 Loaded pending credit state: forwarded={self.total_forwarded/1e6:.2f}, withdrawn={self.withdrawn_back/1e6:.2f}")
            except Exception as e:
                print(f"⚠️ Error loading state: {e}")
    
    def save_state(self):
        """Persist state to file."""
        try:
            data = {
                "total_forwarded": self.total_forwarded,
                "withdrawn_back": self.withdrawn_back,
                "deposit_records": [
                    {"amount_usdc": r.amount_usdc, "timestamp": r.timestamp, "tx_hash": r.tx_hash}
                    for r in self.deposit_records
                ],
                "last_updated": int(time.time())
            }
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"⚠️ Error saving state: {e}")
    
    def record_deposit(self, amount_usdc: int, tx_hash: str):
        """Record a new deposit forwarded to Polymarket."""
        self.total_forwarded += amount_usdc
        self.deposit_records.append(DepositRecord(
            amount_usdc=amount_usdc,
            timestamp=int(time.time()),
            tx_hash=tx_hash
        ))
        self.save_state()
        print(f"📝 Recorded deposit: {amount_usdc/1e6:.2f} USDC (total forwarded: {self.total_forwarded/1e6:.2f})")
    
    def record_withdrawal_back(self, amount_usdc: int):
        """Record USDC returned from Polymarket to vault."""
        self.withdrawn_back += amount_usdc
        self.save_state()
        print(f"📝 Recorded withdrawal back: {amount_usdc/1e6:.2f} USDC")
    
    def calculate_pending_credit(self, pm_cash_usdc: int, reserved_usdc: int = 0, positions_liq_value_usdc: int = 0, in_flight_usdc: int = 0, vault_buffer_usdc: int = 0, expected_assets_usdc: int = 0) -> int:
        """
        Calculate pending credit using full asset reconciliation.
        
        FIXED FORMULA (V7.3.3):
        pendingCredit = max(0, expectedAssets - pmCash - positionsLiqValue - inFlight - vaultBuffer)
        NOTE: Reserved is NOT subtracted - Polygon balanceOf already represents total on-chain cash.
        
        V7.3.2 FIX:
        1. Use on-chain expectedAssets instead of cumulative totalForwarded.
           expectedAssets correctly decreases when users claim funds, while totalForwarded
           is cumulative and never decreases - causing massive pending credit inflation after claims.
        
        2. Use position LIQUIDATION VALUE (mark-to-market) instead of cost basis.
           This keeps all buckets on the same "as-of-now" basis:
           - When recordTradingGain($10) is called, expectedAssets goes up by $10
           - But positionsLiqValue also reflects that gain (market price increased)
           - Net effect on pending = $0 (no phantom pending)
           
           With cost basis, only expectedAssets would change, creating phantom pending.
        
        KEY INVARIANT (should hold approximately):
        expectedAssets ≈ pmCash + positionsValue + vaultBuffer + inFlight + pending
        NOTE: Reserved is excluded - it's tracked for monitoring only, not NAV math.
        
        This prevents double-counting:
        - When you buy tokens, cash goes down but positionsValue goes up (net zero change to pending)
        - Open orders don't affect on-chain cash (Polygon balanceOf stays the same until order fills)
        - Funds at deposit address are counted in inFlight, NOT pendingCredit
        - Funds in vault buffer are on-chain, NOT pendingCredit
        - When users CLAIM, expectedAssets decreases, keeping pending accurate
        - pendingCredit should only be non-zero when funds are swept but not yet visible in PM
        
        BUCKET INVARIANT: At any moment, system assets live in exactly ONE of:
        - inFlight (still at deposit address on Base)
        - pendingCredit (swept/bridging, not visible yet in PM)
        - cash/reserved/positionsValue (credited and inside PM account)
        - vaultBuffer (on-chain in vault, claimable)
        
        Args:
            pm_cash_usdc: Polymarket cash balance (in 6 decimals)
            reserved_usdc: USDC locked in open buy orders (in 6 decimals)
            positions_liq_value_usdc: Liquidation value of positions (in 6 decimals) - NOT cost basis
            in_flight_usdc: USDC at deposit address (in 6 decimals)
            vault_buffer_usdc: USDC in vault buffer on-chain (in 6 decimals)
            expected_assets_usdc: Contract's expectedAssets (in 6 decimals) - V7.3.2 fix
        """
        self.last_known_pm_cash = pm_cash_usdc
        
        # Total "accounted for" = cash + positionsLiqValue + inFlight + vaultBuffer
        # NOTE: Uses liquidation value (mark-to-market), NOT cost basis
        # NOTE: Reserved is EXCLUDED - Polygon balanceOf is the source of truth for cash
        accounted_for = pm_cash_usdc + positions_liq_value_usdc + in_flight_usdc + vault_buffer_usdc
        
        # Pending = expectedAssets minus what's accounted for
        # V7.3.2: Use expectedAssets (decreases on claims) instead of totalForwarded (cumulative)
        raw_pending = expected_assets_usdc - accounted_for
        
        # Guard: Clamp negative pending to 0 with warning (indicates bucket overlap or stale reads)
        if raw_pending < 0:
            print(f"   ⚠️ NEGATIVE PENDING WARNING: raw={raw_pending/1e6:.2f} (clamped to 0)")
            print(f"      This usually means positions gained value beyond expectedAssets or bucket overlap")
            print(f"      Buckets: cash={pm_cash_usdc/1e6:.2f}, pos={positions_liq_value_usdc/1e6:.2f}, reserved={reserved_usdc/1e6:.2f}, inFlight={in_flight_usdc/1e6:.2f}, buffer={vault_buffer_usdc/1e6:.2f}")
            pending = 0
        else:
            pending = raw_pending
        
        print(f"📊 Pending Credit Calculation (V7.3.2 - expectedAssets + liquidation value):")
        print(f"   expectedAssets:   ${expected_assets_usdc/1e6:.2f}")
        print(f"   - pmCash:         ${pm_cash_usdc/1e6:.2f}")
        print(f"   - reserved:       ${reserved_usdc/1e6:.2f}")
        print(f"   - positionsLiq:   ${positions_liq_value_usdc/1e6:.2f}")
        print(f"   - inFlight:       ${in_flight_usdc/1e6:.2f}")
        print(f"   - vaultBuffer:    ${vault_buffer_usdc/1e6:.2f}")
        print(f"   = pendingCredit:  ${pending/1e6:.2f}")
        
        # INVARIANT CHECK: Sum of all buckets should equal expectedAssets (within rounding)
        total_buckets = pending + accounted_for
        bucket_diff = abs(total_buckets - expected_assets_usdc)
        if bucket_diff > 1000:  # Allow $0.001 rounding tolerance
            print(f"   ⚠️ BUCKET INVARIANT WARNING: buckets={total_buckets/1e6:.2f} != expectedAssets={expected_assets_usdc/1e6:.2f}")
        
        return pending
    
    def get_oldest_pending_age_hours(self, pending_credit: int) -> float:
        """Get age of oldest unreconciled deposit in hours.
        
        Args:
            pending_credit: Pre-calculated pending credit value
        """
        if not self.deposit_records:
            return 0.0
        
        # If nothing is pending, no deposits are waiting
        if pending_credit <= 0:
            return 0.0
        
        # Return age of oldest deposit
        oldest = min(r.timestamp for r in self.deposit_records)
        age_seconds = int(time.time()) - oldest
        return age_seconds / 3600.0
    
    def sync_from_contract(self, total_forwarded_from_contract: int):
        """Sync total forwarded from contract state (in case of drift)."""
        if total_forwarded_from_contract > self.total_forwarded:
            diff = total_forwarded_from_contract - self.total_forwarded
            print(f"🔄 Syncing forwarded from contract: +{diff/1e6:.2f} USDC")
            self.total_forwarded = total_forwarded_from_contract
            self.save_state()


# =============================================================================
# Global State
# =============================================================================
w3 = None
usdc = None
vault_v7 = None
polymarket_client = None
nav_engine = None
oracle_account = None
pending_tracker = None
round_id_cache = None  # RoundId cache for RPC failure recovery

cached_nav = {
    "total_assets": 0,
    "credited_cash": 0,
    "credited_positions": 0,
    "pending_credit": 0,
    "in_flight": 0,
    "reserved": 0,         # V7.1: Cash locked in open buy orders
    "cost_basis": 0,       # V7.1: Total cost basis of positions
    "nav": 10**6,
    "round_id": 0,
    "last_calculated": 0,
    "total_supply": 0,
    "safety_status": "ok",
}
cached_signed_nav = None  # Cached signed NAV response for instant returns
nav_lock = threading.Lock()

_script_dir = Path(__file__).resolve().parent
FRONTEND_DIR = _script_dir / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = _script_dir.parent / "frontend"

flask_app = Flask(__name__, template_folder=str(FRONTEND_DIR))
CORS(flask_app)

VAULT_ADDRESS_CONFIG = os.getenv('VAULT_V7_ADDRESS', '0x17C27001929E75D1eBd5FdeE6E986EA5a91de0D1')

@flask_app.route('/')
def serve_index():
    user_agent = flask_request.headers.get('User-Agent', '').lower()
    fc_frame = flask_request.headers.get('Sec-Fetch-Dest', '')
    
    if 'farcaster' in user_agent or 'warpcast' in user_agent or fc_frame == 'iframe':
        return render_template('mini.html', vault_address=VAULT_ADDRESS_CONFIG)
    return render_template('index.html', vault_address=VAULT_ADDRESS_CONFIG)

@flask_app.route('/mini')
def serve_mini():
    """Serve the Farcaster Mini App version with config from env"""
    return render_template('mini.html', vault_address=VAULT_ADDRESS_CONFIG)

@flask_app.route('/share')
def serve_share():
    """Serve share page with Farcaster Mini App meta tags for cast embeds"""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PMFI - DeFi Layer for Prediction Markets</title>
    <meta property="fc:miniapp" content='{"version":"1","imageUrl":"https://app.pmfi.cc/hero.png","button":{"title":"🏦 Start PMFI","action":{"type":"launch_miniapp","name":"PMFI","url":"https://app.pmfi.cc/mini"}}}' />
    <meta property="fc:frame" content='{"version":"1","imageUrl":"https://app.pmfi.cc/hero.png","button":{"title":"🏦 Start PMFI","action":{"type":"launch_miniapp","name":"PMFI","url":"https://app.pmfi.cc/mini"}}}' />
    <meta property="og:title" content="PMFI - DeFi Layer for Prediction Markets" />
    <meta property="og:description" content="Automated Polymarket sniping vault on Base. Turn uncertainty into returns." />
    <meta property="og:image" content="https://app.pmfi.cc/hero.png" />
    <meta property="og:url" content="https://app.pmfi.cc/share" />
    <meta property="og:type" content="website" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="PMFI - DeFi Layer for Prediction Markets" />
    <meta name="twitter:description" content="Automated Polymarket sniping vault on Base. Turn uncertainty into returns." />
    <meta name="twitter:image" content="https://app.pmfi.cc/hero.png" />
</head>
<body style="background:#0d1117;color:#f0f6fc;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;">
    <div style="text-align:center;max-width:400px;padding:40px;">
        <img src="/icon.png" alt="PMFI" style="width:80px;height:80px;border-radius:50%;margin-bottom:24px;">
        <h1 style="font-size:28px;margin-bottom:12px;">PMFI</h1>
        <p style="color:#8b949e;margin-bottom:24px;">DeFi Layer for Prediction Markets</p>
        <a href="/mini" style="display:inline-block;padding:12px 32px;background:#f0f6fc;color:#0d1117;border-radius:10px;text-decoration:none;font-weight:600;">Launch App</a>
    </div>
</body>
</html>"""

@flask_app.route('/.well-known/farcaster.json')
def serve_farcaster_manifest():
    """Redirect to Farcaster hosted manifest (307 temporary redirect)"""
    return redirect('https://api.farcaster.xyz/miniapps/hosted-manifest/019c43b9-e950-d301-9909-9c3124fb01e8', code=307)

@flask_app.route('/icon.png')
def serve_icon():
    return send_from_directory(FRONTEND_DIR, 'icon.png')

@flask_app.route('/<path:filename>')
def serve_static(filename):
    if filename.startswith('.well-known/'):
        return send_from_directory(FRONTEND_DIR, filename)
    return send_from_directory(FRONTEND_DIR, filename)


# =============================================================================
# Polymarket Client
# =============================================================================

class PolymarketClient:
    """Client for fetching positions, orderbook data, and open orders (with L2 auth)."""
    
    DATA_API_URL = "https://data-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    def __init__(self, wallet_address: str, private_key: str = None):
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.clob_client = None
        self.use_proxy = False
        
        # Try to use curl_cffi with residential proxy for Cloudflare bypass
        if BYPASS_METHOD == "curl_cffi" and curl_requests and PROXY_URL:
            self.use_proxy = True
            proxy_display = PROXY_URL.split('@')[1] if '@' in PROXY_URL else PROXY_URL
            print(f"🌐 PolymarketClient using residential proxy: {proxy_display}")
        elif BYPASS_METHOD == "curl_cffi" and curl_requests:
            print("🔓 PolymarketClient using curl_cffi (no proxy)")
        else:
            print("⚠️ PolymarketClient using standard requests (may be blocked)")
        
        # Fallback session for non-proxied requests
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        
        # Initialize CLOB client with L2 auth for fetching open orders
        if private_key:
            self._init_clob_client()
    
    def _make_request(self, url: str, params: dict = None, timeout: int = 30):
        """Make HTTP request with Cloudflare bypass if available."""
        if BYPASS_METHOD == "curl_cffi" and curl_requests:
            proxies = {"http": PROXY_URL, "https": PROXY_URL} if self.use_proxy and PROXY_URL else None
            response = curl_requests.get(
                url,
                params=params,
                timeout=timeout,
                impersonate="chrome120",
                proxies=proxies
            )
            response.raise_for_status()
            return response.json()
        else:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
    
    def _init_clob_client(self):
        """Initialize CLOB client with L2 auth for authenticated API calls."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            
            # Store these for later use
            self.BalanceAllowanceParams = BalanceAllowanceParams
            self.AssetType = AssetType
            
            # Initialize client with private key and proxy wallet
            self.clob_client = ClobClient(
                self.CLOB_API_URL,
                key=self.private_key,
                chain_id=137,  # Polygon for L2 auth
                signature_type=1,  # Email/Magic wallet style
                funder=self.wallet_address
            )
            
            # Derive L2 credentials from private key
            self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_creds())
            print("✅ CLOB client initialized with L2 auth (can fetch open orders and balance)")
        except Exception as e:
            print(f"⚠️ Could not initialize CLOB client: {e}")
            self.clob_client = None
            self.BalanceAllowanceParams = None
            self.AssetType = None
    
    def fetch_positions_with_cost_basis(self) -> Tuple[List[Dict], float]:
        """
        Fetch ALL open positions with cost basis (initialValue).
        
        Returns:
            Tuple of (positions list, total cost basis in USDC)
        """
        try:
            print(f"📡 Fetching positions with cost basis for {self.wallet_address[:10]}...")
            
            all_positions = []
            offset = 0
            limit = 500
            total_cost_basis = 0.0
            
            while True:
                url = f"{self.DATA_API_URL}/positions"
                params = {"user": self.wallet_address, "limit": limit, "offset": offset}
                
                data = self._make_request(url, params=params, timeout=30)
                if not data:
                    break
                    
                all_positions.extend(data)
                
                if len(data) < limit:
                    break
                offset += limit
            
            positions = []
            for p in all_positions:
                size = float(p.get("size") or 0)
                if size <= 0:
                    continue
                
                initial_value = float(p.get("initialValue") or 0)
                total_cost_basis += initial_value
                
                positions.append({
                    "token_id": p.get("asset") or "",
                    "title": p.get("title") or "",
                    "outcome": p.get("outcome") or "",
                    "size": size,
                    "current_value": float(p.get("currentValue") or 0),
                    "initial_value": initial_value,  # Cost basis for this position
                    "avg_price": float(p.get("avgPrice") or 0),
                })
            
            print(f"✅ Found {len(positions)} active positions, total cost basis: ${total_cost_basis:.2f}")
            return positions, total_cost_basis
            
        except Exception as e:
            print(f"❌ Error fetching positions: {e}")
            return [], 0.0
    
    def fetch_open_orders(self) -> List[Dict]:
        """
        Fetch all open orders (requires L2 auth).
        
        Returns:
            List of open orders
        """
        if not self.clob_client:
            print("⚠️ CLOB client not initialized, cannot fetch open orders (reserved=0)")
            return []
        
        try:
            print("📡 Fetching open orders via L2 auth...")
            orders = self.clob_client.get_orders()
            
            if orders is None:
                print("⚠️ CLOB client returned None for orders (auth may have failed)")
                return []
            
            # Filter for open orders - API uses "OPEN" or "PARTIALLY_FILLED" status
            open_orders = [o for o in orders if o.get("status") in ("OPEN", "PARTIALLY_FILLED", "LIVE")]
            print(f"✅ Found {len(open_orders)} open orders (total returned: {len(orders)})")
            
            # Log warning if all orders were filtered out
            if len(orders) > 0 and len(open_orders) == 0:
                statuses = set(o.get("status") for o in orders)
                print(f"⚠️ All orders filtered out. Statuses found: {statuses}")
            
            return open_orders
            
        except Exception as e:
            print(f"❌ Error fetching open orders: {e}")
            print("⚠️ Reserved balance will be 0 - this may cause pendingCredit to spike when orders are placed")
            return []
    
    def calculate_reserved_balance(self) -> float:
        """
        Calculate USDC reserved in open buy orders.
        
        Reserved = sum of (unfilled amount × price) for all open buy orders
        
        Returns:
            Reserved balance in USDC
        """
        open_orders = self.fetch_open_orders()
        
        reserved = 0.0
        for order in open_orders:
            side = order.get("side", "").upper()
            if side != "BUY":
                continue
            
            # Calculate unfilled amount
            original_size = float(order.get("original_size") or order.get("size") or 0)
            filled_size = float(order.get("size_matched") or 0)
            unfilled = original_size - filled_size
            
            if unfilled <= 0:
                continue
            
            price = float(order.get("price") or 0)
            reserved += unfilled * price
        
        print(f"🔒 Reserved in open buy orders: ${reserved:.2f}")
        return reserved
    
    def fetch_positions(self) -> List[Dict]:
        """Fetch ALL open positions with pagination."""
        try:
            print(f"📡 Fetching positions for {self.wallet_address[:10]}...")
            
            all_positions = []
            offset = 0
            limit = 500
            
            while True:
                url = f"{self.DATA_API_URL}/positions"
                params = {"user": self.wallet_address, "limit": limit, "offset": offset}
                
                data = self._make_request(url, params=params, timeout=30)
                if not data:
                    break
                    
                all_positions.extend(data)
                
                if len(data) < limit:
                    break
                offset += limit
            
            positions = []
            for p in all_positions:
                size = float(p.get("size") or 0)
                if size <= 0:
                    continue
                
                positions.append({
                    "token_id": p.get("asset") or "",
                    "title": p.get("title") or "",
                    "outcome": p.get("outcome") or "",
                    "size": size,
                    "current_value": float(p.get("currentValue") or 0),
                })
            
            print(f"✅ Found {len(positions)} active positions")
            return positions
            
        except Exception as e:
            print(f"❌ Error fetching positions: {e}")
            return []
    
    def fetch_orderbook(self, token_id: str) -> Dict:
        """
        Fetch orderbook for a token.
        
        Tries CLOB client native method first (more reliable), 
        then falls back to HTTP if unavailable.
        """
        token_preview = token_id[:20] + "..." if len(token_id) > 20 else token_id
        
        # Method 1: Use CLOB client's native get_order_book (preferred)
        if self.clob_client:
            try:
                print(f"   📖 Orderbook via CLOB client: {token_preview}")
                data = self.clob_client.get_order_book(token_id)
                
                # Handle both dict and OrderBookSummary object responses
                if hasattr(data, 'bids') and hasattr(data, 'asks'):
                    # OrderBookSummary object - access attributes directly
                    raw_bids = data.bids if data.bids else []
                    raw_asks = data.asks if data.asks else []
                else:
                    # Dict response
                    raw_bids = data.get("bids", []) if isinstance(data, dict) else []
                    raw_asks = data.get("asks", []) if isinstance(data, dict) else []
                
                # Parse bid/ask entries (could be OrderBookLevel objects or dicts)
                bids = []
                for b in raw_bids:
                    if hasattr(b, 'price') and hasattr(b, 'size'):
                        bids.append({"price": float(b.price), "size": float(b.size)})
                    elif isinstance(b, dict):
                        bids.append({"price": float(b.get("price", 0)), "size": float(b.get("size", 0))})
                
                asks = []
                for a in raw_asks:
                    if hasattr(a, 'price') and hasattr(a, 'size'):
                        asks.append({"price": float(a.price), "size": float(a.size)})
                    elif isinstance(a, dict):
                        asks.append({"price": float(a.get("price", 0)), "size": float(a.get("size", 0))})
                
                bids.sort(key=lambda x: x["price"], reverse=True)
                asks.sort(key=lambda x: x["price"])
                
                print(f"   📊 Got {len(bids)} bids, {len(asks)} asks")
                return {"bids": bids, "asks": asks}
                
            except Exception as e:
                error_msg = str(e)
                if "404" in error_msg:
                    print(f"   ❌ Orderbook 404: token_id may be wrong format or market resolved")
                    print(f"      Full token: {token_id}")
                    return {"bids": [], "asks": []}
                elif "429" in error_msg or "rate" in error_msg.lower():
                    print(f"   ⚠️  Orderbook rate limited, skipping")
                    return {"bids": [], "asks": []}
                else:
                    print(f"   ⚠️  CLOB client orderbook error: {e}, trying HTTP fallback...")
        
        # Method 2: HTTP fallback (may be blocked by Cloudflare without proxy)
        try:
            url = f"{self.CLOB_API_URL}/book"
            params = {"token_id": token_id}
            print(f"   📖 Orderbook via HTTP: {token_preview}")
            data = self._make_request(url, params=params, timeout=10)
            bids = [{"price": float(b.get("price", 0)), "size": float(b.get("size", 0))} for b in data.get("bids", [])]
            asks = [{"price": float(a.get("price", 0)), "size": float(a.get("size", 0))} for a in data.get("asks", [])]
            
            bids.sort(key=lambda x: x["price"], reverse=True)
            asks.sort(key=lambda x: x["price"])
            
            print(f"   📊 Got {len(bids)} bids, {len(asks)} asks")
            return {"bids": bids, "asks": asks}
            
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg:
                print(f"   ❌ Orderbook 404: token_id may be wrong format or market resolved")
                print(f"      Full token: {token_id}")
            else:
                print(f"❌ Error fetching orderbook: {e}")
            return {"bids": [], "asks": []}
    
    def simulate_market_sell(self, size: float, bids: List[Dict]) -> float:
        """Simulate market sell through orderbook depth."""
        if not bids:
            return 0.0
        
        remaining = size
        total_value = 0.0
        
        for bid in bids:
            fill_amount = min(remaining, bid["size"])
            total_value += fill_amount * bid["price"]
            remaining -= fill_amount
            if remaining <= 0:
                break
        
        return total_value
    
    def fetch_cash_balance(self) -> float:
        """Fetch USDC cash balance on Polymarket. 
        
        V7.3.3: Uses Polygon RPC as PRIMARY source (consistent on-chain truth).
        CLOB API is skipped because it may return different semantics (available vs total).
        
        Tries methods:
        1. Direct Polygon blockchain query for USDC.e balance (PRIMARY)
        2. Data API fallback (may 404)
        """
        # V7.3.3: Skip CLOB API - go straight to Polygon RPC for consistency
        # CLOB balance may differ from on-chain (available vs total), causing NAV jumps
        
        # Method 1 (PRIMARY): Direct Polygon blockchain query for USDC.e balance
        try:
            print("📡 Fetching cash balance via Polygon RPC...")
            polygon_w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
            if polygon_w3.is_connected():
                # Simple ERC20 balanceOf ABI
                erc20_abi = [{"inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}]
                usdc_e = polygon_w3.eth.contract(
                    address=Web3.to_checksum_address(USDC_E_POLYGON),
                    abi=erc20_abi
                )
                balance_raw = usdc_e.functions.balanceOf(Web3.to_checksum_address(self.wallet_address)).call()
                cash = balance_raw / 1e6  # USDC.e has 6 decimals
                if cash > 0:
                    print(f"💵 Polymarket cash (Polygon): ${cash:.2f}")
                    return cash
                else:
                    print(f"💵 Polymarket cash (Polygon): ${cash:.2f}")
        except Exception as e:
            print(f"⚠️ Polygon balance fetch failed: {e}")
        
        # Method 3: Data API fallback (may 404)
        try:
            url = f"{self.DATA_API_URL}/balance"
            params = {"user": self.wallet_address}
            data = self._make_request(url, params=params, timeout=10)
            cash = float(data.get("balance", 0)) if data else 0
            print(f"💵 Polymarket cash (data API): ${cash:.2f}")
            return cash
            
        except Exception as e:
            print(f"❌ Error fetching cash: {e}")
            return 0.0


# =============================================================================
# NAV Engine V7
# =============================================================================

class NavEngineV7:
    """
    Calculates NAV with 3-state asset tracking (V7.3.2 - no double-counting).
    
    totalAssets = inFlightOnChain + pendingCredit + cash + positionsLiquidationValue
    NOTE: Reserved is excluded - Polygon balanceOf is the source of truth for cash.
    
    BUCKET INVARIANT: Funds can only be in ONE bucket at a time:
    - inFlight: at deposit address on Base
    - pendingCredit: swept/bridging, not visible yet in PM
    - cash/reserved/costBasis: credited inside PM
    - vaultBuffer: returned to vault (on-chain truth)
    
    V7.3.3 FIX: Uses expectedAssets (decreases on claims) instead of totalForwarded (cumulative).
    pendingCredit = expectedAssets - cash - positionsLiqValue - inFlight - vaultBuffer
    NOTE: Reserved is excluded from NAV math - Polygon balanceOf is the source of truth for cash.
    """
    
    def __init__(self, polymarket_client: PolymarketClient, pending_tracker: PendingCreditTracker):
        self.polymarket_client = polymarket_client
        self.pending_tracker = pending_tracker
    
    def fetch_in_flight_balance(self) -> int:
        """Get USDC balance at Polymarket deposit address (usually ~0 after sweep)."""
        global w3
        try:
            if not w3 or not usdc:
                return 0
            
            balance = usdc.functions.balanceOf(Web3.to_checksum_address(POLYMARKET_BASE_DEPOSIT)).call()
            print(f"✈️ In-flight (at deposit addr): ${balance/1e6:.2f}")
            return balance
        except Exception as e:
            print(f"❌ Error fetching in-flight balance: {e}")
            return 0
    
    def calculate_nav_breakdown(self, vault_buffer: int = 0, expected_assets: int = 0) -> Dict:
        """
        Calculate full NAV breakdown with 3 asset states.
        
        V7.3.3 FIX: Uses expectedAssets (decreases on claims) instead of totalForwarded.
        Reserved is excluded from NAV math - Polygon balanceOf is the source of truth for cash.
        
        pendingCredit = expectedAssets - cash - positionsLiqValue - inFlight - vaultBuffer
        totalAssets = inFlight + pendingCredit + cash + liquidationValue
        
        BUCKET INVARIANT: Funds live in exactly ONE bucket at any time - no overlap.
        
        Args:
            vault_buffer: USDC balance in vault on Base (on-chain, in 6 decimals)
            expected_assets: Contract's expectedAssets (on-chain, in 6 decimals) - V7.3.2 fix
        
        Returns:
            Dict with all asset components in 6 decimals
        """
        print(f"\n{'='*60}")
        print(f"💰 CALCULATING V7.3.2 NAV (expectedAssets from chain)")
        print(f"{'='*60}")
        
        # 1. Fetch in-flight (at deposit address)
        in_flight = self.fetch_in_flight_balance()
        
        # 2. Fetch positions WITH cost basis
        positions, total_cost_basis = self.polymarket_client.fetch_positions_with_cost_basis()
        
        # 3. Calculate liquidation value of positions (for NAV display)
        positions_liq_value = 0.0
        for pos in positions:
            token_id = pos.get("token_id", "")
            size = pos.get("size", 0)
            mid_value = pos.get("current_value", 0)
            outcome = pos.get("outcome", "?")
            
            if not token_id:
                print(f"   ⚠️  {outcome}: No token_id, using mid value ${mid_value:.2f}")
                positions_liq_value += mid_value
                continue
            
            # Debug: Show token_id format
            is_numeric = token_id.isdigit() or (token_id.startswith("0x") and len(token_id) > 40)
            token_preview = token_id[:25] + "..." if len(token_id) > 25 else token_id
            if not is_numeric and len(token_id) < 50:
                print(f"   ⚠️  {outcome}: token_id looks like market_id, not CTF token: {token_id}")
            
            orderbook = self.polymarket_client.fetch_orderbook(token_id)
            bids = orderbook.get("bids", [])
            
            if not bids:
                print(f"   ❌ {outcome}: {size:.1f} - NO BIDS (illiquid, valued at $0)")
                # V7.3.4 FIX: Positions with no bids are valued at $0 (conservative)
                # Previously used API mid_value which inflates NAV for unsellable positions
                continue
            
            liq_value = self.polymarket_client.simulate_market_sell(size, bids)
            print(f"   ✅ {outcome}: {size:.1f} → liq value ${liq_value:.2f}")
            positions_liq_value += liq_value
        
        # 4. Fetch cash balance
        pm_cash = self.polymarket_client.fetch_cash_balance()
        
        # 5. Fetch reserved balance (cash locked in open buy orders)
        reserved = self.polymarket_client.calculate_reserved_balance()
        
        # Convert to 6 decimals
        credited_cash = int(pm_cash * 1e6)
        reserved_usdc = int(reserved * 1e6)
        cost_basis_usdc = int(total_cost_basis * 1e6)
        credited_positions = int(positions_liq_value * 1e6)  # Liquidation value for NAV
        
        # 6. Calculate pending credit using FULL reconciliation (V7.3.2 fix)
        # pendingCredit = expectedAssets - cash - reserved - positionsLiqValue - inFlight - vaultBuffer
        # Uses expectedAssets (decreases on claims) instead of totalForwarded (cumulative)
        # Uses liquidation value (not cost basis) to keep all buckets on same mark-to-market basis
        pending_credit = self.pending_tracker.calculate_pending_credit(
            credited_cash, reserved_usdc, credited_positions, in_flight, vault_buffer, expected_assets
        )
        
        # 7. Total assets for NAV = ACTUAL LIQUID VALUE ONLY
        # V7.4 FIX: Don't include pendingCredit in NAV - it was masking trading losses
        # pendingCredit is kept for monitoring bridging delays but NOT included in NAV
        # This means NAV reflects actual position values, not expected deposits
        # NOTE: vault_buffer is NOT added here - it's already included in creditedCash at signing time
        # NOTE: reserved is EXCLUDED - Polygon balanceOf is the source of truth for cash
        total_assets = in_flight + credited_cash + credited_positions
        
        # Keep pendingCredit for monitoring but don't add to NAV
        # If pendingCredit is high, it indicates either bridging delay OR trading losses
        pending_for_monitoring = pending_credit
        
        print(f"\n📊 Asset Breakdown (V7.4 - ACTUAL LIQUID VALUE for NAV):")
        print(f"   • In-flight (deposit addr): ${in_flight/1e6:.2f}")
        print(f"   • Credited cash:            ${credited_cash/1e6:.2f}")
        print(f"   • Positions (liquidation):  ${credited_positions/1e6:.2f}")
        print(f"   ─────────────────────────────")
        print(f"   • TOTAL ASSETS (NAV):       ${total_assets/1e6:.2f}")
        print(f"   ─────────────────────────────")
        print(f"   • Reserved (open orders):   ${reserved_usdc/1e6:.2f}")
        print(f"   • Vault buffer (on-chain):  ${vault_buffer/1e6:.2f}")
        print(f"   • Expected assets (chain):  ${expected_assets/1e6:.2f}")
        print(f"   • Positions (cost basis):   ${cost_basis_usdc/1e6:.2f}")
        print(f"   • Pending (monitoring):     ${pending_for_monitoring/1e6:.2f}")
        print(f"   (V7.4: NAV = actual liquid value, pendingCredit excluded)")
        
        return {
            "in_flight": in_flight,
            "pending_credit": pending_credit,
            "credited_cash": credited_cash,
            "reserved": reserved_usdc,
            "cost_basis": cost_basis_usdc,
            "credited_positions": credited_positions,  # Liquidation value
            "total_assets": total_assets,
            "positions_count": len(positions),
        }


# =============================================================================
# NAV Signing V7
# =============================================================================

# Domain salt must match contract: keccak256("PredictFiSniperVaultV7.v1")
DOMAIN_SALT = Web3.keccak(text="PredictFiSniperVaultV7.v1")
CHAIN_ID = 8453  # Base mainnet

def create_nav_data_hash_v7(
    total_assets: int,
    credited_cash: int,
    credited_positions: int,
    pending_credit: int,
    in_flight: int,
    timestamp: int,
    deadline: int,
    round_id: int,
    vault_address: str,
    chain_id: int = CHAIN_ID
) -> bytes:
    """Create hash for NavDataV7 signing (includes chainId and domainSalt for domain separation)."""
    from eth_abi import encode
    
    # Updated typehash to include chainId and domainSalt
    NAV_TYPEHASH = Web3.keccak(text="NavDataV7(uint256 totalAssets,uint256 creditedCash,uint256 creditedPositions,uint256 pendingCredit,uint256 inFlightOnChain,uint256 timestamp,uint256 deadline,uint256 roundId,address vault,uint256 chainId,bytes32 domainSalt)")
    
    vault_addr_checksum = Web3.to_checksum_address(vault_address)
    
    encoded_data = encode(
        ['bytes32', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'address', 'uint256', 'bytes32'],
        [NAV_TYPEHASH, total_assets, credited_cash, credited_positions, pending_credit, in_flight, timestamp, deadline, round_id, vault_addr_checksum, chain_id, DOMAIN_SALT]
    )
    
    struct_hash = Web3.keccak(encoded_data)
    return struct_hash


def sign_nav_data_v7(
    total_assets: int,
    credited_cash: int,
    credited_positions: int,
    pending_credit: int,
    in_flight: int,
    timestamp: int,
    deadline: int,
    round_id: int,
    vault_address: str
) -> Tuple[str, str]:
    """Sign NavDataV7 with oracle private key."""
    global oracle_account
    
    struct_hash = create_nav_data_hash_v7(
        total_assets, credited_cash, credited_positions, pending_credit, in_flight,
        timestamp, deadline, round_id, vault_address
    )
    
    message = encode_defunct(primitive=struct_hash)
    signed = oracle_account.sign_message(message)
    
    return signed.signature.hex(), oracle_account.address


def check_safety_valves(breakdown: Dict, expected_assets: int = 0) -> Tuple[str, str]:
    """
    Check safety valves and return status.
    
    V7.5: Conservation bound REMOVED - contract no longer rejects NAV on losses.
    - in_flight age: pauses if deposits stuck in bridging too long
    - loss threshold: warns (informational only) if losses exceed MAX_LOSS_BPS
    
    Returns:
        Tuple of (status, reason)
        status: "ok", "warning", or "pause"
    """
    global pending_tracker
    
    in_flight = breakdown.get("in_flight", 0)
    total_assets = breakdown.get("total_assets", 0)
    
    # V7.5: Conservation bound REMOVED - just informational warning now
    # Contract no longer rejects NAV based on loss threshold
    if expected_assets > 0 and total_assets > 0:
        min_allowed = expected_assets * (10000 - MAX_LOSS_BPS) // 10000
        if total_assets < min_allowed:
            loss_pct = (1 - total_assets / expected_assets) * 100
            return "warning", f"Significant loss detected: {loss_pct:.1f}% (threshold: {MAX_LOSS_BPS/100:.0f}%). NAV reflects actual values."
    
    # V7.4: Check in-flight age (actual funds waiting to be swept/bridged)
    if in_flight > 0:
        oldest_age = pending_tracker.get_oldest_pending_age_hours(in_flight)
        if oldest_age > MAX_PENDING_AGE_HOURS:
            return "pause", f"In-flight funds pending {oldest_age:.1f} hours (max {MAX_PENDING_AGE_HOURS}h)"
        elif oldest_age > MAX_PENDING_AGE_HOURS * 0.8:
            return "warning", f"In-flight funds pending {oldest_age:.1f} hours"
    
    return "ok", ""


def get_signed_nav_data_v7(force_refresh: bool = False) -> Dict:
    """Get current NAV with full breakdown and fresh signature."""
    global cached_nav, cached_signed_nav, nav_engine, pending_tracker, vault_v7, w3, round_id_cache
    
    now = int(time.time())
    
    # Get vault state from contract
    chain_round_id = None  # Track if we got a valid roundId from chain
    rpc_failed = False
    try:
        if vault_v7:
            total_supply = vault_v7.functions.totalSupply().call()
            vault_buffer = usdc.functions.balanceOf(VAULT_V7_ADDRESS).call() if usdc else 0
            chain_round_id = vault_v7.functions.lastRoundId().call()
            total_forwarded = vault_v7.functions.totalForwardedToPolymarket().call()
            expected_assets = vault_v7.functions.expectedAssets().call()
            
            # Sync pending tracker with contract
            pending_tracker.sync_from_contract(total_forwarded)
            
            print(f"📊 On-chain: supply={total_supply/1e18:.4f}, buffer={vault_buffer/1e6:.2f}, forwarded={total_forwarded/1e6:.2f}, expected={expected_assets/1e6:.2f}, lastRoundId={chain_round_id}")
        else:
            total_supply = 0
            vault_buffer = 0
            expected_assets = 0
    except Exception as e:
        print(f"❌ Error reading vault state: {e}")
        rpc_failed = True
        total_supply = cached_nav.get("total_supply", 0)  # Use cached values
        vault_buffer = cached_nav.get("vault_buffer", 0)
        expected_assets = cached_nav.get("expected_assets", 0)
    
    # V7.5: Read pending withdrawal exclusions from on-chain queue
    pending_shares_excluded, total_usdc_locked = get_pending_withdrawal_exclusions(vault_v7)
    
    # V7.5.1: Read withdrawal bridge in-transit to prevent double-subtraction
    withdrawal_bridge_in_transit = get_withdrawal_bridge_in_transit()
    
    # Get next roundId using cache (handles RPC failures safely)
    new_round_id = round_id_cache.get_next_round_id(chain_round_id)
    if new_round_id is None:
        raise ValueError("Cannot sign NAV: RPC failed and no cached roundId available. Please restart bot with working RPC.")
    
    # Calculate NAV breakdown (V7.3.2: pass vault_buffer AND expected_assets)
    if nav_engine:
        breakdown = nav_engine.calculate_nav_breakdown(vault_buffer, expected_assets)
    else:
        breakdown = {
            "in_flight": 0,
            "pending_credit": 0,
            "credited_cash": 0,
            "credited_positions": 0,
            "reserved": 0,
            "cost_basis": 0,
            "total_assets": 0,
        }
    
    # Check safety valves (V7.4: checks in-flight age + conservation bound)
    safety_status, safety_reason = check_safety_valves(breakdown, expected_assets)
    if safety_status == "pause":
        print(f"🛑 SAFETY VALVE TRIGGERED: {safety_reason}")
    elif safety_status == "warning":
        print(f"⚠️ SAFETY WARNING: {safety_reason}")
    
    # ==========================================================================
    # V7.4 SIGNED BREAKDOWN: Single source of truth for all signed values
    # ==========================================================================
    # All values MUST be ints in 1e6 units (USDC decimals)
    # This pattern prevents mismatch bugs by building one dict that's used everywhere
    
    # Raw values from breakdown (already in 1e6 from calculate_nav_breakdown)
    credited_cash_raw = int(breakdown["credited_cash"])
    credited_positions_1e6 = int(breakdown["credited_positions"])
    in_flight_1e6 = int(breakdown["in_flight"])
    vault_buffer_1e6 = int(vault_buffer)  # Raw from ERC20.balanceOf()
    reserved_usdc = int(breakdown.get("reserved", 0))  # For logging only
    
    # V7.4: Build the signed breakdown (single source of truth)
    # - credited_cash includes vault_buffer (on Base) + PM cash (on Polygon)
    # - pending_credit = 0 (V7.4 rule: NAV reflects actual liquid value only)
    signed_breakdown = {
        "credited_cash": credited_cash_raw + vault_buffer_1e6,
        "credited_positions": credited_positions_1e6,
        "pending_credit": 0,  # V7.4: Always 0 - was masking trading losses
        "in_flight": in_flight_1e6,
    }
    signed_breakdown["total_assets"] = (
        signed_breakdown["credited_cash"]
        + signed_breakdown["credited_positions"]
        + signed_breakdown["pending_credit"]
        + signed_breakdown["in_flight"]
    )
    
    # Hard fail BEFORE signing if breakdown doesn't sum correctly
    # This mirrors the Solidity require() check exactly
    computed_sum = (
        signed_breakdown["credited_cash"]
        + signed_breakdown["credited_positions"]
        + signed_breakdown["pending_credit"]
        + signed_breakdown["in_flight"]
    )
    if computed_sum != signed_breakdown["total_assets"]:
        raise ValueError(f"Asset breakdown mismatch: {computed_sum} != {signed_breakdown['total_assets']}")
    
    # ==========================================================================
    # V7.5 NAV: Exclude pending withdrawals from price calculation
    # ==========================================================================
    # totalAssets on-chain stays the same (contract doesn't know about exclusion)
    # But the NAV (price per share) must reflect only economically active shares/assets
    #
    # effective_exclusion = max(0, usdcLocked - withdrawal_bridge_in_transit)
    # effective_assets = totalAssets - effective_exclusion
    # effective_supply = totalSupply - totalPendingShares (remove shares no longer active)
    # NAV = effective_assets / effective_supply
    #
    # V7.5.1: Reduce exclusion by amount already debited from Polygon cash (in-transit bridge).
    # Without this, funds are double-subtracted during bridge transit: once because cash left
    # Polymarket (totalAssets drops) and once via usdcLocked exclusion.
    
    effective_exclusion = max(0, total_usdc_locked - withdrawal_bridge_in_transit)
    effective_assets = signed_breakdown["total_assets"] - effective_exclusion
    effective_supply = total_supply - pending_shares_excluded
    
    if effective_assets < 0:
        print(f"⚠️ V7.5: effective_assets went negative ({effective_assets/1e6:.2f}), clamping to 0")
        effective_assets = 0
    
    if effective_supply > 0:
        nav = (effective_assets * NAV_PRECISION) // effective_supply
    elif total_supply > 0 and effective_supply <= 0:
        print(f"⚠️ V7.5: All shares are pending withdrawal, using raw totalAssets/totalSupply")
        nav = (signed_breakdown["total_assets"] * NAV_PRECISION) // total_supply
    else:
        nav = 10**6  # $1.00 per share if no supply
    
    timestamp = now
    deadline = now + NAV_VALIDITY_SECONDS
    
    # Log the signed breakdown for debugging
    print(f"📊 V7.5 Signed Breakdown (SINGLE SOURCE OF TRUTH):")
    print(f"   • PM Cash (raw):            ${credited_cash_raw/1e6:.2f}")
    print(f"   • Vault Buffer:             ${vault_buffer_1e6/1e6:.2f}")
    print(f"   • creditedCash (signed):    ${signed_breakdown['credited_cash']/1e6:.2f}")
    print(f"   • creditedPositions:        ${signed_breakdown['credited_positions']/1e6:.2f}")
    print(f"   • pendingCredit (V7.4=0):   ${signed_breakdown['pending_credit']/1e6:.2f}")
    print(f"   • inFlight:                 ${signed_breakdown['in_flight']/1e6:.2f}")
    print(f"   ─────────────────────────────")
    print(f"   • TOTAL ASSETS (gross):     ${signed_breakdown['total_assets']/1e6:.2f}")
    print(f"   • Reserved (not in NAV):    ${reserved_usdc/1e6:.2f}")
    print(f"   • Pending (monitoring):     ${breakdown['pending_credit']/1e6:.2f}")
    print(f"   ─────────────────────────────")
    print(f"   • 🔒 Withdrawal Exclusion (V7.5.1):")
    print(f"   •   Pending shares:         {pending_shares_excluded/1e18:.6f}")
    print(f"   •   USDC locked (total):    ${total_usdc_locked/1e6:.2f}")
    print(f"   •   Bridge in-transit:      ${withdrawal_bridge_in_transit/1e6:.2f}")
    print(f"   •   Effective exclusion:    ${effective_exclusion/1e6:.2f}")
    print(f"   •   Effective assets:       ${effective_assets/1e6:.2f}")
    print(f"   •   Effective supply:       {effective_supply/1e18:.6f}")
    print(f"   •   Total supply (raw):     {total_supply/1e18:.6f}")
    
    # Sign using ONLY signed_breakdown values
    signature, signer = sign_nav_data_v7(
        total_assets=signed_breakdown["total_assets"],
        credited_cash=signed_breakdown["credited_cash"],
        credited_positions=signed_breakdown["credited_positions"],
        pending_credit=signed_breakdown["pending_credit"],
        in_flight=signed_breakdown["in_flight"],
        timestamp=timestamp,
        deadline=deadline,
        round_id=new_round_id,
        vault_address=VAULT_V7_ADDRESS
    )
    
    # Ensure signature has 0x prefix
    sig_hex = signature if signature.startswith("0x") else "0x" + signature
    
    print(f"✅ Signed NAV: ${nav/1e6:.4f}/share (round {new_round_id}) [{safety_status}]")
    
    # Update cache - keep raw PM cash for internal accounting
    with nav_lock:
        cached_nav = {
            "total_assets": signed_breakdown["total_assets"],
            "credited_cash": credited_cash_raw,  # Raw PM cash, NOT signed
            "credited_positions": signed_breakdown["credited_positions"],
            "pending_credit": signed_breakdown["pending_credit"],  # V7.4: 0
            "pending_credit_monitoring": breakdown["pending_credit"],  # Original for monitoring
            "in_flight": signed_breakdown["in_flight"],
            "reserved": reserved_usdc,
            "cost_basis": breakdown.get("cost_basis", 0),
            "nav": nav,
            "round_id": new_round_id,
            "last_calculated": now,
            "total_supply": total_supply,
            "vault_buffer": vault_buffer_1e6,
            "credited_cash_signed": signed_breakdown["credited_cash"],
            "safety_status": safety_status,
            "safety_reason": safety_reason,
            "expected_assets": expected_assets,
            "pending_shares_excluded": pending_shares_excluded,
            "total_usdc_locked": total_usdc_locked,
            "withdrawal_bridge_in_transit": withdrawal_bridge_in_transit,
            "effective_exclusion": effective_exclusion,
            "effective_assets": effective_assets,
            "effective_supply": effective_supply,
        }
    
    # Build response - ALL navData fields come from signed_breakdown
    result = {
        "navData": {
            "totalAssets": str(signed_breakdown["total_assets"]),
            "creditedCash": str(signed_breakdown["credited_cash"]),
            "creditedPositions": str(signed_breakdown["credited_positions"]),
            "pendingCredit": str(signed_breakdown["pending_credit"]),
            "inFlightOnChain": str(signed_breakdown["in_flight"]),
            "timestamp": timestamp,
            "deadline": deadline,
            "roundId": new_round_id,
            "nav": str(nav),
        },
        "signature": sig_hex,
        "signer": signer,
        "metadata": {
            "nav_raw": str(nav),
            "price_per_share": nav / 1e6,
            "total_assets_usdc": signed_breakdown["total_assets"] / 1e6,
            "total_supply": total_supply / 1e18,
            "vault_buffer_usdc": vault_buffer_1e6 / 1e6,
            "credited_cash_usdc": signed_breakdown["credited_cash"] / 1e6,
            "credited_positions_usdc": signed_breakdown["credited_positions"] / 1e6,
            "pending_credit_usdc": signed_breakdown["pending_credit"] / 1e6,
            "pending_credit_monitoring_usdc": breakdown["pending_credit"] / 1e6,
            "in_flight_usdc": signed_breakdown["in_flight"] / 1e6,
            "reserved_usdc": reserved_usdc / 1e6,
            "cost_basis_usdc": breakdown.get("cost_basis", 0) / 1e6,
            "valid_until": deadline,
            "safety_status": safety_status,
            "safety_reason": safety_reason,
            "v75_withdrawal_exclusion": {
                "pending_shares": pending_shares_excluded / 1e18,
                "usdc_locked_total": total_usdc_locked / 1e6,
                "withdrawal_bridge_in_transit": withdrawal_bridge_in_transit / 1e6,
                "effective_exclusion": effective_exclusion / 1e6,
                "effective_assets": effective_assets / 1e6,
                "effective_supply": effective_supply / 1e18,
            },
        },
        "debug": {
            "chain_round_id": chain_round_id,
            "vault_buffer_raw": vault_buffer_1e6,
            "credited_cash_raw": credited_cash_raw,
            "total_supply_raw": str(total_supply),
            "pending_shares_excluded_raw": str(pending_shares_excluded),
            "total_usdc_locked_raw": str(total_usdc_locked),
            "withdrawal_bridge_in_transit_raw": str(withdrawal_bridge_in_transit),
            "effective_exclusion_raw": str(effective_exclusion),
        }
    }
    
    # Update signed NAV cache
    with nav_lock:
        cached_signed_nav = result
    
    return result


# =============================================================================
# Flask Endpoints
# =============================================================================

@flask_app.route('/health', methods=['GET'])
def health():
    with nav_lock:
        status = cached_nav.get("safety_status", "ok")
    
    return jsonify({
        "status": "ok" if status != "pause" else "degraded",
        "version": "v7",
        "timestamp": int(time.time()),
        "oracle_address": oracle_account.address if oracle_account else None,
        "safety_status": status,
    })


@flask_app.route('/price', methods=['GET'])
def get_price():
    """Get cached price (fast, for UI display)."""
    with nav_lock:
        return jsonify({
            "price_per_share": cached_nav["nav"] / 1e6,
            "nav_raw": str(cached_nav["nav"]),
            "total_supply": cached_nav["total_supply"] / 1e18 if cached_nav["total_supply"] else 0,
            "total_assets": cached_nav["total_assets"] / 1e6,
            "vault_buffer_usdc": cached_nav.get("vault_buffer", 0) / 1e6,
            "credited_cash_usdc": cached_nav["credited_cash"] / 1e6,
            "credited_positions_usdc": cached_nav["credited_positions"] / 1e6,
            "pending_credit_usdc": cached_nav["pending_credit"] / 1e6,
            "in_flight_usdc": cached_nav["in_flight"] / 1e6,
            "last_updated": cached_nav["last_calculated"],
            "safety_status": cached_nav.get("safety_status", "ok"),
            "v75_withdrawal_exclusion": {
                "pending_shares": cached_nav.get("pending_shares_excluded", 0) / 1e18 if cached_nav.get("pending_shares_excluded") else 0,
                "usdc_locked_total": cached_nav.get("total_usdc_locked", 0) / 1e6 if cached_nav.get("total_usdc_locked") else 0,
                "withdrawal_bridge_in_transit": cached_nav.get("withdrawal_bridge_in_transit", 0) / 1e6 if cached_nav.get("withdrawal_bridge_in_transit") else 0,
                "effective_exclusion": cached_nav.get("effective_exclusion", 0) / 1e6 if cached_nav.get("effective_exclusion") else 0,
                "effective_assets": cached_nav.get("effective_assets", 0) / 1e6 if cached_nav.get("effective_assets") else 0,
                "effective_supply": cached_nav.get("effective_supply", 0) / 1e18 if cached_nav.get("effective_supply") else 0,
            },
        })


@flask_app.route('/price/refresh', methods=['GET', 'POST'])
def refresh_price():
    """Force refresh of on-chain data and return updated price."""
    try:
        get_signed_nav_data_v7()
        return get_price()
    except Exception as e:
        print(f"❌ Error refreshing price: {e}")
        return jsonify({"error": str(e)}), 500


@flask_app.route('/sign-nav', methods=['GET', 'POST'])
def sign_nav():
    """Get signed NavDataV7 for deposit/withdraw transactions.
    
    Returns cached signed NAV instantly for fast UX.
    Cache is refreshed every 45 seconds by background thread.
    """
    global cached_signed_nav
    
    try:
        with nav_lock:
            if cached_signed_nav is not None:
                # Check if cache is still valid (deadline not expired)
                deadline = cached_signed_nav.get("navData", {}).get("deadline", 0)
                if deadline > int(time.time()) + 30:  # At least 30s remaining
                    print(f"⚡ Returning cached signed NAV (valid until {deadline})")
                    return jsonify(cached_signed_nav)
        
        # No valid cache, generate fresh (should be rare after startup)
        print("🔄 Generating fresh signed NAV (no valid cache)")
        result = get_signed_nav_data_v7()
        return jsonify(result)
    except Exception as e:
        print(f"❌ Error signing NAV: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@flask_app.route('/sign-nav/debug', methods=['GET'])
def sign_nav_debug():
    """Debug endpoint showing full NAV calculation details."""
    try:
        result = get_signed_nav_data_v7()
        
        result["debug"] = {
            "vault_address": VAULT_V7_ADDRESS,
            "oracle_address": oracle_account.address if oracle_account else None,
            "polymarket_wallet": POLYMARKET_PROXY_ADDRESS,
            "polymarket_deposit_address": POLYMARKET_BASE_DEPOSIT,
            "pending_tracker": {
                "total_forwarded": pending_tracker.total_forwarded / 1e6 if pending_tracker else 0,
                "withdrawn_back": pending_tracker.withdrawn_back / 1e6 if pending_tracker else 0,
                "deposit_count": len(pending_tracker.deposit_records) if pending_tracker else 0,
            }
        }
        
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


@flask_app.route('/admin/record-deposit', methods=['POST'])
def record_deposit():
    """Admin endpoint to manually record a deposit (for sync)."""
    try:
        data = flask_request.json
        amount = int(data.get("amount_usdc", 0))
        tx_hash = data.get("tx_hash", "manual")
        
        if amount <= 0:
            return jsonify({"error": "Invalid amount"}), 400
        
        pending_tracker.record_deposit(amount, tx_hash)
        return jsonify({"success": True, "total_forwarded": pending_tracker.total_forwarded / 1e6})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route('/admin/record-withdrawal-back', methods=['POST'])
def record_withdrawal_back():
    """Admin endpoint to record USDC returned from Polymarket."""
    try:
        data = flask_request.json
        amount = int(data.get("amount_usdc", 0))
        
        if amount <= 0:
            return jsonify({"error": "Invalid amount"}), 400
        
        pending_tracker.record_withdrawal_back(amount)
        return jsonify({"success": True, "withdrawn_back": pending_tracker.withdrawn_back / 1e6})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Liquidation Plan Endpoints (for withdrawal servicer)
# =============================================================================

def cleanup_expired_reservations():
    """Remove expired reservations."""
    now = int(time.time())
    with reservation_lock:
        expired = [rid for rid, r in liquidation_reservations.items() if r["expires_at"] < now]
        for rid in expired:
            del liquidation_reservations[rid]
        if expired:
            print(f"🧹 Cleaned up {len(expired)} expired reservations")


def get_reserved_size(token_id: str) -> float:
    """Get total size currently reserved for a token."""
    cleanup_expired_reservations()
    with reservation_lock:
        return sum(
            r["size"] for r in liquidation_reservations.values()
            if r["token_id"] == token_id
        )


@flask_app.route('/pm-cash', methods=['GET'])
def get_pm_cash():
    """Get current Polymarket cash balance."""
    try:
        if polymarket_client:
            cash = polymarket_client.fetch_cash_balance()
            return jsonify({
                "cash_usdc": cash,
                "source": "polygon_rpc",
                "timestamp": int(time.time())
            })
        return jsonify({"error": "Polymarket client not initialized"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route('/liquidation-plan', methods=['GET'])
def get_liquidation_plan():
    """
    Get a liquidation plan to raise the requested USDC amount.
    
    Query params:
      - need_usdc: Amount of USDC to raise (required)
      - max_positions: Max positions to include in plan (default 5)
      - max_slippage_bps: Max slippage in basis points (default 100 = 1%)
    
    Returns a plan with legs (positions to sell) including:
      - token_id, size, limit_price, expected_usdc, reservation_id
    """
    try:
        need_usdc = float(flask_request.args.get('need_usdc', 0))
        max_positions = int(flask_request.args.get('max_positions', 5))
        max_slippage_bps = int(flask_request.args.get('max_slippage_bps', MAX_SLIPPAGE_BPS))
        
        if need_usdc <= 0:
            return jsonify({"error": "need_usdc must be positive"}), 400
        
        if not polymarket_client:
            return jsonify({"error": "Polymarket client not initialized"}), 500
        
        print(f"\n📊 LIQUIDATION PLAN REQUEST: need ${need_usdc:.2f}, max_pos={max_positions}, max_slip={max_slippage_bps}bps")
        
        # Fetch all positions and orderbooks
        positions = polymarket_client.fetch_positions_with_cost_basis()[0]
        if not positions:
            return jsonify({
                "plan_id": str(uuid.uuid4()),
                "need_usdc": need_usdc,
                "legs": [],
                "total_expected_usdc": 0,
                "shortfall": need_usdc,
                "message": "No positions available for liquidation"
            })
        
        # Score and rank positions by liquidation attractiveness
        scored_positions = []
        for pos in positions:
            token_id = pos.get("token_id")
            size = pos.get("size", 0)
            if not token_id or size <= 0:
                continue
            
            # Get orderbook
            orderbook = polymarket_client.fetch_orderbook(token_id)
            bids = orderbook.get("bids", [])
            
            if not bids:
                continue  # Skip positions with no bids
            
            best_bid = bids[0]["price"]
            
            # Calculate available size (minus reservations)
            reserved = get_reserved_size(token_id)
            available_size = max(0, size - reserved)
            if available_size <= 0:
                continue
            
            # Calculate depth at/near best bid (within slippage tolerance)
            min_acceptable_price = best_bid * (1 - max_slippage_bps / 10000)
            available_depth = sum(b["size"] for b in bids if b["price"] >= min_acceptable_price)
            
            # How much can we actually sell?
            sellable_size = min(available_size, available_depth)
            if sellable_size <= 0:
                continue
            
            # Calculate expected USDC from VWAP sweep
            expected_usdc = polymarket_client.simulate_market_sell(sellable_size, bids)
            if expected_usdc < MIN_LIQUIDATION_USDC:
                continue
            
            # Calculate limit price (worst acceptable price with slippage)
            limit_price = min_acceptable_price
            
            scored_positions.append({
                "token_id": token_id,
                "outcome": pos.get("outcome", "Unknown"),
                "title": pos.get("title", "")[:50],
                "size": sellable_size,
                "best_bid": best_bid,
                "limit_price": limit_price,
                "expected_usdc": expected_usdc,
                "depth": available_depth,
                "score": expected_usdc,  # Simple: prioritize by expected value
            })
        
        # Sort by score (highest value first)
        scored_positions.sort(key=lambda x: x["score"], reverse=True)
        
        # Build plan legs until we have enough
        legs = []
        total_expected = 0.0
        remaining_need = need_usdc
        
        for pos in scored_positions[:max_positions]:
            if remaining_need <= 0:
                break
            
            # How much of this position do we need?
            needed_size = pos["size"]
            if pos["expected_usdc"] > remaining_need:
                # Partial: scale down to just what we need
                ratio = remaining_need / pos["expected_usdc"]
                needed_size = pos["size"] * ratio
            
            # Create reservation
            reservation_id = str(uuid.uuid4())[:8]
            expires_at = int(time.time()) + RESERVATION_TTL_SECONDS
            
            with reservation_lock:
                liquidation_reservations[reservation_id] = {
                    "token_id": pos["token_id"],
                    "size": needed_size,
                    "limit_price": pos["limit_price"],
                    "expires_at": expires_at,
                    "created_at": int(time.time())
                }
            
            # Calculate expected USDC for this size
            expected_usdc = (needed_size / pos["size"]) * pos["expected_usdc"] if pos["size"] > 0 else 0
            
            legs.append({
                "token_id": pos["token_id"],
                "outcome": pos["outcome"],
                "title": pos["title"],
                "side": "SELL",
                "size": round(needed_size, 2),
                "best_bid": pos["best_bid"],
                "limit_price": round(pos["limit_price"], 4),
                "expected_usdc": round(expected_usdc, 2),
                "reservation_id": reservation_id,
                "expires_at": expires_at
            })
            
            total_expected += expected_usdc
            remaining_need -= expected_usdc
        
        plan_id = str(uuid.uuid4())
        shortfall = max(0, need_usdc - total_expected)
        
        result = {
            "plan_id": plan_id,
            "need_usdc": round(need_usdc, 2),
            "legs": legs,
            "total_expected_usdc": round(total_expected, 2),
            "shortfall": round(shortfall, 2),
            "assumptions": {
                "max_slippage_bps": max_slippage_bps,
                "reservation_ttl_seconds": RESERVATION_TTL_SECONDS
            }
        }
        
        print(f"   📋 Plan {plan_id}: {len(legs)} legs, expected ${total_expected:.2f}, shortfall ${shortfall:.2f}")
        return jsonify(result)
        
    except Exception as e:
        import traceback
        print(f"❌ Liquidation plan error: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@flask_app.route('/liquidation-result', methods=['POST'])
def post_liquidation_result():
    """
    Report execution result for a liquidation leg.
    
    Body:
      - reservation_id: The reservation to release
      - status: "filled", "partial", "failed", "cancelled"
      - filled_size: Actual size filled (for partial fills)
      - filled_usdc: Actual USDC received
    
    This releases the reservation so the size becomes available again.
    """
    try:
        data = flask_request.json
        reservation_id = data.get("reservation_id")
        status = data.get("status", "unknown")
        filled_size = float(data.get("filled_size", 0))
        filled_usdc = float(data.get("filled_usdc", 0))
        
        if not reservation_id:
            return jsonify({"error": "reservation_id required"}), 400
        
        with reservation_lock:
            if reservation_id in liquidation_reservations:
                reservation = liquidation_reservations[reservation_id]
                del liquidation_reservations[reservation_id]
                
                print(f"📝 Liquidation result: {reservation_id} → {status}")
                print(f"   Token: {reservation['token_id'][:20]}...")
                print(f"   Reserved: {reservation['size']:.2f}, Filled: {filled_size:.2f}, USDC: ${filled_usdc:.2f}")
                
                return jsonify({
                    "success": True,
                    "reservation_released": True,
                    "token_id": reservation["token_id"],
                    "status": status
                })
            else:
                return jsonify({
                    "success": True,
                    "reservation_released": False,
                    "message": "Reservation not found (may have expired)"
                })
                
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route('/liquidation-reservations', methods=['GET'])
def get_reservations():
    """Debug endpoint to see active reservations."""
    cleanup_expired_reservations()
    with reservation_lock:
        return jsonify({
            "count": len(liquidation_reservations),
            "reservations": [
                {
                    "id": rid,
                    "token_id": r["token_id"][:20] + "...",
                    "size": r["size"],
                    "limit_price": r["limit_price"],
                    "expires_in": r["expires_at"] - int(time.time())
                }
                for rid, r in liquidation_reservations.items()
            ]
        })


@flask_app.route('/orderbook', methods=['GET'])
def get_orderbook_endpoint():
    """
    V7.3.4: Fetch fresh orderbook for a token.
    
    Query params:
      - token_id: The token to get orderbook for
    
    Returns:
      - best_bid: Current best bid price
      - best_ask: Current best ask price
      - bids: Top bids (limited)
    """
    try:
        global polymarket_client
        
        token_id = flask_request.args.get('token_id')
        if not token_id:
            return jsonify({"error": "token_id required"}), 400
        
        if not polymarket_client:
            return jsonify({"error": "PolymarketClient not initialized"}), 500
        
        orderbook = polymarket_client.fetch_orderbook(token_id)
        if not orderbook:
            return jsonify({"error": "Failed to fetch orderbook"}), 500
        
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        
        return jsonify({
            "token_id": token_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bids": bids[:10],
            "asks": asks[:10]
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route('/positions', methods=['GET'])
def get_positions_endpoint():
    """
    V7.3.4: Get all positions with liquidation values.
    
    This endpoint exposes the position valuation that bot_v7 already computes,
    so withdrawal_servicer doesn't need to duplicate orderbook fetching.
    
    Returns:
      - positions: List of positions with token_id, size, liq_value, outcome, best_bid
      - total_liq_value: Sum of all liquidation values
      - cash: Current PM cash balance
    """
    try:
        global polymarket_client
        
        if not polymarket_client:
            return jsonify({"error": "PolymarketClient not initialized"}), 500
        
        # Fetch positions with cost basis (includes size, token_id, outcome, etc.)
        positions_data, total_cost_basis = polymarket_client.fetch_positions_with_cost_basis()
        
        print(f"📊 /positions: Got {len(positions_data)} positions from fetch_positions_with_cost_basis()")
        
        if not positions_data:
            return jsonify({
                "positions": [],
                "total_liq_value": 0,
                "cash": polymarket_client.fetch_cash_balance(),
                "timestamp": int(time.time())
            })
        
        # Calculate liquidation value for each position using orderbook
        positions_result = []
        total_liq_value = 0.0
        
        for pos in positions_data:
            token_id = pos.get("token_id")
            size = pos.get("size", 0)
            outcome = pos.get("outcome", "Unknown")
            
            # V7.3.4: Defensive schema check
            if not token_id:
                print(f"   ⚠️ Position missing token_id, keys: {list(pos.keys())[:5]}")
                continue
            if size <= 0:
                continue
            
            # Fetch orderbook and calculate liquidation value
            orderbook = polymarket_client.fetch_orderbook(token_id)
            bids = orderbook.get("bids", []) if orderbook else []
            
            if bids:
                # VWAP sweep through bids
                remaining = size
                liq_value = 0.0
                best_bid = float(bids[0]["price"])
                
                for bid in bids:
                    if remaining <= 0:
                        break
                    bid_price = float(bid["price"])
                    bid_size = float(bid["size"])
                    fill = min(remaining, bid_size)
                    liq_value += fill * bid_price
                    remaining -= fill
                
                positions_result.append({
                    "token_id": token_id,
                    "outcome": outcome[:50],
                    "size": round(size, 2),
                    "liq_value": round(liq_value, 2),
                    "best_bid": round(best_bid, 4),
                    "has_bids": True
                })
                total_liq_value += liq_value
            else:
                # No bids - illiquid position
                positions_result.append({
                    "token_id": token_id,
                    "outcome": outcome[:50],
                    "size": round(size, 2),
                    "liq_value": 0,
                    "best_bid": 0,
                    "has_bids": False
                })
        
        # Sort by liquidation value (highest first)
        positions_result.sort(key=lambda x: x["liq_value"], reverse=True)
        
        print(f"📊 /positions: Returning {len(positions_result)} positions, total_liq_value=${total_liq_value:.2f}")
        
        return jsonify({
            "positions": positions_result,
            "total_liq_value": round(total_liq_value, 2),
            "cash": polymarket_client.fetch_cash_balance(),
            "timestamp": int(time.time())
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Farcaster Mini App API Routes
# =============================================================================

NEYNAR_API_KEY = os.getenv("NEYNAR_API_KEY", "")
FARCASTER_HUB_URL = os.getenv("FARCASTER_HUB_URL", "https://hub.pinata.cloud")

def verify_farcaster_message(message_bytes_hex: str) -> dict:
    """
    Verify a Farcaster message using hub validation.
    Returns dict with 'valid' boolean and 'fid' if valid.
    """
    if not message_bytes_hex:
        return {'valid': False, 'error': 'Missing messageBytes'}
    
    if NEYNAR_API_KEY:
        try:
            resp = requests.post(
                "https://api.neynar.com/v2/farcaster/frame/validate",
                headers={
                    "accept": "application/json",
                    "api_key": NEYNAR_API_KEY,
                    "content-type": "application/json"
                },
                json={"message_bytes_in_hex": message_bytes_hex},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('valid'):
                    return {
                        'valid': True,
                        'fid': data.get('action', {}).get('interactor', {}).get('fid'),
                        'button_index': data.get('action', {}).get('tapped_button', {}).get('index')
                    }
            return {'valid': False, 'error': 'Neynar validation failed'}
        except Exception as e:
            print(f"⚠️ Neynar verification failed: {e}")
    
    try:
        resp = requests.post(
            f"{FARCASTER_HUB_URL}/v1/validateMessage",
            headers={"Content-Type": "application/octet-stream"},
            data=bytes.fromhex(message_bytes_hex.replace('0x', '')),
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('valid'):
                return {
                    'valid': True,
                    'fid': data.get('message', {}).get('data', {}).get('fid')
                }
        return {'valid': False, 'error': 'Hub validation failed'}
    except Exception as e:
        print(f"⚠️ Hub verification failed: {e}")
        return {'valid': False, 'error': str(e)}

@flask_app.route('/api/farcaster/webhook', methods=['POST'])
def farcaster_webhook():
    """Handle Farcaster Frame/Mini App webhook callbacks with verification"""
    try:
        data = flask_request.get_json() or {}
        
        untrusted_data = data.get('untrustedData', {})
        trusted_data = data.get('trustedData', {})
        message_bytes = trusted_data.get('messageBytes', '')
        
        if not message_bytes:
            print("⚠️ Farcaster webhook: No trustedData.messageBytes - rejecting")
            return jsonify({'error': 'Missing signature data'}), 401
        
        verification = verify_farcaster_message(message_bytes)
        
        if not verification.get('valid'):
            print(f"❌ Farcaster webhook: Verification failed - {verification.get('error')}")
            return jsonify({'error': 'Invalid message signature', 'details': verification.get('error')}), 401
        
        verified_fid = verification.get('fid')
        untrusted_fid = untrusted_data.get('fid')
        
        if verified_fid and untrusted_fid and str(verified_fid) != str(untrusted_fid):
            print(f"❌ Farcaster webhook: FID mismatch - untrusted={untrusted_fid}, verified={verified_fid}")
            return jsonify({'error': 'FID mismatch'}), 401
        
        fid = verified_fid or untrusted_fid
        button_index = verification.get('button_index') or untrusted_data.get('buttonIndex')
        input_text = untrusted_data.get('inputText', '')
        
        print(f"✅ Farcaster webhook: VERIFIED fid={fid}, button={button_index}, input={input_text}")
        
        return jsonify({
            'success': True,
            'fid': fid,
            'action': 'acknowledged',
            'verified': True
        })
        
    except Exception as e:
        print(f"❌ Farcaster webhook error: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/farcaster/verify', methods=['POST'])
def farcaster_verify():
    """Verify a Farcaster message signature using Neynar or hub validation"""
    try:
        data = flask_request.get_json() or {}
        message_bytes = data.get('messageBytes', '')
        
        if not message_bytes:
            return jsonify({'valid': False, 'error': 'Missing messageBytes'}), 400
        
        print(f"🔐 Farcaster verify request for message: {message_bytes[:50]}...")
        
        verification = verify_farcaster_message(message_bytes)
        return jsonify(verification)
        
    except Exception as e:
        print(f"❌ Farcaster verify error: {e}")
        return jsonify({'valid': False, 'error': str(e)}), 500


# =============================================================================
# Farcaster FID Allowlist for Mini App
# =============================================================================

# Allowlist of FIDs that can access the mini app
# Format: comma-separated list of FIDs in env var
# SECURITY: Defaults to deny-all unless ALLOWED_FIDS is explicitly set
# Use ALLOWED_FIDS=* to allow all (for development only)
ALLOWED_FIDS_ENV = os.getenv('ALLOWED_FIDS', '')
ALLOWED_FIDS = set()
ALLOW_ALL_FIDS = False

# App domain for JWT audience verification
FC_APP_DOMAIN = os.getenv('FC_APP_DOMAIN', 'app.pmfi.cc')

if ALLOWED_FIDS_ENV.strip() == '*':
    ALLOW_ALL_FIDS = True
    print("🔓 Farcaster Mini App: All FIDs allowed (ALLOWED_FIDS=*)")
elif ALLOWED_FIDS_ENV.strip():
    try:
        ALLOWED_FIDS = set(int(fid.strip()) for fid in ALLOWED_FIDS_ENV.split(',') if fid.strip().isdigit())
        print(f"🔐 Farcaster Mini App: {len(ALLOWED_FIDS)} FIDs on allowlist")
    except Exception as e:
        print(f"⚠️ Error parsing ALLOWED_FIDS: {e}, defaulting to deny-all")
        ALLOWED_FIDS = set()
else:
    print("🚫 Farcaster Mini App: No ALLOWED_FIDS set - deny-all mode")

def is_fid_whitelisted(fid: int) -> bool:
    """Check if FID has access via env allowlist OR database whitelist"""
    if ALLOW_ALL_FIDS:
        return True
    if fid in ALLOWED_FIDS:
        return True
    if DATABASE_URL:
        try:
            conn = get_invite_db()
            cur = conn.cursor()
            cur.execute('SELECT fid FROM whitelisted_fids WHERE fid = %s', (fid,))
            result = cur.fetchone()
            cur.close()
            conn.close()
            return result is not None
        except Exception as e:
            print(f"⚠️ DB whitelist check error: {e}")
    return False

@flask_app.route('/api/mini/redeem', methods=['POST'])
def api_mini_redeem():
    """Redeem an invite code for a Farcaster FID (Mini App).
    
    Requires a verified Quick Auth JWT token in Authorization header.
    The FID is extracted from the verified token, not from request body.
    """
    try:
        import jwt as pyjwt
        from jwt import PyJWKClient
        
        if not DATABASE_URL:
            return jsonify({'error': 'Invite system not available'}), 503
        
        auth_header = flask_request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Authentication required'}), 401
        
        token = auth_header.split(' ')[1]
        
        import base64, json as json_mod, ssl, urllib.request as urllib_req
        
        FARCASTER_JWKS_URL = "https://auth.farcaster.xyz/.well-known/jwks.json"
        VALID_AUDIENCES = [FC_APP_DOMAIN, "miniapps.farcaster.xyz"]
        
        def _decode_jwt_unsafe(t):
            try:
                parts = t.split('.')
                def _pad(s):
                    m = len(s) % 4
                    return s + '=' * (4 - m) if m else s
                header = json_mod.loads(base64.urlsafe_b64decode(_pad(parts[0])))
                payload = json_mod.loads(base64.urlsafe_b64decode(_pad(parts[1])))
                return header, payload
            except Exception:
                return None, None
        
        def _verify_with_key(t, key, algs=["RS256", "ES256"]):
            for aud in VALID_AUDIENCES:
                try:
                    p = pyjwt.decode(t, key, algorithms=algs, issuer="https://auth.farcaster.xyz", audience=aud,
                        options={"verify_aud": True, "verify_exp": True, "verify_iss": True, "require": ["sub", "iss", "exp", "aud"]})
                    return p
                except pyjwt.InvalidAudienceError:
                    continue
            return None
        
        payload = None
        fid = None
        
        try:
            jwks_client = PyJWKClient(FARCASTER_JWKS_URL, cache_keys=True, lifespan=3600)
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload = _verify_with_key(token, signing_key.key)
            if payload:
                print(f"🔑 Redeem JWT verified with PyJWKClient")
        except Exception as pyjwk_err:
            print(f"⚠️ Redeem PyJWKClient failed: {pyjwk_err}, trying manual JWKS...")
        
        if payload is None:
            try:
                ctx = ssl.create_default_context()
                req = urllib_req.Request(FARCASTER_JWKS_URL, headers={'User-Agent': 'pSNIPER/1.0'})
                resp = urllib_req.urlopen(req, timeout=10, context=ctx)
                jwks_data = json_mod.loads(resp.read().decode())
                if jwks_data and jwks_data.get('keys'):
                    from jwt.algorithms import RSAAlgorithm
                    jwt_header, _ = _decode_jwt_unsafe(token)
                    token_kid = jwt_header.get('kid') if jwt_header else None
                    key_data = None
                    if token_kid:
                        for k in jwks_data['keys']:
                            if k.get('kid') == token_kid:
                                key_data = k
                                break
                    if not key_data:
                        key_data = jwks_data['keys'][0]
                    public_key = RSAAlgorithm.from_jwk(json_mod.dumps(key_data))
                    payload = _verify_with_key(token, public_key, algs=["RS256"])
                    if payload:
                        print(f"🔑 Redeem JWT verified with manual JWKS")
            except Exception as manual_err:
                print(f"❌ Redeem manual JWKS failed: {manual_err}")
        
        if payload is None:
            _, raw_payload = _decode_jwt_unsafe(token)
            if raw_payload and raw_payload.get('sub'):
                fid = int(raw_payload['sub'])
                print(f"⚠️ Redeem: JWKS unavailable, using unverified FID {fid} (invite code is the security gate)")
            else:
                return jsonify({'error': 'Authentication failed. Try refreshing.'}), 401
        else:
            fid = int(payload.get('sub', 0))
        
        if not fid:
            return jsonify({'error': 'Invalid token'}), 401
        
        if is_fid_whitelisted(fid):
            print(f"✅ FID {fid} already whitelisted, skipping code redeem")
            return jsonify({'success': True, 'message': 'Already have access!', 'alreadyWhitelisted': True, 'fid': fid})
        
        data = flask_request.get_json() or {}
        code = data.get('code', '').strip()
        
        if not code or len(code) < 6:
            return jsonify({'error': 'Please enter a valid invite code'}), 400
        
        ip = flask_request.remote_addr or '0.0.0.0'
        if not check_invite_rate_limit(ip):
            return jsonify({'error': 'Too many attempts. Please wait 1 minute.'}), 429
        
        code_hash = hash_invite_code(code)
        
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        try:
            cur.execute('BEGIN')
            
            cur.execute('SELECT * FROM invite_codes WHERE code_hash = %s FOR UPDATE', (code_hash,))
            invite_code = cur.fetchone()
            
            if not invite_code:
                cur.execute('ROLLBACK')
                print(f"❌ FID {fid} tried invalid code")
                return jsonify({'error': 'Invalid invite code'}), 400
            
            if invite_code['status'] == 'used' or invite_code['redeemed_by']:
                cur.execute('ROLLBACK')
                return jsonify({'error': 'This code has already been used'}), 400
            
            if invite_code['status'] == 'revoked':
                cur.execute('ROLLBACK')
                return jsonify({'error': 'This code has been revoked'}), 400
            
            if invite_code.get('expires_at') and invite_code['expires_at'] < datetime.now():
                cur.execute('ROLLBACK')
                return jsonify({'error': 'This code has expired'}), 400
            
            cur.execute(
                "UPDATE invite_codes SET status = 'used', redeemed_by = %s, redeemed_at = CURRENT_TIMESTAMP WHERE id = %s",
                (f'fid:{fid}', invite_code['id'])
            )
            
            cur.execute(
                'INSERT INTO whitelisted_fids (fid, code_id) VALUES (%s, %s) ON CONFLICT (fid) DO NOTHING',
                (fid, invite_code['id'])
            )
            
            cur.execute('COMMIT')
            print(f"✅ FID {fid} redeemed invite code and whitelisted")
            return jsonify({'success': True, 'message': 'Access granted! Welcome to pSNIPER Beta.', 'fid': fid})
            
        except Exception as db_err:
            cur.execute('ROLLBACK')
            print(f"❌ Mini redeem DB error: {db_err}")
            return jsonify({'error': 'Server error. Please try again.'}), 500
        finally:
            cur.close()
            conn.close()
    
    except ImportError:
        return jsonify({'error': 'JWT verification not available'}), 500
    except Exception as e:
        print(f"❌ mini/redeem error: {e}")
        return jsonify({'error': 'Server error'}), 500

@flask_app.route('/api/check-fid', methods=['POST'])
def api_check_fid():
    """Check if a FID is on the allowlist for Mini App access
    
    SECURITY NOTE: This endpoint should only be used for UX hints.
    For actual access control, use /api/verify-fc-token with a Quick Auth JWT.
    Client-provided FIDs are NOT trusted for access decisions.
    """
    try:
        data = flask_request.get_json() or {}
        fid = data.get('fid')
        
        if not fid:
            return jsonify({'allowed': False, 'error': 'Missing FID'}), 400
        
        try:
            fid = int(fid)
        except (ValueError, TypeError):
            return jsonify({'allowed': False, 'error': 'Invalid FID format'}), 400
        
        # Check allowlist (used as UX hint - frontend should verify with Quick Auth for security)
        if ALLOW_ALL_FIDS or fid in ALLOWED_FIDS:
            print(f"✅ FID {fid} granted access to Mini App (context-based, verify with token for security)")
            return jsonify({'allowed': True, 'fid': fid, 'verified': False})
        else:
            print(f"❌ FID {fid} not on allowlist")
            return jsonify({'allowed': False, 'fid': fid, 'verified': False})
            
    except Exception as e:
        print(f"❌ check-fid error: {e}")
        return jsonify({'allowed': False, 'error': str(e)}), 500

@flask_app.route('/api/verify-fc-token', methods=['POST'])
def api_verify_fc_token():
    """Verify a Farcaster Quick Auth JWT token and check FID allowlist
    
    Uses proper JWKS-based JWT verification with:
    - Signature verification against Farcaster's JWKS
    - Issuer validation (must be auth.farcaster.xyz)
    - Audience/domain validation (must match our app domain)
    - Expiration check
    """
    try:
        import jwt
        from jwt import PyJWKClient
        
        auth_header = flask_request.headers.get('Authorization', '')
        
        if not auth_header.startswith('Bearer '):
            return jsonify({'allowed': False, 'error': 'Missing or invalid Authorization header'}), 401
        
        token = auth_header.split(' ')[1]
        
        import base64, json as json_mod, ssl, urllib.request
        
        FARCASTER_JWKS_URL = "https://auth.farcaster.xyz/.well-known/jwks.json"
        VALID_AUDIENCES = [FC_APP_DOMAIN, "miniapps.farcaster.xyz"]
        
        def decode_jwt_unsafe(t):
            try:
                parts = t.split('.')
                def pad_b64(s):
                    missing = len(s) % 4
                    return s + '=' * (4 - missing) if missing else s
                header = json_mod.loads(base64.urlsafe_b64decode(pad_b64(parts[0])))
                payload = json_mod.loads(base64.urlsafe_b64decode(pad_b64(parts[1])))
                return header, payload
            except Exception as dec_err:
                print(f"❌ Could not decode JWT: {dec_err}")
                return None, None
        
        def fetch_jwks_manual():
            try:
                ctx = ssl.create_default_context()
                req = urllib.request.Request(FARCASTER_JWKS_URL, headers={'User-Agent': 'pSNIPER/1.0'})
                resp = urllib.request.urlopen(req, timeout=10, context=ctx)
                jwks_data = json_mod.loads(resp.read().decode())
                print(f"🔑 Manual JWKS fetch succeeded, got {len(jwks_data.get('keys', []))} keys")
                return jwks_data
            except Exception as e:
                print(f"❌ Manual JWKS fetch failed: {e}")
                return None
        
        def verify_with_key(signing_key, algorithms=["RS256", "ES256"]):
            for aud in VALID_AUDIENCES:
                try:
                    p = jwt.decode(
                        token, signing_key, algorithms=algorithms,
                        issuer="https://auth.farcaster.xyz", audience=aud,
                        options={"verify_aud": True, "verify_exp": True, "verify_iss": True, "require": ["sub", "iss", "exp", "aud"]}
                    )
                    return p, aud
                except jwt.InvalidAudienceError:
                    continue
            return None, None
        
        payload = None
        verified_aud = None
        
        try:
            jwks_client = PyJWKClient(FARCASTER_JWKS_URL, cache_keys=True, lifespan=3600)
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload, verified_aud = verify_with_key(signing_key.key)
            if payload:
                print(f"🔑 JWT verified with PyJWKClient, audience: {verified_aud}")
        except jwt.ExpiredSignatureError:
            print("❌ JWT expired")
            return jsonify({'allowed': False, 'error': 'Token expired'}), 401
        except jwt.InvalidIssuerError:
            print("❌ JWT invalid issuer")
            return jsonify({'allowed': False, 'error': 'Invalid token issuer'}), 401
        except jwt.MissingRequiredClaimError as e:
            print(f"❌ JWT missing required claim: {e}")
            return jsonify({'allowed': False, 'error': 'Invalid token format'}), 401
        except jwt.InvalidTokenError as e:
            print(f"❌ JWT invalid token: {e}")
            return jsonify({'allowed': False, 'error': 'Invalid token'}), 401
        except Exception as pyjwk_err:
            print(f"⚠️ PyJWKClient failed: {pyjwk_err}, trying manual JWKS fetch...")
        
        if payload is None:
            jwks_data = fetch_jwks_manual()
            if jwks_data and jwks_data.get('keys'):
                try:
                    from jwt.algorithms import RSAAlgorithm
                    jwt_header, _ = decode_jwt_unsafe(token)
                    token_kid = jwt_header.get('kid') if jwt_header else None
                    
                    key_data = None
                    if token_kid:
                        for k in jwks_data['keys']:
                            if k.get('kid') == token_kid:
                                key_data = k
                                break
                    if not key_data:
                        key_data = jwks_data['keys'][0]
                        print(f"⚠️ No kid match, using first key")
                    
                    public_key = RSAAlgorithm.from_jwk(json_mod.dumps(key_data))
                    payload, verified_aud = verify_with_key(public_key, algorithms=["RS256"])
                    if payload:
                        print(f"🔑 JWT verified with manual JWKS (kid={key_data.get('kid')}), audience: {verified_aud}")
                except jwt.ExpiredSignatureError:
                    print("❌ JWT expired")
                    return jsonify({'allowed': False, 'error': 'Token expired'}), 401
                except jwt.InvalidIssuerError:
                    print("❌ JWT invalid issuer")
                    return jsonify({'allowed': False, 'error': 'Invalid token issuer'}), 401
                except Exception as manual_err:
                    print(f"❌ Manual JWT verification failed: {manual_err}")
        
        if payload is None:
            _, raw_payload = decode_jwt_unsafe(token)
            if raw_payload and raw_payload.get('sub'):
                fid = int(raw_payload['sub'])
                actual_aud = raw_payload.get('aud', 'unknown')
                print(f"⚠️ JWKS verification failed - unverified FID {fid} (aud={actual_aud}), showing invite code only")
                return jsonify({'allowed': False, 'fid': fid, 'verified': False})
            else:
                return jsonify({'allowed': False, 'error': 'Could not verify or decode token'}), 401
        
        fid = payload.get('sub')
        if not fid:
            return jsonify({'allowed': False, 'error': 'No FID in token'}), 401
        
        fid = int(fid)
        
        NEYNAR_MIN_SCORE = 0.7
        neynar_api_key = os.environ.get('NEYNAR_API_KEY', '')
        
        if not neynar_api_key:
            print(f"❌ NEYNAR_API_KEY not set, cannot check score for FID {fid}")
            return jsonify({'allowed': False, 'error': 'score_unavailable', 'fid': fid}), 403
        
        try:
            import urllib.request as urllib_req
            import urllib.error
            neynar_url = f"https://api.neynar.com/v2/farcaster/user/bulk?fids={fid}&viewer_fid={fid}"
            neynar_request = urllib_req.Request(neynar_url, headers={
                'accept': 'application/json',
                'x-api-key': neynar_api_key
            })
            neynar_ctx = ssl.create_default_context()
            neynar_resp = urllib_req.urlopen(neynar_request, timeout=10, context=neynar_ctx)
            neynar_data = json_mod.loads(neynar_resp.read().decode())
            
            users = neynar_data.get('users', [])
            if not users:
                print(f"❌ Neynar returned no users for FID {fid}")
                return jsonify({'allowed': False, 'error': 'score_unavailable', 'fid': fid}), 403
            
            user_data = users[0]
            experimental = user_data.get('experimental', {})
            neynar_score = experimental.get('neynar_user_score')
            
            if neynar_score is None or not isinstance(neynar_score, (int, float)):
                print(f"❌ Neynar score missing or invalid for FID {fid}: {neynar_score}")
                return jsonify({'allowed': False, 'error': 'score_unavailable', 'fid': fid}), 403
            
            neynar_score = float(neynar_score)
            
            if neynar_score < NEYNAR_MIN_SCORE:
                print(f"❌ FID {fid} score {neynar_score} below threshold {NEYNAR_MIN_SCORE}")
                return jsonify({
                    'allowed': False,
                    'error': 'score_too_low',
                    'fid': fid,
                    'score': round(neynar_score, 4),
                    'min': NEYNAR_MIN_SCORE,
                    'verified': True
                }), 403
            
            print(f"✅ FID {fid} verified, Neynar score {neynar_score} >= {NEYNAR_MIN_SCORE}, access granted")
            return jsonify({
                'allowed': True,
                'fid': fid,
                'verified': True,
                'score': round(neynar_score, 4),
                'source': 'farcaster'
            })
            
        except urllib.error.HTTPError as http_err:
            print(f"❌ Neynar API HTTP error for FID {fid}: {http_err.code} {http_err.reason}")
            return jsonify({'allowed': False, 'error': 'neynar_error', 'fid': fid, 'details': f'HTTP {http_err.code}'}), 502
        except urllib.error.URLError as url_err:
            print(f"❌ Neynar API connection error for FID {fid}: {url_err.reason}")
            return jsonify({'allowed': False, 'error': 'neynar_error', 'fid': fid, 'details': str(url_err.reason)}), 502
        except Exception as neynar_err:
            print(f"❌ Neynar score check failed for FID {fid}: {neynar_err}")
            return jsonify({'allowed': False, 'error': 'neynar_error', 'fid': fid, 'details': str(neynar_err)}), 502
            
    except ImportError:
        print("⚠️ PyJWT not installed - cannot verify Farcaster tokens")
        return jsonify({'allowed': False, 'error': 'JWT verification not available'}), 500
    except Exception as e:
        print(f"❌ verify-fc-token error: {e}")
        return jsonify({'allowed': False, 'error': str(e)}), 500


# =============================================================================
# Invite Code API Routes
# =============================================================================

@flask_app.route('/api/invite/check-wallet', methods=['POST'])
def api_check_wallet():
    """Check if a wallet is whitelisted"""
    try:
        data = flask_request.get_json() or {}
        wallet = data.get('wallet', '').lower()
        
        if not wallet or len(wallet) != 42:
            return jsonify({'error': 'Invalid wallet address'}), 400
        
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT wallet_address FROM whitelisted_wallets WHERE wallet_address = %s", (wallet,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        return jsonify({'whitelisted': result is not None})
    except Exception as e:
        print(f"❌ check-wallet error: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/invite/validate', methods=['POST'])
def api_validate_code():
    """Validate an invite code (without redeeming)"""
    ip = flask_request.headers.get('X-Forwarded-For', flask_request.remote_addr or 'unknown')
    
    if not check_invite_rate_limit(ip):
        log_invite_action('validate', ip=ip, success=False, error='Rate limited')
        return jsonify({'error': 'Too many attempts. Please wait.'}), 429
    
    try:
        data = flask_request.get_json() or {}
        code = data.get('code', '').strip().upper()
        
        if not code or len(code) != 8:
            return jsonify({'valid': False, 'error': 'Invalid code format'}), 400
        
        code_hash = hash_invite_code(code)
        
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, status, expires_at, redeemed_by 
            FROM invite_codes 
            WHERE code_hash = %s
        """, (code_hash,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if not result:
            log_invite_action('validate', code_hash=code_hash[:16], ip=ip, success=False, error='Code not found')
            return jsonify({'valid': False, 'error': 'Invalid code'})
        
        if result['status'] != 'active':
            log_invite_action('validate', code_hash=code_hash[:16], ip=ip, success=False, error='Code already used')
            return jsonify({'valid': False, 'error': 'Code already used'})
        
        if result['expires_at'] and result['expires_at'] < datetime.now():
            log_invite_action('validate', code_hash=code_hash[:16], ip=ip, success=False, error='Code expired')
            return jsonify({'valid': False, 'error': 'Code expired'})
        
        log_invite_action('validate', code_hash=code_hash[:16], ip=ip, success=True)
        return jsonify({'valid': True})
        
    except Exception as e:
        print(f"❌ validate error: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/invite/redeem', methods=['POST'])
def api_redeem_code():
    """Redeem an invite code and whitelist a wallet"""
    ip = flask_request.headers.get('X-Forwarded-For', flask_request.remote_addr or 'unknown')
    
    if not check_invite_rate_limit(ip):
        return jsonify({'error': 'Too many attempts. Please wait.'}), 429
    
    try:
        data = flask_request.get_json() or {}
        code = data.get('code', '').strip().upper()
        wallet = data.get('wallet', '').lower()
        
        if not code or len(code) != 8:
            return jsonify({'success': False, 'error': 'Invalid code format'}), 400
        
        if not wallet or len(wallet) != 42:
            return jsonify({'success': False, 'error': 'Invalid wallet address'}), 400
        
        code_hash = hash_invite_code(code)
        
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Check if wallet already whitelisted
        cur.execute("SELECT wallet_address FROM whitelisted_wallets WHERE wallet_address = %s", (wallet,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({'success': True, 'message': 'Wallet already whitelisted'})
        
        # Check code validity
        cur.execute("""
            SELECT id, status, expires_at 
            FROM invite_codes 
            WHERE code_hash = %s
        """, (code_hash,))
        result = cur.fetchone()
        
        if not result:
            log_invite_action('redeem', code_hash=code_hash[:16], wallet=wallet, ip=ip, success=False, error='Code not found')
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid code'})
        
        if result['status'] != 'active':
            log_invite_action('redeem', code_hash=code_hash[:16], wallet=wallet, ip=ip, success=False, error='Code already used')
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Code already used'})
        
        if result['expires_at'] and result['expires_at'] < datetime.now():
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'Code expired'})
        
        # Redeem the code
        cur.execute("""
            UPDATE invite_codes 
            SET status = 'redeemed', redeemed_by = %s, redeemed_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (wallet, result['id']))
        
        # Whitelist the wallet
        cur.execute("""
            INSERT INTO whitelisted_wallets (wallet_address, code_id)
            VALUES (%s, %s)
            ON CONFLICT (wallet_address) DO NOTHING
        """, (wallet, result['id']))
        
        conn.commit()
        log_invite_action('redeem', code_hash=code_hash[:16], wallet=wallet, ip=ip, success=True)
        
        cur.close()
        conn.close()
        
        print(f"✅ Wallet {wallet[:10]}... whitelisted with code {code}")
        return jsonify({'success': True, 'message': 'Access granted!'})
        
    except Exception as e:
        print(f"❌ redeem error: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/admin/codes', methods=['GET', 'POST'])
def api_admin_codes():
    """Admin: List or generate invite codes"""
    # Verify admin token
    auth = flask_request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != PMFI_ADMIN_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        if flask_request.method == 'GET':
            # List codes
            conn = get_invite_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT id, status, created_at, expires_at, redeemed_by, redeemed_at, note,
                       SUBSTRING(code_hash, 1, 8) as code_prefix
                FROM invite_codes 
                ORDER BY created_at DESC 
                LIMIT 100
            """)
            codes = cur.fetchall()
            cur.close()
            conn.close()
            
            # Convert datetime objects to strings
            for code in codes:
                if code.get('created_at'):
                    code['created_at'] = code['created_at'].isoformat()
                if code.get('expires_at'):
                    code['expires_at'] = code['expires_at'].isoformat()
                if code.get('redeemed_at'):
                    code['redeemed_at'] = code['redeemed_at'].isoformat()
            
            return jsonify({'codes': codes})
        
        else:  # POST - Generate codes
            data = flask_request.get_json() or {}
            count = min(int(data.get('count', 1)), 50)
            note = data.get('note', '')
            
            codes = []
            conn = get_invite_db()
            cur = conn.cursor()
            
            for _ in range(count):
                code = generate_invite_code()
                code_hash = hash_invite_code(code)
                cur.execute("""
                    INSERT INTO invite_codes (code_hash, note, created_by)
                    VALUES (%s, %s, 'admin')
                """, (code_hash, note))
                codes.append(code)
            
            conn.commit()
            cur.close()
            conn.close()
            
            log_invite_action('create_codes', ip=flask_request.remote_addr, success=True)
            print(f"✅ Generated {len(codes)} invite codes")
            return jsonify({'codes': codes})
            
    except Exception as e:
        print(f"❌ admin/codes error: {e}")
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/admin/codes/<int:code_id>', methods=['DELETE'])
def api_admin_revoke_code(code_id):
    """Admin: Revoke an invite code"""
    auth = flask_request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != PMFI_ADMIN_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_invite_db()
        cur = conn.cursor()
        cur.execute("UPDATE invite_codes SET status = 'revoked' WHERE id = %s", (code_id,))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/admin/wallets', methods=['GET'])
def api_admin_wallets():
    """Admin: List whitelisted wallets"""
    auth = flask_request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != PMFI_ADMIN_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT wallet_address, whitelisted_at 
            FROM whitelisted_wallets 
            ORDER BY whitelisted_at DESC
        """)
        wallets = cur.fetchall()
        cur.close()
        conn.close()
        
        for w in wallets:
            if w.get('whitelisted_at'):
                w['whitelisted_at'] = w['whitelisted_at'].isoformat()
        
        return jsonify({'wallets': wallets})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@flask_app.route('/api/admin/stats', methods=['GET'])
def api_admin_stats():
    """Admin: Get invite code statistics"""
    auth = flask_request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != PMFI_ADMIN_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT COUNT(*) as total FROM invite_codes")
        total_codes = cur.fetchone()['total']
        
        cur.execute("SELECT COUNT(*) as active FROM invite_codes WHERE status = 'active'")
        active_codes = cur.fetchone()['active']
        
        cur.execute("SELECT COUNT(*) as redeemed FROM invite_codes WHERE status = 'redeemed'")
        redeemed_codes = cur.fetchone()['redeemed']
        
        cur.execute("SELECT COUNT(*) as total FROM whitelisted_wallets")
        total_wallets = cur.fetchone()['total']
        
        cur.close()
        conn.close()
        
        return jsonify({
            'total_codes': total_codes,
            'active_codes': active_codes,
            'redeemed_codes': redeemed_codes,
            'whitelisted_wallets': total_wallets
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# XP System API Endpoints
# =============================================================================

@flask_app.route('/api/me', methods=['POST'])
def api_xp_me():
    """Upsert user for XP system. Input: { fid, username, wallet? }"""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        username = data.get('username', '')
        wallet = data.get('wallet')
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/me fid={fid} username={username} wallet={wallet}")
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if wallet:
            cur.execute("""
                INSERT INTO xp_users (fid, username, wallet)
                VALUES (%s, %s, %s)
                ON CONFLICT (fid) DO UPDATE SET username = EXCLUDED.username, wallet = EXCLUDED.wallet
                RETURNING *
            """, (fid, username, wallet.lower() if wallet else None))
        else:
            cur.execute("""
                INSERT INTO xp_users (fid, username)
                VALUES (%s, %s)
                ON CONFLICT (fid) DO UPDATE SET username = EXCLUDED.username
                RETURNING *
            """, (fid, username))
        user = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if user and user.get('created_at'):
            user['created_at'] = user['created_at'].isoformat()
        print(f"✅ [XP] Upserted user fid={fid}")
        return jsonify({'ok': True, 'user': user})
    except Exception as e:
        print(f"❌ [XP] /api/me error: {e}")
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/referral/attach', methods=['POST'])
def api_referral_attach():
    """Attach referrer to user. Input: { fid, ref } where ref is referrer_fid."""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        ref = data.get('ref')
        if not fid or not ref:
            return jsonify({'error': 'fid and ref required'}), 400
        fid = int(fid)
        ref = int(ref)
        if fid == ref:
            return jsonify({'error': 'Cannot refer yourself'}), 400
        print(f"📝 [XP] /api/referral/attach fid={fid} ref={ref}")
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fid, referrer_fid FROM xp_users WHERE fid = %s", (fid,))
        user = cur.fetchone()
        if not user:
            cur.execute("INSERT INTO xp_users (fid, referrer_fid) VALUES (%s, %s)", (fid, ref))
            conn.commit()
            cur.close()
            conn.close()
            print(f"✅ [XP] Created user fid={fid} with referrer={ref}")
            return jsonify({'ok': True, 'attached': True})
        if user['referrer_fid']:
            cur.close()
            conn.close()
            print(f"ℹ️ [XP] fid={fid} already has referrer={user['referrer_fid']}")
            return jsonify({'ok': True, 'attached': False, 'reason': 'already_has_referrer'})
        cur.execute("UPDATE xp_users SET referrer_fid = %s WHERE fid = %s", (ref, fid))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ [XP] Attached referrer={ref} to fid={fid}")
        return jsonify({'ok': True, 'attached': True})
    except Exception as e:
        print(f"❌ [XP] /api/referral/attach error: {e}")
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/state', methods=['GET'])
def api_xp_state():
    """Get full XP state for a user. Query: ?fid=..."""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        fid = flask_request.args.get('fid')
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/state fid={fid}")
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM xp_users WHERE fid = %s", (fid,))
        user = cur.fetchone()
        if not user:
            cur.close()
            conn.close()
            return jsonify({
                'user': None,
                'xpTotal': 0,
                'tasks': [
                    {"id": t["id"], "xp": t["xp"], "status": "AVAILABLE" if t["id"] != "follow_x" else "LOCKED", "locked": t["id"] == "follow_x", "reason": None}
                    for t in TASK_DEFINITIONS
                ],
                'referralLink': f"?ref={fid}",
                'referralStats': {'invitedCount': 0, 'referralXpEarned': 0}
            })
        if user.get('created_at'):
            user['created_at'] = user['created_at'].isoformat()
        cur.execute("""
            SELECT type, status, xp FROM xp_events
            WHERE fid = %s AND type IN ('follow_fc', 'deposit_10', 'invite', 'follow_x')
        """, (fid,))
        events = cur.fetchall()
        event_map = {}
        for ev in events:
            event_map[ev['type']] = ev['status']
        cur.execute("""
            SELECT COALESCE(SUM(xp), 0) as total FROM xp_events
            WHERE fid = %s AND status = 'COMPLETED'
        """, (fid,))
        xp_total = cur.fetchone()['total']
        prereqs_done = all(
            event_map.get(t) == 'COMPLETED'
            for t in ['follow_fc', 'deposit_10', 'invite']
        )
        tasks = []
        for td in TASK_DEFINITIONS:
            tid = td['id']
            existing_status = event_map.get(tid)
            if existing_status:
                tasks.append({"id": tid, "xp": td["xp"], "status": existing_status, "locked": False, "reason": None})
            elif tid == 'follow_x':
                locked = not prereqs_done
                tasks.append({
                    "id": tid, "xp": td["xp"],
                    "status": "LOCKED" if locked else "AVAILABLE",
                    "locked": locked,
                    "reason": "Complete follow_fc, deposit_10, and invite first" if locked else None
                })
            else:
                tasks.append({"id": tid, "xp": td["xp"], "status": "AVAILABLE", "locked": False, "reason": None})
        cur.execute("""
            SELECT COUNT(DISTINCT fid) as cnt FROM xp_users WHERE referrer_fid = %s
        """, (fid,))
        invited_count = cur.fetchone()['cnt']
        cur.execute("""
            SELECT COALESCE(SUM(xp_share), 0) as total FROM referral_earnings WHERE referrer_fid = %s
        """, (fid,))
        referral_xp = cur.fetchone()['total']
        cur.close()
        conn.close()
        return jsonify({
            'user': user,
            'xpTotal': xp_total,
            'tasks': tasks,
            'referralLink': f"?ref={fid}",
            'referralStats': {'invitedCount': invited_count, 'referralXpEarned': referral_xp}
        })
    except Exception as e:
        print(f"❌ [XP] /api/state error: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# XP Task Verification Endpoints
# =============================================================================

def _verify_fid_exists(fid):
    """Check that the FID exists in xp_users (i.e. has passed auth gate). Returns user row or None."""
    conn = get_invite_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM xp_users WHERE fid = %s", (fid,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


@flask_app.route('/api/tasks/verify/follow_fc', methods=['POST'])
def api_verify_follow_fc():
    """Verify that user follows the target Farcaster account. Awards 100 XP."""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/tasks/verify/follow_fc fid={fid}")

        if not _verify_fid_exists(fid):
            return jsonify({'error': 'User not registered'}), 403

        if not XP_FOLLOW_FC_TARGET_FID:
            print("❌ [XP] XP_FOLLOW_FC_TARGET_FID not configured")
            return jsonify({'error': 'Follow target not configured'}), 500

        neynar_api_key = os.environ.get('NEYNAR_API_KEY', '')
        if not neynar_api_key:
            print("❌ [XP] NEYNAR_API_KEY not set for follow verification")
            return jsonify({'error': 'Neynar API not configured'}), 500

        verified = False
        cursor = None
        while True:
            url = f"https://api.neynar.com/v2/farcaster/following?fid={fid}&limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            print(f"🔍 [XP] Checking following list: {url}")
            ctx = ssl.create_default_context()
            req = urllib_req.Request(url, headers={
                'accept': 'application/json',
                'x-api-key': neynar_api_key
            })
            resp = urllib_req.urlopen(req, timeout=15, context=ctx)
            resp_data = json_mod.loads(resp.read().decode())
            users = resp_data.get('users', [])
            for u in users:
                if u.get('fid') == XP_FOLLOW_FC_TARGET_FID:
                    verified = True
                    break
            if verified:
                break
            next_cursor = resp_data.get('next', {}).get('cursor')
            if not next_cursor:
                break
            cursor = next_cursor

        if not verified:
            print(f"❌ [XP] FID {fid} does not follow target FID {XP_FOLLOW_FC_TARGET_FID}")
            return jsonify({'verified': False, 'reason': 'You are not following the required account'}), 200

        event_id, awarded = award_xp(fid, 'follow_fc', 100, {'target_fid': XP_FOLLOW_FC_TARGET_FID}, f"follow_fc:{fid}")
        print(f"✅ [XP] follow_fc verified for fid={fid}, awarded={awarded}")
        return jsonify({'verified': True, 'awarded': awarded, 'xp': 100})
    except Exception as e:
        print(f"❌ [XP] /api/tasks/verify/follow_fc error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/tasks/verify/deposit_10', methods=['POST'])
def api_verify_deposit_10():
    """Verify that user deposited >= 10 USDC into the vault. Awards 500 XP."""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/tasks/verify/deposit_10 fid={fid}")

        user = _verify_fid_exists(fid)
        if not user:
            return jsonify({'error': 'User not registered'}), 403

        if not user.get('wallet'):
            print(f"❌ [XP] FID {fid} has no wallet linked")
            return jsonify({'verified': False, 'reason': 'No wallet connected. Please connect your wallet first.'}), 200

        wallet = user['wallet']
        print(f"🔍 [XP] Checking deposits for wallet {wallet} on vault {VAULT_ADDRESS_CONFIG}")

        w3_check = Web3(Web3.HTTPProvider(RPC_URL))
        if not w3_check.is_connected():
            print("❌ [XP] Cannot connect to Base RPC")
            return jsonify({'error': 'RPC connection failed'}), 500

        deposit_event_sig = Web3.keccak(text="Deposit(address,address,uint256,uint256)")
        wallet_checksum = Web3.to_checksum_address(wallet)
        vault_checksum = Web3.to_checksum_address(VAULT_ADDRESS_CONFIG)

        owner_topic = '0x' + wallet_checksum[2:].lower().zfill(64)

        latest_block = w3_check.eth.block_number
        from_block = max(0, latest_block - 2_000_000)

        total_deposited = 0
        chunk_size = 50_000
        current_block = from_block

        while current_block <= latest_block:
            to_block = min(current_block + chunk_size - 1, latest_block)
            try:
                sig_hex = deposit_event_sig.hex() if isinstance(deposit_event_sig, bytes) else deposit_event_sig
                logs_as_caller = w3_check.eth.get_logs({
                    'address': vault_checksum,
                    'topics': [sig_hex, owner_topic],
                    'fromBlock': current_block,
                    'toBlock': to_block,
                })
                logs_as_owner = w3_check.eth.get_logs({
                    'address': vault_checksum,
                    'topics': [sig_hex, None, owner_topic],
                    'fromBlock': current_block,
                    'toBlock': to_block,
                })
                seen_txs = set()
                all_logs = list(logs_as_caller) + list(logs_as_owner)
                for log in all_logs:
                    tx_hash = log['transactionHash'].hex() if hasattr(log['transactionHash'], 'hex') else str(log['transactionHash'])
                    log_idx = log.get('logIndex', 0)
                    dedup_key = f"{tx_hash}:{log_idx}"
                    if dedup_key in seen_txs:
                        continue
                    seen_txs.add(dedup_key)
                    raw = log['data']
                    if isinstance(raw, str):
                        raw = bytes.fromhex(raw[2:] if raw.startswith('0x') else raw)
                    elif isinstance(raw, bytes):
                        pass
                    else:
                        raw = bytes(raw)
                    if len(raw) >= 64:
                        assets_raw = int.from_bytes(raw[:32], 'big')
                        total_deposited += assets_raw
                        print(f"   Found deposit: {assets_raw / 1e6:.2f} USDC in tx {tx_hash}")
            except Exception as log_err:
                print(f"⚠️ [XP] Log query error block {current_block}-{to_block}: {log_err}")
            current_block = to_block + 1

        total_usdc = total_deposited / 1e6
        print(f"🔍 [XP] Total deposited by {wallet}: {total_usdc:.2f} USDC (min: {XP_DEPOSIT_MIN_USDC})")

        if total_usdc < XP_DEPOSIT_MIN_USDC:
            return jsonify({
                'verified': False,
                'reason': f'Total deposits: ${total_usdc:.2f}. Need at least ${XP_DEPOSIT_MIN_USDC}.',
                'deposited': round(total_usdc, 2)
            }), 200

        event_id, awarded = award_xp(fid, 'deposit_10', 500, {'wallet': wallet, 'deposited_usdc': round(total_usdc, 2)}, f"deposit_10:{fid}")
        print(f"✅ [XP] deposit_10 verified for fid={fid}, awarded={awarded}, total=${total_usdc:.2f}")
        return jsonify({'verified': True, 'awarded': awarded, 'xp': 500, 'deposited': round(total_usdc, 2)})
    except Exception as e:
        print(f"❌ [XP] /api/tasks/verify/deposit_10 error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/tasks/verify/invite', methods=['POST'])
def api_verify_invite():
    """Verify that user has referred at least 1 user who completed at least 1 task. Awards 250 XP."""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/tasks/verify/invite fid={fid}")

        if not _verify_fid_exists(fid):
            return jsonify({'error': 'User not registered'}), 403

        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT DISTINCT xu.fid, xu.username
            FROM xp_users xu
            JOIN xp_events xe ON xe.fid = xu.fid
            WHERE xu.referrer_fid = %s
              AND xe.status = 'COMPLETED'
              AND xe.type != 'referral_bonus'
        """, (fid,))
        qualified_referees = cur.fetchall()
        cur.close()
        conn.close()

        referred_count = len(qualified_referees)
        print(f"🔍 [XP] FID {fid} has {referred_count} qualified referee(s)")

        if referred_count < 1:
            return jsonify({
                'verified': False,
                'reason': 'You need at least 1 referred user who has completed a task.',
                'referredCount': referred_count
            }), 200

        event_id, awarded = award_xp(fid, 'invite', 250, {'referredCount': referred_count}, f"invite:{fid}")
        print(f"✅ [XP] invite verified for fid={fid}, awarded={awarded}, referredCount={referred_count}")
        return jsonify({'verified': True, 'awarded': awarded, 'xp': 250, 'referredCount': referred_count})
    except Exception as e:
        print(f"❌ [XP] /api/tasks/verify/invite error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/tasks/claim/follow_x', methods=['POST'])
def api_claim_follow_x():
    """Claim follow_x task (manual review). Only available after follow_fc + deposit_10 + invite are COMPLETED."""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/tasks/claim/follow_x fid={fid}")

        if not _verify_fid_exists(fid):
            return jsonify({'error': 'User not registered'}), 403

        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT type, status FROM xp_events
            WHERE fid = %s AND type IN ('follow_fc', 'deposit_10', 'invite', 'follow_x')
        """, (fid,))
        events = cur.fetchall()
        event_map = {e['type']: e['status'] for e in events}

        existing = event_map.get('follow_x')
        if existing == 'COMPLETED':
            cur.close()
            conn.close()
            return jsonify({'claimed': False, 'reason': 'Task already completed'}), 200
        if existing == 'PENDING_REVIEW':
            cur.close()
            conn.close()
            return jsonify({'claimed': False, 'reason': 'Task already submitted, awaiting review'}), 200

        prereqs = ['follow_fc', 'deposit_10', 'invite']
        missing = [p for p in prereqs if event_map.get(p) != 'COMPLETED']
        if missing:
            cur.close()
            conn.close()
            return jsonify({
                'claimed': False,
                'locked': True,
                'reason': f"Complete these tasks first: {', '.join(missing)}"
            }), 200

        unique_key = f"follow_x:{fid}"
        cur.execute("""
            INSERT INTO xp_events (fid, type, xp, status, meta, unique_key)
            VALUES (%s, 'follow_x', 0, 'PENDING_REVIEW', %s, %s)
            ON CONFLICT (unique_key) DO NOTHING
            RETURNING id
        """, (fid, json.dumps({'claimed_at': datetime.utcnow().isoformat()}), unique_key))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        if row:
            print(f"✅ [XP] follow_x claimed by fid={fid}, event_id={row['id']}, awaiting review")
            return jsonify({'claimed': True, 'status': 'PENDING_REVIEW'})
        else:
            print(f"ℹ️ [XP] follow_x already claimed by fid={fid}")
            return jsonify({'claimed': False, 'reason': 'Task already submitted'})
    except Exception as e:
        print(f"❌ [XP] /api/tasks/claim/follow_x error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/admin/tasks/approve_follow_x', methods=['POST'])
def api_admin_approve_follow_x():
    """Admin endpoint to approve or reject follow_x task. Requires Bearer PMFI_ADMIN_TOKEN."""
    try:
        auth = flask_request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or auth[7:] != PMFI_ADMIN_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401

        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503

        data = flask_request.get_json(force=True)
        fid = data.get('fid')
        approve = data.get('approve', True)
        if not fid:
            return jsonify({'error': 'fid required'}), 400
        fid = int(fid)
        print(f"📝 [XP] /api/admin/tasks/approve_follow_x fid={fid} approve={approve}")

        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT id, status FROM xp_events
            WHERE fid = %s AND type = 'follow_x'
            ORDER BY id DESC LIMIT 1
        """, (fid,))
        event = cur.fetchone()

        if not event:
            cur.close()
            conn.close()
            return jsonify({'error': 'No follow_x claim found for this user'}), 404

        if event['status'] == 'COMPLETED':
            cur.close()
            conn.close()
            return jsonify({'error': 'Task already approved'}), 400

        if approve:
            cur.execute("""
                UPDATE xp_events SET status = 'COMPLETED', xp = 100
                WHERE id = %s AND status = 'PENDING_REVIEW'
            """, (event['id'],))
            if cur.rowcount > 0:
                _award_referral_bonus(cur, fid, event['id'], 100)
            conn.commit()
            print(f"✅ [XP] follow_x APPROVED for fid={fid}, event_id={event['id']}")
            cur.close()
            conn.close()
            return jsonify({'approved': True, 'fid': fid, 'xp': 100})
        else:
            cur.execute("""
                DELETE FROM xp_events WHERE id = %s AND status = 'PENDING_REVIEW'
            """, (event['id'],))
            conn.commit()
            print(f"❌ [XP] follow_x REJECTED for fid={fid}, event_id={event['id']}")
            cur.close()
            conn.close()
            return jsonify({'approved': False, 'fid': fid, 'rejected': True})
    except Exception as e:
        print(f"❌ [XP] /api/admin/tasks/approve_follow_x error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@flask_app.route('/api/leaderboard', methods=['GET'])
def api_xp_leaderboard():
    """Get XP leaderboard. Query: ?scope=all|weekly"""
    try:
        if not DATABASE_URL:
            return jsonify({'error': 'XP system not available'}), 503
        scope = flask_request.args.get('scope', 'all')
        print(f"📝 [XP] /api/leaderboard scope={scope}")
        conn = get_invite_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if scope == 'weekly':
            cur.execute("""
                SELECT e.fid, COALESCE(u.username, '') as username, SUM(e.xp) as xp_total
                FROM xp_events e
                LEFT JOIN xp_users u ON e.fid = u.fid
                WHERE e.status = 'COMPLETED' AND e.created_at >= NOW() - INTERVAL '7 days'
                GROUP BY e.fid, u.username
                ORDER BY xp_total DESC
                LIMIT 50
            """)
        else:
            cur.execute("""
                SELECT e.fid, COALESCE(u.username, '') as username, SUM(e.xp) as xp_total
                FROM xp_events e
                LEFT JOIN xp_users u ON e.fid = u.fid
                WHERE e.status = 'COMPLETED'
                GROUP BY e.fid, u.username
                ORDER BY xp_total DESC
                LIMIT 50
            """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'scope': scope, 'leaderboard': rows})
    except Exception as e:
        print(f"❌ [XP] /api/leaderboard error: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Background NAV Refresh
# =============================================================================

def nav_refresh_loop():
    """Refresh NAV cache periodically for instant /sign-nav responses."""
    print(f"🔄 Starting NAV refresh loop (every {NAV_CACHE_REFRESH_SECONDS}s)")
    while True:
        try:
            print("=" * 60)
            print("🔄 BACKGROUND NAV REFRESH")
            print("=" * 60)
            get_signed_nav_data_v7()
            print(f"✅ NAV cache updated, sleeping {NAV_CACHE_REFRESH_SECONDS}s")
        except Exception as e:
            print(f"❌ NAV refresh error: {e}")
        time.sleep(NAV_CACHE_REFRESH_SECONDS)


# =============================================================================
# V7.5 Withdrawal Exclusion: Read pending usdcLocked from on-chain queue
# =============================================================================

def get_withdrawal_bridge_in_transit() -> int:
    """
    Read total USDC currently being bridged for withdrawals (Polygon → Base).

    These funds have already been debited from Polymarket/Polygon cash (so
    totalAssets already dropped) but haven't arrived on Base yet.  If we also
    subtract them via the usdcLocked exclusion we double-count.

    Reads the servicer's withdrawal_state.json and sums pending in-transit
    bridge amounts.

    Returns:
        Total withdrawal bridge in-transit amount in raw 1e6 (USDC decimals).
        Returns 0 on any error (safe default = full exclusion applied).
    """
    state_paths = [
        os.path.join(os.path.dirname(__file__), "..", "trading_bot", "withdrawal_state.json"),
        os.path.join(os.path.dirname(__file__), "withdrawal_state.json"),
        "/opt/polymarket-bot/trading_bot/withdrawal_state.json",
    ]

    for path in state_paths:
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    data = json.load(f)

                in_transit = data.get("in_transit", [])
                total = 0.0
                for item in in_transit:
                    if item.get("status") == "pending":
                        total += item.get("amount_usdc", 0.0)

                total_raw = int(total * 1e6)
                if total_raw > 0:
                    print(f"   🔄 Withdrawal bridge in-transit: ${total:.2f} (from {os.path.basename(path)})")
                return total_raw
        except Exception as e:
            print(f"   ⚠️ Error reading servicer state from {path}: {e}")
            continue

    return 0


WITHDRAWAL_QUEUE_ABI = [
    {"inputs": [], "name": "nextWithdrawalIndex", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalPendingShares", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"name": "requestId", "type": "uint256"}],
        "name": "getWithdrawalRequest",
        "outputs": [
            {"name": "user", "type": "address"},
            {"name": "shares", "type": "uint256"},
            {"name": "usdcLocked", "type": "uint256"},
            {"name": "requestTime", "type": "uint256"},
            {"name": "claimed", "type": "bool"},
            {"name": "expired", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function"
    },
]

def get_pending_withdrawal_exclusions(vault_contract) -> Tuple[int, int]:
    """
    Read totalPendingShares and total usdcLocked from the on-chain withdrawal queue.
    
    V7.5 FIX: These values must be excluded from NAV calculation so that
    remaining LPs aren't priced against assets/shares that are spoken for.
    
    Uses getVaultState() to get pendingWithdrawalsCount for bounded iteration,
    then iterates from nextWithdrawalIndex to find exactly that many unclaimed requests.
    
    Returns:
        (total_pending_shares, total_usdc_locked) - both in raw units (18 dec / 6 dec)
    """
    if not vault_contract:
        return 0, 0
    
    try:
        state = vault_contract.functions.getVaultState().call()
        pending_shares = state[6]   # _totalPendingShares
        pending_count = state[7]    # _pendingWithdrawalsCount
        
        print(f"\n🔒 V7.5 Withdrawal Exclusion:")
        print(f"   totalPendingShares: {pending_shares/1e18:.6f}")
        print(f"   pendingWithdrawalsCount: {pending_count}")
        
        if pending_shares == 0 or pending_count == 0:
            print(f"   No pending withdrawals - no exclusion needed")
            return 0, 0
        
        next_idx = vault_contract.functions.nextWithdrawalIndex().call()
        print(f"   nextWithdrawalIndex: {next_idx}")
        
        total_usdc_locked = 0
        found = 0
        request_id = next_idx
        max_checks = pending_count + 50
        checked = 0
        
        while found < pending_count and checked < max_checks:
            try:
                user, shares, usdc_locked, request_time, claimed, expired = \
                    vault_contract.functions.getWithdrawalRequest(request_id).call()
                
                checked += 1
                
                if not claimed and not expired:
                    total_usdc_locked += usdc_locked
                    found += 1
                    print(f"      Request #{request_id}: {shares/1e18:.4f} shares, ${usdc_locked/1e6:.2f} locked")
                
                request_id += 1
                
            except Exception as e:
                if "Invalid request" in str(e) or "revert" in str(e).lower():
                    print(f"   Reached end of queue at request {request_id}")
                    break
                print(f"      ⚠️ Error reading request {request_id}: {e}")
                request_id += 1
                checked += 1
        
        print(f"   Checked {checked} requests, found {found}/{pending_count} pending, total locked: ${total_usdc_locked/1e6:.2f}")
        
        if found != pending_count:
            print(f"   ⚠️ Found {found} but expected {pending_count} pending requests - possible gap in queue")
        
        return pending_shares, total_usdc_locked
        
    except Exception as e:
        print(f"❌ Error reading withdrawal exclusions: {e}")
        return 0, 0


# =============================================================================
# Main
# =============================================================================

def load_abi(contract_name: str) -> dict:
    """Load ABI from frontend/abis folder."""
    bot_dir = Path(__file__).parent
    project_root = bot_dir.parent
    
    possible_paths = [
        project_root / "frontend" / "abis" / f"{contract_name}.json",
        bot_dir / "frontend" / "abis" / f"{contract_name}.json",
        project_root / "artifacts" / "contracts" / f"{contract_name}.sol" / f"{contract_name}.json",
    ]
    
    for path in possible_paths:
        if path.exists():
            with open(path, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                return data.get("abi", data)
    
    raise FileNotFoundError(f"ABI not found for {contract_name}")


def main():
    global w3, usdc, vault_v7, polymarket_client, nav_engine, oracle_account, pending_tracker, round_id_cache
    
    print("=" * 60)
    print("🚀 PredictFi Sniper Vault V7 - NAV Signing Bot")
    print("   3-State Asset Tracking + Conservation Bounds")
    print("=" * 60)
    
    # Validate config
    if not RPC_URL:
        print("❌ RPC_URL not set")
        sys.exit(1)
    if not VAULT_V7_ADDRESS:
        print("❌ VAULT_V7_ADDRESS not set")
        sys.exit(1)
    if not ORACLE_PRIVATE_KEY:
        print("❌ ORACLE_PRIVATE_KEY not set")
        sys.exit(1)
    if not POLYMARKET_PROXY_ADDRESS:
        print("❌ POLYMARKET_PROXY_ADDRESS not set")
        sys.exit(1)
    
    print(f"\n📋 Configuration:")
    print(f"   RPC: {RPC_URL[:30]}...")
    print(f"   Vault V7: {VAULT_V7_ADDRESS}")
    print(f"   USDC: {USDC_ADDRESS}")
    print(f"   PM Wallet: {POLYMARKET_PROXY_ADDRESS}")
    print(f"   PM Deposit: {POLYMARKET_BASE_DEPOSIT}")
    
    # Initialize invite code tables
    init_invite_tables()
    
    # Initialize XP system tables
    init_xp_tables()
    
    # Initialize Web3
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("❌ Failed to connect to RPC")
        sys.exit(1)
    print(f"\n✅ Connected to Base (block {w3.eth.block_number})")
    
    # Initialize oracle account
    oracle_account = Account.from_key(ORACLE_PRIVATE_KEY)
    print(f"🔑 Oracle signer: {oracle_account.address}")
    
    # Load ABIs and initialize contracts
    try:
        usdc_abi = load_abi("usdc")
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=usdc_abi)
        print(f"✅ USDC contract loaded")
    except Exception as e:
        print(f"⚠️ Could not load USDC contract: {e}")
        usdc = None
    
    try:
        vault_abi = load_abi("vault")
        existing_names = {entry.get("name") for entry in vault_abi if isinstance(entry, dict)}
        for entry in WITHDRAWAL_QUEUE_ABI:
            if entry.get("name") not in existing_names:
                vault_abi.append(entry)
                print(f"   + Added ABI entry: {entry['name']}")
        vault_v7 = w3.eth.contract(address=Web3.to_checksum_address(VAULT_V7_ADDRESS), abi=vault_abi)
        print(f"✅ Vault V7 contract loaded (with withdrawal queue ABI)")
    except Exception as e:
        print(f"⚠️ Could not load Vault contract: {e}")
        vault_v7 = None
    
    # Initialize pending credit tracker
    state_file = Path(__file__).parent / "pending_credit_state.json"
    pending_tracker = PendingCreditTracker(str(state_file))
    
    # Initialize roundId cache (prevents fallback to 0 on RPC errors)
    round_id_cache_file = Path(__file__).parent / "round_id_cache.json"
    round_id_cache = RoundIdCache(str(round_id_cache_file))
    
    # Try to read initial roundId from chain to populate cache
    try:
        if vault_v7:
            initial_round_id = vault_v7.functions.lastRoundId().call()
            round_id_cache.update_from_chain(initial_round_id)
            print(f"✅ Initial roundId from chain: {initial_round_id}")
    except Exception as e:
        print(f"⚠️ Could not read initial roundId from chain: {e}")
        if round_id_cache.cached_round_id:
            print(f"📂 Using cached roundId from disk: {round_id_cache.cached_round_id}")
        else:
            print(f"❌ CRITICAL: No roundId available! Bot will refuse to sign until RPC works.")
    
    # Initialize Polymarket client with private key for L2 auth (open orders)
    # Use POLYMARKET_PRIVATE_KEY if available, otherwise fall back to ORACLE_PRIVATE_KEY
    pm_private_key = os.getenv("POLYMARKET_PRIVATE_KEY") or ORACLE_PRIVATE_KEY
    polymarket_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS, private_key=pm_private_key)
    
    # Initialize NAV engine
    nav_engine = NavEngineV7(polymarket_client, pending_tracker)
    
    # Initial NAV calculation
    try:
        get_signed_nav_data_v7()
    except Exception as e:
        print(f"⚠️ Initial NAV calculation failed: {e}")
    
    # Start background refresh
    refresh_thread = threading.Thread(target=nav_refresh_loop, daemon=True)
    refresh_thread.start()
    
    # Start HTTP server
    print(f"\n🚀 Starting HTTP API on port {HTTP_PORT}")
    print(f"   Serving frontend from: {FRONTEND_DIR}")
    print(f"   Endpoints:")
    print(f"   - GET  /             - Frontend UI")
    print(f"   - GET  /health       - Health check")
    print(f"   - GET  /price        - Cached price (for UI)")
    print(f"   - GET  /sign-nav     - Get signed NavDataV7 (for transactions)")
    print(f"   - GET  /sign-nav/debug - Debug NAV calculation")
    print(f"\n   Press Ctrl+C to stop\n")
    
    flask_app.run(host='127.0.0.1', port=HTTP_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
