#!/usr/bin/env python3
"""
Relay.link Bridge - Polygon → Base USDC Bridge

Uses Relay.link API for fast, low-fee bridging.
Requires POL for gas on Polygon (no gasless option for source chain).

Flow:
1. Get quote from Relay API
2. Sign and submit deposit tx on Polygon
3. Poll status until complete on Base
"""

import os
import time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

import requests
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv

load_dotenv()

RELAY_API_URL = "https://api.relay.link"
POLYGON_CHAIN_ID = 137
BASE_CHAIN_ID = 8453

USDC_E_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

MAX_POLL_ATTEMPTS = 60
POLL_INTERVAL_SECONDS = 10

@dataclass
class BridgeQuote:
    """Quote for bridging USDC from Polygon to Base."""
    request_id: str
    amount_in: int
    amount_out: int
    fee_usd: float
    tx_data: Dict
    time_estimate_seconds: int

@dataclass
class BridgeResult:
    """Result of a bridge transaction."""
    success: bool
    request_id: str
    amount_received: float
    tx_hash: str
    error: Optional[str] = None


def get_bridge_quote(
    amount_usdc: float,
    sender_address: str,
    recipient_address: str
) -> Optional[BridgeQuote]:
    """
    Get a quote for bridging USDC from Polygon to Base.
    
    Args:
        amount_usdc: Amount in USDC (e.g., 100.0 for $100)
        sender_address: Address sending on Polygon
        recipient_address: Address receiving on Base (treasury)
    
    Returns:
        BridgeQuote with transaction data, or None if failed
    """
    amount_6dec = int(amount_usdc * 1e6)
    
    payload = {
        "user": sender_address,
        "originChainId": POLYGON_CHAIN_ID,
        "destinationChainId": BASE_CHAIN_ID,
        "originCurrency": USDC_E_POLYGON,
        "destinationCurrency": USDC_BASE,
        "amount": str(amount_6dec),
        "tradeType": "EXACT_INPUT",
        "recipient": recipient_address,
    }
    
    try:
        response = requests.post(
            f"{RELAY_API_URL}/quote",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        
        if response.status_code != 200:
            print(f"❌ Relay quote failed: {response.status_code} - {response.text[:200]}")
            return None
        
        data = response.json()
        
        steps = data.get("steps", [])
        if not steps:
            print("❌ No steps in Relay quote response")
            return None
        
        deposit_step = steps[0]
        items = deposit_step.get("items", [])
        if not items:
            print("❌ No items in deposit step")
            return None
        
        tx_data = items[0].get("data", {})
        request_id = deposit_step.get("requestId", "")
        
        fees = data.get("fees", {})
        gas_fee = fees.get("gas", {})
        fee_usd = float(gas_fee.get("amountUsd", 0))
        
        details = data.get("details", {})
        currency_out = details.get("currencyOut", {})
        amount_out = int(currency_out.get("amount", 0))
        time_estimate = details.get("timeEstimate", 120)
        
        return BridgeQuote(
            request_id=request_id,
            amount_in=amount_6dec,
            amount_out=amount_out,
            fee_usd=fee_usd,
            tx_data=tx_data,
            time_estimate_seconds=time_estimate
        )
        
    except Exception as e:
        print(f"❌ Error getting Relay quote: {e}")
        return None


def submit_bridge_transaction(
    quote: BridgeQuote,
    private_key: str,
    w3_polygon: Web3
) -> Optional[str]:
    """
    Sign and submit the bridge deposit transaction on Polygon.
    
    Args:
        quote: BridgeQuote from get_bridge_quote
        private_key: Private key for signing
        w3_polygon: Web3 instance connected to Polygon
    
    Returns:
        Transaction hash if successful, None otherwise
    """
    try:
        account = Account.from_key(private_key)
        sender = account.address
        
        tx_data = quote.tx_data
        
        nonce = w3_polygon.eth.get_transaction_count(sender)
        gas_price = w3_polygon.eth.gas_price
        
        to_address = tx_data.get("to", "")
        if not to_address:
            print("❌ No 'to' address in quote tx data")
            return None
        data = tx_data.get("data", "0x")
        value = int(tx_data.get("value", 0))
        
        tx = {
            "from": sender,
            "to": Web3.to_checksum_address(to_address),
            "data": data,
            "value": value,
            "nonce": nonce,
            "gas": 300000,
            "gasPrice": gas_price,
            "chainId": POLYGON_CHAIN_ID,
        }
        
        if tx_data.get("maxFeePerGas"):
            tx["maxFeePerGas"] = int(tx_data["maxFeePerGas"])
            tx["maxPriorityFeePerGas"] = int(tx_data.get("maxPriorityFeePerGas", gas_price // 10))
            del tx["gasPrice"]
        
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3_polygon.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"📤 Bridge tx submitted: {tx_hash.hex()}")
        
        receipt = w3_polygon.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.get("status", 0) != 1:  # type: ignore
            print(f"❌ Bridge tx failed on Polygon")
            return None
        
        print(f"✅ Bridge deposit confirmed on Polygon")
        return tx_hash.hex()
        
    except Exception as e:
        print(f"❌ Error submitting bridge tx: {e}")
        return None


def poll_bridge_status(request_id: str) -> Tuple[bool, str]:
    """
    Poll Relay API for bridge completion status.
    
    Args:
        request_id: The requestId from the quote
    
    Returns:
        (success, status_message)
    """
    print(f"⏳ Waiting for bridge completion (requestId: {request_id[:20]}...)")
    
    for attempt in range(MAX_POLL_ATTEMPTS):
        try:
            response = requests.get(
                f"{RELAY_API_URL}/intents/status/v2",
                params={"requestId": request_id},
                timeout=15
            )
            
            if response.status_code != 200:
                print(f"   Poll attempt {attempt + 1}: status check failed")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            
            data = response.json()
            status = data.get("status", "unknown")
            
            if status == "success" or status == "completed":
                print(f"✅ Bridge complete!")
                return True, "Bridge completed successfully"
            
            if status == "failed" or status == "refunded":
                error = data.get("error", "Unknown error")
                print(f"❌ Bridge failed: {error}")
                return False, f"Bridge failed: {error}"
            
            print(f"   Status: {status} (attempt {attempt + 1}/{MAX_POLL_ATTEMPTS})")
            time.sleep(POLL_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"   Poll error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
    
    return False, "Bridge status polling timed out"


def bridge_usdc_polygon_to_base(
    amount_usdc: float,
    sender_address: str,
    recipient_address: str,
    private_key: str,
    polygon_rpc_url: str
) -> BridgeResult:
    """
    Complete bridge flow: quote → submit → poll status.
    
    Args:
        amount_usdc: Amount to bridge in USDC
        sender_address: Address on Polygon with USDC
        recipient_address: Address on Base to receive USDC
        private_key: Private key for Polygon wallet
        polygon_rpc_url: Polygon RPC endpoint
    
    Returns:
        BridgeResult with success status and details
    """
    print(f"\n🌉 BRIDGE: ${amount_usdc:.2f} USDC from Polygon → Base")
    print(f"   From: {sender_address}")
    print(f"   To: {recipient_address}")
    
    quote = get_bridge_quote(amount_usdc, sender_address, recipient_address)
    if not quote:
        return BridgeResult(
            success=False,
            request_id="",
            amount_received=0,
            tx_hash="",
            error="Failed to get bridge quote"
        )
    
    print(f"   Quote: receive ~${quote.amount_out / 1e6:.2f} USDC")
    print(f"   Fee: ~${quote.fee_usd:.4f}")
    print(f"   Est time: {quote.time_estimate_seconds}s")
    
    w3 = Web3(Web3.HTTPProvider(polygon_rpc_url))
    if not w3.is_connected():
        return BridgeResult(
            success=False,
            request_id=quote.request_id,
            amount_received=0,
            tx_hash="",
            error="Failed to connect to Polygon RPC"
        )
    
    tx_hash = submit_bridge_transaction(quote, private_key, w3)
    if not tx_hash:
        return BridgeResult(
            success=False,
            request_id=quote.request_id,
            amount_received=0,
            tx_hash="",
            error="Failed to submit bridge transaction"
        )
    
    success, message = poll_bridge_status(quote.request_id)
    
    return BridgeResult(
        success=success,
        request_id=quote.request_id,
        amount_received=quote.amount_out / 1e6 if success else 0,
        tx_hash=tx_hash,
        error=None if success else message
    )


def check_usdc_approval(
    wallet_address: str,
    spender_address: str,
    amount: int,
    w3_polygon: Web3
) -> bool:
    """Check if USDC is approved for the bridge contract."""
    usdc_abi = [
        {"inputs": [{"type": "address"}, {"type": "address"}], "name": "allowance", 
         "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    ]
    
    usdc = w3_polygon.eth.contract(
        address=Web3.to_checksum_address(USDC_E_POLYGON),
        abi=usdc_abi
    )
    
    try:
        allowance = usdc.functions.allowance(wallet_address, spender_address).call()
        return allowance >= amount
    except Exception as e:
        print(f"❌ Error checking allowance: {e}")
        return False


def approve_usdc_for_bridge(
    spender_address: str,
    amount: int,
    private_key: str,
    w3_polygon: Web3
) -> bool:
    """Approve USDC spending for the bridge contract."""
    usdc_abi = [
        {"inputs": [{"type": "address"}, {"type": "uint256"}], "name": "approve",
         "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    ]
    
    try:
        account = Account.from_key(private_key)
        usdc = w3_polygon.eth.contract(
            address=Web3.to_checksum_address(USDC_E_POLYGON),
            abi=usdc_abi
        )
        
        nonce = w3_polygon.eth.get_transaction_count(account.address)
        gas_price = w3_polygon.eth.gas_price
        
        tx = usdc.functions.approve(
            Web3.to_checksum_address(spender_address),
            amount
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": gas_price,
            "chainId": POLYGON_CHAIN_ID,
        })
        
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3_polygon.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"📤 Approval tx: {tx_hash.hex()}")
        
        receipt = w3_polygon.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        
        if receipt.get("status", 0) == 1:  # type: ignore
            print(f"✅ USDC approved for bridge")
            return True
        else:
            print(f"❌ Approval failed")
            return False
            
    except Exception as e:
        print(f"❌ Error approving USDC: {e}")
        return False


if __name__ == "__main__":
    POLYGON_RPC = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    TREASURY = os.getenv("TREASURY_ADDRESS", "")
    
    if not PRIVATE_KEY:
        print("❌ POLYMARKET_PRIVATE_KEY not set")
        exit(1)
    
    account = Account.from_key(PRIVATE_KEY)
    print(f"Wallet: {account.address}")
    print(f"Treasury: {TREASURY or 'Not set'}")
    
    quote = get_bridge_quote(
        amount_usdc=10.0,
        sender_address=account.address,
        recipient_address=TREASURY or account.address
    )
    
    if quote:
        print(f"\n✅ Quote received:")
        print(f"   Input: ${quote.amount_in / 1e6:.2f}")
        print(f"   Output: ${quote.amount_out / 1e6:.2f}")
        print(f"   Fee: ${quote.fee_usd:.4f}")
        print(f"   Time: ~{quote.time_estimate_seconds}s")
    else:
        print("❌ Failed to get quote")
