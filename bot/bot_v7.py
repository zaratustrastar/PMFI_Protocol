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

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================
RPC_URL = os.getenv("RPC_URL")
VAULT_V7_ADDRESS = os.getenv("VAULT_V7_ADDRESS") or os.getenv("VAULT_V6_ADDRESS") or ""
USDC_ADDRESS = os.getenv("USDC_ADDRESS") or "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POLYMARKET_BASE_DEPOSIT = "0xa76a91208FC7CB88420070AF978D12F440cab2F0"

ORACLE_PRIVATE_KEY = os.getenv("ORACLE_PRIVATE_KEY") or os.getenv("KEEPER_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS")

HTTP_PORT = int(os.getenv("BOT_HTTP_PORT", 8080))

NAV_VALIDITY_SECONDS = 30
NAV_PRECISION = 10**18

# Safety valve thresholds
MAX_PENDING_AGE_HOURS = 2  # Pause if any deposit pending > 2 hours
MAX_PENDING_RATIO = 0.5   # Pause if pendingCredit > 50% of totalAssets

# Rate limiting
REFRESH_RATE_LIMIT_SECONDS = 5
last_refresh_request = {}

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
    
    def calculate_pending_credit(self, pm_cash_usdc: int) -> int:
        """
        Calculate pending credit using cash-only reconciliation.
        
        pendingCredit = max(0, totalForwarded - pmCash - withdrawnBack)
        """
        self.last_known_pm_cash = pm_cash_usdc
        pending = max(0, self.total_forwarded - pm_cash_usdc - self.withdrawn_back)
        return pending
    
    def get_oldest_pending_age_hours(self, pm_cash_usdc: int) -> float:
        """Get age of oldest unreconciled deposit in hours."""
        if not self.deposit_records:
            return 0.0
        
        # Find deposits that haven't been credited yet
        # (simple heuristic: if total_forwarded > pm_cash + withdrawn, some are pending)
        pending = self.calculate_pending_credit(pm_cash_usdc)
        if pending <= 0:
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

cached_nav = {
    "total_assets": 0,
    "credited_cash": 0,
    "credited_positions": 0,
    "pending_credit": 0,
    "in_flight": 0,
    "nav": 10**6,
    "round_id": 0,
    "last_calculated": 0,
    "total_supply": 0,
    "safety_status": "ok",
}
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
        """Fetch orderbook for a token."""
        try:
            url = f"{self.CLOB_API_URL}/book"
            params = {"token_id": token_id}
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            bids = [{"price": float(b.get("price", 0)), "size": float(b.get("size", 0))} for b in data.get("bids", [])]
            asks = [{"price": float(a.get("price", 0)), "size": float(a.get("size", 0))} for a in data.get("asks", [])]
            
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
            fill_amount = min(remaining, bid["size"])
            total_value += fill_amount * bid["price"]
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
# NAV Engine V7
# =============================================================================

class NavEngineV7:
    """
    Calculates NAV with 3-state asset tracking.
    
    totalAssets = inFlightOnChain + pendingCredit + creditedAssets
    creditedAssets = pmCash + positionsLiquidationValue
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
        
        Returns:
            Dict with all asset components in 6 decimals
        """
        print(f"\n{'='*60}")
        print(f"💰 CALCULATING V7 NAV (3-State)")
        print(f"{'='*60}")
        
        # 1. Fetch in-flight (at deposit address)
        in_flight = self.fetch_in_flight_balance()
        
        # 2. Fetch credited assets from Polymarket
        positions = self.polymarket_client.fetch_positions()
        
        # Calculate liquidation value of positions
        positions_liq_value = 0.0
        for pos in positions:
            token_id = pos.get("token_id", "")
            size = pos.get("size", 0)
            mid_value = pos.get("current_value", 0)
            
            if not token_id:
                positions_liq_value += mid_value
                continue
            
            orderbook = self.polymarket_client.fetch_orderbook(token_id)
            bids = orderbook.get("bids", [])
            
            if not bids:
                print(f"   ❌ {pos['outcome']}: {size:.1f} - NO BIDS (illiquid)")
                continue
            
            liq_value = self.polymarket_client.simulate_market_sell(size, bids)
            positions_liq_value += liq_value
        
        pm_cash = self.polymarket_client.fetch_cash_balance()
        
        # Convert to 6 decimals
        credited_cash = int(pm_cash * 1e6)
        credited_positions = int(positions_liq_value * 1e6)
        
        # 3. Calculate pending credit using cash-only reconciliation
        pending_credit = self.pending_tracker.calculate_pending_credit(credited_cash)
        
        # Total assets
        total_assets = in_flight + pending_credit + credited_cash + credited_positions
        
        print(f"\n📊 Asset Breakdown:")
        print(f"   • In-flight (deposit addr): ${in_flight/1e6:.2f}")
        print(f"   • Pending credit:           ${pending_credit/1e6:.2f}")
        print(f"   • Credited cash:            ${credited_cash/1e6:.2f}")
        print(f"   • Credited positions:       ${credited_positions/1e6:.2f}")
        print(f"   ─────────────────────────────")
        print(f"   • TOTAL ASSETS:             ${total_assets/1e6:.2f}")
        
        return {
            "in_flight": in_flight,
            "pending_credit": pending_credit,
            "credited_cash": credited_cash,
            "credited_positions": credited_positions,
            "total_assets": total_assets,
            "positions_count": len(positions),
        }


# =============================================================================
# NAV Signing V7
# =============================================================================

def create_nav_data_hash_v7(
    total_assets: int,
    credited_cash: int,
    credited_positions: int,
    pending_credit: int,
    in_flight: int,
    timestamp: int,
    deadline: int,
    round_id: int,
    vault_address: str
) -> bytes:
    """Create hash for NavDataV7 signing."""
    from eth_abi import encode
    
    NAV_TYPEHASH = Web3.keccak(text="NavDataV7(uint256 totalAssets,uint256 creditedCash,uint256 creditedPositions,uint256 pendingCredit,uint256 inFlightOnChain,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)")
    
    vault_addr_checksum = Web3.to_checksum_address(vault_address)
    
    encoded_data = encode(
        ['bytes32', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'address'],
        [NAV_TYPEHASH, total_assets, credited_cash, credited_positions, pending_credit, in_flight, timestamp, deadline, round_id, vault_addr_checksum]
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


def check_safety_valves(breakdown: Dict, pm_cash: int) -> Tuple[str, str]:
    """
    Check safety valves and return status.
    
    Returns:
        Tuple of (status, reason)
        status: "ok", "warning", or "pause"
    """
    global pending_tracker
    
    pending_credit = breakdown.get("pending_credit", 0)
    total_assets = breakdown.get("total_assets", 0)
    
    # Check max pending age
    oldest_age = pending_tracker.get_oldest_pending_age_hours(pm_cash)
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


def get_signed_nav_data_v7() -> Dict:
    """Get current NAV with full breakdown and fresh signature."""
    global cached_nav, nav_engine, pending_tracker, vault_v7, w3
    
    now = int(time.time())
    
    # Get vault state from contract
    try:
        if vault_v7:
            total_supply = vault_v7.functions.totalSupply().call()
            vault_buffer = usdc.functions.balanceOf(VAULT_V7_ADDRESS).call() if usdc else 0
            last_round_id = vault_v7.functions.lastRoundId().call()
            total_forwarded = vault_v7.functions.totalForwardedToPolymarket().call()
            expected_assets = vault_v7.functions.expectedAssets().call()
            
            # Sync pending tracker with contract
            pending_tracker.sync_from_contract(total_forwarded)
            
            print(f"📊 On-chain: supply={total_supply/1e18:.4f}, buffer={vault_buffer/1e6:.2f}, forwarded={total_forwarded/1e6:.2f}, lastRoundId={last_round_id}")
        else:
            total_supply = 0
            vault_buffer = 0
            last_round_id = 0
            expected_assets = 0
    except Exception as e:
        print(f"❌ Error reading vault state: {e}")
        total_supply = 0
        vault_buffer = 0
        last_round_id = 0
        expected_assets = 0
    
    # Calculate NAV breakdown
    if nav_engine:
        breakdown = nav_engine.calculate_nav_breakdown()
    else:
        breakdown = {
            "in_flight": 0,
            "pending_credit": 0,
            "credited_cash": 0,
            "credited_positions": 0,
            "total_assets": 0,
        }
    
    # Check safety valves
    safety_status, safety_reason = check_safety_valves(breakdown, breakdown.get("credited_cash", 0))
    if safety_status == "pause":
        print(f"🛑 SAFETY VALVE TRIGGERED: {safety_reason}")
    elif safety_status == "warning":
        print(f"⚠️ SAFETY WARNING: {safety_reason}")
    
    # Calculate NAV per share
    total_assets = breakdown["total_assets"]
    if total_supply > 0:
        nav = (total_assets * NAV_PRECISION) // total_supply
    else:
        nav = 10**6  # $1.00 per share
    
    # Increment round ID
    new_round_id = last_round_id + 1
    
    timestamp = now
    deadline = now + NAV_VALIDITY_SECONDS
    
    # Sign the full breakdown
    signature, signer = sign_nav_data_v7(
        total_assets,
        breakdown["credited_cash"],
        breakdown["credited_positions"],
        breakdown["pending_credit"],
        breakdown["in_flight"],
        timestamp,
        deadline,
        new_round_id,
        VAULT_V7_ADDRESS
    )
    
    # Update cache
    with nav_lock:
        cached_nav = {
            "total_assets": total_assets,
            "credited_cash": breakdown["credited_cash"],
            "credited_positions": breakdown["credited_positions"],
            "pending_credit": breakdown["pending_credit"],
            "in_flight": breakdown["in_flight"],
            "nav": nav,
            "round_id": new_round_id,
            "last_calculated": now,
            "total_supply": total_supply,
            "vault_buffer": vault_buffer,
            "safety_status": safety_status,
            "safety_reason": safety_reason,
            "expected_assets": expected_assets,
        }
    
    print(f"✅ Signed NAV: ${nav/1e6:.4f}/share (round {new_round_id}) [{safety_status}]")
    
    return {
        "navData": {
            "totalAssets": str(total_assets),
            "creditedCash": str(breakdown["credited_cash"]),
            "creditedPositions": str(breakdown["credited_positions"]),
            "pendingCredit": str(breakdown["pending_credit"]),
            "inFlightOnChain": str(breakdown["in_flight"]),
            "timestamp": timestamp,
            "deadline": deadline,
            "roundId": new_round_id,
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
            "valid_until": deadline,
            "safety_status": safety_status,
            "safety_reason": safety_reason,
        }
    }


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
    """Get signed NavDataV7 for deposit/withdraw transactions."""
    client_ip = flask_request.remote_addr or "unknown"
    current_time = time.time()
    
    if client_ip in last_refresh_request:
        time_since_last = current_time - last_refresh_request[client_ip]
        if time_since_last < REFRESH_RATE_LIMIT_SECONDS:
            pass  # Still generate fresh for transactions
    
    last_refresh_request[client_ip] = current_time
    
    try:
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
    """Refresh NAV cache periodically."""
    while True:
        try:
            get_signed_nav_data_v7()
        except Exception as e:
            print(f"❌ NAV refresh error: {e}")
        time.sleep(30)


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
    global w3, usdc, vault_v7, polymarket_client, nav_engine, oracle_account, pending_tracker
    
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
    
    # Initialize Polymarket client
    polymarket_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS)
    
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
