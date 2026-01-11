#!/usr/bin/env python3
"""
Withdrawal Servicer - V7.2 Simplified Withdrawal Flow

Algorithm (matches V7.2 "100% to PM" architecture):
1. Read pending withdrawals in USDC (pendingShares × NAV)
2. Read vault USDC already on Base
3. Subtract in-transit USDC (already bridging)
4. Calculate: needed = pendingUSDC - vaultUSDC - inTransitUSDC
5. If needed <= 0: nothing to do
6. Withdraw PM cash (min of needed vs withdrawable)
7. If cash < needed: liquidate positions (largest/most liquid first)
8. Bridge to vault address on Base
9. Track in-transit until arrival
10. Repeat with rate limits and kill switches

Key differences from liquidity_keeper.py:
- NO treasury wallet intermediary
- Funds bridge DIRECTLY to vault address
- Simple 3-step: PM cash → liquidate if needed → bridge to vault
"""

import os
import sys
import time
import json
import subprocess
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
import requests

try:
    from curl_cffi import requests as curl_requests
    BYPASS_METHOD = "curl_cffi"
except ImportError:
    import requests as curl_requests
    BYPASS_METHOD = "standard"

try:
    from relay_bridge import bridge_usdc_polygon_to_base, get_bridge_quote
    HAS_RELAY_BRIDGE = True
except ImportError:
    HAS_RELAY_BRIDGE = False
    print("⚠️  relay_bridge not available, bridging disabled")

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
    from py_clob_client.order_builder.constants import SELL
    import py_clob_client.http_helpers.helpers as http_helpers
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    http_helpers = None
    print("⚠️  py-clob-client not available, liquidation disabled")

# Module-level patched CLOB client (initialized once, retries on failure)
_CLOB_CLIENT = None

load_dotenv()

# =============================================================================
# PRIVATE KEY NORMALIZATION
# =============================================================================

def normalize_privkey(raw: str, name: str = "key") -> str:
    """
    Normalize and validate a private key.
    Strips quotes, whitespace, validates hex format.
    Returns normalized key with 0x prefix or raises ValueError.
    """
    if not raw:
        return ""
    
    s = raw.strip().strip('"').strip("'").strip()
    
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    
    if len(s) != 64:
        raise ValueError(f"{name} wrong length: got {len(s)}, expected 64 hex chars. Value: {repr(raw[:20])}...")
    
    try:
        int(s, 16)
    except ValueError:
        raise ValueError(f"{name} contains non-hex characters. Value: {repr(raw[:20])}...")
    
    return "0x" + s.lower()


# =============================================================================
# CONFIGURATION
# =============================================================================

VAULT_ADDRESS = os.getenv("VAULT_V7_ADDRESS", "0xfcfa01291d1e75f71e97c4EE53f675D7622988b4")
PM_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")

# Normalize private key at startup
_raw_pm_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
try:
    PM_PRIVATE_KEY = normalize_privkey(_raw_pm_key, "POLYMARKET_PRIVATE_KEY") if _raw_pm_key else ""
except ValueError as e:
    print(f"❌ {e}")
    PM_PRIVATE_KEY = ""

# CLOB L2 API credentials (for trading - derived from create_or_derive_api_creds)
# These can be pre-derived and stored in .env to avoid Cloudflare blocking
PM_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
PM_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
PM_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")

# Signature type for Polymarket CLOB:
#   0 = Standard EOA (MetaMask hardware wallet, direct signing)
#   1 = Magic/Email wallet (EOA signs for Magic proxy)
#   2 = Browser wallet proxy (MetaMask browser extension -> Polymarket proxy)
# Default to 2 for MetaMask browser wallet users
PM_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))

BASE_RPC_URL = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
PROXY_URL = os.getenv("PROXY_URL", "")  # Use socks5:// for SOCKS proxies, http:// for HTTP proxies

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

NAV_PRECISION = 10**18
LOOP_INTERVAL_SECONDS = 60
MIN_WITHDRAWAL_USDC = 5.0
MAX_DAILY_WITHDRAWAL_USDC = 50000.0
MAX_PER_CYCLE_LIQUIDATION_USDC = 2000.0
MAX_SLIPPAGE_BPS = 300
WITHDRAWAL_SLIPPAGE_BPS = 50  # 0.5% buffer for bridge fees and rounding

STATE_FILE = os.path.join(os.path.dirname(__file__), "withdrawal_state.json")

# Bot URL for NAV fetching (bot_v7.py running on VPS or localhost)
BOT_URL = os.getenv("BOT_V7_URL", "http://localhost:8080")

# Relay API for bridge status polling
RELAY_API_URL = "https://api.relay.link"

# =============================================================================
# ABIs
# =============================================================================

VAULT_V7_ABI = [
    {"inputs": [], "name": "totalPendingShares", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "nextWithdrawalIndex", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [],
        "name": "getVaultState",
        "outputs": [
            {"name": "_lastRoundId", "type": "uint256"},
            {"name": "_lastNavTimestamp", "type": "uint256"},
            {"name": "_totalSupply", "type": "uint256"},
            {"name": "_vaultBuffer", "type": "uint256"},
            {"name": "_expectedAssets", "type": "uint256"},
            {"name": "_totalForwarded", "type": "uint256"},
            {"name": "_totalPendingShares", "type": "uint256"},
            {"name": "_pendingWithdrawalsCount", "type": "uint256"},
            {"name": "_paused", "type": "bool"},
            {"name": "_depositsThrottled", "type": "bool"},
            {"name": "_maxLossBps", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function"
    },
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

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# Transfer event ABI for scanning USDC transfers
TRANSFER_EVENT_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"}
    ],
    "name": "Transfer",
    "type": "event"
}

# Event-driven in-transit configuration
STALE_THRESHOLD_SECONDS = 180  # 3 minutes before widening log window
MAX_LOG_LOOKBACK_BLOCKS = 1000  # ~30 min of Base blocks
DUST_THRESHOLD_USDC = 0.50  # Allow 50 cents absolute difference for matching
FEE_TOLERANCE_PERCENT = 3.0  # Allow 3% fee deduction for matching (covers Relay fees)
MAX_PENDING_AGE_SECONDS = 900  # 15 min max before auto-clearing stuck pending item

# Fallback RPC URLs for self-healing
BASE_RPC_FALLBACKS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base.meowrpc.com",
    "https://1rpc.io/base",
]


def get_patched_clob_client():
    """
    Get or create a CLOB client with curl_cffi proxy patching for Cloudflare bypass.
    Initialized once per process. Retries on transient failures.
    """
    global _CLOB_CLIENT
    
    # Return cached client if already initialized successfully
    if _CLOB_CLIENT is not None:
        return _CLOB_CLIENT
    
    # These are permanent failures - no retry
    if not HAS_CLOB_CLIENT:
        return None
    
    # Check if we have signing prerequisites for proxy wallet trading
    # API credentials will be derived from the private key
    if not PM_PRIVATE_KEY:
        print("⚠️  Missing POLYMARKET_PRIVATE_KEY (EOA that controls the proxy)")
        return None
    
    if not PM_PROXY_ADDRESS:
        print("⚠️  Missing POLYMARKET_PROXY_ADDRESS (your Polymarket proxy wallet)")
        return None
    
    try:
        print("🔧 Initializing patched CLOB client for liquidation...")
        
        # Create CLOB client - private key needed for signing orders even with explicit API creds
        if not PM_PRIVATE_KEY:
            print("⚠️  POLYMARKET_PRIVATE_KEY required for signing orders")
            return None
        
        # Normalize the private key - strip 0x prefix for py-clob-client
        normalized_key = normalize_privkey(PM_PRIVATE_KEY)
        if not normalized_key:
            print("⚠️  Invalid POLYMARKET_PRIVATE_KEY format")
            return None
        # py-clob-client works with both formats, but docs recommend without 0x
        key_for_client = normalized_key[2:] if normalized_key.startswith("0x") else normalized_key
        
        # Derive EOA address from private key - for diagnostics only
        from eth_account import Account
        eoa_address = Account.from_key(normalized_key).address
        print(f"   📋 EOA (signer): {eoa_address[:10]}...")
        print(f"   📋 Proxy wallet (funder): {PM_PROXY_ADDRESS[:10]}..." if PM_PROXY_ADDRESS else "   📋 Proxy wallet: NOT SET")
        
        # For Polymarket proxy wallets:
        # - signature_type=0: Standard EOA (direct signing, no proxy)
        # - signature_type=1: Magic/email wallet (EOA signs for Magic proxy)
        # - signature_type=2: Browser wallet proxy (MetaMask browser -> PM proxy)
        # - funder: PROXY address (where funds are held on Polymarket)
        # - key: Private key of the EOA that controls the proxy (without 0x prefix)
        print(f"   📋 Signature type: {PM_SIGNATURE_TYPE} ({'EOA' if PM_SIGNATURE_TYPE == 0 else 'Magic' if PM_SIGNATURE_TYPE == 1 else 'Browser proxy'})")
        client = ClobClient(
            "https://clob.polymarket.com",
            key=key_for_client,
            chain_id=137,
            signature_type=PM_SIGNATURE_TYPE,
            funder=PM_PROXY_ADDRESS,
        )
        
        # Patch HTTP helpers with curl_cffi for Cloudflare bypass
        if BYPASS_METHOD == "curl_cffi" and http_helpers:
            proxy_config = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
            
            def get_browser_headers(original_headers: dict = None) -> dict:
                browser_headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Referer': 'https://polymarket.com/',
                    'Origin': 'https://polymarket.com',
                }
                if original_headers:
                    browser_headers.update(original_headers)
                return browser_headers
            
            def patched_get(endpoint: str, headers: dict = None, params: dict = None, **kwargs):
                # Remove keys we handle explicitly, pass rest through
                kwargs.pop('proxies', None)  # We use our proxy_config
                timeout = kwargs.pop('timeout', 30)
                response = curl_requests.get(
                    endpoint,
                    headers=get_browser_headers(headers),
                    params=params,
                    impersonate="chrome120",
                    proxies=proxy_config,
                    timeout=timeout,
                    **kwargs,
                )
                return response.json() if response.text else {}
            
            def patched_post(endpoint: str, headers: dict = None, body: dict = None, data=None, json=None, **kwargs):
                # py-clob-client may pass data= or json= or body=
                # Normalize to json for curl_requests
                payload = json or body
                raw_data = None
                if payload is None and data is not None:
                    # data might be a dict or JSON string
                    if isinstance(data, dict):
                        payload = data
                    elif isinstance(data, (str, bytes)):
                        try:
                            import json as _json
                            payload = _json.loads(data)
                        except:
                            raw_data = data  # Keep as raw data if not JSON
                
                # Remove keys we handle explicitly, pass rest through
                kwargs.pop('proxies', None)  # We use our proxy_config
                timeout = kwargs.pop('timeout', 30)
                
                response = curl_requests.post(
                    endpoint,
                    headers=get_browser_headers(headers),
                    json=payload,
                    data=raw_data,
                    impersonate="chrome120",
                    proxies=proxy_config,
                    timeout=timeout,
                    **kwargs,
                )
                return response.json() if response.text else {}
            
            def patched_delete(endpoint: str, headers: dict = None, **kwargs):
                # Remove keys we handle explicitly, pass rest through
                kwargs.pop('proxies', None)  # We use our proxy_config
                timeout = kwargs.pop('timeout', 30)
                response = curl_requests.delete(
                    endpoint,
                    headers=get_browser_headers(headers),
                    impersonate="chrome120",
                    proxies=proxy_config,
                    timeout=timeout,
                    **kwargs,
                )
                return response.json() if response.text else {}
            
            http_helpers.get = patched_get
            http_helpers.post = patched_post
            http_helpers.delete = patched_delete
            
            # CRITICAL: Also patch the direct imports in py_clob_client.client
            # The client.py imports `from .http_helpers.helpers import post, get, delete`
            # which creates local bindings that don't update when we patch http_helpers
            import py_clob_client.client as clob_client_module
            clob_client_module.get = patched_get
            clob_client_module.post = patched_post
            clob_client_module.delete = patched_delete
            
            if PROXY_URL:
                proxy_display = PROXY_URL.split('@')[1] if '@' in PROXY_URL else PROXY_URL
                print(f"   🌐 Using proxy: {proxy_display}")
            print("   🔧 Patched HTTP with curl_cffi (Chrome 120 TLS)")
        
        # Startup diagnostics (without leaking secrets)
        wallet_preview = PM_PROXY_ADDRESS[:10] + "..." if PM_PROXY_ADDRESS else "NOT SET"
        pk_present = bool(PM_PRIVATE_KEY and len(PM_PRIVATE_KEY) > 10)
        print(f"   📋 Wallet: {wallet_preview}")
        print(f"   📋 Private key present: {pk_present}")
        print(f"   📋 CLOB host: https://clob.polymarket.com")
        
        # For proxy wallet trading (signature_type=1), we need L2 CLOB credentials.
        # Priority:
        # 1. Use pre-derived credentials from env vars (POLYMARKET_API_KEY/SECRET/PASSPHRASE)
        # 2. Try to derive credentials using create_or_derive_api_creds() (may fail due to Cloudflare)
        
        from py_clob_client.clob_types import ApiCreds
        
        creds = None
        
        # FIRST: Check for pre-derived credentials in env vars
        if PM_API_KEY and PM_API_SECRET and PM_API_PASSPHRASE:
            print("   🔑 Using pre-derived API credentials from env vars...")
            creds = ApiCreds(
                api_key=PM_API_KEY,
                api_secret=PM_API_SECRET,
                api_passphrase=PM_API_PASSPHRASE
            )
            client.set_api_creds(creds)
            print(f"   ✅ Credentials loaded (key: {PM_API_KEY[:8]}...)")
        else:
            # FALLBACK: Try to derive credentials (may fail due to Cloudflare)
            print("   🔑 No pre-derived creds, attempting derivation...")
            try:
                creds = client.create_or_derive_api_creds()
                if creds and hasattr(creds, 'api_key') and creds.api_key:
                    client.set_api_creds(creds)
                    print(f"   ✅ Credentials derived successfully (key: {creds.api_key[:8]}...)")
                    print(f"   💡 TIP: Save these to .env to avoid Cloudflare issues:")
                    print(f"      POLYMARKET_API_KEY={creds.api_key}")
                    print(f"      POLYMARKET_API_SECRET={creds.api_secret}")
                    print(f"      POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
                else:
                    print(f"   ❌ Credential derivation returned empty")
                    print("   💡 Run derivation locally and add to .env:")
                    print("      POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE")
                    return None
            except Exception as cred_error:
                print(f"   ❌ Credential derivation failed: {cred_error}")
                print("   💡 Run derivation locally and add to .env:")
                print("      POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE")
                return None
        
        # CRITICAL: Verify L2 auth works before caching - fail fast
        try:
            client.assert_level_2_auth()
            print("   ✅ L2 auth verified - CLOB client ready for trading")
        except Exception as auth_error:
            print(f"   ❌ L2 auth check failed: {auth_error}")
            print("   💡 Cannot post orders without L2 auth")
            return None
        
        _CLOB_CLIENT = client
        return client
        
    except Exception as e:
        print(f"⚠️  Failed to initialize CLOB client (will retry): {e}")
        return None


# =============================================================================
# STATE TRACKING
# =============================================================================

@dataclass
class InTransitItem:
    """Represents funds currently bridging from Polygon to Base."""
    request_id: str
    amount_usdc: float
    initiated_at: float
    tx_hash: str = ""
    status: str = "pending"

@dataclass 
class ServicerState:
    """Persistent state for the withdrawal servicer."""
    in_transit: List[Dict] = field(default_factory=list)
    daily_withdrawn_usdc: float = 0.0
    daily_reset_timestamp: float = 0.0
    last_run_timestamp: float = 0.0
    prev_vault_balance: float = 0.0
    unclaimed_delta: float = 0.0


def load_state() -> ServicerState:
    """Load servicer state from disk."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                state = ServicerState(
                    in_transit=data.get("in_transit", []),
                    daily_withdrawn_usdc=data.get("daily_withdrawn_usdc", 0.0),
                    daily_reset_timestamp=data.get("daily_reset_timestamp", 0.0),
                    last_run_timestamp=data.get("last_run_timestamp", 0.0),
                    prev_vault_balance=data.get("prev_vault_balance", 0.0),
                    unclaimed_delta=data.get("unclaimed_delta", 0.0),
                )
                now = time.time()
                if now - state.daily_reset_timestamp > 86400:
                    state.daily_withdrawn_usdc = 0.0
                    state.daily_reset_timestamp = now
                return state
    except Exception as e:
        print(f"⚠️  Error loading state: {e}")
    return ServicerState(daily_reset_timestamp=time.time())


def save_state(state: ServicerState) -> None:
    """Save servicer state to disk."""
    try:
        data = {
            "in_transit": state.in_transit,
            "daily_withdrawn_usdc": state.daily_withdrawn_usdc,
            "daily_reset_timestamp": state.daily_reset_timestamp,
            "last_run_timestamp": state.last_run_timestamp,
            "prev_vault_balance": state.prev_vault_balance,
            "unclaimed_delta": state.unclaimed_delta,
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️  Error saving state: {e}")


# =============================================================================
# TELEGRAM ALERTS
# =============================================================================

def send_telegram_alert(message: str, is_error: bool = False) -> None:
    """Send alert to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    prefix = "🚨" if is_error else "💰"
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"{prefix} [WithdrawalServicer]\n{message}",
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"⚠️  Telegram send failed: {e}")


# =============================================================================
# BLOCKCHAIN READS
# =============================================================================

def get_nav_from_bot() -> Tuple[float, Optional[Dict]]:
    """
    Fetch current NAV from bot_v7.py /sign-nav endpoint.
    This uses the actual signed NAV that includes pendingCredit and trading PnL.
    
    Returns:
        (nav_per_share, full_response_dict or None)
    """
    try:
        response = requests.get(f"{BOT_URL}/sign-nav", timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                nav_data = data.get("navData", {})
                total_assets = nav_data.get("totalAssets", 0)
                total_supply = nav_data.get("totalSupply", 0)
                
                if total_supply > 0 and total_assets > 0:
                    nav = (total_assets / 1e6) / (total_supply / 1e18)
                    print(f"   NAV from bot /sign-nav: ${nav:.6f} (totalAssets=${total_assets/1e6:.2f})")
                    return nav, data
            
            cached_nav = data.get("cachedNav", 1.0)
            print(f"   NAV from bot (cached): ${cached_nav:.6f}")
            return cached_nav, data
    except Exception as e:
        print(f"⚠️  Error fetching NAV from bot: {e}")
    
    try:
        response = requests.get(f"{BOT_URL}/price", timeout=10)
        if response.status_code == 200:
            data = response.json()
            nav = data.get("nav", 1.0)
            print(f"   NAV from bot /price fallback: ${nav:.6f}")
            return nav, None
    except:
        pass
    
    return 1.0, None


def get_pending_usdc(w3_base: Web3, vault_contract) -> Tuple[float, float]:
    """
    Read pending withdrawals in USDC from vault.
    
    V7.3.3 FIX: Reads actual usdcLocked values from pending withdrawal requests
    instead of recalculating with current NAV. This ensures the servicer bridges
    the exact amount locked in the contract, not a recalculated estimate.
    
    Returns:
        (pending_usdc, nav_per_share)
    """
    nav_per_share, nav_data = get_nav_from_bot()
    
    try:
        state = vault_contract.functions.getVaultState().call()
        pending_shares = state[6]
        pending_count = state[7]
        
        print(f"   Pending shares: {pending_shares/1e18:.6f}")
        print(f"   Pending withdrawals count: {pending_count}")
        
        if pending_count == 0 or pending_shares == 0:
            return 0.0, nav_per_share
        
        pending_usdc = get_pending_usdc_from_queue(vault_contract, pending_count)
        print(f"   Actual usdcLocked from queue: ${pending_usdc:.2f}")
        
        return pending_usdc, nav_per_share
        
    except Exception as e:
        print(f"⚠️  getVaultState failed: {e}, falling back to queue iteration")
        
        try:
            pending_usdc = get_pending_usdc_from_queue(vault_contract)
            return pending_usdc, nav_per_share
        except Exception as e2:
            print(f"❌ Error reading pending withdrawals: {e2}")
            return 0.0, nav_per_share


def get_pending_usdc_from_queue(vault_contract, pending_count: int = None) -> float:
    """
    Iterate through withdrawal queue and sum usdcLocked for unclaimed requests.
    
    V7.3.3: This is the correct way to calculate pending USDC - read the actual
    locked values from the contract instead of recalculating with current NAV.
    
    The queue structure: requests are added sequentially, nextWithdrawalIndex points
    to the first unprocessed request. We iterate exactly pending_count requests
    starting from nextWithdrawalIndex.
    
    Args:
        vault_contract: Web3 contract instance
        pending_count: Number of pending requests (from getVaultState)
    
    Returns:
        Total usdcLocked for all unclaimed, non-expired requests (in USDC, not raw)
    """
    total_usdc_locked = 0
    
    try:
        next_idx = vault_contract.functions.nextWithdrawalIndex().call()
        print(f"   Next withdrawal index: {next_idx}")
        
        if pending_count is None or pending_count == 0:
            state = vault_contract.functions.getVaultState().call()
            pending_count = state[7]
        
        if pending_count == 0:
            print(f"   No pending requests")
            return 0.0
        
        print(f"   Iterating {pending_count} pending requests...")
        
        checked = 0
        found = 0
        request_id = next_idx
        
        while found < pending_count and checked < 200:
            try:
                user, shares, usdc_locked, request_time, claimed, expired = \
                    vault_contract.functions.getWithdrawalRequest(request_id).call()
                
                checked += 1
                
                if not claimed and not expired:
                    total_usdc_locked += usdc_locked
                    found += 1
                    print(f"      Request #{request_id}: {shares/1e18:.4f} shares, ${usdc_locked/1e6:.2f} locked")
                elif claimed:
                    pass
                
                request_id += 1
                
            except Exception as e:
                if "Invalid request" in str(e) or "revert" in str(e).lower():
                    print(f"   Reached end of queue at request {request_id}")
                    break
                print(f"      ⚠️ Error reading request {request_id}: {e}")
                request_id += 1
                checked += 1
        
        print(f"   Checked {checked} requests, found {found} pending, total locked: ${total_usdc_locked/1e6:.2f}")
        return total_usdc_locked / 1e6
        
    except Exception as e:
        print(f"❌ Error iterating withdrawal queue: {e}")
        return 0.0


def get_vault_usdc(w3_base: Web3, usdc_contract) -> float:
    """Read USDC balance in vault on Base."""
    try:
        balance = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(VAULT_ADDRESS)
        ).call()
        return balance / 1e6
    except Exception as e:
        print(f"❌ Error reading vault USDC: {e}")
        return 0.0


def get_in_transit_usdc(state: ServicerState) -> float:
    """Calculate total USDC currently in transit (bridging) - only PENDING records."""
    total = 0.0
    for item in state.in_transit:
        if item.get("status") == "pending":
            total += item.get("amount_usdc", 0.0)
    return total


# =============================================================================
# EVENT-DRIVEN TRANSFER DETECTION (V7.3)
# =============================================================================

def get_web3_with_fallback(primary_url: str = None) -> Optional[Web3]:
    """Try primary RPC, then fallbacks. Returns connected Web3 or None."""
    urls_to_try = []
    if primary_url:
        urls_to_try.append(primary_url)
    urls_to_try.extend(BASE_RPC_FALLBACKS)
    
    for url in urls_to_try:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def scan_transfer_events_to_vault(
    w3_base: Web3,
    from_block: int,
    to_block: int = None
) -> List[Dict]:
    """
    Scan USDC Transfer events where 'to' == vault address.
    
    Returns list of transfers: [{block, tx_hash, amount_usdc, timestamp}]
    """
    if to_block is None:
        to_block = w3_base.eth.block_number
    
    vault_addr = Web3.to_checksum_address(VAULT_ADDRESS)
    usdc_addr = Web3.to_checksum_address(USDC_BASE)
    
    # ERC20 Transfer event signature: Transfer(address,address,uint256)
    transfer_topic = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()
    
    # Pad vault address to 32 bytes for indexed parameter
    vault_topic = "0x" + vault_addr.lower()[2:].zfill(64)
    
    try:
        logs = w3_base.eth.get_logs({
            "address": usdc_addr,
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [
                transfer_topic,
                None,  # from: any
                vault_topic  # to: vault
            ]
        })
        
        transfers = []
        for log in logs:
            amount_raw = int(log["data"].hex(), 16)
            amount_usdc = amount_raw / 1e6
            
            transfers.append({
                "block": log["blockNumber"],
                "tx_hash": log["transactionHash"].hex(),
                "amount_usdc": amount_usdc,
                "amount_raw": amount_raw,
            })
        
        return transfers
        
    except Exception as e:
        print(f"   ⚠️  Error scanning transfer events: {e}")
        return []


def scan_transfers_with_fallback(
    w3_base: Web3,
    from_block: int,
    to_block: int = None,
    use_wide_window: bool = False
) -> List[Dict]:
    """
    Scan transfers with fallback RPCs and optional wider window.
    Self-healing: tries multiple RPCs if primary fails.
    """
    # Try primary first
    transfers = scan_transfer_events_to_vault(w3_base, from_block, to_block)
    if transfers:
        return transfers
    
    # If stale/no results, try wider window with fallback RPCs
    if use_wide_window:
        try:
            current_block = w3_base.eth.block_number
            wide_from_block = max(0, current_block - MAX_LOG_LOOKBACK_BLOCKS)
            
            for fallback_url in BASE_RPC_FALLBACKS:
                try:
                    w3_fallback = Web3(Web3.HTTPProvider(fallback_url, request_kwargs={'timeout': 15}))
                    if not w3_fallback.is_connected():
                        continue
                    
                    transfers = scan_transfer_events_to_vault(w3_fallback, wide_from_block, current_block)
                    if transfers:
                        print(f"   📡 Found {len(transfers)} transfers via fallback RPC")
                        return transfers
                except Exception:
                    continue
        except Exception as e:
            print(f"   ⚠️  Wide window scan failed: {e}")
    
    return []


def match_transfers_to_pending(
    transfers: List[Dict],
    pending_items: List[Dict]
) -> List[Tuple[Dict, Dict]]:
    """
    Match incoming transfers to pending in-transit records.
    
    Matching rules (relaxed for fee tolerance):
    1. Transfer amount >= expected * (1 - FEE_TOLERANCE_PERCENT/100) - DUST_THRESHOLD
    2. Transfer amount <= expected * 1.05 (allow small overage)
    3. Each transfer can only match one pending item (first match wins)
    
    Returns: list of (pending_item, matching_transfer) tuples
    """
    matches = []
    used_transfers = set()
    
    # Sort pending by initiated_at (oldest first)
    sorted_pending = sorted(pending_items, key=lambda x: x.get("initiated_at", 0))
    
    for item in sorted_pending:
        expected_amount = item.get("amount_usdc", 0)
        
        # Calculate tolerance: allow up to FEE_TOLERANCE_PERCENT reduction + dust
        min_acceptable = expected_amount * (1 - FEE_TOLERANCE_PERCENT / 100) - DUST_THRESHOLD_USDC
        max_acceptable = expected_amount * 1.05  # Small overage allowed
        
        for i, transfer in enumerate(transfers):
            if i in used_transfers:
                continue
            
            transfer_amount = transfer.get("amount_usdc", 0)
            
            # Check if amounts match (within tolerance range)
            if min_acceptable <= transfer_amount <= max_acceptable:
                matches.append((item, transfer))
                used_transfers.add(i)
                break
    
    return matches


def check_in_transit_via_events(
    state: ServicerState,
    w3_base: Web3,
    usdc_contract
) -> Tuple[ServicerState, float]:
    """
    Event-driven in-transit completion check.
    
    Instead of balance reconciliation, directly scans USDC Transfer events
    to vault address and matches them to pending in-transit records.
    
    Self-healing:
    - Uses fallback RPCs if primary fails
    - Widens log window for stale records
    - Never blocks claims - users can still claim if vault has funds
    
    Returns:
        (updated_state, current_vault_balance)
    """
    current_vault_balance = get_vault_usdc(w3_base, usdc_contract)
    
    if not state.in_transit:
        return state, current_vault_balance
    
    pending_items = [item for item in state.in_transit if item.get("status") == "pending"]
    if not pending_items:
        state.in_transit = [item for item in state.in_transit if item.get("status") == "pending"]
        return state, current_vault_balance
    
    # Determine scan window based on oldest pending item
    now = time.time()
    oldest_initiated = min(item.get("initiated_at", now) for item in pending_items)
    age_seconds = now - oldest_initiated
    is_stale = age_seconds > STALE_THRESHOLD_SECONDS
    
    # Calculate from_block (estimate based on 2 sec/block on Base)
    try:
        current_block = w3_base.eth.block_number
        # Look back slightly more than the age of oldest pending item
        blocks_to_scan = min(int((age_seconds + 60) / 2), MAX_LOG_LOOKBACK_BLOCKS)
        from_block = max(0, current_block - blocks_to_scan)
    except Exception as e:
        print(f"   ⚠️  Error getting block number: {e}")
        from_block = 0
    
    # Scan for transfers
    transfers = scan_transfers_with_fallback(
        w3_base, 
        from_block, 
        use_wide_window=is_stale
    )
    
    if transfers:
        print(f"   📡 Found {len(transfers)} USDC transfers to vault since block {from_block}")
    
    # Match transfers to pending items
    matches = match_transfers_to_pending(transfers, pending_items)
    matched_item_ids = set()
    
    for item, transfer in matches:
        item_id = item.get("request_id") or f"{item.get('initiated_at', 0)}"
        matched_item_ids.add(item_id)
        
        amount = item.get("amount_usdc", 0)
        print(f"   ✅ Bridge CONFIRMED via event: ${amount:.2f} (tx: {transfer['tx_hash'][:16]}...)")
        send_telegram_alert(f"✅ Bridge complete: ${amount:.2f} arrived in vault")
    
    # Update in_transit list - keep only unmatched pending items
    updated_in_transit = []
    for item in state.in_transit:
        if item.get("status") != "pending":
            continue
        
        item_id = item.get("request_id") or f"{item.get('initiated_at', 0)}"
        if item_id in matched_item_ids:
            # This item was matched - don't keep it
            continue
        
        # Still pending
        amount = item.get("amount_usdc", 0)
        initiated_at = item.get("initiated_at", 0)
        age_seconds = now - initiated_at
        request_id = item.get("request_id", "")
        
        # Also check Relay API as backup confirmation
        if request_id:
            bridge_status, error = poll_bridge_status(request_id)
            if bridge_status == "success":
                print(f"   ✅ Bridge CONFIRMED via Relay API: ${amount:.2f}")
                send_telegram_alert(f"✅ Bridge complete (Relay): ${amount:.2f} arrived in vault")
                continue
            elif bridge_status == "failed":
                print(f"   ❌ Bridge FAILED: ${amount:.2f} - {error}")
                send_telegram_alert(f"❌ Bridge failed: ${amount:.2f}\n{error}", is_error=True)
                item["status"] = "failed"
                continue
        
        # Auto-clear very old pending items (failsafe to prevent permanent stall)
        if age_seconds > MAX_PENDING_AGE_SECONDS:
            print(f"   ⚠️ Auto-clearing stale pending: ${amount:.2f} ({int(age_seconds/60)}min old)")
            continue  # Don't add to updated list
        
        # Still waiting
        if is_stale and age_seconds > STALE_THRESHOLD_SECONDS:
            print(f"   ⏳ In-transit (stale, self-healing): ${amount:.2f} ({int(age_seconds/60)}min old)")
        else:
            print(f"   ⏳ In-transit: ${amount:.2f} ({int(age_seconds/60)}min old)")
        
        updated_in_transit.append(item)
    
    state.in_transit = updated_in_transit
    return state, current_vault_balance


# =============================================================================
# POLYMARKET API (with Cloudflare bypass)
# =============================================================================

def get_pm_balance() -> Tuple[float, float]:
    """
    Get Polymarket withdrawable cash (collateral) and position value.
    
    V7.3.3 FIX: Uses Polygon RPC as PRIMARY source for on-chain USDC balance (like bot_v7).
    CLOB API is only used as a sanity check - it can return stale data.
    
    Returns:
        (withdrawable_cash, position_value)
    """
    if not PM_PROXY_ADDRESS:
        print("❌ PM_PROXY_ADDRESS not set")
        return 0.0, 0.0
    
    cash = 0.0
    positions = 0.0
    
    # PRIMARY: Get cash balance via Polygon RPC (on-chain truth)
    try:
        cash, _ = get_pm_balance_from_rpc()
        print(f"   📊 On-chain USDC (Polygon RPC): ${cash:.2f}")
    except Exception as e:
        print(f"   ❌ RPC balance error: {e}")
        cash = 0.0
    
    # SANITY CHECK: Compare with CLOB API (optional, may be stale)
    try:
        if HAS_CLOB_CLIENT:
            client = get_patched_clob_client()
            if client:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                resp = client.get_balance_allowance(params)
                clob_balance = 0.0
                if resp:
                    if isinstance(resp, dict):
                        clob_balance = float(resp.get('balance', 0)) / 1e6  # micro-USDC to USDC
                    elif hasattr(resp, 'balance'):
                        clob_balance = float(resp.balance) / 1e6 if resp.balance else 0.0  # micro-USDC to USDC
                
                if clob_balance > 0:
                    delta = abs(cash - clob_balance)
                    if delta > 1.0:
                        print(f"   ⚠️ CLOB/RPC mismatch: CLOB=${clob_balance:.2f} vs RPC=${cash:.2f} (delta ${delta:.2f})")
                    else:
                        print(f"   ✅ CLOB confirms: ${clob_balance:.2f}")
    except Exception as e:
        print(f"   ⚠️ CLOB sanity check failed: {e}")
    
    # V7.3.4: Get position values from bot_v7 (avoids duplicate orderbook fetching)
    positions = 0.0
    try:
        positions_list, total_liq_value, bot_cash = get_positions_from_bot()
        
        if total_liq_value > 0:
            positions = total_liq_value
            # Log top positions for visibility
            if positions_list:
                print(f"   📊 Top positions (from bot_v7):")
                for i, p in enumerate(positions_list[:5]):
                    bid_note = "" if p.get("has_bids") else " (no bids)"
                    print(f"      {i+1}. {p['outcome'][:35]}: {p['size']:.2f} tokens, ${p['liq_value']:.2f}{bid_note}")
        else:
            # V7.3.4: Fallback to local calculation if bot returns zero
            print(f"   ⚠️ Bot returned zero position value, using local fallback")
            local_positions = get_positions_for_liquidation()
            positions = sum(p.get("liq_value", 0) for p in local_positions)
            if positions > 0:
                print(f"   ✅ Local fallback found ${positions:.2f} in positions")
    except Exception as e:
        print(f"   ⚠️ Position valuation error (bot_v7): {e}")
        # Fallback to local calculation
        try:
            local_positions = get_positions_for_liquidation()
            positions = sum(p.get("liq_value", 0) for p in local_positions)
        except Exception as e2:
            print(f"   ⚠️ Local fallback also failed: {e2}")
    
    print(f"   📊 PM balances: cash=${cash:.2f}, positions=${positions:.2f}")
    return cash, positions


def get_pm_balance_from_rpc() -> Tuple[float, float]:
    """Fallback: get PM balance directly from Polygon RPC."""
    try:
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
        if not w3.is_connected():
            return 0.0, 0.0
        
        usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_POLYGON),
            abi=ERC20_ABI
        )
        
        balance = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(PM_PROXY_ADDRESS)
        ).call()
        
        return balance / 1e6, 0.0
        
    except Exception as e:
        print(f"❌ RPC fallback error: {e}")
        return 0.0, 0.0


def get_orderbook_bids(token_id: str, verbose: bool = True) -> List[Tuple[float, float]]:
    """
    Get full bid ladder from orderbook, sorted highest to lowest price.
    
    V7.3.3 FIX: Uses authenticated CLOB client (like bot_v7) instead of raw HTTP.
    This avoids Cloudflare blocking that caused "Orderbook unavailable" errors.
    
    Returns: List of (price, size) tuples sorted by price descending
    """
    token_preview = token_id[:20] + "..." if len(token_id) > 20 else token_id
    
    # Method 1: Use patched CLOB client's native get_order_book (preferred - like bot_v7)
    try:
        client = get_patched_clob_client()
        if client:
            if verbose:
                print(f"   📖 Orderbook via CLOB client: {token_preview}")
            data = client.get_order_book(token_id)
            
            # Handle both dict and OrderBookSummary object responses
            if hasattr(data, 'bids') and hasattr(data, 'asks'):
                raw_bids = data.bids if data.bids else []
            else:
                raw_bids = data.get("bids", []) if isinstance(data, dict) else []
            
            # Parse bid entries (could be OrderBookLevel objects or dicts)
            bid_ladder = []
            for b in raw_bids:
                if hasattr(b, 'price') and hasattr(b, 'size'):
                    price = float(b.price)
                    size = float(b.size)
                else:
                    price = float(b.get("price", 0))
                    size = float(b.get("size", 0))
                if price > 0 and size > 0:
                    bid_ladder.append((price, size))
            
            bid_ladder.sort(key=lambda x: x[0], reverse=True)
            
            if verbose:
                print(f"   📊 Got {len(bid_ladder)} bids")
            
            if bid_ladder:
                return bid_ladder
            
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg:
            if verbose:
                print(f"   ❌ Orderbook 404: token_id may be wrong format or market resolved")
            return []
        else:
            if verbose:
                print(f"   ⚠️ CLOB client orderbook error: {e}, trying HTTP fallback...")
    
    # Method 2: HTTP fallback (may be blocked by Cloudflare)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        
        if BYPASS_METHOD == "curl_cffi":
            response = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                impersonate="chrome120",
                timeout=15
            )
        else:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=15)
        
        if response.status_code != 200:
            return []
        
        data = response.json()
        raw_bids = data.get("bids", [])
        
        if not raw_bids:
            return []
        
        bid_ladder = []
        for bid in raw_bids:
            price = float(bid.get("price", 0))
            size = float(bid.get("size", 0))
            if price > 0 and size > 0:
                bid_ladder.append((price, size))
        
        bid_ladder.sort(key=lambda x: x[0], reverse=True)
        return bid_ladder
        
    except Exception as e:
        if verbose:
            print(f"   ⚠️  Error fetching orderbook bids: {e}")
        return []


def calculate_liquidation_value(token_id: str, size: float, verbose: bool = True, strict_mode: bool = False) -> float:
    """
    Calculate USDC proceeds from selling 'size' tokens using VWAP sweep.
    
    Uses depth-aware VWAP sweep through orderbook bid levels.
    No fees/haircuts applied (Polymarket has no taker fees).
    Unfilled remainder valued at 0.
    
    Returns: USDC proceeds from liquidation
    """
    if size <= 0:
        return 0.0
    
    bids = get_orderbook_bids(token_id, verbose=False)
    
    if not bids:
        if verbose:
            print(f"      ⚠️  No bids for VWAP calculation")
        return 0.0
    
    remaining = size
    gross_proceeds = 0.0
    
    for price, depth in bids:
        if remaining <= 0:
            break
        fill = min(remaining, depth)
        gross_proceeds += fill * price
        remaining -= fill
    
    if verbose and remaining > 0:
        print(f"      ⚠️  Insufficient depth: {remaining:.2f} tokens unfilled (valued at $0)")
    
    return gross_proceeds


def get_liquidatable_cash() -> float:
    """
    V7.3.4: Get total liquidatable value from bot_v7.
    
    Returns: Total USDC that can be obtained from liquidation
    """
    positions_list, total_liq_value, _ = get_positions_from_bot()
    return total_liq_value


def get_orderbook_best_bid(token_id: str) -> Tuple[float, float]:
    """
    Get best bid price and available size from order book.
    
    Returns: (best_bid_price, available_bid_size)
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        token_preview = token_id[:20] + "..." if len(token_id) > 20 else token_id
        print(f"   📖 Orderbook query: {token_preview}")
        
        if BYPASS_METHOD == "curl_cffi":
            response = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                impersonate="chrome120",
                timeout=15
            )
        else:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=15)
        
        if response.status_code == 404:
            print(f"   ❌ Orderbook 404: token_id may be wrong format or market resolved")
            print(f"      Full token: {token_id}")
            return 0.0, 0.0
        elif response.status_code != 200:
            print(f"   ⚠️  Orderbook returned {response.status_code}")
            return 0.0, 0.0
        
        data = response.json()
        bids = data.get("bids", [])
        
        if not bids:
            print(f"   📊 No bids available (market illiquid)")
            return 0.0, 0.0
        
        # Debug: show raw bid data (first 3 entries)
        print(f"   🔍 Raw bids (first 3): {bids[:3]}")
        
        # Find the BEST (highest price) bid - don't assume bids[0] is sorted
        best_bid = None
        best_price = 0.0
        total_depth = 0.0
        
        for bid in bids:
            bid_price = float(bid.get("price", 0))
            bid_size = float(bid.get("size", 0))
            
            # Track total depth at prices >= 10% of best price (for slippage estimation)
            if bid_price > best_price:
                best_price = bid_price
                best_bid = bid
            
            # Accumulate size for depth calculation
            total_depth += bid_size
        
        if not best_bid or best_price <= 0:
            print(f"   📊 No valid bids found")
            return 0.0, 0.0
        
        best_size = float(best_bid.get("size", 0))
        
        print(f"   📊 Best bid: ${best_price:.4f}, size at best: {best_size:.2f}, total depth: {total_depth:.2f}")
        return best_price, best_size
        
    except Exception as e:
        print(f"   ⚠️  Error fetching orderbook: {e}")
        return 0.0, 0.0


def get_positions_for_liquidation() -> List[Dict]:
    """
    Get list of positions sorted by liquidation value (largest first).
    
    Returns list of dicts with: token_id, size, best_bid, bid_depth, liq_value
    """
    if not PM_PROXY_ADDRESS:
        return []
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        
        url = f"https://data-api.polymarket.com/positions?user={PM_PROXY_ADDRESS.lower()}"
        
        if BYPASS_METHOD == "curl_cffi":
            response = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                impersonate="chrome120",
                timeout=30
            )
        else:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
        
        if response.status_code != 200:
            print(f"⚠️  Positions API returned {response.status_code}")
            return []
        
        positions_data = response.json()
        
        if not isinstance(positions_data, list):
            if isinstance(positions_data, dict) and "positions" in positions_data:
                positions_data = positions_data["positions"]
            elif isinstance(positions_data, dict):
                positions_data = [positions_data]
            else:
                print(f"⚠️  Positions API returned unexpected type: {type(positions_data)}")
                return []
        
        positions = []
        for pos in positions_data:
            if not isinstance(pos, dict):
                continue
            size = float(pos.get("size", 0))
            if size <= 0:
                continue
            
            token_id = pos.get("asset")
            if not token_id:
                continue
            
            # Use VWAP sweep for accurate liquidation value (what you'd actually get)
            liq_value = calculate_liquidation_value(token_id, size, verbose=False)
            
            # V7.3.3 FIX: Fallback to API-reported value when orderbook fails
            # This prevents large positions from being ranked below dust when bids are unavailable
            api_value = float(pos.get("value", 0))
            avg_price = float(pos.get("avgPrice", 0))
            fallback_value = api_value if api_value > 0 else (size * avg_price)
            
            # Use liq_value if available, otherwise fall back to API value
            effective_liq_value = liq_value if liq_value > 0 else fallback_value
            used_fallback = liq_value <= 0 and fallback_value > 0
            
            if used_fallback:
                print(f"   ⚠️ Orderbook unavailable for {pos.get('outcome', 'Unknown')[:30]}, using API value ${fallback_value:.2f}")
            
            # Get best bid for reference (used in liquidation execution)
            bids = get_orderbook_bids(token_id, verbose=False)
            best_bid = bids[0][0] if bids else 0.0
            
            positions.append({
                "token_id": token_id,
                "outcome": pos.get("outcome", "Unknown"),
                "size": size,
                "best_bid": best_bid,
                "liq_value": effective_liq_value,
                "used_fallback": used_fallback,
            })
        
        # Sort by effective liquidation value (largest first)
        positions.sort(key=lambda x: x["liq_value"], reverse=True)
        
        # Log top positions for debugging
        print(f"   📊 Top positions for liquidation (sorted by value):")
        for i, p in enumerate(positions[:5]):
            fallback_note = " (API fallback)" if p.get("used_fallback") else ""
            print(f"      {i+1}. {p['outcome'][:35]}: {p['size']:.2f} tokens, ${p['liq_value']:.2f}{fallback_note}")
        
        return positions
        
    except Exception as e:
        print(f"⚠️  Error fetching positions: {e}")
        return []


# =============================================================================
# WITHDRAWAL & LIQUIDATION
# =============================================================================

def withdraw_pm_cash_to_bridge(amount_usdc: float, dry_run: bool = False) -> Tuple[bool, str, float]:
    """
    Withdraw PM cash and bridge directly to vault on Base.
    
    Uses safe_proxy_withdraw.ts (with @polymarket/builder-relayer-client) 
    for Safe proxy wallet support.
    
    Returns:
        (success, request_id, amount_bridged)
    """
    import re
    
    if amount_usdc < MIN_WITHDRAWAL_USDC:
        print(f"⚠️  Amount ${amount_usdc:.2f} below minimum ${MIN_WITHDRAWAL_USDC}")
        return False, "", 0.0
    
    print(f"\n💸 WITHDRAW: ${amount_usdc:.2f} from PM → Vault")
    
    cmd_args = [
        "npx", "tsx", 
        os.path.join(os.path.dirname(__file__), "safe_proxy_withdraw.ts"),
        "withdraw", str(amount_usdc)
    ]
    if dry_run:
        cmd_args.append("--dry-run")
    
    try:
        result = subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            timeout=300,
            env={
                **os.environ,
                "TREASURY_ADDRESS": VAULT_ADDRESS,
            }
        )
        
        output = result.stdout + result.stderr
        print(f"   Output (first 2000 chars): {output[:2000]}")
        
        request_id = ""
        request_id_match = re.search(r'requestId["\s:]+([a-f0-9-]+)', output, re.IGNORECASE)
        if request_id_match:
            request_id = request_id_match.group(1)
            print(f"   Extracted requestId: {request_id}")
        
        tx_hash = ""
        tx_hash_match = re.search(r'txHash["\s:]+0x([a-f0-9]+)', output, re.IGNORECASE)
        if tx_hash_match:
            tx_hash = "0x" + tx_hash_match.group(1)
            print(f"   Extracted txHash: {tx_hash[:20]}...")
        
        if "success" in output.lower() or result.returncode == 0:
            if not request_id and not tx_hash:
                print("⚠️  Withdraw reported success but no requestId/txHash found")
                send_telegram_alert(
                    f"⚠️ Bridge initiated but no tracking ID\n"
                    f"Amount: ${amount_usdc:.2f}\n"
                    f"Check Relay dashboard manually",
                    is_error=True
                )
            return True, request_id, amount_usdc
        else:
            print(f"❌ Withdraw script failed (exit {result.returncode})")
            send_telegram_alert(
                f"❌ Withdraw script failed\n"
                f"Amount: ${amount_usdc:.2f}\n"
                f"Exit code: {result.returncode}",
                is_error=True
            )
            return False, "", 0.0
            
    except subprocess.TimeoutExpired:
        print("❌ Withdraw script timed out (5 min)")
        send_telegram_alert(
            f"❌ Withdraw script timeout\n"
            f"Amount: ${amount_usdc:.2f}\n"
            f"Manual check required",
            is_error=True
        )
        return False, "", 0.0
    except Exception as e:
        print(f"❌ Withdraw error: {e}")
        send_telegram_alert(f"❌ Withdraw error: {e}", is_error=True)
        return False, "", 0.0


def get_orderbook_from_bot(token_id: str) -> Optional[Dict]:
    """
    Fetch fresh orderbook data from bot_v7.
    
    V7.3.4: Used before each order to get live best_bid for slippage protection.
    Orderbook is REQUIRED - caller should handle None/error cases.
    
    Returns: Dict with 'best_bid', 'bids', etc or dict with 'error' key
    """
    try:
        url = f"{BOT_URL}/orderbook"
        response = requests.get(url, params={"token_id": token_id}, timeout=10)
        
        if response.status_code != 200:
            error_text = response.text[:100] if response.text else "Unknown"
            return {"error": f"HTTP {response.status_code}: {error_text}"}
        
        data = response.json()
        if "error" in data:
            return {"error": data["error"]}
        
        return data
    except requests.exceptions.Timeout:
        return {"error": "Orderbook request timeout"}
    except Exception as e:
        return {"error": str(e)}


def get_liquidation_plan_from_bot(needed_usdc: float, max_positions: int = 5) -> Optional[Dict]:
    """
    Get a liquidation plan from bot_v7's /liquidation-plan endpoint.
    
    This delegates position selection and orderbook analysis to bot_v7,
    which has working orderbook fetching. We only handle execution here.
    
    Returns: Plan dict with legs, or None if unavailable
    """
    try:
        url = f"{BOT_URL}/liquidation-plan"
        params = {
            "need_usdc": needed_usdc,
            "max_positions": max_positions,
            "max_slippage_bps": 100  # 1% max slippage
        }
        
        print(f"   📡 Requesting liquidation plan from bot_v7...")
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code != 200:
            print(f"   ❌ bot_v7 returned {response.status_code}: {response.text}")
            return None
        
        plan = response.json()
        if "error" in plan:
            print(f"   ❌ bot_v7 error: {plan['error']}")
            return None
        
        print(f"   ✅ Got plan: {len(plan.get('legs', []))} legs, expected ${plan.get('total_expected_usdc', 0):.2f}")
        return plan
        
    except Exception as e:
        print(f"   ❌ Failed to get liquidation plan: {e}")
        return None


def report_liquidation_result(reservation_id: str, status: str, filled_size: float, filled_usdc: float) -> bool:
    """Report execution result back to bot_v7 to release reservation."""
    try:
        url = f"{BOT_URL}/liquidation-result"
        data = {
            "reservation_id": reservation_id,
            "status": status,
            "filled_size": filled_size,
            "filled_usdc": filled_usdc
        }
        
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
        
    except Exception as e:
        print(f"   ⚠️ Failed to report result: {e}")
        return False


def report_once(reservation_id: str, status: str, filled_size: float, filled_usdc: float, 
                reported_ids: set) -> bool:
    """
    V7.3.4: Report reservation result exactly once.
    
    Checks if reservation was already reported; if so, skips the call.
    This prevents double-cancellation issues.
    """
    if not reservation_id:
        return True
    
    if reservation_id in reported_ids:
        print(f"   ⏭️ Skipping already-reported reservation {reservation_id[:8]}...")
        return True
    
    result = report_liquidation_result(reservation_id, status, filled_size, filled_usdc)
    if result:
        reported_ids.add(reservation_id)
    return result


def get_pm_cash_from_bot() -> float:
    """Get PM cash balance from bot_v7's /pm-cash endpoint."""
    try:
        response = requests.get(f"{BOT_URL}/pm-cash", timeout=10)
        if response.status_code == 200:
            return response.json().get("cash_usdc", 0.0)
        return 0.0
    except Exception:
        return 0.0


def get_positions_from_bot() -> Tuple[List[Dict], float, float]:
    """
    V7.3.4: Get positions with liquidation values from bot_v7.
    
    This replaces local orderbook fetching - bot_v7 already has working
    CLOB access and does VWAP sweeps for position valuation.
    
    Returns: (positions_list, total_liq_value, cash)
    """
    try:
        response = requests.get(f"{BOT_URL}/positions", timeout=60)  # May take time for many positions
        if response.status_code == 200:
            data = response.json()
            positions = data.get("positions", [])
            total_liq = data.get("total_liq_value", 0)
            cash = data.get("cash", 0)
            return positions, total_liq, cash
        else:
            print(f"   ⚠️ Bot positions endpoint returned {response.status_code}")
            return [], 0.0, 0.0
    except Exception as e:
        print(f"   ⚠️ Failed to get positions from bot: {e}")
        return [], 0.0, 0.0


def liquidate_positions(needed_usdc: float, initial_pm_cash: float = 0.0) -> float:
    """
    Liquidate positions to get needed USDC.
    
    V7.3.4 REFACTOR: Delegates position selection to bot_v7 via /liquidation-plan.
    Bot_v7 has working orderbook fetching, so we avoid duplicating that logic.
    
    Flow:
    1. Request liquidation plan from bot_v7 (positions, sizes, limit prices)
    2. Execute each leg with limit price protection (pre-trade check)
    3. Report results back to bot_v7 to release reservations
    4. Repeat if shortfall remains (max 3 rounds)
    
    INVARIANT: total_obtained will never exceed needed_usdc (usdcLocked cap)
    
    Args:
        needed_usdc: Amount of USDC needed (must match withdrawal's usdcLocked)
        initial_pm_cash: PM cash balance at start (for detecting new cash arrivals)
    
    Returns: USDC obtained from liquidation
    """
    if not HAS_CLOB_CLIENT:
        print("⚠️  CLOB client not available - manual liquidation needed")
        send_telegram_alert(
            f"🔥 Liquidation needed: ${needed_usdc:.2f}\n"
            f"⚠️ Auto-liquidation unavailable",
            is_error=True
        )
        return 0.0
    
    # V7.3.4 FIX: Store original cap to enforce usdcLocked invariant
    original_cap = needed_usdc  # This is the withdrawal's usdcLocked
    
    capped_needed = min(needed_usdc, MAX_PER_CYCLE_LIQUIDATION_USDC)
    print(f"\n🔥 LIQUIDATE V7.3.4: Need ${capped_needed:.2f} USDC (capped from ${needed_usdc:.2f})")
    print(f"   Initial PM cash: ${initial_pm_cash:.2f}")
    print(f"   usdcLocked cap: ${original_cap:.2f} (will not exceed)")
    
    # Upfront guard - reduce target by available cash
    still_needed = capped_needed
    if initial_pm_cash >= still_needed:
        print(f"   💵 PM cash ${initial_pm_cash:.2f} >= need ${still_needed:.2f}, skipping liquidation!")
        return 0.0
    elif initial_pm_cash > 0.01:
        print(f"   💵 Partial cash ${initial_pm_cash:.2f}, reducing target from ${still_needed:.2f}")
        still_needed -= initial_pm_cash
        print(f"   📉 New liquidation target: ${still_needed:.2f}")
    
    total_obtained = 0.0
    max_rounds = 10  # V7.3.4: Increased to allow more rounds for thin liquidity
    consecutive_failures = 0  # V7.3.4: Hoist outside round loop for overall circuit breaker
    reported_reservation_ids = set()  # V7.3.4: Track all reported reservations to prevent double-cancellation
    
    for round_num in range(max_rounds):
        if still_needed <= 0.05:  # V7.3.4: Lowered from $0.50 to $0.05 to complete small liquidations
            break
        
        print(f"\n   📋 ROUND {round_num + 1}: Still need ${still_needed:.2f}")
        
        # Get liquidation plan from bot_v7
        plan = get_liquidation_plan_from_bot(still_needed)
        if not plan or not plan.get("legs"):
            print("   ⚠️ No liquidation plan available")
            send_telegram_alert(
                f"⚠️ Cannot get liquidation plan\n"
                f"Still need: ${still_needed:.2f}\n"
                f"Manual intervention may be required",
                is_error=True
            )
            break
        
        legs = plan["legs"]
        plan_id = plan.get('plan_id', 'unknown')
        plan_expected = plan.get('total_expected_usdc', 0)
        plan_shortfall = plan.get('shortfall', 0)
        print(f"   📊 Plan {plan_id[:8]}: {len(legs)} legs, expected ${plan_expected:.2f}, shortfall ${plan_shortfall:.2f}")
        
        # Track which legs were executed for cleanup
        executed_leg_indices = set()
        
        # V7.3.4 FIX: Lowered threshold from $0.50 to $0.05 to allow small liquidations
        # This is critical for meeting the $5 minimum bridge requirement when close to target
        # Bot already filters dust positions < $0.10, so anything that comes through should execute
        if plan_expected < 0.05:
            print(f"   ⚠️ Plan has negligible expected value: ${plan_expected:.2f}")
            # Cancel reservations and try next round
            for i, leg in enumerate(legs):
                res_id = leg.get("reservation_id", "")
                if res_id:
                    report_once(res_id, "cancelled", 0, 0, reported_reservation_ids)
                    executed_leg_indices.add(i)
            consecutive_failures += 1
            if consecutive_failures >= 3:
                print(f"   ❌ 3 consecutive low-value plans - no liquidity available")
                break
            continue  # Try next round
        
        # Execute each leg
        for leg_idx, leg in enumerate(legs):
            if still_needed <= 0.05:  # V7.3.4: Lowered from $0.50
                # Cancel remaining unexecuted legs and mark as handled
                for remaining_idx in range(leg_idx, len(legs)):
                    if remaining_idx not in executed_leg_indices:
                        res_id = legs[remaining_idx].get("reservation_id", "")
                        if res_id:
                            report_once(res_id, "cancelled", 0, 0, reported_reservation_ids)
                            executed_leg_indices.add(remaining_idx)  # Mark as handled
                break
            
            token_id = leg["token_id"]
            size = leg["size"]
            limit_price = leg.get("limit_price", leg.get("best_bid", 0))
            reservation_id = leg.get("reservation_id", "")
            outcome = leg.get("outcome", "Unknown")
            
            if not token_id or size <= 0 or limit_price <= 0:
                print(f"   ⚠️ Invalid leg: {leg}")
                continue
            
            # V7.3.4 FIX: Scale order size to not exceed usdcLocked cap
            # Use bot_v7's expected_usdc (VWAP-based) which is more accurate than size * limit_price
            remaining_cap = original_cap - total_obtained
            expected_leg_usdc = leg.get("expected_usdc", size * limit_price)
            
            if remaining_cap <= 0.05:  # V7.3.4: Lowered from $0.50
                print(f"   ✅ Cap reached, releasing remaining reservations")
                report_once(reservation_id, "cancelled", 0, 0, reported_reservation_ids)
                break
            
            if expected_leg_usdc > remaining_cap:
                # Scale down the order size proportionally to stay within cap
                scale_factor = remaining_cap / expected_leg_usdc
                original_size = size
                size = size * scale_factor
                expected_leg_usdc = remaining_cap  # Update expectation
                print(f"   📉 Scaling order: {original_size:.2f} → {size:.2f} tokens (cap ${remaining_cap:.2f})")
            
            print(f"   🎯 Leg: {outcome[:30]} | {size:.2f} tokens @ limit ${limit_price:.4f}")
            
            # Check PM cash before each order to catch late-arriving funds
            try:
                current_cash = get_pm_cash_from_bot()
                if current_cash > 0:
                    new_cash = current_cash - initial_pm_cash - total_obtained
                    if new_cash > 0.50:
                        print(f"   💵 New cash ${new_cash:.2f} detected, reducing target")
                        still_needed -= new_cash
                        if still_needed <= 0:
                            report_once(reservation_id, "cancelled", 0, 0, reported_reservation_ids)
                            print(f"   ✅ Cash covers need, stopping!")
                            return total_obtained
            except Exception as e:
                print(f"   ⚠️ Cash check failed: {e}")
            
            # Execute the sell order with try/finally to ensure reservation cleanup
            try:
                # V7.3.4 FIX: Fetch FRESH best_bid from bot_v7 - REQUIRED, no fallback
                # The plan's cached best_bid may be stale by the time we execute
                fresh_ob = get_orderbook_from_bot(token_id)
                
                if not fresh_ob or fresh_ob.get("error"):
                    error_msg = fresh_ob.get("error") if fresh_ob else "No response"
                    print(f"   ❌ ORDERBOOK FETCH FAILED: {error_msg}")
                    consecutive_failures += 1
                    send_telegram_alert(
                        f"⚠️ Orderbook fetch failed\n"
                        f"Plan: {plan_id[:8]}, Leg: {leg_idx + 1}/{len(legs)}\n"
                        f"Reservation: {reservation_id[:8]}...\n"
                        f"Token: {token_id[:20]}...\n"
                        f"Error: {error_msg}",
                        is_error=True
                    )
                    report_once(reservation_id, "cancelled", 0, 0, reported_reservation_ids)  # Cancel, not failed
                    executed_leg_indices.add(leg_idx)  # Mark as handled
                    
                    # V7.3.4: Halt on 2+ consecutive failures - likely systemic issue
                    if consecutive_failures >= 2:
                        print(f"   🚨 {consecutive_failures} consecutive failures - HALTING")
                        send_telegram_alert(
                            f"🚨 Liquidation halted: {consecutive_failures} consecutive orderbook failures\n"
                            f"Plan: {plan_id[:8]}\n"
                            f"Total obtained: ${total_obtained:.2f}\n"
                            f"Still needed: ${still_needed:.2f}\n"
                            f"Manual intervention required",
                            is_error=True
                        )
                        
                        # Cancel remaining reservations in this plan
                        for remaining_leg in legs[leg_idx + 1:]:
                            remaining_res_id = remaining_leg.get("reservation_id", "")
                            if remaining_res_id:
                                report_once(remaining_res_id, "cancelled", 0, 0, reported_reservation_ids)
                        
                        return total_obtained
                    continue  # Skip this leg, try next
                
                # Reset failure counter on successful fetch
                consecutive_failures = 0
                
                best_bid = fresh_ob.get("best_bid", 0)
                if best_bid <= 0:
                    print(f"   ❌ EMPTY ORDERBOOK: no bids available")
                    send_telegram_alert(
                        f"⚠️ Empty orderbook - HALTING\n"
                        f"Plan: {plan_id[:8]}, Leg: {leg_idx + 1}/{len(legs)}\n"
                        f"Reservation: {reservation_id[:8]}...\n"
                        f"Token: {token_id[:20]}...\n"
                        f"Total obtained: ${total_obtained:.2f}",
                        is_error=True
                    )
                    report_once(reservation_id, "cancelled", 0, 0, reported_reservation_ids)
                    
                    # Cancel remaining reservations in this plan
                    for remaining_leg in legs[leg_idx + 1:]:
                        remaining_res_id = remaining_leg.get("reservation_id", "")
                        if remaining_res_id:
                            report_once(remaining_res_id, "cancelled", 0, 0, reported_reservation_ids)
                    
                    return total_obtained  # Empty orderbook is critical - halt
                
                print(f"   📊 Fresh best_bid: ${best_bid:.4f} (limit: ${limit_price:.4f})")
                
                success, usdc, tokens_filled, slippage_breach = execute_liquidation_order(
                    token_id,
                    size,
                    best_bid,
                    limit_price  # V7.3.4: Pass limit price for slippage protection
                )
                
                # V7.3.4 FIX: Halt on slippage breach to prevent further adverse fills
                if slippage_breach:
                    print(f"   🚨 SLIPPAGE BREACH - HALTING ALL LIQUIDATION")
                    send_telegram_alert(
                        f"🚨 SLIPPAGE BREACH - HALTING!\n"
                        f"Plan: {plan_id[:8]}, Leg: {leg_idx + 1}/{len(legs)}\n"
                        f"Reservation: {reservation_id[:8]}...\n"
                        f"Token: {token_id[:20]}...\n"
                        f"Best bid: ${best_bid:.4f}, Limit: ${limit_price:.4f}\n"
                        f"Total obtained: ${total_obtained + usdc:.2f}",
                        is_error=True
                    )
                    # Still record any fill we got, but cancel the reservation
                    if usdc > 0:
                        report_once(reservation_id, "filled", tokens_filled, usdc, reported_reservation_ids)
                        total_obtained += usdc
                    else:
                        report_once(reservation_id, "cancelled", 0, 0, reported_reservation_ids)
                    
                    # Cancel any remaining reservations in this plan
                    for remaining_leg in legs[leg_idx + 1:]:
                        remaining_res_id = remaining_leg.get("reservation_id", "")
                        if remaining_res_id:
                            report_once(remaining_res_id, "cancelled", 0, 0, reported_reservation_ids)
                    
                    return total_obtained  # Early exit, stop all liquidation
                
                # Report result back to bot_v7
                if success and usdc > 0:
                    # V7.3.4 FIX: Enforce usdcLocked cap - never exceed original request
                    remaining_cap = original_cap - total_obtained
                    actual_usdc = usdc
                    actual_tokens = tokens_filled
                    
                    if usdc > remaining_cap:
                        # Clamp USDC and adjust tokens proportionally
                        clamp_ratio = remaining_cap / usdc
                        actual_usdc = remaining_cap
                        actual_tokens = tokens_filled * clamp_ratio
                        print(f"   ⚠️ Cap enforcement: clamping ${usdc:.2f} → ${actual_usdc:.2f} ({actual_tokens:.2f} tokens)")
                    
                    report_once(reservation_id, "filled", actual_tokens, actual_usdc, reported_reservation_ids)
                    executed_leg_indices.add(leg_idx)
                    total_obtained += actual_usdc
                    # V7.3.4: Recompute still_needed from original_cap to stay coherent with cap
                    still_needed = max(0, original_cap - total_obtained)
                    consecutive_failures = 0  # Reset on success
                    print(f"   ✅ Filled: ${actual_usdc:.2f} USDC ({actual_tokens:.2f} tokens)")
                    print(f"      Total: ${total_obtained:.2f}, still need: ${still_needed:.2f}")
                elif success:
                    # V7.3.4: Zero fill = treat as cancellation, not partial
                    report_once(reservation_id, "cancelled", 0, 0, reported_reservation_ids)
                    executed_leg_indices.add(leg_idx)
                    consecutive_failures = 0  # Reset - order submitted successfully
                    print(f"   ⚠️ Order success but no fill (reservation cancelled)")
                else:
                    report_once(reservation_id, "failed", 0, 0, reported_reservation_ids)
                    executed_leg_indices.add(leg_idx)
                    print(f"   ❌ Order failed")
            except Exception as exec_error:
                # Ensure reservation is released on any exception
                print(f"   ❌ Execution error: {exec_error}")
                report_once(reservation_id, "failed", 0, 0, reported_reservation_ids)
                executed_leg_indices.add(leg_idx)
            
            # Rate limiting
            time.sleep(0.5)
        
        # V7.3.4 FIX: Cancel any unexecuted legs at end of round
        # Only cancel legs that were not yet handled (not in executed_leg_indices)
        for i, leg in enumerate(legs):
            if i not in executed_leg_indices:
                res_id = leg.get("reservation_id", "")
                if res_id:
                    print(f"   🧹 Cleaning up unexecuted leg {i + 1} reservation")
                    report_once(res_id, "cancelled", 0, 0, reported_reservation_ids)
        
        # V7.3.4: Reset consecutive_failures after each round
        consecutive_failures = 0
        
        # V7.3.4: Recompute still_needed for next round
        still_needed = max(0, original_cap - total_obtained)
        
        # V7.3.4 FIX: Check if cap reached - stop if we've hit the limit
        if total_obtained >= original_cap - 0.05 or still_needed < 0.05:
            print(f"   ✅ usdcLocked cap reached: ${total_obtained:.2f} >= ${original_cap:.2f}")
            break
    
    # V7.3.4 FIX: Shortfall handling with alert
    # Use original_cap for consistency (capped_needed is redundant)
    shortfall = max(0, original_cap - total_obtained)
    print(f"\n   💰 Total liquidated: ${total_obtained:.2f}")
    
    if shortfall > 1.0:
        print(f"   ⚠️ SHORTFALL: ${shortfall:.2f} could not be liquidated")
        send_telegram_alert(
            f"⚠️ Liquidation shortfall: ${shortfall:.2f}\n"
            f"Obtained: ${total_obtained:.2f} / ${original_cap:.2f}\n"
            f"May need manual intervention or more liquidity",
            is_error=True
        )
    
    if total_obtained > 0:
        send_telegram_alert(
            f"🔥 Liquidated ${total_obtained:.2f} from positions\n"
            f"Ready to bridge to vault"
        )
    
    return total_obtained


def execute_liquidation_order(token_id: str, size: float, best_bid: float, limit_price: float = 0.0) -> Tuple[bool, float, float, bool]:
    """
    Execute a market sell for liquidation with slippage protection.
    
    V7.3.4 FIX: Enforces limit_price check before order execution.
    If current best bid is below limit_price, order is rejected.
    Post-fill: detects slippage breach and returns halt signal.
    
    Per Polymarket docs and py-clob-client:
    - Uses MarketOrderArgs with amount (in USDC terms)
    - Uses create_market_order() for proper market order handling  
    - Uses FAK (Fill-And-Kill) to allow partial fills if liquidity is limited
    
    Args:
        token_id: The token to sell
        size: Number of tokens to sell
        best_bid: Current best bid price (for USDC estimation)
        limit_price: Minimum acceptable price (slippage floor)
    
    Returns:
        (success, usdc_obtained, tokens_filled, slippage_breach)
        slippage_breach: True if fill was >10% worse than limit_price (caller should halt)
    """
    client = get_patched_clob_client()
    
    if not client:
        print("   ❌ CLOB client not available")
        return False, 0.0, 0.0, False
    
    try:
        # V7.3.4 FIX: Enforce slippage protection BEFORE order execution
        if limit_price > 0 and best_bid < limit_price:
            print(f"   ⚠️ PRE-TRADE SLIPPAGE: best_bid ${best_bid:.4f} < limit ${limit_price:.4f}")
            send_telegram_alert(
                f"⚠️ Pre-trade slippage rejection\n"
                f"Best bid: ${best_bid:.4f}\n"
                f"Limit: ${limit_price:.4f}\n"
                f"Order not submitted",
                is_error=True
            )
            return False, 0.0, 0.0, True  # True = slippage breach, should halt
        
        usdc_amount = size * best_bid  # For logging only
        
        # V7.3.4 FIX: For SELL orders, amount = TOKEN count (not USDC value)
        # The API interprets amount as tokens when selling
        market_order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size,  # Token count, not USDC!
            side=SELL,
        )
        
        print(f"   📤 Creating market sell: {size:.2f} tokens (~${usdc_amount:.2f}) [limit: ${limit_price:.4f}]")
        signed_order = client.create_market_order(market_order_args)
        resp = client.post_order(signed_order, OrderType.FAK)
        
        if resp.get("success"):
            status = resp.get("status", "unknown")
            
            # Get USDC received (takingAmount for sell = USDC we get)
            # Note: Polymarket returns amounts as decimal strings e.g. "14.925372"
            taking_str = resp.get("takingAmount", "0")
            taking = float(taking_str) if taking_str else 0.0
            
            # NOTE: makingAmount is UNRELIABLE - it echoes requested size even on partial/zero fills
            # We only trust takingAmount (USDC actually received) as ground truth
            making_str = resp.get("makingAmount", "0")
            
            print(f"   📋 Response: status={status}, takingAmount={taking_str}, makingAmount={making_str}")
            
            # CRITICAL: For FAK orders, verify we actually got USDC
            # If takingAmount is 0 or very small, the order didn't fill
            if taking <= 0.001:
                print(f"   ⚠️  Order accepted but NO FILL (takingAmount={taking_str})")
                return True, 0.0, 0.0, False  # Success=true but 0 fill triggers the guard in caller
            
            # V7.3.4 FIX: Post-fill slippage verification
            # Compare expected USDC (if we got limit_price) vs actual USDC received
            # FAK response doesn't give actual tokens sold, only USDC received (takingAmount)
            expected_usdc = size * limit_price if limit_price > 0 else size * best_bid
            slippage_pct = ((expected_usdc - taking) / expected_usdc * 100) if expected_usdc > 0 else 0
            slippage_breach = False
            
            # V7.3.4 FIX: Tightened thresholds
            # >2% slippage = breach (halt), 1-2% = warning
            # This ensures withdrawals never settle materially below locked NAV
            if limit_price > 0 and taking < expected_usdc * 0.98:  # >2% slippage = breach
                slippage_breach = True
                print(f"   🚨 SLIPPAGE BREACH: expected ${expected_usdc:.2f}, got ${taking:.2f} ({slippage_pct:.1f}% slip)")
                send_telegram_alert(
                    f"🚨 SLIPPAGE BREACH - HALTING!\n"
                    f"Expected: ${expected_usdc:.2f} (at limit ${limit_price:.4f})\n"
                    f"Actual: ${taking:.2f}\n"
                    f"Slippage: {slippage_pct:.1f}%\n"
                    f"⚠️ Liquidation halted - manual review required",
                    is_error=True
                )
            elif limit_price > 0 and taking < expected_usdc * 0.99:  # 1-2% slippage = warning
                print(f"   ⚠️ SLIPPAGE WARNING: expected ${expected_usdc:.2f}, got ${taking:.2f} ({slippage_pct:.1f}% slip)")
                send_telegram_alert(
                    f"⚠️ Slippage warning\n"
                    f"Expected: ${expected_usdc:.2f}\n"
                    f"Actual: ${taking:.2f}\n"
                    f"Slippage: {slippage_pct:.1f}%",
                    is_error=True
                )
            
            # Estimate tokens filled from USDC received / reference price
            tokens_filled = taking / best_bid if best_bid > 0 else 0.0
            print(f"   ✅ MARKET SELL filled: ${taking:.2f} USDC (~{tokens_filled:.2f} tokens, {slippage_pct:.1f}% slip)")
            
            return True, taking, tokens_filled, slippage_breach
        else:
            error_msg = resp.get("errorMsg", "Unknown error")
            print(f"   ❌ Market sell failed: {error_msg}")
            print(f"   📋 Full response: {resp}")
            return False, 0.0, 0.0, False
            
    except Exception as e:
        print(f"   ❌ Liquidation order error: {e}")
        import traceback
        traceback.print_exc()
        return False, 0.0, 0.0, False


# =============================================================================
# BRIDGE STATUS TRACKING
# =============================================================================

def poll_bridge_status(request_id: str) -> Tuple[str, Optional[str]]:
    """
    Poll Relay API for bridge completion status.
    
    Args:
        request_id: The requestId from the bridge quote
    
    Returns:
        (status, error_message)
        status: "pending", "success", "failed"
    """
    if not request_id:
        return "pending", None
    
    try:
        response = requests.get(
            f"{RELAY_API_URL}/intents/status/v2",
            params={"requestId": request_id},
            timeout=15
        )
        
        if response.status_code != 200:
            return "pending", None
        
        data = response.json()
        status = data.get("status", "unknown")
        
        if status in ("success", "completed"):
            return "success", None
        
        if status in ("failed", "refunded"):
            error = data.get("error", "Unknown error")
            return "failed", error
        
        return "pending", None
        
    except Exception as e:
        print(f"   ⚠️  Bridge status poll error: {e}")
        return "pending", None


def check_in_transit_arrivals(
    state: ServicerState, 
    w3_base: Web3, 
    usdc_contract
) -> Tuple[ServicerState, float]:
    """
    Check if any in-transit funds have arrived on Base.
    Uses Relay API + vault balance reconciliation with per-item credit tracking.
    
    SAFETY: Only clears in-transit if BOTH conditions are met:
    1. Relay API confirms success (relay_confirmed = True) for tracked bridges
    2. balance_credited >= 98% of amount (accumulated across iterations)
    
    CRITICAL: Only RELAY-CONFIRMED bridges can consume balance delta!
    Unconfirmed bridges wait for Relay before getting any credit.
    state.unclaimed_delta accumulates balance increases across iterations,
    so delta arriving before Relay confirms is preserved for later.
    
    This prevents the out-of-order misattribution bug:
    - Bridge A ($100) starts, Bridge B ($50) starts
    - Bridge B lands first (+$50 to vault), but Relay hasn't confirmed either
    - Delta adds to unclaimed_delta (+$50)
    - When Relay confirms B, B claims from unclaimed_delta
    - A continues waiting for its own Relay + balance
    
    CREDIT DISTRIBUTION (priority order):
    1. NEWLY Relay-confirmed bridges get FIRST priority for balance delta
    2. Already relay-confirmed bridges needing more credit get second priority
    3. Unconfirmed tracked bridges get NO credit (must wait for Relay)
    4. Untracked bridges get whatever's left
    
    Returns:
        (updated_state, current_vault_balance)
    """
    current_vault_balance = get_vault_usdc(w3_base, usdc_contract)
    
    new_delta = current_vault_balance - state.prev_vault_balance if state.prev_vault_balance > 0 else 0
    if new_delta > 0:
        state.unclaimed_delta += new_delta
        print(f"   📈 Vault balance increased: +${new_delta:.2f} (unclaimed pool now ${state.unclaimed_delta:.2f})")
    elif new_delta < 0:
        drain_amount = min(abs(new_delta), state.unclaimed_delta)
        excess = abs(new_delta) - drain_amount
        if drain_amount > 0:
            state.unclaimed_delta -= drain_amount
            if excess > 0:
                print(f"   📉 Vault balance decreased: ${new_delta:.2f} - drained ${drain_amount:.2f} from pool, ${excess:.2f} was already-credited funds (user claim)")
            else:
                print(f"   📉 Vault balance decreased: ${new_delta:.2f} - drained ${drain_amount:.2f} from unclaimed pool (pool now ${state.unclaimed_delta:.2f})")
        else:
            print(f"   📉 Vault balance decreased: ${new_delta:.2f} (user claimed previously-credited funds)")
    
    state.prev_vault_balance = current_vault_balance
    
    available_delta = state.unclaimed_delta
    
    if not state.in_transit:
        return state, current_vault_balance
    
    updated_in_transit = []
    
    pending_items = [item for item in state.in_transit if item.get("status") == "pending"]
    tracked_items = [item for item in pending_items if item.get("request_id")]
    untracked_items = sorted(
        [item for item in pending_items if not item.get("request_id")],
        key=lambda x: x.get("initiated_at", 0)
    )
    
    newly_confirmed = []
    already_confirmed = []
    still_pending = []
    
    for item in tracked_items:
        request_id = item.get("request_id", "")
        amount = item.get("amount_usdc", 0)
        relay_confirmed = item.get("relay_confirmed", False)
        
        if not relay_confirmed:
            bridge_status, error = poll_bridge_status(request_id)
            
            if bridge_status == "success":
                item["relay_confirmed"] = True
                item["just_confirmed"] = True
                newly_confirmed.append(item)
                print(f"   📡 Relay JUST confirmed ${amount:.2f} - prioritizing for balance credit")
                continue
            
            if bridge_status == "failed":
                print(f"   ❌ Bridge FAILED: ${amount:.2f} - {error}")
                send_telegram_alert(
                    f"❌ Bridge failed: ${amount:.2f}\n{error}\nManual intervention needed!",
                    is_error=True
                )
                item["status"] = "failed"
                item["error"] = error
                continue
            
            still_pending.append(item)
        else:
            already_confirmed.append(item)
    
    confirmed_bridges = newly_confirmed + already_confirmed
    
    for item in confirmed_bridges:
        amount = item.get("amount_usdc", 0)
        initiated_at = item.get("initiated_at", 0)
        age_seconds = time.time() - initiated_at
        balance_credited = item.get("balance_credited", 0.0)
        just_confirmed = item.get("just_confirmed", False)
        
        needed_credit = amount - balance_credited
        if available_delta > 0 and needed_credit > 0:
            credit_now = min(available_delta, needed_credit)
            item["balance_credited"] = balance_credited + credit_now
            balance_credited = item["balance_credited"]
            available_delta -= credit_now
            priority_note = " (PRIORITY - just confirmed)" if just_confirmed else ""
            print(f"   💰 Credited ${credit_now:.2f} to CONFIRMED bridge{priority_note} (total: ${balance_credited:.2f}/${amount:.2f})")
        
        item.pop("just_confirmed", None)
        
        if balance_credited >= amount * 0.98:
            print(f"   ✅ Bridge fully confirmed: ${amount:.2f} (Relay + balance verified)")
            send_telegram_alert(f"✅ Bridge complete: ${amount:.2f} arrived in vault")
            continue
        
        if age_seconds > 7200:
            print(f"   ⚠️ Relay confirmed but balance not seen after 2hr: ${amount:.2f}")
            send_telegram_alert(
                f"⚠️ Bridge stale: ${amount:.2f}\n"
                f"Relay confirmed 2hr ago but balance not verified (credited: ${balance_credited:.2f})\n"
                f"Entry RETAINED - manual check recommended",
                is_error=True
            )
        
        print(f"   ⏳ In-transit (tracked): ${amount:.2f} ({int(age_seconds/60)}min old, relay-confirmed, credited ${balance_credited:.2f}/${amount:.2f})")
        updated_in_transit.append(item)
    
    for item in still_pending:
        amount = item.get("amount_usdc", 0)
        initiated_at = item.get("initiated_at", 0)
        age_seconds = time.time() - initiated_at
        print(f"   ⏳ In-transit (tracked): ${amount:.2f} ({int(age_seconds/60)}min old, awaiting Relay - NO CREDIT until confirmed)")
        updated_in_transit.append(item)
    
    if available_delta > 0 and untracked_items:
        print(f"   📊 Balance delta available for untracked: ${available_delta:.2f}")
    
    for item in untracked_items:
        amount = item.get("amount_usdc", 0)
        initiated_at = item.get("initiated_at", 0)
        age_seconds = time.time() - initiated_at
        balance_credited = item.get("balance_credited", 0.0)
        
        needed_credit = amount - balance_credited
        if available_delta > 0 and needed_credit > 0:
            credit_now = min(available_delta, needed_credit)
            item["balance_credited"] = balance_credited + credit_now
            balance_credited = item["balance_credited"]
            available_delta -= credit_now
            print(f"   💰 Credited ${credit_now:.2f} to untracked bridge (total: ${balance_credited:.2f}/${amount:.2f})")
        
        if balance_credited >= amount * 0.98:
            print(f"   ✅ Bridge confirmed via balance: ${amount:.2f}")
            send_telegram_alert(f"✅ Bridge complete (balance verified): ${amount:.2f} arrived")
            continue
        
        if age_seconds > 3600:
            print(f"   ❌ Bridge UNTRACKED and stale (1hr): ${amount:.2f} (credited: ${balance_credited:.2f})")
            send_telegram_alert(
                f"❌ Bridge untracked timeout: ${amount:.2f}\n"
                f"No requestId and 1hr elapsed (credited: ${balance_credited:.2f})\n"
                f"MANUAL CHECK REQUIRED - this amount will NOT be re-withdrawn automatically",
                is_error=True
            )
            item["status"] = "untracked_timeout"
            continue
        
        print(f"   ⏳ In-transit (UNTRACKED): ${amount:.2f} ({int(age_seconds/60)}min old, credited ${balance_credited:.2f})")
        updated_in_transit.append(item)
    
    state.in_transit = updated_in_transit
    state.unclaimed_delta = available_delta
    
    if available_delta > 0:
        print(f"   📊 Unclaimed delta remaining: ${available_delta:.2f}")
    
    return state, current_vault_balance


# =============================================================================
# KILL SWITCHES & SAFETY
# =============================================================================

def check_kill_switches(w3_base: Web3, vault_contract) -> Tuple[bool, str]:
    """Check if it's safe to proceed with withdrawals."""
    try:
        is_paused = vault_contract.functions.paused().call()
        if is_paused:
            return False, "Vault is paused"
    except:
        pass
    
    return True, "All checks passed"


def check_daily_limit(state: ServicerState, amount: float) -> Tuple[bool, float]:
    """Check if withdrawal would exceed daily limit."""
    remaining = MAX_DAILY_WITHDRAWAL_USDC - state.daily_withdrawn_usdc
    
    if remaining <= 0:
        return False, 0.0
    
    allowed = min(amount, remaining)
    return True, allowed


# =============================================================================
# MAIN SERVICER LOOP
# =============================================================================

def servicer_iteration(
    w3_base: Web3,
    vault_contract,
    usdc_base_contract,
    state: ServicerState,
    dry_run: bool = False
) -> ServicerState:
    """
    Single iteration of the withdrawal servicer.
    
    V7.3: Uses event-driven in-transit detection via USDC Transfer logs.
    No balance reconciliation - purely matches transfer events to pending records.
    
    Returns:
        updated_state
    """
    print(f"\n{'='*60}")
    print(f"💰 WITHDRAWAL SERVICER V7.3 - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    is_safe, reason = check_kill_switches(w3_base, vault_contract)
    if not is_safe:
        print(f"🚫 STOPPED: {reason}")
        return state
    
    # V7.3: Event-driven in-transit detection (replaces balance reconciliation)
    state, vault_usdc = check_in_transit_via_events(
        state, w3_base, usdc_base_contract
    )
    
    pending_usdc, nav = get_pending_usdc(w3_base, vault_contract)
    in_transit_usdc = get_in_transit_usdc(state)
    
    print(f"\n📊 STATUS:")
    print(f"   Pending withdrawals: ${pending_usdc:.2f}")
    print(f"   Vault USDC (Base): ${vault_usdc:.2f}")
    print(f"   In-transit USDC: ${in_transit_usdc:.2f}")
    print(f"   NAV: ${nav:.6f}/share")
    print(f"   Daily withdrawn: ${state.daily_withdrawn_usdc:.2f} / ${MAX_DAILY_WITHDRAWAL_USDC:.2f}")
    
    needed_base = pending_usdc - vault_usdc - in_transit_usdc
    slippage_multiplier = 1 + (WITHDRAWAL_SLIPPAGE_BPS / 10000)
    needed = needed_base * slippage_multiplier if needed_base > 0 else 0
    print(f"\n   Needed (base): ${needed_base:.2f}")
    print(f"   Needed (with {WITHDRAWAL_SLIPPAGE_BPS/100:.1f}% slippage): ${needed:.2f}")
    
    # V7.3.4 FIX: If shortfall exists but is less than $5, bump up to $5 minimum
    # This prevents users getting stuck with sub-$5 withdrawals that never get serviced
    # Surplus stays in vault buffer for future withdrawals
    if needed <= 0:
        print(f"\n✅ No action needed (no shortfall)")
        return state
    elif needed < MIN_WITHDRAWAL_USDC:
        print(f"\n⚠️ Shortfall ${needed:.2f} < ${MIN_WITHDRAWAL_USDC} minimum, bumping to ${MIN_WITHDRAWAL_USDC}")
        needed = MIN_WITHDRAWAL_USDC
    
    # V7.3: Rate limiting - max 1 pending bridge at a time, but allow if pending is stale
    pending_items = [item for item in state.in_transit if item.get("status") == "pending"]
    pending_count = len(pending_items)
    
    if pending_count > 0:
        # Check if all pending items are stale (past self-healing threshold)
        now = time.time()
        all_stale = all(
            (now - item.get("initiated_at", 0)) > STALE_THRESHOLD_SECONDS 
            for item in pending_items
        )
        
        if not all_stale:
            print(f"\n⏳ Waiting for {pending_count} pending bridge(s) to complete before initiating new one")
            return state
        else:
            print(f"\n⚠️ {pending_count} stale pending bridge(s), allowing new bridge initiation")
    
    allowed, max_allowed = check_daily_limit(state, needed)
    if not allowed:
        print(f"\n⏳ Daily limit reached. Will retry tomorrow.")
        send_telegram_alert(
            f"⏳ Daily withdrawal limit reached\n"
            f"Pending: ${pending_usdc:.2f}\n"
            f"Will resume tomorrow"
        )
        return state
    
    needed = min(needed, max_allowed)
    print(f"\n⚠️  WITHDRAWAL NEEDED: ${needed:.2f} (includes slippage buffer)")
    
    pm_cash, pm_positions = get_pm_balance()
    liquidatable_positions = get_liquidatable_cash()
    print(f"\n📊 Polymarket:")
    print(f"   Cash: ${pm_cash:.2f}")
    print(f"   Positions (NAV): ${pm_positions:.2f}")
    print(f"   Liquidatable: ${liquidatable_positions:.2f} (VWAP)")
    
    withdraw_amount = min(needed, pm_cash)
    
    if withdraw_amount >= MIN_WITHDRAWAL_USDC:
        success, request_id, amount_bridged = withdraw_pm_cash_to_bridge(
            withdraw_amount, 
            dry_run=dry_run
        )
        
        if success:
            state.in_transit.append({
                "request_id": request_id,
                "amount_usdc": amount_bridged,
                "initiated_at": time.time(),
                "status": "pending",
            })
            state.daily_withdrawn_usdc += amount_bridged
            needed -= amount_bridged
            
            send_telegram_alert(
                f"💸 Bridging ${amount_bridged:.2f} USDC to vault\n"
                f"Remaining needed: ${needed:.2f}"
            )
    
    # Check if liquidation needed
    if needed >= MIN_WITHDRAWAL_USDC and liquidatable_positions > 0.01:
        # V7.3.3: Re-fetch PM cash to check if new deposits/claims arrived since initial withdrawal
        pm_cash_before_liq, _ = get_pm_balance()
        
        # If new cash appeared, try to withdraw it first before liquidating
        if pm_cash_before_liq >= MIN_WITHDRAWAL_USDC:
            print(f"\n💵 New cash available: ${pm_cash_before_liq:.2f}, attempting withdrawal before liquidation")
            withdraw_amt = min(needed, pm_cash_before_liq)
            success, request_id, amount_bridged = withdraw_pm_cash_to_bridge(withdraw_amt, dry_run=dry_run)
            
            if success:
                state.in_transit.append({
                    "request_id": request_id,
                    "amount_usdc": amount_bridged,
                    "initiated_at": time.time(),
                    "status": "pending",
                })
                state.daily_withdrawn_usdc += amount_bridged
                needed -= amount_bridged
                
                send_telegram_alert(
                    f"💸 Pre-liquidation bridge: ${amount_bridged:.2f} USDC\n"
                    f"Remaining needed: ${needed:.2f}"
                )
        
        # Recheck if liquidation still needed after potential withdrawal
        # V7.3.4 FIX: Always target at least MIN_WITHDRAWAL_USDC to prevent sub-$5 stuck withdrawals
        # If we need $4.33, we liquidate $5 and bridge $5 (surplus stays in vault)
        if needed > 0:
            # Ensure we meet minimum bridge threshold
            liquidation_target = max(needed, MIN_WITHDRAWAL_USDC)
            # But don't exceed what's liquidatable
            liquidation_target = min(liquidation_target, liquidatable_positions)
            
            if liquidation_target < MIN_WITHDRAWAL_USDC and liquidatable_positions >= MIN_WITHDRAWAL_USDC:
                # Edge case: need < $5 but have >= $5 liquidatable - force to $5
                liquidation_target = MIN_WITHDRAWAL_USDC
            
            print(f"\n⚠️  Cash insufficient, need to liquidate ${needed:.2f}")
            print(f"   Liquidatable: ${liquidatable_positions:.2f}")
            print(f"   Targeting: ${liquidation_target:.2f} (min ${MIN_WITHDRAWAL_USDC})")
            
            # Re-fetch PM cash again for accurate baseline in liquidation loop
            pm_cash_at_liq_start, _ = get_pm_balance()
            liquidated = liquidate_positions(liquidation_target, initial_pm_cash=pm_cash_at_liq_start)
            
            # V7.3.3: If liquidate_positions returned 0, it may mean cash became sufficient
            # during the call. Try one more cash withdrawal.
            if liquidated == 0 and needed >= MIN_WITHDRAWAL_USDC:
                pm_cash_post_check, _ = get_pm_balance()
                if pm_cash_post_check >= MIN_WITHDRAWAL_USDC:
                    print(f"   💵 Post-liquidation-check: ${pm_cash_post_check:.2f} available, attempting bridge")
                    success, request_id, amount_bridged = withdraw_pm_cash_to_bridge(
                        min(needed, pm_cash_post_check), dry_run=dry_run
                    )
                    if success:
                        state.in_transit.append({
                            "request_id": request_id,
                            "amount_usdc": amount_bridged,
                            "initiated_at": time.time(),
                            "status": "pending",
                        })
                        state.daily_withdrawn_usdc += amount_bridged
                        needed -= amount_bridged
                        send_telegram_alert(
                            f"💸 Cash-only bridge (no liquidation needed): ${amount_bridged:.2f} USDC\n"
                            f"Remaining needed: ${needed:.2f}"
                        )
                # V7.3.3 FIX: Set needed=0 to prevent double bridge via post-liquidation block
                # This is the cash-only path - no further bridging needed
                needed = 0.0
                print(f"   ✅ Cash-only bridge complete, skipping post-liquidation bridge")
        else:
            print(f"   ✅ Cash withdrawal covered the need, no liquidation required")
            liquidated = 0.0
    elif needed >= MIN_WITHDRAWAL_USDC:
        print(f"\n⚠️  Cash insufficient and no liquidatable positions")
        liquidated = 0.0
    else:
        liquidated = 0.0
    
    if needed >= MIN_WITHDRAWAL_USDC and liquidated > 0:
        pm_cash_after, _ = get_pm_balance()
        
        if pm_cash_after >= MIN_WITHDRAWAL_USDC:
            success, request_id, amount_bridged = withdraw_pm_cash_to_bridge(
                min(needed, pm_cash_after),
                dry_run=dry_run
            )
            
            if success:
                state.in_transit.append({
                    "request_id": request_id,
                    "amount_usdc": amount_bridged,
                    "initiated_at": time.time(),
                    "status": "pending",
                })
                state.daily_withdrawn_usdc += amount_bridged
                needed -= amount_bridged  # Track remaining to prevent double-counting
                
                send_telegram_alert(
                    f"💸 Post-liquidation bridge: ${amount_bridged:.2f} USDC\n"
                    f"Remaining needed: ${needed:.2f}"
                )
    
    state.last_run_timestamp = time.time()
    save_state(state)
    
    return state


def run_servicer(dry_run: bool = False) -> None:
    """Main servicer loop."""
    print("\n" + "="*60)
    print("🚀 WITHDRAWAL SERVICER V7.3 (Event-Driven)")
    print("="*60)
    print(f"\nConfiguration:")
    print(f"   Vault: {VAULT_ADDRESS}")
    print(f"   PM Proxy: {PM_PROXY_ADDRESS[:20]}..." if PM_PROXY_ADDRESS else "   PM Proxy: NOT SET")
    print(f"   Bot URL: {BOT_URL}")
    print(f"   Dry run: {dry_run}")
    print(f"   Min withdrawal: ${MIN_WITHDRAWAL_USDC}")
    print(f"   Max daily: ${MAX_DAILY_WITHDRAWAL_USDC}")
    print(f"   Bypass method: {BYPASS_METHOD}")
    
    if not VAULT_ADDRESS:
        print("❌ VAULT_ADDRESS not set")
        return
    
    if not PM_PROXY_ADDRESS:
        print("❌ POLYMARKET_PROXY_ADDRESS not set")
        return
    
    w3_base = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3_base.is_connected():
        print("❌ Cannot connect to Base RPC")
        return
    
    vault_contract = w3_base.eth.contract(
        address=Web3.to_checksum_address(VAULT_ADDRESS),
        abi=VAULT_V7_ABI
    )
    
    usdc_base_contract = w3_base.eth.contract(
        address=Web3.to_checksum_address(USDC_BASE),
        abi=ERC20_ABI
    )
    
    state = load_state()
    
    if state.prev_vault_balance == 0:
        state.prev_vault_balance = get_vault_usdc(w3_base, usdc_base_contract)
    
    print(f"\n✅ Servicer initialized")
    print(f"   Daily withdrawn so far: ${state.daily_withdrawn_usdc:.2f}")
    print(f"   In-transit items: {len(state.in_transit)}")
    print(f"   Tracked vault balance: ${state.prev_vault_balance:.2f}")
    print(f"   Unclaimed delta pool: ${state.unclaimed_delta:.2f}")
    
    send_telegram_alert(
        f"🚀 Withdrawal Servicer started\n"
        f"Vault: {VAULT_ADDRESS[:15]}...\n"
        f"Dry run: {dry_run}"
    )
    
    consecutive_failures = 0
    
    while True:
        try:
            state = servicer_iteration(
                w3_base,
                vault_contract,
                usdc_base_contract,
                state,
                dry_run=dry_run
            )
            consecutive_failures = 0
            
        except KeyboardInterrupt:
            print("\n\n👋 Servicer stopped by user")
            save_state(state)
            send_telegram_alert("👋 Withdrawal Servicer stopped")
            break
            
        except Exception as e:
            print(f"❌ Error in iteration: {e}")
            import traceback
            traceback.print_exc()
            consecutive_failures += 1
            
            if consecutive_failures >= 5:
                send_telegram_alert(
                    f"❌ Servicer failing repeatedly!\n{e}",
                    is_error=True
                )
        
        print(f"\n💤 Sleeping {LOOP_INTERVAL_SECONDS}s...")
        time.sleep(LOOP_INTERVAL_SECONDS)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="V7.2 Withdrawal Servicer")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without executing")
    parser.add_argument("--once", action="store_true", help="Run single iteration and exit")
    
    args = parser.parse_args()
    
    if args.once:
        w3_base = Web3(Web3.HTTPProvider(BASE_RPC_URL))
        vault_contract = w3_base.eth.contract(
            address=Web3.to_checksum_address(VAULT_ADDRESS),
            abi=VAULT_V7_ABI
        )
        usdc_base_contract = w3_base.eth.contract(
            address=Web3.to_checksum_address(USDC_BASE),
            abi=ERC20_ABI
        )
        state = load_state()
        if state.prev_vault_balance == 0:
            state.prev_vault_balance = get_vault_usdc(w3_base, usdc_base_contract)
        state = servicer_iteration(
            w3_base, vault_contract, usdc_base_contract, state,
            dry_run=args.dry_run
        )
        save_state(state)
    else:
        run_servicer(dry_run=args.dry_run)
