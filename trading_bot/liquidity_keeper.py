#!/usr/bin/env python3
"""
Liquidity Keeper - Autonomous Vault Buffer Refill System

This script runs alongside the trading bot to ensure the vault always has
sufficient buffer to process withdrawals.

Architecture:
- bot_v5 = Read-only NAV signer (no PM credentials, no fund movements)
- This script = Liquidity Keeper (has PM credentials, handles fund movements)

Loop Logic:
1. Read vault state (pendingShares, buffer, NAV) from on-chain
2. Calculate shortfall = (pendingShares * NAV / 1e18) - buffer
3. If shortfall > 0:
   - Use PM cash first (withdraw from Polymarket)
   - If still short, sell positions
   - Bridge USDC to Base → send to vault
4. Check stop conditions (kill switches, API errors, hourly limits)
5. Sleep N seconds (30-60 seconds)

Stop Conditions:
- NAV kill switches triggered (check bot_v5 /health)
- PM API unreachable
- Hourly liquidation limit hit
- Circuit breaker active
"""

import os
import sys
import time
import json
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
import requests

try:
    from curl_cffi import requests as curl_requests
    BYPASS_METHOD = "curl_cffi"
    print("🔓 Using curl_cffi for Cloudflare bypass")
except ImportError:
    import requests as curl_requests
    BYPASS_METHOD = "standard"
    print("⚠️  curl_cffi not available, using standard requests")

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

VAULT_V5_ADDRESS = os.getenv("VAULT_V5_ADDRESS", "")
BASE_RPC_URL = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")

BOT_V5_URL = os.getenv("BOT_V5_URL", "http://localhost:5001")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PROXY_URL = os.getenv("PROXY_URL", "")

NAV_PRECISION = 10**18
LOOP_INTERVAL_SECONDS = 60
MIN_SHORTFALL_USDC = 10.0
MAX_HOURLY_LIQUIDATION_USDC = 5000.0

# =============================================================================
# ABIs
# =============================================================================

VAULT_V5_ABI = [
    {"inputs": [], "name": "totalSupply", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastNav", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "lastRoundId", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalPendingShares", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "targetBuffer", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getPendingWithdrawalShares", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getVaultState", "outputs": [
        {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "uint256"}, {"type": "uint256"}, {"type": "bool"}, {"type": "bool"}, {"type": "uint256"}
    ], "stateMutability": "view", "type": "function"},
]

ERC20_ABI = [
    {"inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "transfer", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

# =============================================================================
# GLOBALS
# =============================================================================

w3_base: Optional[Web3] = None
vault_contract = None
usdc_base = None
keeper_account = None

hourly_tracker = {
    "hour": 0,
    "liquidated_usdc": 0.0,
}

# =============================================================================
# TELEGRAM ALERTS
# =============================================================================

def send_telegram_alert(message: str, is_error: bool = False):
    """Send alert to Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"⚠️  Telegram not configured: {message}")
        return False
    
    try:
        prefix = "🚨 LIQUIDITY KEEPER ALERT" if is_error else "📢 LIQUIDITY KEEPER"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"{prefix}\n\n{message}",
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False

# =============================================================================
# POLYMARKET CLIENT (for reading positions and cash)
# =============================================================================

class PolymarketClient:
    """Client for reading Polymarket positions and cash balance."""
    
    DATA_API_URL = "https://data-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    def __init__(self, wallet_address: str):
        self.wallet_address = wallet_address
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
    
    def fetch_cash_balance(self) -> float:
        """Fetch USDC cash balance on Polymarket (Polygon)."""
        try:
            url = f"{self.DATA_API_URL}/balance"
            params = {"user": self.wallet_address}
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            cash = float(data.get("balance", 0)) if data else 0
            return cash
            
        except Exception as e:
            print(f"❌ Error fetching PM cash: {e}")
            return 0.0
    
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
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                
                positions.append({
                    "token_id": p.get("asset") or "",
                    "title": p.get("title") or "",
                    "outcome": p.get("outcome") or "",
                    "size": size,
                    "current_value": float(p.get("currentValue", 0)),
                })
            
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
            
            for bid in data.get("bids", []):
                price = float(bid.get("price", 0))
                size = float(bid.get("size", 0))
                if size * price >= 5.0:  # Min $5 bid size filter
                    bids.append({"price": price, "size": size})
            
            bids.sort(key=lambda x: x["price"], reverse=True)
            return {"bids": bids}
            
        except Exception as e:
            print(f"❌ Error fetching orderbook: {e}")
            return {"bids": []}

# =============================================================================
# VAULT STATE READING
# =============================================================================

@dataclass
class VaultState:
    """Current state of the vault."""
    pending_shares: int  # Shares waiting to be redeemed (6 decimals)
    buffer_balance: int  # USDC in vault buffer (6 decimals)
    last_nav: int  # Last NAV (18 decimals)
    total_supply: int  # Total vault shares (6 decimals)
    shortfall: int  # USDC shortfall (6 decimals)
    
    @property
    def pending_shares_usdc(self) -> float:
        """Pending shares converted to USDC value."""
        if self.last_nav > 0:
            return (self.pending_shares * self.last_nav) / NAV_PRECISION / 1e6
        return 0.0
    
    @property
    def buffer_usdc(self) -> float:
        """Buffer balance in USDC."""
        return self.buffer_balance / 1e6
    
    @property
    def shortfall_usdc(self) -> float:
        """Shortfall in USDC."""
        return self.shortfall / 1e6


def read_vault_state() -> Optional[VaultState]:
    """Read current vault state from Base chain."""
    global vault_contract, usdc_base
    
    if not vault_contract or not usdc_base:
        print("❌ Vault contract not initialized")
        return None
    
    try:
        pending_shares = vault_contract.functions.getPendingWithdrawalShares().call()
        buffer_balance = usdc_base.functions.balanceOf(VAULT_V5_ADDRESS).call()
        
        state = vault_contract.functions.getVaultState().call()
        last_nav = state[0]
        total_supply = vault_contract.functions.totalSupply().call()
        
        pending_usdc_value = (pending_shares * last_nav) // NAV_PRECISION if last_nav > 0 else 0
        shortfall = max(0, pending_usdc_value - buffer_balance)
        
        return VaultState(
            pending_shares=pending_shares,
            buffer_balance=buffer_balance,
            last_nav=last_nav,
            total_supply=total_supply,
            shortfall=shortfall
        )
        
    except Exception as e:
        print(f"❌ Error reading vault state: {e}")
        return None

# =============================================================================
# KILL SWITCH CHECK
# =============================================================================

def check_kill_switches() -> Tuple[bool, str]:
    """
    Check bot_v5 /health endpoint for kill switches.
    Returns (is_safe, reason).
    """
    try:
        response = requests.get(f"{BOT_V5_URL}/health", timeout=10)
        if response.status_code != 200:
            return False, f"Bot V5 health check failed (status {response.status_code})"
        
        data = response.json()
        status = data.get("status", "unknown")
        
        if status != "ok":
            return False, f"Kill switch active: {status}"
        
        kill_switches = data.get("kill_switches", {})
        if kill_switches.get("circuit_breaker"):
            return False, "Circuit breaker triggered"
        if kill_switches.get("hourly_nav"):
            return False, "Hourly NAV kill switch triggered"
        if kill_switches.get("orderbook"):
            return False, "Orderbook kill switch triggered"
        
        return True, "All systems operational"
        
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to bot_v5 at {BOT_V5_URL}"
    except Exception as e:
        return False, f"Error checking kill switches: {e}"

# =============================================================================
# HOURLY LIQUIDATION LIMIT
# =============================================================================

def check_hourly_limit(amount_usdc: float) -> Tuple[bool, float]:
    """
    Check if we can liquidate this amount within hourly limit.
    Returns (allowed, max_allowed_amount).
    """
    global hourly_tracker
    
    current_hour = int(time.time()) // 3600
    
    if hourly_tracker["hour"] != current_hour:
        hourly_tracker = {
            "hour": current_hour,
            "liquidated_usdc": 0.0,
        }
    
    remaining = MAX_HOURLY_LIQUIDATION_USDC - hourly_tracker["liquidated_usdc"]
    
    if remaining <= 0:
        return False, 0.0
    
    allowed_amount = min(amount_usdc, remaining)
    return True, allowed_amount


def record_liquidation(amount_usdc: float):
    """Record a liquidation against the hourly limit."""
    global hourly_tracker
    hourly_tracker["liquidated_usdc"] += amount_usdc
    print(f"📊 Hourly liquidation: ${hourly_tracker['liquidated_usdc']:.2f} / ${MAX_HOURLY_LIQUIDATION_USDC:.2f}")

# =============================================================================
# POLYMARKET BRIDGE (Polygon → Base)
# =============================================================================

# NOTE: The Polymarket USDC bridge requires:
# 1. Withdraw USDC from Polymarket to Polygon wallet
# 2. Bridge from Polygon to Base (via LayerZero, Across, or official bridge)
# This is a complex multi-step process that may take 15-30 minutes.
#
# For now, we'll implement a simplified version that assumes USDC is already
# available on Base and just needs to be transferred to the vault.

def estimate_bridge_time() -> int:
    """Estimate bridge time in seconds (Polygon → Base)."""
    return 1800  # 30 minutes conservative estimate


def bridge_usdc_to_base(amount_usdc: float) -> bool:
    """
    Bridge USDC from Polymarket (Polygon) to Base.
    
    TODO: Implement actual bridge logic using:
    - LayerZero (fastest, ~15 min)
    - Across Protocol (fast, ~10 min)
    - Official Base bridge (slow, hours)
    
    For now, this is a placeholder that logs the intent.
    """
    print(f"🌉 BRIDGE REQUEST: ${amount_usdc:.2f} USDC from Polygon → Base")
    print(f"   Estimated time: ~{estimate_bridge_time() // 60} minutes")
    
    # TODO: Implement actual bridge transaction
    # This would involve:
    # 1. Connect to Polygon RPC
    # 2. Approve bridge contract
    # 3. Call bridge function
    # 4. Wait for confirmation on Base
    
    send_telegram_alert(
        f"🌉 Bridge requested: ${amount_usdc:.2f} USDC\n"
        f"From: Polygon (Polymarket)\n"
        f"To: Base (Vault)\n"
        f"ETA: ~{estimate_bridge_time() // 60} minutes"
    )
    
    return False  # Not implemented yet


def send_usdc_to_vault(amount_usdc: float) -> bool:
    """
    Send USDC from keeper wallet to vault on Base.
    
    Assumes USDC is already in the keeper wallet on Base.
    """
    global w3_base, keeper_account, usdc_base
    
    if not keeper_account:
        print("❌ Keeper account not initialized")
        return False
    
    amount_6dec = int(amount_usdc * 1e6)
    
    try:
        keeper_address = keeper_account.address
        balance = usdc_base.functions.balanceOf(keeper_address).call()
        
        if balance < amount_6dec:
            print(f"❌ Insufficient USDC on Base. Have: ${balance/1e6:.2f}, Need: ${amount_usdc:.2f}")
            return False
        
        nonce = w3_base.eth.get_transaction_count(keeper_address)
        gas_price = w3_base.eth.gas_price
        
        tx = usdc_base.functions.transfer(
            Web3.to_checksum_address(VAULT_V5_ADDRESS),
            amount_6dec
        ).build_transaction({
            'from': keeper_address,
            'nonce': nonce,
            'gas': 100000,
            'gasPrice': gas_price,
            'chainId': 8453,  # Base mainnet
        })
        
        signed_tx = keeper_account.sign_transaction(tx)
        tx_hash = w3_base.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"📤 Sent ${amount_usdc:.2f} USDC to vault")
        print(f"   TX: {tx_hash.hex()}")
        
        receipt = w3_base.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print(f"✅ Transfer confirmed!")
            send_telegram_alert(f"✅ Sent ${amount_usdc:.2f} USDC to vault\nTX: {tx_hash.hex()[:16]}...")
            return True
        else:
            print(f"❌ Transfer failed!")
            return False
            
    except Exception as e:
        print(f"❌ Error sending USDC to vault: {e}")
        send_telegram_alert(f"❌ Failed to send USDC to vault: {e}", is_error=True)
        return False

# =============================================================================
# POSITION LIQUIDATION
# =============================================================================

def liquidate_positions_for_usdc(needed_usdc: float, pm_client: PolymarketClient) -> float:
    """
    Liquidate Polymarket positions to get needed USDC.
    
    TODO: Implement actual position selling using py-clob-client.
    
    Returns: Amount of USDC obtained (0 if not implemented yet)
    """
    print(f"🔥 LIQUIDATION REQUEST: Need ${needed_usdc:.2f} USDC")
    
    positions = pm_client.fetch_positions()
    if not positions:
        print("   No positions to liquidate")
        return 0.0
    
    total_liquidatable = 0.0
    for pos in positions:
        orderbook = pm_client.fetch_orderbook(pos["token_id"])
        bids = orderbook.get("bids", [])
        
        if not bids:
            continue
        
        remaining = pos["size"]
        liq_value = 0.0
        for bid in bids:
            fill = min(remaining, bid["size"])
            liq_value += fill * bid["price"]
            remaining -= fill
            if remaining <= 0:
                break
        
        total_liquidatable += liq_value
        print(f"   • {pos['outcome']}: ${liq_value:.2f} liquidatable")
    
    print(f"   Total liquidatable: ${total_liquidatable:.2f}")
    
    # TODO: Implement actual selling logic
    # This requires using py-clob-client to place market sell orders
    
    send_telegram_alert(
        f"🔥 Liquidation needed: ${needed_usdc:.2f} USDC\n"
        f"Available to liquidate: ${total_liquidatable:.2f}\n"
        f"⚠️ Manual intervention may be required"
    )
    
    return 0.0  # Not implemented yet

# =============================================================================
# MAIN KEEPER LOOP
# =============================================================================

def keeper_iteration(pm_client: PolymarketClient) -> bool:
    """
    Single iteration of the keeper loop.
    Returns True if successful, False if should pause/stop.
    """
    print(f"\n{'='*60}")
    print(f"🔄 LIQUIDITY KEEPER ITERATION - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    is_safe, reason = check_kill_switches()
    if not is_safe:
        print(f"🚫 STOPPED: {reason}")
        send_telegram_alert(f"🚫 Keeper paused: {reason}", is_error=True)
        return False
    
    vault_state = read_vault_state()
    if not vault_state:
        print("❌ Cannot read vault state")
        return False
    
    print(f"\n📊 VAULT STATE:")
    print(f"   Pending withdrawals: ${vault_state.pending_shares_usdc:.2f}")
    print(f"   Buffer balance: ${vault_state.buffer_usdc:.2f}")
    print(f"   Shortfall: ${vault_state.shortfall_usdc:.2f}")
    print(f"   NAV: {vault_state.last_nav / NAV_PRECISION:.6f}")
    
    if vault_state.shortfall_usdc < MIN_SHORTFALL_USDC:
        print(f"✅ No action needed (shortfall < ${MIN_SHORTFALL_USDC:.2f})")
        return True
    
    allowed, max_amount = check_hourly_limit(vault_state.shortfall_usdc)
    if not allowed:
        print("⏳ Hourly liquidation limit reached, waiting...")
        return True
    
    needed_usdc = min(vault_state.shortfall_usdc, max_amount)
    print(f"\n💰 NEED TO REFILL: ${needed_usdc:.2f} USDC")
    
    pm_cash = pm_client.fetch_cash_balance()
    print(f"   Polymarket cash available: ${pm_cash:.2f}")
    
    if pm_cash >= needed_usdc:
        print(f"   ✅ Sufficient PM cash - initiating bridge")
        if bridge_usdc_to_base(needed_usdc):
            record_liquidation(needed_usdc)
            if send_usdc_to_vault(needed_usdc):
                print(f"✅ Refill complete!")
                return True
    else:
        cash_to_use = pm_cash
        still_needed = needed_usdc - pm_cash
        
        print(f"   Using ${cash_to_use:.2f} from PM cash")
        print(f"   Need to liquidate ${still_needed:.2f} more")
        
        liquidated = liquidate_positions_for_usdc(still_needed, pm_client)
        total_available = cash_to_use + liquidated
        
        if total_available > 0:
            if bridge_usdc_to_base(total_available):
                record_liquidation(total_available)
                if send_usdc_to_vault(total_available):
                    print(f"✅ Partial refill complete: ${total_available:.2f}")
                    return True
    
    print("⚠️  Could not complete refill - will retry")
    return True


def run_keeper():
    """Main keeper loop."""
    global w3_base, vault_contract, usdc_base, keeper_account
    
    print(f"\n{'='*60}")
    print(f"🚀 STARTING LIQUIDITY KEEPER")
    print(f"{'='*60}")
    
    if not VAULT_V5_ADDRESS:
        print("❌ VAULT_V5_ADDRESS not set")
        sys.exit(1)
    
    if not POLYMARKET_PROXY_ADDRESS:
        print("❌ POLYMARKET_PROXY_ADDRESS not set")
        sys.exit(1)
    
    print(f"\nConfiguration:")
    print(f"   Vault: {VAULT_V5_ADDRESS}")
    print(f"   PM Wallet: {POLYMARKET_PROXY_ADDRESS}")
    print(f"   Base RPC: {BASE_RPC_URL}")
    print(f"   Bot V5 URL: {BOT_V5_URL}")
    print(f"   Loop interval: {LOOP_INTERVAL_SECONDS}s")
    print(f"   Min shortfall: ${MIN_SHORTFALL_USDC:.2f}")
    print(f"   Hourly limit: ${MAX_HOURLY_LIQUIDATION_USDC:.2f}")
    
    print(f"\n🔌 Connecting to Base...")
    w3_base = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3_base.is_connected():
        print("❌ Cannot connect to Base RPC")
        sys.exit(1)
    print(f"   ✅ Connected to Base (block {w3_base.eth.block_number})")
    
    vault_contract = w3_base.eth.contract(
        address=Web3.to_checksum_address(VAULT_V5_ADDRESS),
        abi=VAULT_V5_ABI
    )
    
    usdc_base = w3_base.eth.contract(
        address=Web3.to_checksum_address(USDC_BASE_ADDRESS),
        abi=ERC20_ABI
    )
    
    if POLYMARKET_PRIVATE_KEY:
        keeper_account = Account.from_key(POLYMARKET_PRIVATE_KEY)
        print(f"   Keeper wallet: {keeper_account.address}")
    else:
        print("⚠️  POLYMARKET_PRIVATE_KEY not set - read-only mode")
    
    pm_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS)
    
    is_safe, reason = check_kill_switches()
    print(f"\n🔍 Initial kill switch check: {reason}")
    
    send_telegram_alert(
        f"🚀 Liquidity Keeper started\n"
        f"Vault: {VAULT_V5_ADDRESS[:10]}...{VAULT_V5_ADDRESS[-8:]}\n"
        f"Mode: {'Active' if keeper_account else 'Read-only'}"
    )
    
    print(f"\n🔄 Starting keeper loop...")
    
    consecutive_failures = 0
    max_failures = 5
    
    while True:
        try:
            success = keeper_iteration(pm_client)
            
            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    print(f"❌ {max_failures} consecutive failures, pausing for 5 minutes")
                    send_telegram_alert(
                        f"❌ Keeper paused after {max_failures} failures\n"
                        f"Will resume in 5 minutes",
                        is_error=True
                    )
                    time.sleep(300)
                    consecutive_failures = 0
            
        except KeyboardInterrupt:
            print("\n\n👋 Keeper stopped by user")
            send_telegram_alert("👋 Keeper stopped by user")
            break
            
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            import traceback
            traceback.print_exc()
            consecutive_failures += 1
        
        print(f"\n💤 Sleeping {LOOP_INTERVAL_SECONDS}s...")
        time.sleep(LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_keeper()
