#!/usr/bin/env python3
"""
PredictFi Sniper Vault V6 - NAV Signing Bot (Zero Gas Oracle)

=============================================================================
ARCHITECTURE:
=============================================================================

This bot is a ZERO-GAS oracle for the V6 vault:
- Signs NAV data off-chain (free - no gas needed)
- Users include the signature when they deposit/withdraw (they pay gas)
- Bot calculates liquidation NAV from Polymarket orderbooks

=============================================================================
ENDPOINTS:
=============================================================================

1. /health           - Health check
2. /price            - Get cached price (fast, for UI display)
3. /sign-nav         - Get signed NavData for deposit/withdraw (the key endpoint!)
4. /sign-nav/debug   - Debug endpoint showing NAV calculation details

=============================================================================
SECURITY:
=============================================================================

- Only the ORACLE_PRIVATE_KEY can sign valid NAVs
- Signatures include vault address (domain separation)
- NavData includes timestamp, deadline, roundId (replay protection)
- Users CANNOT fake or manipulate the NAV

=============================================================================
ENVIRONMENT VARIABLES:
=============================================================================

Required:
- RPC_URL: Base Mainnet RPC endpoint
- VAULT_V6_ADDRESS: PredictFiSniperVaultV6 contract address
- USDC_ADDRESS: USDC contract address
- ORACLE_PRIVATE_KEY: Private key for signing NAV (same as deployer)
- POLYMARKET_PROXY_ADDRESS: Polymarket wallet with positions

Optional:
- BOT_HTTP_PORT: HTTP API port (default 8080)

"""

import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from flask import Flask, jsonify, request as flask_request, send_from_directory
from flask_cors import CORS

# Load environment variables
load_dotenv()

# =============================================================================
# Configuration
# =============================================================================
RPC_URL = os.getenv("RPC_URL")
# Support both VAULT_V6_ADDRESS (new) and VAULT_V4_ADDRESS (legacy) env vars
VAULT_V6_ADDRESS = os.getenv("VAULT_V6_ADDRESS") or os.getenv("VAULT_V4_ADDRESS")
USDC_ADDRESS = os.getenv("USDC_ADDRESS")

# Oracle key - this is the key that signs NAV updates
# Should match the navSigner address in the V6 contract
ORACLE_PRIVATE_KEY = os.getenv("ORACLE_PRIVATE_KEY") or os.getenv("KEEPER_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")

# Polymarket wallet address (for fetching positions)
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS")

# HTTP API port
HTTP_PORT = int(os.getenv("BOT_HTTP_PORT", 8080))

# NAV signing parameters
NAV_VALIDITY_SECONDS = 30  # Signature valid for 30 seconds
NAV_PRECISION = 10**18  # 1e18 precision for NAV

# Rate limiting
REFRESH_RATE_LIMIT_SECONDS = 5
last_refresh_request = {}

# Global state
w3 = None
usdc = None
vault_v6 = None
polymarket_client = None
nav_engine = None
oracle_account = None

# Global cached NAV data
cached_nav = {
    "nav": 10**6,  # 1e6 = $1.00 per share (matches contract formula)
    "round_id": 0,
    "last_calculated": 0,
    "total_supply": 0,
    "vault_balance": 0,
    "liquidation_value": 0,
    "pm_cash": 0,
}
nav_lock = threading.Lock()

# Flask app
flask_app = Flask(__name__)
CORS(flask_app)

# Frontend directory (check multiple possible locations)
_script_dir = Path(__file__).resolve().parent
FRONTEND_DIR = _script_dir / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = _script_dir.parent / "frontend"
if not FRONTEND_DIR.exists():
    print(f"⚠️  Frontend directory not found at {FRONTEND_DIR}")

@flask_app.route('/')
def serve_index():
    """Serve the main frontend page."""
    return send_from_directory(FRONTEND_DIR, 'index.html')

@flask_app.route('/<path:filename>')
def serve_static(filename):
    """Serve static files from frontend directory."""
    return send_from_directory(FRONTEND_DIR, filename)


# =============================================================================
# POLYMARKET CLIENT (copied from bot.py)
# =============================================================================

class PolymarketClient:
    """Read-only client for fetching positions and orderbook data."""
    
    DATA_API_URL = "https://data-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    def __init__(self, wallet_address: str):
        self.wallet_address = wallet_address
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
    
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
                
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                if not data:
                    break
                    
                all_positions.extend(data)
                print(f"   Fetched {len(data)} positions (total: {len(all_positions)})")
                
                if len(data) < limit:
                    break
                offset += limit
            
            def safe_float(val, default=0.0):
                if val is None:
                    return default
                try:
                    return float(val)
                except:
                    return default
            
            positions = []
            for p in all_positions:
                size = safe_float(p.get("size"))
                if size <= 0:
                    continue
                
                positions.append({
                    "token_id": p.get("asset") or "",
                    "title": p.get("title") or "",
                    "outcome": p.get("outcome") or "",
                    "size": size,
                    "current_value": safe_float(p.get("currentValue")),
                })
            
            print(f"✅ Found {len(positions)} active positions")
            return positions
            
        except Exception as e:
            print(f"❌ Error fetching positions: {e}")
            return []
    
    def fetch_orderbook(self, token_id: str) -> Dict:
        """Fetch orderbook for a token."""
        try:
            url = f"{self.CLOB_API_URL}/book"
            params = {"token_id": token_id}
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            bids = []
            asks = []
            
            for bid in data.get("bids", []):
                bids.append({"price": float(bid.get("price", 0)), "size": float(bid.get("size", 0))})
            for ask in data.get("asks", []):
                asks.append({"price": float(ask.get("price", 0)), "size": float(ask.get("size", 0))})
            
            bids.sort(key=lambda x: x["price"], reverse=True)
            asks.sort(key=lambda x: x["price"])
            
            return {"bids": bids, "asks": asks}
            
        except Exception as e:
            print(f"❌ Error fetching orderbook: {e}")
            return {"bids": [], "asks": []}
    
    def simulate_market_sell(self, size: float, bids: List[Dict]) -> float:
        """Simulate market sell through orderbook depth."""
        if not bids:
            return 0.0
        
        remaining = size
        total_value = 0.0
        
        for bid in bids:
            bid_price = bid["price"]
            bid_size = bid["size"]
            
            fill_amount = min(remaining, bid_size)
            total_value += fill_amount * bid_price
            remaining -= fill_amount
            
            if remaining <= 0:
                break
        
        return total_value
    
    def fetch_cash_balance(self) -> float:
        """Fetch USDC cash balance on Polymarket."""
        try:
            url = f"{self.DATA_API_URL}/balance"
            params = {"user": self.wallet_address}
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            cash = float(data.get("balance", 0)) if data else 0
            print(f"💵 Polymarket cash: ${cash:.2f}")
            return cash
            
        except Exception as e:
            print(f"❌ Error fetching cash: {e}")
            return 0.0


# =============================================================================
# NAV ENGINE
# =============================================================================

class NavEngine:
    """Calculates realistic liquidation NAV from Polymarket positions."""
    
    def __init__(self, polymarket_client: PolymarketClient):
        self.polymarket_client = polymarket_client
    
    def calculate_liquidation_nav(self) -> Tuple[float, Dict]:
        """
        Calculate realistic liquidation NAV.
        
        Returns:
            Tuple of (nav_usdc, details_dict)
        """
        print(f"\n{'='*60}")
        print(f"💰 CALCULATING LIQUIDATION NAV")
        print(f"{'='*60}")
        
        positions = self.polymarket_client.fetch_positions()
        
        total_liquidation_value = 0.0
        total_mid_value = 0.0
        
        for pos in positions:
            token_id = pos.get("token_id", "")
            size = pos.get("size", 0)
            mid_value = pos.get("current_value", 0)
            total_mid_value += mid_value
            
            if not token_id:
                total_liquidation_value += mid_value
                continue
            
            orderbook = self.polymarket_client.fetch_orderbook(token_id)
            bids = orderbook.get("bids", [])
            
            if not bids:
                # Illiquid - value = 0
                print(f"   ❌ {pos['outcome']}: {size:.1f} - NO BIDS (illiquid)")
                continue
            
            liq_value = self.polymarket_client.simulate_market_sell(size, bids)
            total_liquidation_value += liq_value
            
            diff_pct = ((liq_value - mid_value) / mid_value * 100) if mid_value > 0 else 0
            print(f"   • {pos['outcome']}: {size:.1f} → ${liq_value:.2f} ({diff_pct:+.0f}%)")
        
        pm_cash = self.polymarket_client.fetch_cash_balance()
        
        total_nav = total_liquidation_value + pm_cash
        
        print(f"\n📊 NAV: ${total_nav:.2f} (positions: ${total_liquidation_value:.2f}, cash: ${pm_cash:.2f})")
        
        return total_nav, {
            "positions_count": len(positions),
            "mid_value": total_mid_value,
            "liquidation_value": total_liquidation_value,
            "pm_cash": pm_cash,
            "total": total_nav,
        }


# =============================================================================
# NAV SIGNING (THE KEY PART!)
# =============================================================================

def create_nav_data_hash(nav: int, timestamp: int, deadline: int, round_id: int, vault_address: str) -> bytes:
    """
    Create the hash for NAV data signing.
    
    This must match the contract's _verifyAndApplyNav function:
    
    bytes32 structHash = keccak256(abi.encode(
        NAV_TYPEHASH,
        nav,
        timestamp,
        deadline,
        roundId,
        address(this)
    ));
    
    IMPORTANT: Solidity's abi.encode pads each argument to 32 bytes.
    Web3.solidity_keccak uses abi.encodePacked (no padding), so we must
    use eth_abi.encode instead.
    """
    import eth_abi
    
    # NAV_TYPEHASH from contract
    NAV_TYPEHASH = Web3.keccak(text="NavData(uint256 nav,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)")
    
    # Convert vault address to checksum format
    vault_addr_checksum = Web3.to_checksum_address(vault_address)
    
    # Use eth_abi.encode to match Solidity's abi.encode (with proper padding)
    encoded_data = eth_abi.encode(
        ['bytes32', 'uint256', 'uint256', 'uint256', 'uint256', 'address'],
        [NAV_TYPEHASH, nav, timestamp, deadline, round_id, vault_addr_checksum]
    )
    
    # Hash the encoded data
    struct_hash = Web3.keccak(encoded_data)
    
    return struct_hash


def sign_nav_data(nav: int, timestamp: int, deadline: int, round_id: int, vault_address: str) -> Tuple[str, str]:
    """
    Sign NAV data with the oracle private key.
    
    Returns:
        Tuple of (signature_hex, signer_address)
    """
    global oracle_account
    
    # Create the struct hash
    struct_hash = create_nav_data_hash(nav, timestamp, deadline, round_id, vault_address)
    
    # Sign as Ethereum signed message (matches toEthSignedMessageHash in contract)
    message = encode_defunct(primitive=struct_hash)
    signed = oracle_account.sign_message(message)
    
    return signed.signature.hex(), oracle_account.address


def get_signed_nav_data() -> Dict:
    """
    Get the current NAV with a fresh signature.
    
    This is the key endpoint - frontend calls this before deposit/withdraw.
    
    Returns:
        Dict with navData fields and signature
    """
    global cached_nav, nav_engine, polymarket_client, usdc, vault_v6, w3
    
    # Calculate current NAV
    now = int(time.time())
    
    # Get vault state
    try:
        total_supply = vault_v6.functions.totalSupply().call() if vault_v6 else 0
        vault_balance = usdc.functions.balanceOf(VAULT_V6_ADDRESS).call() if usdc and VAULT_V6_ADDRESS else 0
        last_round_id = vault_v6.functions.lastRoundId().call() if vault_v6 else 0
    except Exception as e:
        print(f"❌ Error reading vault state: {e}")
        total_supply = 0
        vault_balance = 0
        last_round_id = 0
    
    # Calculate liquidation value from Polymarket
    pm_liquidation_value = 0
    pm_cash = 0
    
    if nav_engine:
        try:
            total_pm_value, details = nav_engine.calculate_liquidation_nav()
            pm_liquidation_value = int(total_pm_value * 1e6)  # Convert to 6 decimals
            pm_cash = int(details.get("pm_cash", 0) * 1e6)
        except Exception as e:
            print(f"❌ Error calculating PM NAV: {e}")
    
    # Total assets = vault buffer + Polymarket value
    total_assets_6dec = vault_balance + pm_liquidation_value
    
    # Calculate NAV per share
    # Contract formula for deposit: sharesToMint = usdcAmount_6dec * 1e18 / nav
    # Contract formula for withdraw: grossUsdc_6dec = shares_18dec * nav / 1e18
    # 
    # For NAV = $1.00/share, depositing 4 USDC should give 4e18 shares:
    #   4e6 * 1e18 / nav = 4e18  →  nav = 1e6
    #
    # So: nav = (total_assets_6dec * 1e18) / total_supply_18dec
    # When total_assets = 10 USDC (10e6) and total_supply = 10 shares (10e18):
    #   nav = 10e6 * 1e18 / 10e18 = 1e6 ✓
    if total_supply > 0:
        nav = (total_assets_6dec * NAV_PRECISION) // total_supply
    else:
        # Initial NAV when no supply: use 1e6 to represent $1.00/share
        # This matches: usdcAmount * 1e18 / 1e6 = usdcAmount * 1e12 shares
        # For 1 USDC (1e6): 1e6 * 1e18 / 1e6 = 1e18 shares ✓
        nav = 10**6  # 1e6 = $1.00 per share
    
    # Increment round ID
    new_round_id = last_round_id + 1
    
    # Create NavData
    timestamp = now
    deadline = now + NAV_VALIDITY_SECONDS
    
    # Sign the NAV data
    signature, signer = sign_nav_data(nav, timestamp, deadline, new_round_id, VAULT_V6_ADDRESS)
    
    # Update cached values
    with nav_lock:
        cached_nav = {
            "nav": nav,
            "round_id": new_round_id,
            "last_calculated": now,
            "total_supply": total_supply,
            "vault_balance": vault_balance,
            "liquidation_value": pm_liquidation_value,
            "pm_cash": pm_cash,
            "total_assets": total_assets_6dec,
        }
    
    # NAV is ~1e6 for $1/share, so divide by 1e6 to get human-readable price
    print(f"✅ Signed NAV: ${nav / 1e6:.4f}/share (round {new_round_id})")
    
    return {
        "navData": {
            "nav": str(nav),
            "timestamp": timestamp,
            "deadline": deadline,
            "roundId": new_round_id,
        },
        "signature": "0x" + signature,
        "signer": signer,
        "metadata": {
            "price_per_share": nav / 1e6,  # NAV is ~1e6 for $1/share
            "total_assets_usdc": total_assets_6dec / 1e6,
            "total_supply": total_supply / 1e18,
            "vault_buffer_usdc": vault_balance / 1e6,
            "pm_value_usdc": pm_liquidation_value / 1e6,
            "valid_until": deadline,
        }
    }


# =============================================================================
# FLASK ENDPOINTS
# =============================================================================

@flask_app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "version": "v4",
        "timestamp": int(time.time()),
        "oracle_address": oracle_account.address if oracle_account else None,
    })


@flask_app.route('/price', methods=['GET'])
def get_price():
    """Get cached price (fast, for UI display)."""
    with nav_lock:
        return jsonify({
            "price_per_share": cached_nav["nav"] / 1e6,  # NAV is ~1e6 for $1/share
            "nav_raw": str(cached_nav["nav"]),
            "total_supply": cached_nav["total_supply"] / 1e18 if cached_nav["total_supply"] else 0,
            "vault_buffer_usdc": cached_nav["vault_balance"] / 1e6 if cached_nav["vault_balance"] else 0,
            "pm_value_usdc": cached_nav["liquidation_value"] / 1e6 if cached_nav["liquidation_value"] else 0,
            "last_updated": cached_nav["last_calculated"],
        })


@flask_app.route('/sign-nav', methods=['GET', 'POST'])
def sign_nav():
    """
    Get signed NavData for deposit/withdraw.
    
    This is THE KEY ENDPOINT. Frontend calls this right before a transaction.
    
    Returns:
    {
        "navData": {
            "nav": "1000000000000000000",  // NAV in 1e18 format
            "timestamp": 1702300000,
            "deadline": 1702300030,
            "roundId": 42
        },
        "signature": "0x...",
        "signer": "0x...",
        "metadata": {
            "price_per_share": 1.0,
            "total_assets_usdc": 10000.0,
            ...
        }
    }
    """
    # Rate limiting
    client_ip = flask_request.remote_addr or "unknown"
    current_time = time.time()
    
    if client_ip in last_refresh_request:
        time_since_last = current_time - last_refresh_request[client_ip]
        if time_since_last < REFRESH_RATE_LIMIT_SECONDS:
            # Return cached if rate limited (but still valid signature)
            pass  # For now, always generate fresh for transactions
    
    last_refresh_request[client_ip] = current_time
    
    try:
        result = get_signed_nav_data()
        return jsonify(result)
    except Exception as e:
        print(f"❌ Error signing NAV: {e}")
        return jsonify({"error": str(e)}), 500


@flask_app.route('/sign-nav/debug', methods=['GET'])
def sign_nav_debug():
    """Debug endpoint showing NAV calculation details."""
    try:
        result = get_signed_nav_data()
        
        # Add extra debug info
        result["debug"] = {
            "vault_address": VAULT_V6_ADDRESS,
            "oracle_address": oracle_account.address if oracle_account else None,
            "polymarket_wallet": POLYMARKET_PROXY_ADDRESS,
        }
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "traceback": str(e.__traceback__)}), 500


# =============================================================================
# BACKGROUND NAV REFRESH (for cached /price endpoint)
# =============================================================================

def nav_refresh_loop():
    """Refresh NAV cache periodically (for /price endpoint)."""
    while True:
        try:
            get_signed_nav_data()
        except Exception as e:
            print(f"❌ NAV refresh error: {e}")
        time.sleep(30)  # Refresh every 30 seconds for display purposes


# =============================================================================
# MAIN
# =============================================================================

def load_abi(contract_name: str) -> dict:
    """Load ABI from Hardhat artifacts."""
    project_root = Path(__file__).parent.parent
    artifact_path = project_root / "artifacts" / "contracts" / f"{contract_name}.sol" / f"{contract_name}.json"
    
    if not artifact_path.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")
    
    with open(artifact_path, "r") as f:
        artifact = json.load(f)
    
    return artifact["abi"]


def main():
    """Main entry point for V4 NAV signing bot."""
    global w3, usdc, vault_v6, polymarket_client, nav_engine, oracle_account
    
    print("\n" + "="*60)
    print("🏦 PredictFi Sniper V4 - NAV Signing Bot (Zero Gas Oracle)")
    print("="*60 + "\n")
    
    # Validate required environment variables
    if not RPC_URL:
        print("❌ RPC_URL not set")
        sys.exit(1)
    if not ORACLE_PRIVATE_KEY:
        print("❌ ORACLE_PRIVATE_KEY not set")
        sys.exit(1)
    if not VAULT_V6_ADDRESS:
        print("⚠️  VAULT_V6_ADDRESS not set - signing will use placeholder")
    if not POLYMARKET_PROXY_ADDRESS:
        print("⚠️  POLYMARKET_PROXY_ADDRESS not set - NAV will be 1.0")
    
    # Setup oracle account
    oracle_account = Account.from_key(ORACLE_PRIVATE_KEY)
    print(f"✅ Oracle signer: {oracle_account.address}")
    
    # Connect to chain
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print(f"❌ Failed to connect to RPC: {RPC_URL}")
        sys.exit(1)
    print(f"✅ Connected to chain (ID: {w3.eth.chain_id})")
    
    # Load contracts
    try:
        usdc_abi = load_abi("TestUSDC")
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=usdc_abi
        )
        print(f"✅ USDC contract: {USDC_ADDRESS}")
    except Exception as e:
        print(f"⚠️  Could not load USDC contract: {e}")
        usdc = None
    
    try:
        vault_abi = load_abi("PredictFiSniperVaultV4")
        vault_v6 = w3.eth.contract(
            address=Web3.to_checksum_address(VAULT_V6_ADDRESS),
            abi=vault_abi
        )
        print(f"✅ Vault V4 contract: {VAULT_V6_ADDRESS}")
        
        # Verify oracle is the signer
        on_chain_signer = vault_v6.functions.navSigner().call()
        if on_chain_signer.lower() != oracle_account.address.lower():
            print(f"\n⚠️  WARNING: Vault navSigner ({on_chain_signer}) != Oracle ({oracle_account.address})")
            print(f"   Signatures will be REJECTED by the contract!")
        else:
            print(f"✅ Oracle matches vault navSigner")
    except Exception as e:
        print(f"⚠️  Could not load Vault V4 contract: {e}")
        vault_v6 = None
    
    # Initialize Polymarket client
    if POLYMARKET_PROXY_ADDRESS:
        polymarket_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS)
        nav_engine = NavEngine(polymarket_client)
        print(f"✅ Polymarket client initialized")
    else:
        print("⚠️  Polymarket client not initialized")
    
    # Initial NAV calculation
    print("\n📊 Initial NAV calculation...")
    try:
        get_signed_nav_data()
    except Exception as e:
        print(f"⚠️  Initial NAV calc failed: {e}")
    
    # Start background NAV refresh
    refresh_thread = threading.Thread(target=nav_refresh_loop, daemon=True)
    refresh_thread.start()
    
    # Start Flask server
    print(f"\n🚀 Starting HTTP API on port {HTTP_PORT}")
    print(f"   Serving frontend from: {FRONTEND_DIR}")
    print(f"   Endpoints:")
    print(f"   - GET  /             - Frontend UI")
    print(f"   - GET  /health       - Health check")
    print(f"   - GET  /price        - Cached price (for UI)")
    print(f"   - GET  /sign-nav     - Get signed NavData (for transactions)")
    print(f"   - GET  /sign-nav/debug - Debug NAV calculation")
    print(f"\n   Press Ctrl+C to stop\n")
    
    flask_app.run(host='0.0.0.0', port=HTTP_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
