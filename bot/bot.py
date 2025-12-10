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
# Currently uses dummy data - replace with real API calls for production.
# =============================================================================

class PolymarketClient:
    """
    Client for interacting with Polymarket API.
    
    Currently returns dummy data for testing. To switch to real Polymarket data:
    1. Replace fetch_positions() with real API call
    2. Replace fetch_orderbook() with real API call
    
    This class is READ-ONLY and does not execute any trades.
    """
    
    def __init__(self, wallet_address: str, api_base_url: str = "https://api.polymarket.com"):
        """
        Initialize the Polymarket client.
        
        Args:
            wallet_address: The wallet address to fetch positions for
            api_base_url: Base URL for Polymarket API (default: https://api.polymarket.com)
        """
        self.wallet_address = wallet_address
        self.api_base_url = api_base_url
    
    def fetch_positions(self) -> List[Dict]:
        """
        Fetch open positions for the wallet from Polymarket.
        
        TODO: Replace with a real API call to Polymarket to get open positions.
        
        Expected real behaviour:
        - Call something like GET {api_base_url}/positions?owner={wallet_address}
        - Parse response into a list of dicts:
            [{"market_id": "...", "side": "yes" or "no", "size": int}, ...]
        
        Example real implementation:
        ```python
        url = f"{self.api_base_url}/positions"
        params = {"owner": self.wallet_address}
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        return [
            {"market_id": p["marketId"], "side": p["side"], "size": p["size"]}
            for p in data["positions"]
        ]
        ```
        
        Returns:
            List of position dicts with market_id, side, and size
        """
        # DUMMY DATA - replace with real API call
        return [
            {"market_id": "market1", "side": "yes", "size": 10000},
            {"market_id": "market2", "side": "no", "size": 5000},
        ]
    
    def fetch_orderbook(self, market_id: str, side: str) -> Dict:
        """
        Fetch orderbook for a specific market and side from Polymarket.
        
        TODO: Replace with real orderbook call to Polymarket.
        
        Expected real behaviour:
        - Call something like GET {api_base_url}/markets/{market_id}/orderbook?side={side}
        - Read bid levels
        - Return a dict: {"bids": [{"price": float, "size": int}, ...]}
        
        Example real implementation:
        ```python
        url = f"{self.api_base_url}/markets/{market_id}/orderbook"
        params = {"side": side}
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        return {
            "bids": [
                {"price": float(b["price"]), "size": int(b["size"])}
                for b in data["bids"]
            ]
        }
        ```
        
        Args:
            market_id: The market identifier
            side: "yes" or "no"
        
        Returns:
            Dict with bids list containing price and size
        """
        # DUMMY DATA - replace with real API call
        return {
            "bids": [
                {"price": 0.09, "size": 3000},
                {"price": 0.085, "size": 5000},
                {"price": 0.08, "size": 20000},
            ]
        }


# =============================================================================
# NAV ENGINE
# =============================================================================
# Calculates Net Asset Value using PolymarketClient data
# =============================================================================

class NavEngine:
    """
    Calculates the Net Asset Value (NAV) of the strategy.
    
    Uses PolymarketClient to fetch positions and orderbooks,
    then calculates mark-to-market value of all positions.
    """
    
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
    
    def calculate_nav(self) -> int:
        """
        Calculate the total NAV of the strategy.
        
        NAV = USDC balance + mark-to-market value of all positions
        
        For each position:
        - Fetch orderbook
        - Use best bid price for valuation (conservative)
        - position_value = size * best_bid_price
        
        Returns:
            NAV in USDC (6 decimals)
        """
        # Get USDC balance held by strategy contract
        usdc_balance = self.usdc_contract.functions.balanceOf(self.strategy_address).call()
        
        # Fetch positions from Polymarket
        positions = self.polymarket_client.fetch_positions()
        
        # Calculate mark-to-market value of positions
        total_position_value = 0
        
        for position in positions:
            market_id = position["market_id"]
            side = position["side"]
            size = position["size"]
            
            # Fetch orderbook for this market
            orderbook = self.polymarket_client.fetch_orderbook(market_id, side)
            
            # Use best bid for conservative valuation
            if orderbook["bids"]:
                best_bid = orderbook["bids"][0]["price"]
                position_value = int(size * best_bid)
                total_position_value += position_value
        
        # Total NAV = USDC balance + position values
        nav = usdc_balance + total_position_value
        
        print(f"📊 NAV Calculation:")
        print(f"   USDC Balance:     {usdc_balance / 1e6:.2f} USDC")
        print(f"   Positions Value:  {total_position_value / 1e6:.2f} USDC")
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
    
    try:
        # Connect to chain
        w3 = connect_to_chain()
        
        # Load contracts
        vault, usdc, strategy = get_contracts(w3)
        
        # Initialize PolymarketClient (uses KEEPER_ADDRESS as wallet to track)
        # This is READ-ONLY - it only fetches positions and orderbooks
        wallet_to_track = KEEPER_ADDRESS or "0x0000000000000000000000000000000000000000"
        polymarket_client = PolymarketClient(
            wallet_address=wallet_to_track,
            api_base_url="https://api.polymarket.com"
        )
        print(f"✅ Initialized PolymarketClient (wallet: {wallet_to_track[:10]}...)")
        
        # Initialize NavEngine with PolymarketClient
        nav_engine = NavEngine(
            polymarket_client=polymarket_client,
            usdc_contract=usdc,
            strategy_address=STRATEGY_ADDRESS
        )
        print(f"✅ Initialized NavEngine")
        
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
