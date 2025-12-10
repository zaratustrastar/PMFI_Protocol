#!/usr/bin/env python3
"""
PredictFi Sniper Vault - NAV Updater & Liquidity Management Bot

=============================================================================
CURRENT FUNCTIONALITY:
=============================================================================

1. NAV UPDATES (every 60 seconds):
   - Reads USDC balance in strategy contract
   - Calculates NAV (currently: balance + 5% simulated profit)
   - Pushes NAV to chain via strategy.updateStrategyValue()
   
2. LIQUIDITY SHORTFALL MONITORING (every 10 seconds):
   - Watches for LiquidityShortfall events from the vault
   - Logs requested amount (assetsNeeded) and available amount (availableAssets)
   - Does NOT move funds yet - this is a TODO for production

=============================================================================
ENVIRONMENT VARIABLES REQUIRED:
=============================================================================
- RPC_URL: Base Sepolia RPC endpoint
- VAULT_ADDRESS: PredictFiSniperVaultV2 contract address
- USDC_ADDRESS: TestUSDC contract address  
- STRATEGY_ADDRESS: MockSniperStrategy contract address
- KEEPER_ADDRESS: Wallet address with keeper role on strategy
- KEEPER_PRIVATE_KEY (or PRIVATE_KEY): Private key for signing transactions

=============================================================================
TODO FOR PRODUCTION:
=============================================================================
- Replace dummy NAV calculation with real Polymarket orderbook valuation
- Implement actual liquidity handling: withdraw from Polymarket, send to vault

This is a prototype - not production code.
"""

import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import List, Dict, Optional

import requests
from dotenv import load_dotenv
from web3 import Web3

# Load environment variables
load_dotenv()

# =============================================================================
# Configuration from environment
# =============================================================================
RPC_URL = os.getenv("RPC_URL")
VAULT_ADDRESS = os.getenv("VAULT_ADDRESS")
USDC_ADDRESS = os.getenv("USDC_ADDRESS")
STRATEGY_ADDRESS = os.getenv("STRATEGY_ADDRESS")

# Keeper credentials (for updating NAV on-chain)
# Try KEEPER_PRIVATE_KEY first, fallback to PRIVATE_KEY
KEEPER_PRIVATE_KEY = os.getenv("KEEPER_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
KEEPER_ADDRESS = os.getenv("KEEPER_ADDRESS")

# Polymarket configuration
# POLYMARKET_PROXY_ADDRESS is the address that holds positions on Polymarket
# (for email/Magic logins, this is the proxy address, not your EOA)
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS")

# Polling intervals
NAV_UPDATE_INTERVAL = 60   # Update NAV every 60 seconds
SHORTFALL_POLL_INTERVAL = 10  # Check for shortfall events every 10 seconds

# Global web3 and contract instances
w3 = None
vault = None
usdc = None
strategy = None

# Global Polymarket client and NAV engine instances
polymarket_client = None
nav_engine = None


# =============================================================================
# POLYMARKET CLIENT
# =============================================================================
# Read-only client for fetching positions and orderbook data from Polymarket.
# Uses real Polymarket API endpoints.
# =============================================================================

class PolymarketClient:
    """
    Client for interacting with Polymarket API.
    
    Uses real Polymarket endpoints:
    - Data API (https://data-api.polymarket.com) for positions
    - CLOB API (https://clob.polymarket.com) for orderbook
    
    This class is READ-ONLY and does not execute any trades.
    """
    
    DATA_API_URL = "https://data-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    def __init__(self, wallet_address: str):
        """
        Initialize the Polymarket client.
        
        Args:
            wallet_address: The wallet address to fetch positions for
                           (use proxy address for email/Magic logins)
        """
        self.wallet_address = wallet_address
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
    
    def fetch_positions(self) -> List[Dict]:
        """
        Fetch ALL open positions for the wallet from Polymarket Data API.
        Uses pagination to get all positions (API returns max ~100-200 per request).
        
        Endpoint: GET https://data-api.polymarket.com/positions?user={wallet}&limit=500&offset=0
        
        Returns:
            List of position dicts with token_id, side, size, and current_value
        """
        try:
            print(f"📡 Fetching ALL positions for {self.wallet_address[:10]}...")
            
            all_positions_raw = []
            offset = 0
            limit = 500  # Max per request
            
            # Paginate through all positions
            while True:
                url = f"{self.DATA_API_URL}/positions"
                params = {"user": self.wallet_address, "limit": limit, "offset": offset}
                
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                if not data:
                    break
                    
                all_positions_raw.extend(data)
                print(f"   Fetched {len(data)} positions (offset={offset}, total={len(all_positions_raw)})")
                
                if len(data) < limit:
                    break  # No more pages
                offset += limit
            
            # Helper to safely convert to float (handles null/None from API)
            def safe_float(val, default=0.0):
                if val is None:
                    return default
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return default
            
            # Parse positions
            positions = []
            for p in all_positions_raw:
                # Skip positions with zero size
                size = safe_float(p.get("size"))
                if size <= 0:
                    continue
                
                positions.append({
                    "token_id": p.get("asset") or "",  # Token ID for orderbook lookup
                    "condition_id": p.get("conditionId") or "",
                    "title": p.get("title") or "",  # Market title
                    "outcome": p.get("outcome") or "",  # "Yes" or "No"
                    "side": (p.get("outcome") or "").lower(),  # "yes" or "no"
                    "size": size,
                    "avg_price": safe_float(p.get("avgPrice")),
                    "current_value": safe_float(p.get("currentValue")),
                    "initial_value": safe_float(p.get("initialValue")),
                    "pnl": safe_float(p.get("cashPnl")),  # Use cashPnl for actual P&L
                    "pnl_percent": safe_float(p.get("percentPnl")),
                    "realized_pnl": safe_float(p.get("realizedPnl")),
                    "cur_price": safe_float(p.get("curPrice")),
                    "redeemable": bool(p.get("redeemable", False)),
                })
            
            print(f"✅ Found {len(positions)} active positions (from {len(all_positions_raw)} total)")
            return positions
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching positions: {e}")
            return []
        except (KeyError, ValueError) as e:
            print(f"❌ Error parsing positions response: {e}")
            return []
    
    def simulate_market_sell(self, size: float, bids: List[Dict]) -> float:
        """
        Simulate a market sell order by walking through the orderbook bids.
        
        This calculates the realistic liquidation value by filling against
        actual bid orders, accounting for order book depth and illiquidity.
        
        Args:
            size: Number of shares to sell
            bids: List of bid orders [{"price": float, "size": float}, ...] sorted high→low
        
        Returns:
            Total value received from the simulated sell (before fees)
        """
        remaining = size
        value = 0.0
        
        for bid in bids:
            if remaining <= 0:
                break
            
            bid_price = float(bid.get("price", 0))
            bid_size = float(bid.get("size", 0))
            
            if bid_price <= 0 or bid_size <= 0:
                continue
            
            # Fill as much as possible at this price level
            fill = min(remaining, bid_size)
            value += fill * bid_price
            remaining -= fill
        
        # Any remaining shares have no bids = illiquid, value = 0
        if remaining > 0:
            print(f"      ⚠️ {remaining:.2f} shares illiquid (no bids)")
        
        return value
    
    def fetch_cash_balance(self) -> float:
        """
        Fetch the USDC cash balance held on Polymarket.
        
        Note: This requires the wallet API which may need authentication.
        For now, we return 0 as a placeholder - cash balance can be added manually.
        
        Returns:
            Cash balance in USDC (float)
        """
        # TODO: Implement if Polymarket provides a public endpoint for cash balance
        # For now, return 0 - user can add cash balance manually if needed
        return 0.0
    
    def fetch_orderbook(self, token_id: str) -> Dict:
        """
        Fetch orderbook for a specific token from Polymarket CLOB API.
        
        Endpoint: GET https://clob.polymarket.com/book?token_id={token_id}
        
        Args:
            token_id: The token ID (asset) for the market outcome
        
        Returns:
            Dict with bids and asks lists containing price and size
        """
        try:
            url = f"{self.CLOB_API_URL}/book"
            params = {"token_id": token_id}
            
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            # Parse orderbook - CLOB returns bids and asks arrays
            # Helper for safe float conversion
            def safe_float(val, default=0.0):
                if val is None:
                    return default
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return default
            
            bids = []
            for b in data.get("bids") or []:
                price = safe_float(b.get("price") if isinstance(b, dict) else None)
                size = safe_float(b.get("size") if isinstance(b, dict) else None)
                if price > 0:
                    bids.append({"price": price, "size": size})
            
            asks = []
            for a in data.get("asks") or []:
                price = safe_float(a.get("price") if isinstance(a, dict) else None)
                size = safe_float(a.get("size") if isinstance(a, dict) else None)
                if price > 0:
                    asks.append({"price": price, "size": size})
            
            return {"bids": bids, "asks": asks}
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching orderbook for {token_id[:20]}...: {e}")
            return {"bids": [], "asks": []}
        except (KeyError, ValueError) as e:
            print(f"❌ Error parsing orderbook response: {e}")
            return {"bids": [], "asks": []}
    
    def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """
        Get the midpoint price for a token.
        
        Endpoint: GET https://clob.polymarket.com/midpoint?token_id={token_id}
        
        Args:
            token_id: The token ID for the market outcome
        
        Returns:
            Midpoint price as float, or None if unavailable
        """
        try:
            url = f"{self.CLOB_API_URL}/midpoint"
            params = {"token_id": token_id}
            
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            return float(data.get("mid", 0))
            
        except Exception as e:
            print(f"❌ Error fetching midpoint for {token_id[:20]}...: {e}")
            return None


# =============================================================================
# NAV ENGINE
# =============================================================================
# Calculates Net Asset Value using PolymarketClient data
# =============================================================================

class NavEngine:
    """
    Calculates the Net Asset Value (NAV) of the strategy.
    
    Uses PolymarketClient to fetch positions from Data API.
    
    Three calculation methods available:
    1. Fast: Use currentValue from Data API (mid-market price)
    2. Best-bid: Use best bid price from orderbook
    3. Liquidation (RECOMMENDED): Simulate market sell through orderbook depth
    """
    
    # Fee/slippage haircut for realistic liquidation value
    HAIRCUT_PERCENT = 0.02  # 2% for fees + slippage margin
    
    def __init__(self, polymarket_client: PolymarketClient, usdc_contract, strategy_address: str):
        """
        Initialize the NAV engine.
        
        Args:
            polymarket_client: PolymarketClient instance for fetching market data
            usdc_contract: USDC contract instance for checking balances
            strategy_address: Strategy contract address
        """
        self.polymarket_client = polymarket_client
        self.usdc_contract = usdc_contract
        self.strategy_address = strategy_address
    
    def calculate_liquidation_nav(self, verbose: bool = True) -> int:
        """
        Calculate the realistic LIQUIDATION NAV of the strategy.
        
        This is the TRUE value you would get if you sold everything right now:
        1. Fetches ALL positions with pagination
        2. For each position, simulates a market sell through the orderbook
        3. Applies a 2% haircut for fees and slippage
        4. Adds any cash balance on Polymarket
        5. Adds on-chain USDC balance
        
        Returns:
            Liquidation NAV in USDC (6 decimals)
        """
        print(f"\n{'='*60}")
        print(f"💰 CALCULATING REALISTIC LIQUIDATION NAV")
        print(f"{'='*60}")
        
        # Get USDC balance held by strategy contract (on-chain)
        usdc_balance = self.usdc_contract.functions.balanceOf(self.strategy_address).call()
        
        # Fetch ALL positions from Polymarket Data API (with pagination)
        positions = self.polymarket_client.fetch_positions()
        
        if not positions:
            print(f"📊 No active Polymarket positions found")
            nav = usdc_balance
            print(f"\n📊 Liquidation NAV: {nav / 1e6:.2f} USDC (on-chain only)")
            return nav
        
        print(f"\n📊 Simulating market sell for {len(positions)} positions...")
        
        total_liquidation_value = 0.0
        total_mid_market_value = 0.0
        illiquid_count = 0
        
        for i, position in enumerate(positions):
            token_id = position.get("token_id", "")
            size = position.get("size", 0)
            outcome = position.get("outcome", "?")
            title = position.get("title", "Unknown")[:40]
            mid_value = position.get("current_value", 0)
            
            total_mid_market_value += mid_value
            
            if not token_id:
                # No token_id, use mid-market as fallback
                total_liquidation_value += mid_value
                continue
            
            # Fetch orderbook and simulate market sell
            orderbook = self.polymarket_client.fetch_orderbook(token_id)
            bids = orderbook.get("bids", [])
            
            if not bids:
                # No bids = completely illiquid, value = 0
                illiquid_count += 1
                if verbose:
                    print(f"   ❌ {outcome}: {size:.1f} shares - NO BIDS (illiquid)")
                    print(f"      └─ {title}")
                continue
            
            # Simulate market sell
            liquidation_value = self.polymarket_client.simulate_market_sell(size, bids)
            total_liquidation_value += liquidation_value
            
            if verbose:
                diff = liquidation_value - mid_value
                diff_pct = (diff / mid_value * 100) if mid_value > 0 else 0
                print(f"   • {outcome}: {size:.1f} → ${liquidation_value:.2f} (mid: ${mid_value:.2f}, {diff_pct:+.0f}%)")
                print(f"      └─ {title}")
        
        # Apply haircut for fees + slippage
        value_after_haircut = total_liquidation_value * (1 - self.HAIRCUT_PERCENT)
        
        # Get Polymarket cash balance (if any)
        pm_cash = self.polymarket_client.fetch_cash_balance()
        
        # Total liquidation NAV
        # Convert to 6 decimals (USDC format)
        position_value_6dec = int(value_after_haircut * 1e6)
        pm_cash_6dec = int(pm_cash * 1e6)
        nav = usdc_balance + position_value_6dec + pm_cash_6dec
        
        print(f"\n{'='*60}")
        print(f"📊 LIQUIDATION NAV SUMMARY")
        print(f"{'='*60}")
        print(f"   Positions analyzed:     {len(positions)}")
        print(f"   Illiquid positions:     {illiquid_count}")
        print(f"   Mid-market value:       ${total_mid_market_value:.2f}")
        print(f"   Liquidation value:      ${total_liquidation_value:.2f}")
        print(f"   After {self.HAIRCUT_PERCENT*100:.0f}% haircut:      ${value_after_haircut:.2f}")
        print(f"   Polymarket cash:        ${pm_cash:.2f}")
        print(f"   On-chain USDC:          ${usdc_balance / 1e6:.2f}")
        print(f"   ─────────────────────────────────")
        print(f"   LIQUIDATION NAV:        ${nav / 1e6:.2f} USDC")
        print(f"{'='*60}")
        
        return nav
    
    def calculate_nav(self, use_orderbook: bool = False, use_liquidation: bool = True) -> int:
        """
        Calculate the total NAV of the strategy.
        
        NAV = USDC balance (on-chain) + Polymarket position values
        
        Args:
            use_orderbook: If True, fetch orderbook for each position and use
                          best bid for valuation. If False, use mid-market value.
            use_liquidation: If True (default), use realistic liquidation NAV
                            that simulates market sells through orderbook depth.
        
        Returns:
            NAV in USDC (6 decimals)
        """
        # Default to liquidation NAV (most accurate)
        if use_liquidation:
            return self.calculate_liquidation_nav()
        
        # Get USDC balance held by strategy contract (on-chain)
        usdc_balance = self.usdc_contract.functions.balanceOf(self.strategy_address).call()
        
        # Fetch positions from Polymarket Data API
        positions = self.polymarket_client.fetch_positions()
        
        # Calculate mark-to-market value of positions
        total_position_value = 0
        
        if not positions:
            print(f"📊 No active Polymarket positions found")
        else:
            print(f"📊 Calculating value for {len(positions)} positions...")
        
        for position in positions:
            token_id = position.get("token_id", "")
            size = position.get("size", 0)
            outcome = position.get("outcome", "?")
            
            if use_orderbook and token_id:
                # Conservative: use best bid from orderbook
                orderbook = self.polymarket_client.fetch_orderbook(token_id)
                if orderbook["bids"]:
                    best_bid = orderbook["bids"][0]["price"]
                    position_value = size * best_bid
                else:
                    # No bids, use currentValue as fallback
                    position_value = position.get("current_value", 0)
            else:
                # Fast: use currentValue from Data API
                position_value = position.get("current_value", 0)
            
            total_position_value += position_value
            
            # Log each position
            title = position.get("title", "Unknown market")[:50]
            pnl = position.get("pnl", 0)
            pnl_sign = "+" if pnl >= 0 else ""
            redeemable = " [REDEEMABLE]" if position.get("redeemable", False) else ""
            print(f"   • {outcome}: {size:.2f} @ ${position_value:.2f} ({pnl_sign}${pnl:.2f}){redeemable}")
            print(f"     └─ {title}")
        
        # Total NAV = strategy USDC balance + Polymarket position values
        # Convert position value to 6 decimals (USDC format)
        position_value_6dec = int(total_position_value * 1e6)
        nav = usdc_balance + position_value_6dec
        
        print(f"\n📊 NAV Summary:")
        print(f"   Strategy USDC:    {usdc_balance / 1e6:.2f} USDC")
        print(f"   Polymarket Value: ${total_position_value:.2f}")
        print(f"   Total NAV:        {nav / 1e6:.2f} USDC")
        
        return nav


def load_abi(contract_name: str) -> dict:
    """
    Load ABI from Hardhat artifacts.
    
    Args:
        contract_name: Name of the contract (e.g., "PredictFiSniperVault")
    
    Returns:
        The contract ABI as a dict
    """
    project_root = Path(__file__).parent.parent
    artifact_path = project_root / "artifacts" / "contracts" / f"{contract_name}.sol" / f"{contract_name}.json"
    
    if not artifact_path.exists():
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")
    
    with open(artifact_path, "r") as f:
        artifact = json.load(f)
    
    return artifact["abi"]


def connect_to_chain() -> Web3:
    """
    Connect to the blockchain via RPC.
    
    Returns:
        Web3 instance connected to the chain
    """
    if not RPC_URL:
        raise ValueError("RPC_URL environment variable not set")
    
    web3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    if not web3.is_connected():
        raise ConnectionError(f"Failed to connect to RPC at {RPC_URL}")
    
    print(f"✅ Connected to chain: {RPC_URL}")
    print(f"   Chain ID: {web3.eth.chain_id}")
    print(f"   Latest block: {web3.eth.block_number}")
    
    return web3


def get_contracts(web3: Web3) -> tuple:
    """
    Load and instantiate the vault, USDC, and strategy contracts.
    
    Args:
        web3: Web3 instance
    
    Returns:
        Tuple of (vault_contract, usdc_contract, strategy_contract)
    """
    if not VAULT_ADDRESS:
        raise ValueError("VAULT_ADDRESS environment variable not set")
    if not USDC_ADDRESS:
        raise ValueError("USDC_ADDRESS environment variable not set")
    if not STRATEGY_ADDRESS:
        raise ValueError("STRATEGY_ADDRESS environment variable not set")
    
    # Load ABIs
    vault_abi = load_abi("PredictFiSniperVaultV2")
    usdc_abi = load_abi("TestUSDC")
    strategy_abi = load_abi("MockSniperStrategy")
    
    # Instantiate contracts
    vault_contract = web3.eth.contract(
        address=Web3.to_checksum_address(VAULT_ADDRESS),
        abi=vault_abi
    )
    usdc_contract = web3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=usdc_abi
    )
    strategy_contract = web3.eth.contract(
        address=Web3.to_checksum_address(STRATEGY_ADDRESS),
        abi=strategy_abi
    )
    
    print(f"✅ Loaded contracts:")
    print(f"   Vault:    {VAULT_ADDRESS}")
    print(f"   USDC:     {USDC_ADDRESS}")
    print(f"   Strategy: {STRATEGY_ADDRESS}")
    
    return vault_contract, usdc_contract, strategy_contract


# =============================================================================
# NAV CALCULATION
# =============================================================================
# TODO: Replace this with real Polymarket-based NAV calculation using orderbooks
# =============================================================================

def get_strategy_nav_dummy() -> int:
    """
    Calculate the strategy NAV (dummy implementation).
    
    Currently:
    - Reads the USDC balance held by the strategy contract
    - Pretends there's an extra 5% profit on top (simulating unrealized gains)
    
    TODO: Replace this with real Polymarket NAV calculation:
    - Fetch open positions from Polymarket API
    - Calculate mark-to-market value using orderbook mid prices
    - Sum all position values + USDC balance
    
    Returns:
        NAV in USDC (6 decimals)
    """
    global usdc, strategy
    
    # Get USDC balance held by strategy contract
    usdc_balance = usdc.functions.balanceOf(STRATEGY_ADDRESS).call()
    
    # ==========================================================
    # TODO: Replace with real Polymarket NAV calculation
    # ==========================================================
    # Example pseudocode for real implementation:
    #
    # positions = polymarket_client.get_positions()
    # total_position_value = 0
    # 
    # for position in positions:
    #     orderbook = polymarket_client.get_orderbook(position.market_id)
    #     mid_price = (orderbook.best_bid + orderbook.best_ask) / 2
    #     position_value = position.quantity * mid_price
    #     total_position_value += position_value
    #
    # nav = usdc_balance + total_position_value
    # ==========================================================
    
    # For now, simulate 5% profit on top of USDC balance
    nav = int(usdc_balance * 105 / 100)
    
    print(f"📊 NAV Calculation (dummy):")
    print(f"   USDC Balance:  {usdc_balance / 1e6:.2f} USDC")
    print(f"   Simulated NAV: {nav / 1e6:.2f} USDC (+5% profit)")
    
    return nav


def push_nav_to_chain():
    """
    Push the calculated NAV to the strategy contract on-chain.
    
    This function:
    1. Calculates the current NAV using NavEngine (or fallback to dummy)
    2. Builds a transaction to call strategy.updateStrategyValue(nav)
    3. Signs it with KEEPER_PRIVATE_KEY
    4. Sends the transaction and prints the tx hash
    """
    global w3, strategy, nav_engine
    
    if not KEEPER_PRIVATE_KEY:
        print("❌ Cannot push NAV: KEEPER_PRIVATE_KEY not set")
        return
    if not KEEPER_ADDRESS:
        print("❌ Cannot push NAV: KEEPER_ADDRESS not set")
        return
    
    try:
        # Get current NAV using NavEngine if available, otherwise use dummy
        if nav_engine:
            nav = nav_engine.calculate_nav()
        else:
            nav = get_strategy_nav_dummy()
        
        # Get current on-chain value for comparison
        current_on_chain = strategy.functions.totalStrategyValue().call()
        
        if nav == current_on_chain:
            print(f"ℹ️  NAV unchanged ({nav / 1e6:.2f} USDC), skipping update")
            return
        
        print(f"📤 Pushing NAV to chain...")
        print(f"   Current on-chain: {current_on_chain / 1e6:.2f} USDC")
        print(f"   New NAV:          {nav / 1e6:.2f} USDC")
        
        # Build transaction
        keeper_address = Web3.to_checksum_address(KEEPER_ADDRESS)
        nonce = w3.eth.get_transaction_count(keeper_address)
        
        tx = strategy.functions.updateStrategyValue(nav).build_transaction({
            'from': keeper_address,
            'nonce': nonce,
            'gas': 100000,
            'gasPrice': w3.eth.gas_price,
            'chainId': w3.eth.chain_id
        })
        
        # Sign transaction
        signed_tx = w3.eth.account.sign_transaction(tx, KEEPER_PRIVATE_KEY)
        
        # Send transaction
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"✅ NAV update sent!")
        print(f"   Tx Hash: {tx_hash.hex()}")
        
        # Wait for confirmation (optional)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status == 1:
            print(f"✅ NAV update confirmed in block {receipt.blockNumber}")
        else:
            print(f"❌ NAV update failed!")
            
    except Exception as e:
        print(f"❌ Error pushing NAV: {e}")


# =============================================================================
# LIQUIDITY SHORTFALL HANDLING
# =============================================================================
# TODO: Implement real response to shortfalls by withdrawing from Polymarket
# =============================================================================

def handle_liquidity_shortfall(event: dict):
    """
    Handle a LiquidityShortfall event.
    
    TODO: Implement real handling:
    1. Withdraw USDC from Polymarket positions
    2. Send USDC from strategy wallet to vault
    
    Args:
        event: The event data
    """
    assets_needed = event["args"]["assetsNeeded"]
    available_assets = event["args"]["availableAssets"]
    shortfall = assets_needed - available_assets
    
    print(f"\n{'='*60}")
    print(f"⚠️  LIQUIDITY SHORTFALL DETECTED")
    print(f"{'='*60}")
    print(f"   Assets Needed:    {assets_needed / 1e6:.2f} USDC")
    print(f"   Available Assets: {available_assets / 1e6:.2f} USDC")
    print(f"   Shortfall:        {shortfall / 1e6:.2f} USDC")
    print(f"   Block:            {event['blockNumber']}")
    print(f"   Tx Hash:          {event['transactionHash'].hex()}")
    print(f"{'='*60}")
    
    # ==========================================================
    # TODO: Implement Polymarket withdrawal
    # ==========================================================
    # Steps to implement:
    # 1. Check current Polymarket positions
    # 2. Calculate which positions to close/sell
    # 3. Execute trades to recover USDC
    # 4. Wait for settlement
    #
    # Example pseudocode:
    # polymarket_balance = polymarket_client.get_usdc_balance()
    # if polymarket_balance >= shortfall:
    #     polymarket_client.withdraw(shortfall)
    # else:
    #     # Need to sell positions first
    #     positions = polymarket_client.get_positions()
    #     for pos in positions:
    #         polymarket_client.market_sell(pos)
    #     # Then withdraw
    #     polymarket_client.withdraw(shortfall)
    # ==========================================================
    
    # ==========================================================
    # TODO: Send USDC from strategy wallet to vault
    # ==========================================================
    # Once USDC is available, send to vault:
    #
    # tx = usdc.functions.transfer(
    #     VAULT_ADDRESS,
    #     shortfall
    # ).build_transaction({...})
    # 
    # signed_tx = w3.eth.account.sign_transaction(tx, KEEPER_PRIVATE_KEY)
    # tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    # print(f"Sent {shortfall / 1e6:.2f} USDC to vault. Tx: {tx_hash.hex()}")
    # ==========================================================
    
    print(f"\n   [TODO] Would handle shortfall of {shortfall / 1e6:.2f} USDC")
    print(f"   [TODO] Polymarket withdrawal + vault transfer not implemented yet")


def listen_liquidity_shortfall(vault_contract=None, start_block=None):
    """
    Listen for LiquidityShortfall events from the vault.
    
    This function:
    - Polls the vault contract for LiquidityShortfall events
    - Logs the requested amount (assetsNeeded) and idle available
    - TODO: In production, free liquidity on Polymarket and send USDC to vault
    
    Args:
        vault_contract: The vault contract instance (uses global if None)
        start_block: Block to start listening from (uses current if None)
    """
    global w3, vault
    
    # Use provided contract or global
    target_vault = vault_contract if vault_contract else vault
    
    print(f"\n🔍 Listening for LiquidityShortfall events...")
    print(f"   Poll interval: {SHORTFALL_POLL_INTERVAL}s")
    
    # If start_block is None, set it to current block number
    if start_block is None:
        start_block = w3.eth.block_number
    
    # Track the last checked block
    last_block = start_block
    
    while True:
        try:
            current_block = w3.eth.block_number
            
            if current_block > last_block:
                # Get events from last_block to current_block
                # Event: LiquidityShortfall(uint256 assetsNeeded, uint256 availableAssets)
                events = target_vault.events.LiquidityShortfall.get_logs(
                    from_block=last_block + 1,
                    to_block=current_block
                )
                
                for event in events:
                    handle_liquidity_shortfall(event)
                    # Update start_block to event.blockNumber + 1 for next iteration
                    last_block = event['blockNumber']
                
                # If no events, just update to current
                if not events:
                    last_block = current_block
            
        except Exception as e:
            print(f"❌ Error polling events: {e}")
        
        # Wait before next poll
        time.sleep(SHORTFALL_POLL_INTERVAL)


def nav_update_loop():
    """
    Periodically push NAV updates to the chain.
    
    Runs every NAV_UPDATE_INTERVAL seconds.
    """
    print(f"\n📈 Starting NAV update loop...")
    print(f"   Update interval: {NAV_UPDATE_INTERVAL}s")
    
    while True:
        try:
            push_nav_to_chain()
        except Exception as e:
            print(f"❌ Error in NAV update loop: {e}")
        
        time.sleep(NAV_UPDATE_INTERVAL)


def main():
    """
    Main entry point for the NAV updater & liquidity management bot.
    
    This bot:
    - Updates NAV on-chain every 60 seconds using PolymarketClient + NavEngine
    - Watches for LiquidityShortfall events and logs them (does not move funds yet)
    
    Runs two loops:
    1. NAV update loop (every 60 seconds)
    2. Liquidity shortfall listener (every 10 seconds)
    """
    global w3, vault, usdc, strategy, polymarket_client, nav_engine
    
    print("\n" + "="*60)
    print("🏦 PredictFi Sniper Vault - NAV Updater Bot")
    print("="*60 + "\n")
    
    # Validate required environment variables
    required_vars = [
        ("RPC_URL", RPC_URL),
        ("VAULT_ADDRESS", VAULT_ADDRESS),
        ("USDC_ADDRESS", USDC_ADDRESS),
        ("STRATEGY_ADDRESS", STRATEGY_ADDRESS),
    ]
    
    missing = [name for name, value in required_vars if not value]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        print("   Please check your .env file")
        sys.exit(1)
    
    # Warn if keeper credentials not set
    if not KEEPER_PRIVATE_KEY:
        print("⚠️  KEEPER_PRIVATE_KEY not set - cannot push NAV updates")
    if not KEEPER_ADDRESS:
        print("⚠️  KEEPER_ADDRESS not set - cannot push NAV updates")
    if not POLYMARKET_PROXY_ADDRESS:
        print("⚠️  POLYMARKET_PROXY_ADDRESS not set - cannot fetch Polymarket positions")
    
    try:
        # Connect to chain
        w3 = connect_to_chain()
        
        # Load contracts
        vault, usdc, strategy = get_contracts(w3)
        
        # Initialize PolymarketClient with the Polymarket proxy address
        # This is READ-ONLY - it only fetches positions and orderbooks
        # POLYMARKET_PROXY_ADDRESS is the address that holds positions on Polymarket
        if POLYMARKET_PROXY_ADDRESS:
            polymarket_client = PolymarketClient(wallet_address=POLYMARKET_PROXY_ADDRESS)
            print(f"✅ Initialized PolymarketClient (wallet: {POLYMARKET_PROXY_ADDRESS[:10]}...)")
        else:
            polymarket_client = None
            print("⚠️  PolymarketClient not initialized - no proxy address")
        
        # Initialize NavEngine with PolymarketClient (if available)
        if polymarket_client:
            nav_engine = NavEngine(
                polymarket_client=polymarket_client,
                usdc_contract=usdc,
                strategy_address=STRATEGY_ADDRESS
            )
            print(f"✅ Initialized NavEngine with real Polymarket data")
        else:
            nav_engine = None
            print("⚠️  NavEngine not initialized - will use dummy NAV calculation")
        
        # Verify keeper is set on strategy
        if KEEPER_ADDRESS:
            on_chain_keeper = strategy.functions.keeper().call()
            if on_chain_keeper.lower() != KEEPER_ADDRESS.lower():
                print(f"\n⚠️  WARNING: On-chain keeper ({on_chain_keeper}) != KEEPER_ADDRESS ({KEEPER_ADDRESS})")
                print(f"   You may need to call strategy.setKeeper() first")
        
        print(f"\n🚀 Starting bot loops...")
        print(f"   NAV updates: every {NAV_UPDATE_INTERVAL}s")
        print(f"   Shortfall checks: every {SHORTFALL_POLL_INTERVAL}s")
        print(f"   Press Ctrl+C to stop\n")
        
        # Start NAV update loop in a separate thread
        nav_thread = threading.Thread(target=nav_update_loop, daemon=True)
        nav_thread.start()
        
        # Run shortfall listener in main thread
        listen_liquidity_shortfall()
        
    except KeyboardInterrupt:
        print("\n\n👋 Bot stopped")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
