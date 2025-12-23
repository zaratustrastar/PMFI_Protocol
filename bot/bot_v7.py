#!/usr/bin/env python3
"""
PredictFi Sniper Vault V7 - NAV Signing Bot with 3-State Asset Tracking

=============================================================================
ARCHITECTURE:
=============================================================================

V7 tracks THREE asset states for accurate NAV during Polymarket bridging:

1. inFlightOnChain - USDC sitting at Polymarket Base deposit address (usually ~0)
2. pendingCredit - Forwarded to PM but not yet visible in API (during bridge)
3. creditedAssets - PM cash + positions visible via API

pendingCredit reconciliation (cash-only, avoids market fluctuations):
    pendingCredit = max(0, totalForwarded - polymarketCash - withdrawnBackToVault)

Conservation bound replaces 5% NAV change limit:
    totalAssets >= expectedAssets * (1 - maxLossBps)

=============================================================================
SAFETY VALVES:
=============================================================================

1. maxPendingAge - If any deposit pending > X hours, pause deposits
2. maxPendingRatio - If pendingCredit > 50% of totalAssets, pause deposits

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
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

import requests
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from flask import Flask, jsonify, request as flask_request, send_from_directory
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
POLYMARKET_BASE_DEPOSIT = "0xa76a91208FC7CB88420070AF978D12F440cab2F0"

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
MAX_PENDING_RATIO = 0.5   # Pause if pendingCredit > 50% of totalAssets

# Rate limiting
REFRESH_RATE_LIMIT_SECONDS = 5
last_refresh_request = {}

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
        if round_id > 0:
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
        if chain_round_id is not None and chain_round_id > 0:
            # Chain read succeeded - this is authoritative
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
    
    def calculate_pending_credit(self, pm_cash_usdc: int, reserved_usdc: int = 0, cost_basis_usdc: int = 0, in_flight_usdc: int = 0) -> int:
        """
        Calculate pending credit using full asset reconciliation.
        
        FIXED FORMULA (V7.2):
        pendingCredit = max(0, totalForwarded - pmCash - reserved - costBasis - withdrawnBack - inFlight)
        
        This prevents double-counting:
        - When you buy tokens, cash goes down but costBasis goes up (net zero change to pending)
        - When you place buy orders, cash goes down but reserved goes up (net zero change to pending)
        - Funds at deposit address are counted in inFlight, NOT pendingCredit
        - pendingCredit should only be non-zero when funds are swept but not yet visible in PM
        
        At any moment, forwarded funds live in exactly ONE of:
        - inFlight (still at deposit address on Base)
        - pendingCredit (swept/bridging, not visible yet in PM)
        - cash/reserved/costBasis (credited and inside PM account)
        - withdrawnBack (returned on-chain)
        
        Args:
            pm_cash_usdc: Polymarket cash balance (in 6 decimals)
            reserved_usdc: USDC locked in open buy orders (in 6 decimals)
            cost_basis_usdc: Total cost basis of positions (in 6 decimals)
            in_flight_usdc: USDC at deposit address (in 6 decimals) - V7.2 fix
        """
        self.last_known_pm_cash = pm_cash_usdc
        
        # Total "accounted for" = cash + reserved + costBasis + inFlight
        accounted_for = pm_cash_usdc + reserved_usdc + cost_basis_usdc + in_flight_usdc
        
        # Pending = what we sent minus what's accounted for minus what came back
        pending = max(0, self.total_forwarded - accounted_for - self.withdrawn_back)
        
        print(f"📊 Pending Credit Calculation (V7.2 - no double-count):")
        print(f"   totalForwarded: ${self.total_forwarded/1e6:.2f}")
        print(f"   - pmCash:       ${pm_cash_usdc/1e6:.2f}")
        print(f"   - reserved:     ${reserved_usdc/1e6:.2f}")
        print(f"   - costBasis:    ${cost_basis_usdc/1e6:.2f}")
        print(f"   - inFlight:     ${in_flight_usdc/1e6:.2f}")
        print(f"   - withdrawn:    ${self.withdrawn_back/1e6:.2f}")
        print(f"   = pendingCredit: ${pending/1e6:.2f}")
        
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

flask_app = Flask(__name__)
CORS(flask_app)

_script_dir = Path(__file__).resolve().parent
FRONTEND_DIR = _script_dir / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = _script_dir.parent / "frontend"

@flask_app.route('/')
def serve_index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@flask_app.route('/<path:filename>')
def serve_static(filename):
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
        
        Tries multiple methods:
        1. CLOB API get_balance_allowance (requires L2 auth)
        2. Direct Polygon blockchain query for USDC.e balance
        3. Data API fallback (may 404)
        """
        # Method 1: Use CLOB client with L2 auth (get_balance_allowance with params)
        if self.clob_client and self.BalanceAllowanceParams and self.AssetType:
            try:
                print("📡 Fetching cash balance via L2 auth...")
                # Must pass BalanceAllowanceParams with asset_type=COLLATERAL for USDC balance
                params = self.BalanceAllowanceParams(asset_type=self.AssetType.COLLATERAL)
                balance_data = self.clob_client.get_balance_allowance(params=params)
                
                if balance_data:
                    # Balance is returned in USDC units (string format)
                    cash = float(balance_data.get("balance", 0))
                    if cash > 0:
                        print(f"💵 Polymarket cash (CLOB): ${cash:.2f}")
                        return cash
                    
            except Exception as e:
                print(f"⚠️ CLOB balance fetch failed: {e}")
        
        # Method 2: Direct Polygon blockchain query for USDC.e balance
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
    Calculates NAV with 3-state asset tracking (V7.2 - no double-counting).
    
    totalAssets = inFlightOnChain + pendingCredit + cash + reserved + positionsLiquidationValue
    
    Key insight: Funds can only be in ONE bucket at a time:
    - inFlight: at deposit address on Base
    - pendingCredit: swept/bridging, not visible yet in PM
    - cash/reserved/costBasis: credited inside PM
    - withdrawnBack: returned to vault
    
    pendingCredit = totalForwarded - cash - reserved - costBasis - inFlight - withdrawnBack
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
    
    def calculate_nav_breakdown(self) -> Dict:
        """
        Calculate full NAV breakdown with 3 asset states.
        
        V7.2 FIX: Subtracts inFlight from pendingCredit to prevent double-counting
        when funds are at the deposit address.
        
        pendingCredit = totalForwarded - cash - reserved - costBasis - inFlight - withdrawn
        totalAssets = inFlight + pendingCredit + cash + reserved + liquidationValue
        
        Funds live in exactly ONE bucket at any time - no overlap.
        
        Returns:
            Dict with all asset components in 6 decimals
        """
        print(f"\n{'='*60}")
        print(f"💰 CALCULATING V7.2 NAV (No double-count)")
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
                print(f"   ❌ {outcome}: {size:.1f} - NO BIDS (illiquid)")
                # Use mid value as fallback for illiquid positions
                positions_liq_value += mid_value
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
        
        # 6. Calculate pending credit using FULL reconciliation (V7.2 fix)
        # pendingCredit = totalForwarded - cash - reserved - costBasis - inFlight - withdrawn
        # This prevents double-counting when funds are at the deposit address
        pending_credit = self.pending_tracker.calculate_pending_credit(
            credited_cash, reserved_usdc, cost_basis_usdc, in_flight
        )
        
        # 7. Total assets for NAV = inFlight + pending + cash + reserved + liquidationValue
        # NOTE: We use liquidation value for NAV (share pricing), NOT cost basis
        total_assets = in_flight + pending_credit + credited_cash + reserved_usdc + credited_positions
        
        print(f"\n📊 Asset Breakdown (V7.2 - no double-count):")
        print(f"   • In-flight (deposit addr): ${in_flight/1e6:.2f}")
        print(f"   • Pending credit:           ${pending_credit/1e6:.2f}")
        print(f"   • Credited cash:            ${credited_cash/1e6:.2f}")
        print(f"   • Reserved (open orders):   ${reserved_usdc/1e6:.2f}")
        print(f"   • Positions (liquidation):  ${credited_positions/1e6:.2f}")
        print(f"   • Positions (cost basis):   ${cost_basis_usdc/1e6:.2f}")
        print(f"   ─────────────────────────────")
        print(f"   • TOTAL ASSETS:             ${total_assets/1e6:.2f}")
        print(f"   (inFlight + pending subtracted from total to avoid overlap)")
        
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


def check_safety_valves(breakdown: Dict) -> Tuple[str, str]:
    """
    Check safety valves and return status.
    
    Returns:
        Tuple of (status, reason)
        status: "ok", "warning", or "pause"
    """
    global pending_tracker
    
    pending_credit = breakdown.get("pending_credit", 0)
    total_assets = breakdown.get("total_assets", 0)
    
    # Check max pending age (now uses pre-calculated pending_credit)
    oldest_age = pending_tracker.get_oldest_pending_age_hours(pending_credit)
    if oldest_age > MAX_PENDING_AGE_HOURS:
        return "pause", f"Oldest deposit pending {oldest_age:.1f} hours (max {MAX_PENDING_AGE_HOURS}h)"
    
    # Check max pending ratio
    if total_assets > 0:
        pending_ratio = pending_credit / total_assets
        if pending_ratio > MAX_PENDING_RATIO:
            return "pause", f"Pending ratio {pending_ratio:.1%} exceeds {MAX_PENDING_RATIO:.0%}"
        elif pending_ratio > MAX_PENDING_RATIO * 0.8:
            return "warning", f"Pending ratio {pending_ratio:.1%} approaching limit"
    
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
            
            print(f"📊 On-chain: supply={total_supply/1e18:.4f}, buffer={vault_buffer/1e6:.2f}, forwarded={total_forwarded/1e6:.2f}, lastRoundId={chain_round_id}")
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
    
    # Get next roundId using cache (handles RPC failures safely)
    new_round_id = round_id_cache.get_next_round_id(chain_round_id)
    if new_round_id is None:
        # Cannot determine safe roundId - refuse to sign
        raise ValueError("Cannot sign NAV: RPC failed and no cached roundId available. Please restart bot with working RPC.")
    
    # Calculate NAV breakdown
    if nav_engine:
        breakdown = nav_engine.calculate_nav_breakdown()
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
    
    # Check safety valves (V7.1: now uses pending_credit from breakdown)
    safety_status, safety_reason = check_safety_valves(breakdown)
    if safety_status == "pause":
        print(f"🛑 SAFETY VALVE TRIGGERED: {safety_reason}")
    elif safety_status == "warning":
        print(f"⚠️ SAFETY WARNING: {safety_reason}")
    
    # Calculate NAV per share
    # V7.3 FIX: Include vault buffer AND reserved INSIDE creditedCash for signing
    # This ensures: totalAssets = creditedCash + creditedPositions + pendingCredit + inFlight
    # The contract's asset breakdown check requires this exact equality (only 4 fields)
    reserved_usdc = breakdown.get("reserved", 0)
    credited_cash_signed = breakdown["credited_cash"] + vault_buffer + reserved_usdc
    total_assets = credited_cash_signed + breakdown["credited_positions"] + breakdown["pending_credit"] + breakdown["in_flight"]
    
    # V7.3 ASSERTION: Verify totalAssets equals the sum of 4 signed fields
    expected_sum = credited_cash_signed + breakdown["credited_positions"] + breakdown["pending_credit"] + breakdown["in_flight"]
    if total_assets != expected_sum:
        raise ValueError(f"Asset breakdown mismatch! totalAssets={total_assets} != sum={expected_sum}")
    
    print(f"📊 Total Assets Breakdown (V7.3):")
    print(f"   • PM Cash:        ${breakdown['credited_cash']/1e6:.2f}")
    print(f"   • Reserved:       ${reserved_usdc/1e6:.2f}")
    print(f"   • Vault Buffer:   ${vault_buffer/1e6:.2f}")
    print(f"   • Credited Cash (signed): ${credited_cash_signed/1e6:.2f}")
    print(f"   • Positions:      ${breakdown['credited_positions']/1e6:.2f}")
    print(f"   • Pending Credit: ${breakdown['pending_credit']/1e6:.2f}")
    print(f"   • In-Flight:      ${breakdown['in_flight']/1e6:.2f}")
    print(f"   ─────────────────────────────")
    print(f"   • TOTAL ASSETS:   ${total_assets/1e6:.2f}")
    print(f"   ✅ Breakdown check PASSED")
    
    if total_supply > 0:
        nav = (total_assets * NAV_PRECISION) // total_supply
    else:
        nav = 10**6  # $1.00 per share
    
    timestamp = now
    deadline = now + NAV_VALIDITY_SECONDS
    
    # Sign the full breakdown (credited_cash includes vault buffer)
    signature, signer = sign_nav_data_v7(
        total_assets,
        credited_cash_signed,
        breakdown["credited_positions"],
        breakdown["pending_credit"],
        breakdown["in_flight"],
        timestamp,
        deadline,
        new_round_id,
        VAULT_V7_ADDRESS
    )
    
    # Update cache - IMPORTANT: Keep raw PM cash for pending credit calculations
    # The signed credited_cash is only used for signing, not for internal accounting
    with nav_lock:
        cached_nav = {
            "total_assets": total_assets,
            "credited_cash": breakdown["credited_cash"],  # Raw PM cash, NOT signed
            "credited_positions": breakdown["credited_positions"],
            "pending_credit": breakdown["pending_credit"],
            "in_flight": breakdown["in_flight"],
            "reserved": reserved_usdc,                    # Separate reserved tracking
            "cost_basis": breakdown.get("cost_basis", 0),
            "nav": nav,
            "round_id": new_round_id,
            "last_calculated": now,
            "total_supply": total_supply,
            "vault_buffer": vault_buffer,
            "credited_cash_signed": credited_cash_signed,  # Store signed version separately
            "safety_status": safety_status,
            "safety_reason": safety_reason,
            "expected_assets": expected_assets,
        }
    
    print(f"✅ Signed NAV: ${nav/1e6:.4f}/share (round {new_round_id}) [{safety_status}]")
    
    # Build response and cache it
    result = {
        "navData": {
            "totalAssets": str(total_assets),
            "creditedCash": str(credited_cash_signed),
            "creditedPositions": str(breakdown["credited_positions"]),
            "pendingCredit": str(breakdown["pending_credit"]),
            "inFlightOnChain": str(breakdown["in_flight"]),
            "timestamp": timestamp,
            "deadline": deadline,
            "roundId": new_round_id,
            "nav": str(nav),
        },
        "signature": "0x" + signature,
        "signer": signer,
        "metadata": {
            "nav_raw": str(nav),
            "price_per_share": nav / 1e6,
            "total_assets_usdc": total_assets / 1e6,
            "total_supply": total_supply / 1e18,
            "vault_buffer_usdc": vault_buffer / 1e6 if vault_buffer else 0,
            "credited_cash_usdc": breakdown["credited_cash"] / 1e6,
            "credited_positions_usdc": breakdown["credited_positions"] / 1e6,
            "pending_credit_usdc": breakdown["pending_credit"] / 1e6,
            "in_flight_usdc": breakdown["in_flight"] / 1e6,
            "reserved_usdc": breakdown.get("reserved", 0) / 1e6,      # V7.1
            "cost_basis_usdc": breakdown.get("cost_basis", 0) / 1e6,  # V7.1
            "valid_until": deadline,
            "safety_status": safety_status,
            "safety_reason": safety_reason,
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
        vault_v7 = w3.eth.contract(address=Web3.to_checksum_address(VAULT_V7_ADDRESS), abi=vault_abi)
        print(f"✅ Vault V7 contract loaded")
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
    
    flask_app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
