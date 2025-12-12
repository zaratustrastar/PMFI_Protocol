#!/usr/bin/env python3
"""
pSNIPER V5 Bot - Withdrawal Watchdog + NAV Oracle

Key Features:
1. Signed NAV Oracle (zero gas for protocol)
2. Withdrawal Watchdog: monitors pending withdrawals, refills buffer
3. Anti-manipulation filters in NAV engine
4. 90/10 auto-split monitoring

Endpoints:
- GET /health - Health check
- GET /nav - Current NAV info
- GET /sign-nav - Get signed NAV for deposit/withdraw
- GET /withdrawals - Pending withdrawal queue status
- POST /refill-trigger - Manually trigger buffer refill check
"""

import os
import time
import json
import threading
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
import eth_abi
import requests

load_dotenv()


def send_telegram_alert(message: str):
    """Send alert to Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"⚠️  Telegram not configured: {message}")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚨 pSNIPER V5 ALERT\n\n{message}",
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

# =============================================================================
# CONFIGURATION
# =============================================================================

ORACLE_PRIVATE_KEY = os.getenv("ORACLE_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
VAULT_V5_ADDRESS = os.getenv("VAULT_V5_ADDRESS", "")
RPC_URL = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
USDC_ADDRESS = os.getenv("USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

NAV_PRECISION = 10**18
NAV_VALIDITY_SECONDS = 30
WATCHDOG_INTERVAL_SECONDS = 30

MIN_BID_SIZE_USDC = 5.0
MAX_NAV_SANITY_CHANGE_PCT = 10.0
NAV_HAIRCUT = 0.995

MAX_HOURLY_LIQUIDATION_USDC = 5000.0
CIRCUIT_BREAKER_NAV_DROP_PCT = 15.0
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# =============================================================================
# GLOBALS
# =============================================================================

app = Flask(__name__)
CORS(app)

w3: Optional[Web3] = None
oracle_account = None
usdc = None
vault_v5 = None
polymarket_client = None
nav_engine = None

cached_nav = {
    "nav": NAV_PRECISION,
    "timestamp": 0,
    "round_id": 0,
    "details": {}
}
nav_lock = threading.Lock()

previous_nav_value = NAV_PRECISION

hourly_liquidation_tracker = {
    "hour": 0,
    "amount": 0.0,
}
circuit_breaker_triggered = False
baseline_nav = None

# Hourly NAV kill switch tracking
nav_history: List[Tuple[int, int]] = []  # List of (timestamp, nav_value) tuples
hourly_nav_kill_switch = False
HOURLY_NAV_CHANGE_LIMIT_PCT = 10.0  # Max allowed NAV change in rolling 1-hour window

# Empty orderbook kill switch
orderbook_kill_switch = False

# =============================================================================
# ABI DEFINITIONS
# =============================================================================

VAULT_V5_ABI = [
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastNav", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastRoundId", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalPendingShares", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "nextWithdrawalIndex", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "targetBuffer", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getPendingWithdrawalShares", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getIdleBalance", "outputs": [
        {"type": "uint256"}, {"type": "uint256"}
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getVaultState", "outputs": [
        {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "uint256"}, {"type": "uint256"}, {"type": "bool"}, {"type": "bool"}, {"type": "uint256"}
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "getWithdrawalRequest", "outputs": [
        {"type": "address"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "bool"}, {"type": "bool"}
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "withdrawalQueue", "outputs": [
        {"type": "address"}, {"type": "uint256"}, {"type": "uint256"}, {"type": "bool"}
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "investIdle", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]

ERC20_ABI = [
    {"inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]


# =============================================================================
# POLYMARKET CLIENT
# =============================================================================

def safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except:
        return default


class PolymarketClient:
    """Client for reading Polymarket positions and orderbooks."""
    
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
        """Fetch current positions from Polymarket."""
        try:
            url = f"{self.DATA_API_URL}/positions"
            params = {"user": self.wallet_address}
            response = self.session.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            positions = []
            
            for p in data if isinstance(data, list) else []:
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
                price = float(bid.get("price", 0))
                size = float(bid.get("size", 0))
                if size * price >= MIN_BID_SIZE_USDC:
                    bids.append({"price": price, "size": size})
            
            for ask in data.get("asks", []):
                asks.append({"price": float(ask.get("price", 0)), "size": float(ask.get("size", 0))})
            
            bids.sort(key=lambda x: x["price"], reverse=True)
            asks.sort(key=lambda x: x["price"])
            
            return {"bids": bids, "asks": asks}
            
        except Exception as e:
            print(f"❌ Error fetching orderbook: {e}")
            return {"bids": [], "asks": []}
    
    def simulate_market_sell(self, size: float, bids: List[Dict]) -> float:
        """Simulate market sell through orderbook depth with anti-manipulation filter."""
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
# NAV ENGINE WITH ANTI-MANIPULATION
# =============================================================================

class NavEngine:
    """Calculates realistic liquidation NAV with anti-manipulation filters."""
    
    def __init__(self, polymarket_client: PolymarketClient):
        self.polymarket_client = polymarket_client
        self.previous_nav = None
    
    def calculate_liquidation_nav(self) -> Tuple[float, Dict]:
        """
        Calculate realistic liquidation NAV with safety checks.
        
        Anti-manipulation features:
        1. Minimum bid size filter (ignore bids < $5)
        2. Sanity check vs previous NAV (max 10% change warning)
        """
        global previous_nav_value
        
        print(f"\n{'='*60}")
        print(f"💰 CALCULATING LIQUIDATION NAV (with anti-manipulation)")
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
                print(f"   ❌ {pos['outcome']}: {size:.1f} - NO BIDS (illiquid)")
                continue
            
            liq_value = self.polymarket_client.simulate_market_sell(size, bids)
            total_liquidation_value += liq_value
            
            diff_pct = ((liq_value - mid_value) / mid_value * 100) if mid_value > 0 else 0
            print(f"   • {pos['outcome']}: {size:.1f} → ${liq_value:.2f} ({diff_pct:+.0f}%)")
        
        pm_cash = self.polymarket_client.fetch_cash_balance()
        
        total_nav = total_liquidation_value + pm_cash
        
        if self.previous_nav is not None and self.previous_nav > 0:
            change_pct = abs(total_nav - self.previous_nav) / self.previous_nav * 100
            if change_pct > MAX_NAV_SANITY_CHANGE_PCT:
                print(f"⚠️  WARNING: NAV changed {change_pct:.1f}% from previous!")
                print(f"   Previous: ${self.previous_nav:.2f}, Current: ${total_nav:.2f}")
        
        self.previous_nav = total_nav
        
        print(f"\n📊 PM Liquidation Value: ${total_nav:.2f} (positions: ${total_liquidation_value:.2f}, cash: ${pm_cash:.2f})")
        
        return total_nav, {
            "positions_count": len(positions),
            "mid_value": total_mid_value,
            "liquidation_value": total_liquidation_value,
            "pm_cash": pm_cash,
            "total": total_nav,
        }


# =============================================================================
# NAV SIGNING
# =============================================================================

def create_nav_data_hash(nav: int, timestamp: int, deadline: int, round_id: int, vault_address: str) -> bytes:
    """Create the hash for NAV data signing matching Solidity's abi.encode."""
    NAV_TYPEHASH = Web3.keccak(text="NavData(uint256 nav,uint256 timestamp,uint256 deadline,uint256 roundId,address vault)")
    
    vault_addr_checksum = Web3.to_checksum_address(vault_address)
    
    encoded_data = eth_abi.encode(
        ['bytes32', 'uint256', 'uint256', 'uint256', 'uint256', 'address'],
        [NAV_TYPEHASH, nav, timestamp, deadline, round_id, vault_addr_checksum]
    )
    
    struct_hash = Web3.keccak(encoded_data)
    
    return struct_hash


def sign_nav_data(nav: int, timestamp: int, deadline: int, round_id: int, vault_address: str) -> Tuple[str, str]:
    """Sign NAV data with the oracle private key."""
    global oracle_account
    
    struct_hash = create_nav_data_hash(nav, timestamp, deadline, round_id, vault_address)
    message = encode_defunct(primitive=struct_hash)
    signed = oracle_account.sign_message(message)
    
    return signed.signature.hex(), oracle_account.address


def check_hourly_nav_kill_switch(current_nav: int) -> Tuple[bool, Optional[str]]:
    """
    Check if NAV has changed more than 10% from any value in the last hour.
    Returns (is_triggered, error_message).
    """
    global nav_history, hourly_nav_kill_switch
    
    if hourly_nav_kill_switch:
        return True, "Hourly NAV kill switch already triggered"
    
    now = int(time.time())
    one_hour_ago = now - 3600
    
    # Clean old entries and keep only last hour
    nav_history[:] = [(ts, nav) for ts, nav in nav_history if ts >= one_hour_ago]
    
    # Check against all historical values in the last hour
    for ts, historical_nav in nav_history:
        if historical_nav > 0:
            change_pct = abs(current_nav - historical_nav) / historical_nav * 100
            if change_pct >= HOURLY_NAV_CHANGE_LIMIT_PCT:
                hourly_nav_kill_switch = True
                msg = (f"NAV changed {change_pct:.1f}% in last hour!\n"
                       f"Historical ({(now - ts)//60}m ago): {historical_nav/NAV_PRECISION:.4f}\n"
                       f"Current: {current_nav/NAV_PRECISION:.4f}")
                print(f"🚨 HOURLY NAV KILL SWITCH TRIGGERED: {msg}")
                send_telegram_alert(f"HOURLY NAV KILL SWITCH\n\n{msg}")
                return True, msg
    
    # Add current NAV to history
    nav_history.append((now, current_nav))
    return False, None


def check_orderbook_liquidity(details: Dict) -> Tuple[bool, Optional[str]]:
    """
    Check if orderbooks have sufficient liquidity.
    Returns (has_liquidity, error_message if no liquidity).
    """
    global orderbook_kill_switch
    
    if orderbook_kill_switch:
        return False, "Orderbook kill switch already triggered"
    
    # Check if positions exist but no liquidation value (empty orderbooks)
    positions_count = details.get("positions_count", 0)
    liquidation_value = details.get("liquidation_value", 0)
    
    if positions_count > 0 and liquidation_value == 0:
        orderbook_kill_switch = True
        msg = f"EMPTY ORDERBOOKS: {positions_count} positions but $0 liquidation value!"
        print(f"🚨 ORDERBOOK KILL SWITCH TRIGGERED: {msg}")
        send_telegram_alert(f"ORDERBOOK KILL SWITCH\n\n{msg}")
        return False, msg
    
    return True, None


def get_signed_nav_data() -> Dict:
    """Get the current NAV with a fresh signature."""
    global cached_nav, nav_engine, polymarket_client, usdc, vault_v5, w3
    global hourly_nav_kill_switch, orderbook_kill_switch
    
    # Check if kill switches are already triggered
    if hourly_nav_kill_switch:
        raise Exception("KILL SWITCH: Hourly NAV change limit exceeded - signing disabled")
    
    if orderbook_kill_switch:
        raise Exception("KILL SWITCH: Empty orderbooks detected - signing disabled")
    
    now = int(time.time())
    
    try:
        total_supply = vault_v5.functions.totalSupply().call() if vault_v5 else 0
        vault_balance = usdc.functions.balanceOf(VAULT_V5_ADDRESS).call() if usdc and VAULT_V5_ADDRESS else 0
        last_round_id = vault_v5.functions.lastRoundId().call() if vault_v5 else 0
    except Exception as e:
        print(f"❌ Error reading vault state: {e}")
        total_supply = 0
        vault_balance = 0
        last_round_id = 0
    
    pm_liquidation_value = 0
    pm_cash = 0
    pm_details = {}
    
    if nav_engine:
        try:
            total_pm_value, pm_details = nav_engine.calculate_liquidation_nav()
            pm_liquidation_value = int(total_pm_value * 1e6)
            pm_cash = int(pm_details.get("pm_cash", 0) * 1e6)
            
            # Check orderbook liquidity kill switch
            has_liquidity, liquidity_error = check_orderbook_liquidity(pm_details)
            if not has_liquidity:
                raise Exception(f"KILL SWITCH: {liquidity_error}")
                
        except Exception as e:
            if "KILL SWITCH" in str(e):
                raise
            print(f"❌ Error calculating PM NAV: {e}")
    
    total_assets_6dec = vault_balance + pm_liquidation_value
    
    if total_supply > 0:
        raw_nav = (total_assets_6dec * NAV_PRECISION * 10**12) // total_supply
    else:
        raw_nav = NAV_PRECISION
    
    nav = int(raw_nav * NAV_HAIRCUT)
    
    # Check hourly NAV kill switch before signing
    is_hourly_triggered, hourly_error = check_hourly_nav_kill_switch(nav)
    if is_hourly_triggered:
        raise Exception(f"KILL SWITCH: {hourly_error}")
    
    print(f"📊 Final NAV: {nav / NAV_PRECISION:.6f} (raw: {raw_nav / NAV_PRECISION:.6f}, 0.5% haircut applied)")
    
    new_round_id = last_round_id + 1
    
    timestamp = now
    deadline = now + NAV_VALIDITY_SECONDS
    
    signature, signer = sign_nav_data(nav, timestamp, deadline, new_round_id, VAULT_V5_ADDRESS)
    
    with nav_lock:
        cached_nav = {
            "nav": nav,
            "timestamp": timestamp,
            "round_id": new_round_id,
            "details": {
                "vault_buffer": vault_balance,
                "pm_liquidation_value": pm_liquidation_value,
                "pm_cash": pm_cash,
                "total_supply": total_supply,
                "total_assets_6dec": total_assets_6dec,
            }
        }
    
    return {
        "navData": {
            "nav": str(nav),
            "timestamp": timestamp,
            "deadline": deadline,
            "roundId": new_round_id,
        },
        "signature": "0x" + signature if not signature.startswith("0x") else signature,
        "signer": signer,
        "details": cached_nav["details"],
    }


# =============================================================================
# WITHDRAWAL WATCHDOG
# =============================================================================

class WithdrawalWatchdog:
    """
    Monitors pending withdrawals and refills buffer as needed.
    
    Priority order:
    1. Use existing on-chain buffer
    2. Pull Polymarket cash into vault
    3. Liquidate Polymarket positions if still insufficient
    """
    
    def __init__(self):
        self.last_check = 0
        self.refill_in_progress = False
    
    def check_pending_withdrawals(self) -> Dict:
        """Check current withdrawal queue status."""
        global vault_v5, usdc, nav_engine
        
        if not vault_v5:
            return {"error": "Vault not connected"}
        
        try:
            pending_shares = vault_v5.functions.getPendingWithdrawalShares().call()
            buffer_balance = usdc.functions.balanceOf(VAULT_V5_ADDRESS).call()
            target_buffer = vault_v5.functions.targetBuffer().call()
            
            state = vault_v5.functions.getVaultState().call()
            last_nav = state[0]
            pending_count = state[7]
            deposits_throttled = state[9]
            
            pending_usdc = (pending_shares * last_nav) // NAV_PRECISION if last_nav > 0 else 0
            
            shortfall = pending_usdc - buffer_balance if pending_usdc > buffer_balance else 0
            
            return {
                "pending_withdrawals_count": pending_count,
                "pending_shares": pending_shares / 1e6,
                "pending_usdc_estimate": pending_usdc / 1e6,
                "buffer_balance": buffer_balance / 1e6,
                "target_buffer": target_buffer / 1e6,
                "shortfall": shortfall / 1e6,
                "deposits_throttled": deposits_throttled,
                "needs_refill": shortfall > 0,
            }
        except Exception as e:
            print(f"❌ Error checking withdrawals: {e}")
            return {"error": str(e)}
    
    def calculate_refill_needed(self) -> Tuple[float, Dict]:
        """Calculate how much USDC needs to be added to buffer."""
        status = self.check_pending_withdrawals()
        
        if "error" in status:
            return 0, status
        
        shortfall = status["shortfall"]
        
        if shortfall <= 0:
            return 0, {"message": "No refill needed", **status}
        
        pm_cash = 0
        pm_liquidatable = 0
        
        if polymarket_client:
            pm_cash = polymarket_client.fetch_cash_balance()
            
            if nav_engine:
                _, details = nav_engine.calculate_liquidation_nav()
                pm_liquidatable = details.get("liquidation_value", 0)
        
        refill_plan = {
            "shortfall": shortfall,
            "pm_cash_available": pm_cash,
            "pm_liquidatable": pm_liquidatable,
            "total_available": pm_cash + pm_liquidatable,
            "can_cover": (pm_cash + pm_liquidatable) >= shortfall,
        }
        
        if pm_cash >= shortfall:
            refill_plan["action"] = "PULL_PM_CASH"
            refill_plan["amount"] = shortfall
        elif pm_cash + pm_liquidatable >= shortfall:
            refill_plan["action"] = "PULL_CASH_THEN_LIQUIDATE"
            refill_plan["cash_to_pull"] = pm_cash
            refill_plan["liquidate_amount"] = shortfall - pm_cash
        else:
            refill_plan["action"] = "INSUFFICIENT_FUNDS"
            refill_plan["warning"] = "Cannot fully cover pending withdrawals!"
        
        return shortfall, refill_plan
    
    def get_watchdog_status(self) -> Dict:
        """Get full watchdog status for API."""
        global circuit_breaker_triggered, hourly_liquidation_tracker
        
        withdrawal_status = self.check_pending_withdrawals()
        shortfall, refill_plan = self.calculate_refill_needed()
        
        current_hour = int(time.time()) // 3600
        hourly_used = hourly_liquidation_tracker["amount"] if hourly_liquidation_tracker["hour"] == current_hour else 0
        
        return {
            "withdrawals": withdrawal_status,
            "refill_plan": refill_plan,
            "last_check": self.last_check,
            "refill_in_progress": self.refill_in_progress,
            "circuit_breaker": circuit_breaker_triggered,
            "hourly_liquidation": {
                "used": hourly_used,
                "limit": MAX_HOURLY_LIQUIDATION_USDC,
                "remaining": max(0, MAX_HOURLY_LIQUIDATION_USDC - hourly_used),
            },
        }
    
    def check_circuit_breaker(self) -> bool:
        """Check if circuit breaker should be triggered based on NAV drop."""
        global circuit_breaker_triggered, baseline_nav, cached_nav
        
        if circuit_breaker_triggered:
            return True
        
        with nav_lock:
            current_nav = cached_nav.get("nav", NAV_PRECISION)
        
        if baseline_nav is None:
            baseline_nav = current_nav
            return False
        
        if baseline_nav > 0:
            drop_pct = (baseline_nav - current_nav) / baseline_nav * 100
            if drop_pct >= CIRCUIT_BREAKER_NAV_DROP_PCT:
                circuit_breaker_triggered = True
                msg = f"NAV dropped {drop_pct:.1f}% from baseline!\nBaseline: {baseline_nav/NAV_PRECISION:.4f}\nCurrent: {current_nav/NAV_PRECISION:.4f}"
                print(f"🚨 CIRCUIT BREAKER TRIGGERED: {msg}")
                send_telegram_alert(msg)
                return True
        
        return False
    
    def can_liquidate(self, amount_usdc: float) -> Tuple[bool, str]:
        """Check if liquidation is allowed within hourly limits."""
        global hourly_liquidation_tracker
        
        current_hour = int(time.time()) // 3600
        
        if hourly_liquidation_tracker["hour"] != current_hour:
            hourly_liquidation_tracker = {"hour": current_hour, "amount": 0.0}
        
        remaining = MAX_HOURLY_LIQUIDATION_USDC - hourly_liquidation_tracker["amount"]
        
        if amount_usdc > remaining:
            return False, f"Hourly limit: ${remaining:.2f} remaining, need ${amount_usdc:.2f}"
        
        return True, "OK"
    
    def record_liquidation(self, amount_usdc: float):
        """Record a liquidation against the hourly limit."""
        global hourly_liquidation_tracker
        
        current_hour = int(time.time()) // 3600
        
        if hourly_liquidation_tracker["hour"] != current_hour:
            hourly_liquidation_tracker = {"hour": current_hour, "amount": 0.0}
        
        hourly_liquidation_tracker["amount"] += amount_usdc


watchdog = WithdrawalWatchdog()


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    global circuit_breaker_triggered, hourly_liquidation_tracker
    global hourly_nav_kill_switch, orderbook_kill_switch
    
    current_hour = int(time.time()) // 3600
    hourly_used = hourly_liquidation_tracker["amount"] if hourly_liquidation_tracker["hour"] == current_hour else 0
    
    # Determine overall status
    any_kill_switch = circuit_breaker_triggered or hourly_nav_kill_switch or orderbook_kill_switch
    if circuit_breaker_triggered:
        status = "CIRCUIT_BREAKER"
    elif hourly_nav_kill_switch:
        status = "HOURLY_NAV_KILL_SWITCH"
    elif orderbook_kill_switch:
        status = "ORDERBOOK_KILL_SWITCH"
    else:
        status = "ok"
    
    return jsonify({
        "status": status,
        "version": "v5.1",
        "vault": VAULT_V5_ADDRESS,
        "oracle": oracle_account.address if oracle_account else None,
        "polymarket_wallet": POLYMARKET_PROXY_ADDRESS,
        "kill_switches": {
            "circuit_breaker": circuit_breaker_triggered,
            "hourly_nav": hourly_nav_kill_switch,
            "orderbook": orderbook_kill_switch,
        },
        "hourly_liquidation_used": hourly_used,
        "hourly_liquidation_limit": MAX_HOURLY_LIQUIDATION_USDC,
        "nav_history_count": len(nav_history),
    })


@app.route("/nav", methods=["GET"])
def get_nav():
    """Get current NAV info (cached)."""
    with nav_lock:
        return jsonify({
            "nav": str(cached_nav["nav"]),
            "nav_formatted": cached_nav["nav"] / NAV_PRECISION,
            "timestamp": cached_nav["timestamp"],
            "round_id": cached_nav["round_id"],
            "details": cached_nav["details"],
        })


@app.route("/sign-nav", methods=["GET"])
def sign_nav():
    """Get fresh signed NAV for deposit/withdraw transactions."""
    try:
        signed_data = get_signed_nav_data()
        return jsonify(signed_data)
    except Exception as e:
        print(f"❌ Error signing NAV: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/withdrawals", methods=["GET"])
def get_withdrawals():
    """Get pending withdrawal queue status."""
    return jsonify(watchdog.get_watchdog_status())


@app.route("/refill-trigger", methods=["POST"])
def trigger_refill():
    """Manually trigger buffer refill check."""
    shortfall, plan = watchdog.calculate_refill_needed()
    return jsonify({
        "shortfall": shortfall,
        "plan": plan,
        "message": "Refill plan calculated. Manual action required to move funds from Polymarket.",
    })


# =============================================================================
# BACKGROUND TASKS
# =============================================================================

def nav_update_loop():
    """Background loop to update NAV periodically."""
    while True:
        try:
            get_signed_nav_data()
            print(f"✅ NAV updated at {time.strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"❌ NAV update error: {e}")
        
        time.sleep(30)


def watchdog_loop():
    """Background loop to monitor pending withdrawals and check safety limits."""
    global watchdog
    
    while True:
        try:
            if watchdog.check_circuit_breaker():
                print(f"🚨 Circuit breaker active - watchdog paused")
                time.sleep(WATCHDOG_INTERVAL_SECONDS)
                continue
            
            status = watchdog.check_pending_withdrawals()
            watchdog.last_check = int(time.time())
            
            if status.get("needs_refill"):
                shortfall = status['shortfall']
                print(f"⚠️  WATCHDOG: Refill needed! Shortfall: ${shortfall:.2f}")
                _, plan = watchdog.calculate_refill_needed()
                print(f"   Plan: {plan.get('action')}")
                
                can_liq, reason = watchdog.can_liquidate(shortfall)
                if not can_liq:
                    print(f"   ⛔ Liquidation blocked: {reason}")
                    send_telegram_alert(f"Buffer refill needed (${shortfall:.2f}) but blocked:\n{reason}")
                else:
                    send_telegram_alert(f"Buffer shortfall detected: ${shortfall:.2f}\nPlan: {plan.get('action')}\n\nManual action may be required.")
            
        except Exception as e:
            print(f"❌ Watchdog error: {e}")
        
        time.sleep(WATCHDOG_INTERVAL_SECONDS)


# =============================================================================
# INITIALIZATION
# =============================================================================

def initialize():
    """Initialize all connections and clients."""
    global w3, oracle_account, usdc, vault_v5, polymarket_client, nav_engine
    
    print(f"\n{'='*60}")
    print(f"🚀 PSNIPER V5.1 BOT STARTING (Autonomous)")
    print(f"{'='*60}")
    
    if not ORACLE_PRIVATE_KEY:
        print("❌ Missing ORACLE_PRIVATE_KEY")
        return False
    
    oracle_account = Account.from_key(ORACLE_PRIVATE_KEY)
    print(f"🔑 Oracle address: {oracle_account.address}")
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print(f"❌ Failed to connect to RPC: {RPC_URL}")
        return False
    print(f"🌐 Connected to RPC: {RPC_URL}")
    
    if USDC_ADDRESS:
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
        print(f"💵 USDC contract: {USDC_ADDRESS}")
    
    if VAULT_V5_ADDRESS:
        vault_v5 = w3.eth.contract(address=Web3.to_checksum_address(VAULT_V5_ADDRESS), abi=VAULT_V5_ABI)
        print(f"🏦 Vault V5 contract: {VAULT_V5_ADDRESS}")
    else:
        print("⚠️  No VAULT_V5_ADDRESS set - running in limited mode")
    
    if POLYMARKET_PROXY_ADDRESS:
        polymarket_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS)
        nav_engine = NavEngine(polymarket_client)
        print(f"📈 Polymarket wallet: {POLYMARKET_PROXY_ADDRESS}")
    else:
        print("⚠️  No POLYMARKET_PROXY_ADDRESS set - NAV calculation disabled")
    
    nav_thread = threading.Thread(target=nav_update_loop, daemon=True)
    nav_thread.start()
    
    wd_thread = threading.Thread(target=watchdog_loop, daemon=True)
    wd_thread.start()
    
    print(f"\n✅ V5.1 Bot initialized successfully!")
    print(f"   - NAV updates every 30 seconds (with 0.5% haircut)")
    print(f"   - Watchdog monitors every {WATCHDOG_INTERVAL_SECONDS} seconds")
    print(f"   - Circuit breaker at {CIRCUIT_BREAKER_NAV_DROP_PCT}% NAV drop")
    print(f"   - Max liquidation: ${MAX_HOURLY_LIQUIDATION_USDC}/hour")
    
    return True


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    if initialize():
        print(f"\n🌐 Starting Flask server on port 5001...")
        app.run(host="0.0.0.0", port=5001, debug=False)
    else:
        print("❌ Initialization failed")
