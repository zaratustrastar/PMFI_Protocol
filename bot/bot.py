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
    1. Calculates the current NAV using get_strategy_nav_dummy()
    2. Builds a transaction to call strategy.updateStrategyValue(nav)
    3. Signs it with KEEPER_PRIVATE_KEY
    4. Sends the transaction and prints the tx hash
    """
    global w3, strategy
    
    if not KEEPER_PRIVATE_KEY:
        print("❌ Cannot push NAV: KEEPER_PRIVATE_KEY not set")
        return
    if not KEEPER_ADDRESS:
        print("❌ Cannot push NAV: KEEPER_ADDRESS not set")
        return
    
    try:
        # Get current NAV
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
    
    Runs two loops:
    1. NAV update loop (every 60 seconds)
    2. Liquidity shortfall listener (every 10 seconds)
    """
    global w3, vault, usdc, strategy
    
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
