#!/usr/bin/env python3
"""
PredictFi Sniper Vault - Liquidity Management Bot

This bot monitors the vault for LiquidityShortfall events and will
eventually handle withdrawing funds from Polymarket to cover shortfalls.

This is a skeleton - not production code.
"""

import os
import sys
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3

# Load environment variables
load_dotenv()

# Configuration from environment
RPC_URL = os.getenv("RPC_URL")
VAULT_ADDRESS = os.getenv("VAULT_ADDRESS")
USDC_ADDRESS = os.getenv("USDC_ADDRESS")
STRATEGY_PRIVATE_KEY = os.getenv("STRATEGY_PRIVATE_KEY")
STRATEGY_ADDRESS = os.getenv("STRATEGY_ADDRESS")

# Polling interval in seconds
POLL_INTERVAL = 5


def load_abi(contract_name: str) -> dict:
    """
    Load ABI from Hardhat artifacts.
    
    Args:
        contract_name: Name of the contract (e.g., "PredictFiSniperVault")
    
    Returns:
        The contract ABI as a dict
    """
    # Find the project root (parent of bot/)
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
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    if not w3.is_connected():
        raise ConnectionError(f"Failed to connect to RPC at {RPC_URL}")
    
    print(f"✅ Connected to chain: {RPC_URL}")
    print(f"   Chain ID: {w3.eth.chain_id}")
    print(f"   Latest block: {w3.eth.block_number}")
    
    return w3


def get_contracts(w3: Web3) -> tuple:
    """
    Load and instantiate the vault and USDC contracts.
    
    Args:
        w3: Web3 instance
    
    Returns:
        Tuple of (vault_contract, usdc_contract)
    """
    if not VAULT_ADDRESS:
        raise ValueError("VAULT_ADDRESS environment variable not set")
    if not USDC_ADDRESS:
        raise ValueError("USDC_ADDRESS environment variable not set")
    
    # Load ABIs
    vault_abi = load_abi("PredictFiSniperVault")
    usdc_abi = load_abi("TestUSDC")
    
    # Instantiate contracts
    vault = w3.eth.contract(
        address=Web3.to_checksum_address(VAULT_ADDRESS),
        abi=vault_abi
    )
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=usdc_abi
    )
    
    print(f"✅ Loaded contracts:")
    print(f"   Vault: {VAULT_ADDRESS}")
    print(f"   USDC:  {USDC_ADDRESS}")
    
    return vault, usdc


def handle_liquidity_shortfall(event: dict, w3: Web3, vault, usdc):
    """
    Handle a LiquidityShortfall event.
    
    This is where we would:
    1. Withdraw USDC from Polymarket positions
    2. Send USDC from strategy wallet to vault
    
    Args:
        event: The event data
        w3: Web3 instance
        vault: Vault contract instance
        usdc: USDC contract instance
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
    # Here we would:
    # 1. Check current Polymarket positions
    # 2. Calculate which positions to close/sell
    # 3. Execute trades to recover USDC
    # 4. Wait for settlement
    #
    # Example pseudocode:
    # polymarket_balance = polymarket_client.get_balance()
    # if polymarket_balance >= shortfall:
    #     polymarket_client.withdraw(shortfall)
    # ==========================================================
    
    # ==========================================================
    # TODO: Send USDC from strategy wallet to vault
    # ==========================================================
    # Once we have USDC available, send it to the vault:
    #
    # strategy_account = w3.eth.account.from_key(STRATEGY_PRIVATE_KEY)
    # 
    # # Build transfer transaction
    # tx = usdc.functions.transfer(
    #     VAULT_ADDRESS,
    #     shortfall
    # ).build_transaction({
    #     'from': strategy_account.address,
    #     'nonce': w3.eth.get_transaction_count(strategy_account.address),
    #     'gas': 100000,
    #     'gasPrice': w3.eth.gas_price
    # })
    # 
    # # Sign and send
    # signed_tx = w3.eth.account.sign_transaction(tx, STRATEGY_PRIVATE_KEY)
    # tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    # print(f"   Sent {shortfall / 1e6:.2f} USDC to vault. Tx: {tx_hash.hex()}")
    # ==========================================================
    
    print(f"\n   [SKELETON] Would handle shortfall of {shortfall / 1e6:.2f} USDC")
    print(f"   [SKELETON] Polymarket withdrawal + vault transfer not implemented yet")


def listen_liquidity_shortfall(w3: Web3, vault, usdc):
    """
    Listen for LiquidityShortfall events from the vault.
    
    Polls for new events every POLL_INTERVAL seconds and handles them.
    
    Args:
        w3: Web3 instance
        vault: Vault contract instance
        usdc: USDC contract instance
    """
    print(f"\n🔍 Listening for LiquidityShortfall events...")
    print(f"   Poll interval: {POLL_INTERVAL}s")
    print(f"   Press Ctrl+C to stop\n")
    
    # Create event filter starting from latest block
    event_filter = vault.events.LiquidityShortfall.create_filter(fromBlock='latest')
    
    try:
        while True:
            # Poll for new events
            new_events = event_filter.get_new_entries()
            
            for event in new_events:
                handle_liquidity_shortfall(event, w3, vault, usdc)
            
            # Wait before next poll
            time.sleep(POLL_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\n👋 Stopped listening for events")


def main():
    """
    Main entry point for the liquidity management bot.
    """
    print("\n" + "="*60)
    print("🏦 PredictFi Sniper Vault - Liquidity Bot")
    print("="*60 + "\n")
    
    # Validate required environment variables
    required_vars = [
        ("RPC_URL", RPC_URL),
        ("VAULT_ADDRESS", VAULT_ADDRESS),
        ("USDC_ADDRESS", USDC_ADDRESS),
    ]
    
    missing = [name for name, value in required_vars if not value]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        print("   Please check your .env file")
        sys.exit(1)
    
    # Optional but warn if missing
    if not STRATEGY_PRIVATE_KEY:
        print("⚠️  STRATEGY_PRIVATE_KEY not set - cannot send transactions")
    if not STRATEGY_ADDRESS:
        print("⚠️  STRATEGY_ADDRESS not set")
    
    try:
        # Connect to chain
        w3 = connect_to_chain()
        
        # Load contracts
        vault, usdc = get_contracts(w3)
        
        # Start listening for events
        listen_liquidity_shortfall(w3, vault, usdc)
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
