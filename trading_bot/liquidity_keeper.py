#!/usr/bin/env python3
"""
Liquidity Keeper - Treasury-Backed Vault Buffer Refill System

Architecture:
- Base Treasury Wallet: Pre-funded USDC for instant vault refills
- Keeper Loop: Detects shortfall → sends from treasury → alerts if treasury low
- PM Liquidation: Sells positions → withdraws to Polygon wallet (manual bridge later)

Flow:
1. Read vault state (pendingShares, buffer, NAV) from Base chain
2. Calculate shortfall = (pendingShares * NAV / 1e18) - buffer
3. If shortfall > 0:
   - Check treasury balance on Base
   - If treasury has funds → send to vault instantly
   - If treasury low → alert via Telegram + liquidate PM positions on Polygon
4. PM liquidation stays on Polygon (manual bridge to Base treasury later)
5. Check stop conditions (kill switches, hourly limits)
6. Sleep and repeat

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
import subprocess
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
import requests

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    HAS_CLOB_CLIENT = True
except ImportError:
    HAS_CLOB_CLIENT = False
    print("⚠️  py-clob-client not available, liquidation disabled")

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
    print("⚠️  relay_bridge not available, auto-bridging disabled")

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

VAULT_V5_ADDRESS = os.getenv("VAULT_V5_ADDRESS", "")
BASE_RPC_URL = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")

TREASURY_ADDRESS = os.getenv("TREASURY_ADDRESS", "")

BOT_V5_URL = os.getenv("BOT_V5_URL", "http://localhost:5001")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PROXY_URL = os.getenv("PROXY_URL", "")

POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
USDC_POLYGON_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

NAV_PRECISION = 10**18
LOOP_INTERVAL_SECONDS = 60
MIN_SHORTFALL_USDC = 10.0
MAX_HOURLY_LIQUIDATION_USDC = 5000.0
TREASURY_LOW_THRESHOLD_USDC = 500.0
TREASURY_CRITICAL_THRESHOLD_USDC = 100.0

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

w3_base: Web3 = None
w3_polygon: Web3 = None
vault_contract = None
usdc_base = None
usdc_polygon = None
treasury_account = None
pm_clob_client = None

hourly_tracker = {
    "hour": 0,
    "liquidated_usdc": 0.0,
}

last_treasury_alert_time = 0

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
# POLYMARKET CLIENT (for reading positions and orderbooks)
# =============================================================================

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
    
    def fetch_cash_balance(self) -> float:
        """Fetch USDC cash balance on Polymarket (Polygon)."""
        try:
            url = f"{self.DATA_API_URL}/balance"
            params = {"user": self.wallet_address}
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            return float(data.get("balance", 0)) if data else 0
            
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
                    "condition_id": p.get("conditionId") or "",
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
                if size * price >= 5.0:
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
    pending_shares: int
    buffer_balance: int
    last_nav: int
    total_supply: int
    shortfall: int
    
    @property
    def pending_shares_usdc(self) -> float:
        if self.last_nav > 0:
            return (self.pending_shares * self.last_nav) / NAV_PRECISION / 1e6
        return 0.0
    
    @property
    def buffer_usdc(self) -> float:
        return self.buffer_balance / 1e6
    
    @property
    def shortfall_usdc(self) -> float:
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
# TREASURY MANAGEMENT (Base chain)
# =============================================================================

def get_treasury_balance() -> float:
    """Get treasury USDC balance on Base."""
    global usdc_base
    
    if not usdc_base or not TREASURY_ADDRESS:
        return 0.0
    
    try:
        balance = usdc_base.functions.balanceOf(
            Web3.to_checksum_address(TREASURY_ADDRESS)
        ).call()
        return balance / 1e6
    except Exception as e:
        print(f"❌ Error reading treasury balance: {e}")
        return 0.0


def send_from_treasury_to_vault(amount_usdc: float) -> bool:
    """Send USDC from treasury wallet to vault on Base."""
    global w3_base, treasury_account, usdc_base
    
    if not treasury_account:
        print("❌ Treasury account not initialized")
        return False
    
    amount_6dec = int(amount_usdc * 1e6)
    
    try:
        treasury_addr = treasury_account.address
        balance = usdc_base.functions.balanceOf(treasury_addr).call()
        
        if balance < amount_6dec:
            print(f"❌ Insufficient treasury USDC. Have: ${balance/1e6:.2f}, Need: ${amount_usdc:.2f}")
            return False
        
        nonce = w3_base.eth.get_transaction_count(treasury_addr)
        gas_price = w3_base.eth.gas_price
        
        tx = usdc_base.functions.transfer(
            Web3.to_checksum_address(VAULT_V5_ADDRESS),
            amount_6dec
        ).build_transaction({
            'from': treasury_addr,
            'nonce': nonce,
            'gas': 100000,
            'gasPrice': gas_price,
            'chainId': 8453,
        })
        
        signed_tx = treasury_account.sign_transaction(tx)
        tx_hash = w3_base.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"📤 Sending ${amount_usdc:.2f} USDC from treasury to vault...")
        print(f"   TX: {tx_hash.hex()}")
        
        receipt = w3_base.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print(f"✅ Transfer confirmed!")
            send_telegram_alert(
                f"✅ Vault refilled from treasury\n"
                f"Amount: ${amount_usdc:.2f} USDC\n"
                f"TX: {tx_hash.hex()[:20]}..."
            )
            return True
        else:
            print(f"❌ Transfer failed!")
            return False
            
    except Exception as e:
        print(f"❌ Error sending from treasury: {e}")
        send_telegram_alert(f"❌ Treasury transfer failed: {e}", is_error=True)
        return False


def check_treasury_thresholds(treasury_balance: float):
    """Alert if treasury is running low."""
    global last_treasury_alert_time
    
    now = time.time()
    if now - last_treasury_alert_time < 3600:
        return
    
    if treasury_balance < TREASURY_CRITICAL_THRESHOLD_USDC:
        send_telegram_alert(
            f"🚨 TREASURY CRITICAL\n"
            f"Balance: ${treasury_balance:.2f} USDC\n"
            f"Threshold: ${TREASURY_CRITICAL_THRESHOLD_USDC:.2f}\n\n"
            f"⚠️ Bridge funds from Polygon immediately!",
            is_error=True
        )
        last_treasury_alert_time = now
    elif treasury_balance < TREASURY_LOW_THRESHOLD_USDC:
        send_telegram_alert(
            f"⚠️ TREASURY LOW\n"
            f"Balance: ${treasury_balance:.2f} USDC\n"
            f"Threshold: ${TREASURY_LOW_THRESHOLD_USDC:.2f}\n\n"
            f"Consider bridging funds from Polygon."
        )
        last_treasury_alert_time = now

# =============================================================================
# KILL SWITCH CHECK
# =============================================================================

def check_kill_switches() -> Tuple[bool, str]:
    """Check bot_v5 /health endpoint for kill switches."""
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
        return True, f"Bot V5 unreachable (proceeding with caution)"
    except Exception as e:
        return False, f"Error checking kill switches: {e}"

# =============================================================================
# HOURLY LIQUIDATION LIMIT
# =============================================================================

def check_hourly_limit(amount_usdc: float) -> Tuple[bool, float]:
    """Check if we can liquidate this amount within hourly limit."""
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
    
    return True, min(amount_usdc, remaining)


def record_liquidation(amount_usdc: float):
    """Record a liquidation against the hourly limit."""
    global hourly_tracker
    hourly_tracker["liquidated_usdc"] += amount_usdc
    print(f"📊 Hourly liquidation: ${hourly_tracker['liquidated_usdc']:.2f} / ${MAX_HOURLY_LIQUIDATION_USDC:.2f}")

# =============================================================================
# POSITION LIQUIDATION (Polygon - stays on Polymarket)
# =============================================================================

def init_clob_client():
    """Initialize py-clob-client for position liquidation."""
    global pm_clob_client
    
    if not HAS_CLOB_CLIENT:
        return False
    
    if not POLYMARKET_PRIVATE_KEY:
        print("❌ POLYMARKET_PRIVATE_KEY not set")
        return False
    
    try:
        pm_clob_client = ClobClient(
            "https://clob.polymarket.com",
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=137,
            signature_type=1,
            funder=POLYMARKET_PROXY_ADDRESS
        )
        pm_clob_client.set_api_creds(pm_clob_client.create_or_derive_api_creds())
        print("✅ CLOB client initialized for liquidation")
        return True
    except Exception as e:
        print(f"❌ Failed to initialize CLOB client: {e}")
        return False


def liquidate_position(token_id: str, size: float, min_price: float = 0.01) -> Tuple[bool, float]:
    """
    Place a market sell order for a position.
    Returns (success, usdc_obtained).
    """
    global pm_clob_client
    
    if not pm_clob_client:
        print("❌ CLOB client not initialized")
        return False, 0.0
    
    if size < 1.0:
        print(f"   Skip: size {size:.2f} too small")
        return False, 0.0
    
    try:
        order_args = OrderArgs(
            price=min_price,
            size=size,
            side=SELL,
            token_id=token_id,
        )
        
        signed_order = pm_clob_client.create_order(order_args)
        response = pm_clob_client.post_order(signed_order, OrderType.FOK)
        
        if response.get("success"):
            order_id = response.get("orderID", "")
            print(f"   ✅ Market sell placed: {size:.2f} shares @ FOK")
            
            time.sleep(2)
            
            try:
                order_status = pm_clob_client.get_order(order_id)
                if order_status:
                    filled_size = float(order_status.get("size_matched", 0))
                    avg_price = float(order_status.get("avg_price", min_price))
                    usdc_obtained = filled_size * avg_price
                    return True, usdc_obtained
            except:
                pass
            
            return True, size * min_price
        else:
            error = response.get("error", "Unknown")
            print(f"   ❌ Sell failed: {error}")
            return False, 0.0
            
    except Exception as e:
        print(f"   ❌ Liquidation error: {e}")
        return False, 0.0


def liquidate_positions_for_usdc(needed_usdc: float, pm_client: PolymarketClient) -> float:
    """
    Liquidate Polymarket positions to get needed USDC.
    Returns amount of USDC obtained on Polygon.
    """
    print(f"\n🔥 LIQUIDATION: Need ${needed_usdc:.2f} USDC on Polygon")
    
    if not pm_clob_client:
        if not init_clob_client():
            send_telegram_alert(
                f"🔥 Liquidation needed: ${needed_usdc:.2f} USDC\n"
                f"⚠️ CLOB client not available - manual action required",
                is_error=True
            )
            return 0.0
    
    positions = pm_client.fetch_positions()
    if not positions:
        print("   No positions to liquidate")
        return 0.0
    
    positions_by_value = []
    for pos in positions:
        if not pos["token_id"]:
            continue
        
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
        
        if liq_value > 0:
            positions_by_value.append({
                **pos,
                "liq_value": liq_value,
                "best_bid": bids[0]["price"] if bids else 0,
            })
    
    positions_by_value.sort(key=lambda x: x["liq_value"], reverse=True)
    
    total_obtained = 0.0
    still_needed = needed_usdc
    
    for pos in positions_by_value:
        if still_needed <= 0:
            break
        
        size_to_sell = pos["size"]
        if pos["liq_value"] > still_needed:
            ratio = still_needed / pos["liq_value"]
            size_to_sell = pos["size"] * ratio * 1.1
        
        print(f"\n   Liquidating {pos['outcome']}: {size_to_sell:.2f} shares (~${pos['liq_value']:.2f})")
        
        success, usdc = liquidate_position(
            pos["token_id"],
            size_to_sell,
            pos["best_bid"] * 0.95
        )
        
        if success:
            total_obtained += usdc
            still_needed -= usdc
            record_liquidation(usdc)
    
    print(f"\n   Total liquidated: ${total_obtained:.2f} USDC (on Polygon)")
    
    return total_obtained


def bridge_polygon_to_base(amount_usdc: float) -> Tuple[bool, float]:
    """
    Bridge USDC from Polygon to Base treasury using Relay.link.
    
    Args:
        amount_usdc: Amount to bridge
    
    Returns:
        (success, amount_received_on_base)
    """
    if not HAS_RELAY_BRIDGE:
        print("❌ Relay bridge not available")
        send_telegram_alert(
            f"🌉 Bridge needed: ${amount_usdc:.2f} USDC\n"
            f"Auto-bridge unavailable - bridge manually via relay.link",
            is_error=True
        )
        return False, 0.0
    
    if not POLYMARKET_PRIVATE_KEY:
        print("❌ No private key for bridging")
        return False, 0.0
    
    sender = Account.from_key(POLYMARKET_PRIVATE_KEY).address
    recipient = TREASURY_ADDRESS or sender
    
    print(f"\n🌉 AUTO-BRIDGE: ${amount_usdc:.2f} USDC")
    print(f"   From: Polygon {sender[:10]}...")
    print(f"   To: Base {recipient[:10]}...")
    
    try:
        result = bridge_usdc_polygon_to_base(
            amount_usdc=amount_usdc,
            sender_address=sender,
            recipient_address=recipient,
            private_key=POLYMARKET_PRIVATE_KEY,
            polygon_rpc_url=POLYGON_RPC_URL
        )
        
        if result.success:
            print(f"✅ Bridge complete! Received ${result.amount_received:.2f} on Base")
            return True, result.amount_received
        else:
            print(f"❌ Bridge failed: {result.error}")
            send_telegram_alert(
                f"❌ Bridge failed: {result.error}\n"
                f"Amount: ${amount_usdc:.2f} USDC\n"
                f"Bridge manually via relay.link",
                is_error=True
            )
            return False, 0.0
            
    except Exception as e:
        print(f"❌ Bridge error: {e}")
        send_telegram_alert(f"❌ Bridge error: {e}", is_error=True)
        return False, 0.0


# =============================================================================
# PROXY WITHDRAW (via TypeScript module)
# =============================================================================

def get_proxy_usdc_balance() -> float:
    """Get USDC.e balance in the PM proxy wallet via proxy_withdraw.ts."""
    try:
        result = subprocess.run(
            ["npx", "tsx", "proxy_withdraw.ts", "info"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        if result.returncode != 0:
            print(f"❌ proxy_withdraw info failed: {result.stderr}")
            return 0.0
        
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    proxy_info = json.loads(line)
                    if "usdcBalance" in proxy_info:
                        return float(proxy_info["usdcBalance"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
            elif line.startswith("{"):
                json_buffer = [line]
                for next_line in result.stdout.split("\n")[result.stdout.split("\n").index(line)+1:]:
                    json_buffer.append(next_line.strip())
                    combined = "".join(json_buffer)
                    try:
                        proxy_info = json.loads(combined)
                        if "usdcBalance" in proxy_info:
                            return float(proxy_info["usdcBalance"])
                        break
                    except json.JSONDecodeError:
                        if next_line.strip().endswith("}"):
                            break
                        continue
        
        print("❌ No valid proxy info JSON found in output")
        return 0.0
    except subprocess.TimeoutExpired:
        print("❌ proxy_withdraw info timed out")
        return 0.0
    except Exception as e:
        print(f"❌ Error getting proxy balance: {e}")
        return 0.0


def withdraw_from_proxy_to_treasury(amount_usdc: float, dry_run: bool = False) -> Tuple[bool, float]:
    """
    Withdraw USDC.e from PM Proxy to Base treasury via Relay bridge.
    Uses proxy_withdraw.ts TypeScript module.
    
    Args:
        amount_usdc: Amount to withdraw and bridge
        dry_run: If True, simulate without executing
    
    Returns:
        (success, amount_bridged)
    """
    if not TREASURY_ADDRESS:
        print("❌ TREASURY_ADDRESS not set - cannot withdraw from proxy")
        return False, 0.0
    
    print(f"\n🔧 PROXY WITHDRAW: ${amount_usdc:.2f} USDC via Relay")
    print(f"   From: PM Proxy {POLYMARKET_PROXY_ADDRESS[:10]}...")
    print(f"   To: Base Treasury {TREASURY_ADDRESS[:10]}...")
    
    cmd = ["npx", "tsx", "proxy_withdraw.ts", "withdraw", str(amount_usdc)]
    if dry_run:
        cmd.append("--dry-run")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        print(f"   stdout: {result.stdout[-500:] if len(result.stdout) > 500 else result.stdout}")
        
        if result.returncode == 0:
            print(f"✅ Proxy withdrawal complete!")
            
            if not dry_run:
                send_telegram_alert(
                    f"✅ Proxy withdrawal complete\n"
                    f"Amount: ${amount_usdc:.2f} USDC\n"
                    f"Flow: PM Proxy → Relay → Base Treasury"
                )
            
            return True, amount_usdc
        else:
            error_msg = result.stderr[-200:] if result.stderr else "Unknown error"
            print(f"❌ Proxy withdrawal failed: {error_msg}")
            
            if not dry_run:
                send_telegram_alert(
                    f"❌ Proxy withdrawal failed\n"
                    f"Amount: ${amount_usdc:.2f}\n"
                    f"Error: {error_msg[:100]}",
                    is_error=True
                )
            
            return False, 0.0
            
    except subprocess.TimeoutExpired:
        print("❌ Proxy withdrawal timed out (5 min)")
        send_telegram_alert("❌ Proxy withdrawal timed out", is_error=True)
        return False, 0.0
    except Exception as e:
        print(f"❌ Proxy withdrawal error: {e}")
        send_telegram_alert(f"❌ Proxy withdrawal error: {e}", is_error=True)
        return False, 0.0


# =============================================================================
# MAIN KEEPER LOOP
# =============================================================================

def keeper_iteration(pm_client: PolymarketClient) -> bool:
    """Single iteration of the keeper loop."""
    print(f"\n{'='*60}")
    print(f"🔄 LIQUIDITY KEEPER - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    is_safe, reason = check_kill_switches()
    if not is_safe:
        print(f"🚫 STOPPED: {reason}")
        return False
    print(f"✅ Kill switches: {reason}")
    
    vault_state = read_vault_state()
    if not vault_state:
        print("❌ Cannot read vault state")
        return False
    
    treasury_balance = get_treasury_balance()
    
    print(f"\n📊 STATUS:")
    print(f"   Vault buffer: ${vault_state.buffer_usdc:.2f}")
    print(f"   Pending withdrawals: ${vault_state.pending_shares_usdc:.2f}")
    print(f"   Shortfall: ${vault_state.shortfall_usdc:.2f}")
    print(f"   Treasury (Base): ${treasury_balance:.2f}")
    print(f"   NAV: {vault_state.last_nav / NAV_PRECISION:.6f}")
    
    check_treasury_thresholds(treasury_balance)
    
    if vault_state.shortfall_usdc < MIN_SHORTFALL_USDC:
        print(f"\n✅ No action needed (shortfall < ${MIN_SHORTFALL_USDC:.2f})")
        return True
    
    print(f"\n⚠️  SHORTFALL DETECTED: ${vault_state.shortfall_usdc:.2f}")
    
    if treasury_balance >= vault_state.shortfall_usdc:
        print(f"\n💰 Treasury has sufficient funds - refilling vault...")
        success = send_from_treasury_to_vault(vault_state.shortfall_usdc)
        if success:
            print("✅ Vault refilled from treasury!")
            return True
        else:
            print("❌ Treasury transfer failed")
    elif treasury_balance > MIN_SHORTFALL_USDC:
        print(f"\n💰 Using partial treasury funds: ${treasury_balance:.2f}")
        send_from_treasury_to_vault(treasury_balance * 0.9)
    
    still_needed = vault_state.shortfall_usdc - treasury_balance
    if still_needed > MIN_SHORTFALL_USDC:
        print(f"\n🔥 Need to liquidate ${still_needed:.2f} from Polymarket...")
        
        allowed, max_amount = check_hourly_limit(still_needed)
        if not allowed:
            print("⏳ Hourly liquidation limit reached")
            send_telegram_alert(
                f"⏳ Hourly liquidation limit reached\n"
                f"Still need: ${still_needed:.2f} USDC\n"
                f"Limit resets in {60 - (int(time.time()) % 3600) // 60} minutes"
            )
            return True
        
        pm_cash = pm_client.fetch_cash_balance()
        print(f"   PM cash available: ${pm_cash:.2f}")
        
        liquidated = 0.0
        if pm_cash < min(max_amount, still_needed):
            liquidate_amount = min(max_amount, still_needed) - pm_cash
            liquidated = liquidate_positions_for_usdc(liquidate_amount, pm_client)
        
        if liquidated > 0 or pm_cash > 0:
            proxy_usdc = get_proxy_usdc_balance()
            print(f"   PM Proxy USDC.e balance: ${proxy_usdc:.2f}")
            
            if proxy_usdc > MIN_SHORTFALL_USDC and TREASURY_ADDRESS:
                withdraw_amount = min(proxy_usdc, max_amount)
                print(f"\n🔧 Withdrawing ${withdraw_amount:.2f} from PM Proxy to Base Treasury...")
                
                withdraw_success, withdrawn = withdraw_from_proxy_to_treasury(withdraw_amount)
                if withdraw_success:
                    print(f"✅ Proxy withdrawal complete - ${withdrawn:.2f} bridged to treasury")
                else:
                    wallet_address = Account.from_key(POLYMARKET_PRIVATE_KEY).address
                    wallet_usdc = usdc_polygon.functions.balanceOf(
                        Web3.to_checksum_address(wallet_address)
                    ).call() / 1e6 if usdc_polygon else 0
                    
                    print(f"   Falling back to EOA. Wallet USDC: ${wallet_usdc:.2f}")
                    
                    if wallet_usdc > MIN_SHORTFALL_USDC:
                        print(f"\n🌉 Bridging ${wallet_usdc:.2f} from Polygon to Base...")
                        bridge_success, bridged_amount = bridge_polygon_to_base(wallet_usdc)
                        if bridge_success:
                            print(f"✅ Bridge complete - ${bridged_amount:.2f} now in treasury")
            elif proxy_usdc > 0 and not TREASURY_ADDRESS:
                send_telegram_alert(
                    f"💵 Funds in PM Proxy: ${proxy_usdc:.2f} USDC.e\n"
                    f"⚠️ TREASURY_ADDRESS not set - configure to enable auto-withdrawal",
                    is_error=True
                )
            else:
                send_telegram_alert(
                    f"💵 Funds on Polymarket: ${pm_cash + liquidated:.2f}\n"
                    f"Waiting for funds to settle in proxy...",
                    is_error=False
                )
    
    return True


def run_keeper():
    """Main keeper loop."""
    global w3_base, w3_polygon, vault_contract, usdc_base, usdc_polygon, treasury_account
    
    print(f"\n{'='*60}")
    print(f"🚀 LIQUIDITY KEEPER - Treasury-Backed Architecture")
    print(f"{'='*60}")
    
    if not VAULT_V5_ADDRESS:
        print("❌ VAULT_V5_ADDRESS not set")
        sys.exit(1)
    
    if not TREASURY_ADDRESS:
        print("⚠️  TREASURY_ADDRESS not set - will use keeper wallet as treasury")
    
    print(f"\nConfiguration:")
    print(f"   Vault: {VAULT_V5_ADDRESS}")
    print(f"   Treasury: {TREASURY_ADDRESS or 'Same as keeper'}")
    print(f"   PM Wallet: {POLYMARKET_PROXY_ADDRESS}")
    print(f"   Base RPC: {BASE_RPC_URL}")
    print(f"   Polygon RPC: {POLYGON_RPC_URL}")
    print(f"   Loop interval: {LOOP_INTERVAL_SECONDS}s")
    print(f"   Treasury low threshold: ${TREASURY_LOW_THRESHOLD_USDC:.2f}")
    print(f"   Hourly liquidation limit: ${MAX_HOURLY_LIQUIDATION_USDC:.2f}")
    
    print(f"\n🔌 Connecting to Base...")
    w3_base = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3_base.is_connected():
        print("❌ Cannot connect to Base RPC")
        sys.exit(1)
    print(f"   ✅ Connected (block {w3_base.eth.block_number})")
    
    vault_contract = w3_base.eth.contract(
        address=Web3.to_checksum_address(VAULT_V5_ADDRESS),
        abi=VAULT_V5_ABI
    )
    
    usdc_base = w3_base.eth.contract(
        address=Web3.to_checksum_address(USDC_BASE_ADDRESS),
        abi=ERC20_ABI
    )
    
    print(f"\n🔌 Connecting to Polygon...")
    w3_polygon = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
    if w3_polygon.is_connected():
        print(f"   ✅ Connected (block {w3_polygon.eth.block_number})")
        usdc_polygon = w3_polygon.eth.contract(
            address=Web3.to_checksum_address(USDC_POLYGON_ADDRESS),
            abi=ERC20_ABI
        )
    else:
        print("   ⚠️  Cannot connect to Polygon (liquidation may fail)")
    
    if POLYMARKET_PRIVATE_KEY:
        treasury_account = Account.from_key(POLYMARKET_PRIVATE_KEY)
        actual_treasury = TREASURY_ADDRESS or treasury_account.address
        print(f"\n   Treasury wallet: {actual_treasury}")
    else:
        print("\n⚠️  POLYMARKET_PRIVATE_KEY not set - read-only mode")
    
    pm_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS)
    
    treasury_bal = get_treasury_balance()
    print(f"\n💰 Initial treasury balance: ${treasury_bal:.2f}")
    
    if treasury_bal < TREASURY_LOW_THRESHOLD_USDC:
        send_telegram_alert(
            f"⚠️ Treasury balance low at startup: ${treasury_bal:.2f}\n"
            f"Consider pre-funding the treasury on Base."
        )
    
    send_telegram_alert(
        f"🚀 Liquidity Keeper started\n"
        f"Vault: {VAULT_V5_ADDRESS[:10]}...\n"
        f"Treasury: ${treasury_bal:.2f} USDC\n"
        f"Mode: {'Active' if treasury_account else 'Read-only'}"
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
                    print(f"❌ {max_failures} consecutive failures, pausing 5 min")
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
