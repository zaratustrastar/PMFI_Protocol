#!/usr/bin/env python3
"""One-time script: update pARB vault arbServicerWallet to the trading wallet.

The vault was initially deployed with arbServicerWallet = deployer/navSigner
(0x59D0461ec7C4688dd3DAab7Ea903d93d109dB9E0). All deposited USDC was forwarded
there, but that address has no trading accounts on any platform.

This script calls setArbServicerWallet(0xba32aa4cF8800b0e57c79900C11B9c839C6bAeAF)
from the owner key (ARB_NAV_SIGNER_PRIVATE_KEY) on Base mainnet.

Usage:
    # Dry-run (default) — prints what would happen, sends nothing
    python3 scripts/fix_arb_servicer_wallet.py

    # Execute — actually send the transaction
    python3 scripts/fix_arb_servicer_wallet.py --execute
"""

import os
import sys
import json
import time
import struct

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEW_SERVICER_WALLET = "0xba32aa4cF8800b0e57c79900C11B9c839C6bAeAF"
BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
BASE_CHAIN_ID = 8453

EXECUTE = "--execute" in sys.argv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rpc(method, params):
    import requests
    resp = requests.post(BASE_RPC, json={
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1
    }, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return result["result"]


def to_checksum(addr: str) -> str:
    import eth_utils
    return eth_utils.to_checksum_address(addr)


def build_calldata(new_addr: str) -> str:
    """Encode setArbServicerWallet(address) calldata."""
    import eth_utils
    selector = eth_utils.keccak(text="setArbServicerWallet(address)")[:4].hex()
    padded = "000000000000000000000000" + new_addr.lower().replace("0x", "")
    return "0x" + selector + padded


def wei_to_gwei(wei: int) -> float:
    return wei / 1e9


def send_transaction(private_key: str, to: str, data: str) -> str:
    """Build, sign, and send a raw transaction. Returns tx hash."""
    from eth_account import Account
    import eth_utils

    account = Account.from_key(private_key)
    sender = account.address
    print(f"   Sender (owner):  {sender}")

    nonce_hex = rpc("eth_getTransactionCount", [sender, "latest"])
    nonce = int(nonce_hex, 16)
    print(f"   Nonce:           {nonce}")

    gas_price_hex = rpc("eth_gasPrice", [])
    gas_price = int(gas_price_hex, 16)
    # Add 20% buffer
    gas_price = int(gas_price * 1.2)
    print(f"   Gas price:       {wei_to_gwei(gas_price):.2f} gwei")

    # Estimate gas
    try:
        gas_est_hex = rpc("eth_estimateGas", [{
            "from": sender, "to": to, "data": data, "value": "0x0"
        }])
        gas_limit = int(int(gas_est_hex, 16) * 1.3)
    except Exception:
        gas_limit = 80_000
    print(f"   Gas limit:       {gas_limit}")

    tx = {
        "chainId": BASE_CHAIN_ID,
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": gas_limit,
        "to": to_checksum(to),
        "value": 0,
        "data": data,
    }

    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + raw.hex()])
    return tx_hash


def wait_for_receipt(tx_hash: str, timeout: int = 60) -> dict:
    print(f"   Waiting for receipt (up to {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
        except Exception:
            pass
        time.sleep(3)
    raise TimeoutError(f"Receipt not found within {timeout}s for {tx_hash}")


def verify_current_servicer(vault_addr: str) -> str:
    """Read current arbServicerWallet from the contract."""
    import eth_utils
    selector = eth_utils.keccak(text="arbServicerWallet()")[:4].hex()
    result = rpc("eth_call", [{"to": vault_addr, "data": "0x" + selector}, "latest"])
    if result and result != "0x":
        return "0x" + result[-40:]
    return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("pARB Vault — setArbServicerWallet fix script")
    print("=" * 60)

    owner_key = os.environ.get("ARB_NAV_SIGNER_PRIVATE_KEY", "")
    if not owner_key:
        print("❌ ARB_NAV_SIGNER_PRIVATE_KEY not set in environment")
        sys.exit(1)

    vault_addr = os.environ.get("ARB_VAULT_V1_ADDRESS", "")
    if not vault_addr:
        print("❌ ARB_VAULT_V1_ADDRESS not set in environment")
        sys.exit(1)

    print(f"\nVault:            {vault_addr}")
    print(f"New servicer:     {NEW_SERVICER_WALLET}")
    print(f"RPC:              {BASE_RPC}")
    print(f"Mode:             {'EXECUTE' if EXECUTE else 'DRY-RUN'}")

    current = "unknown"
    try:
        current = verify_current_servicer(vault_addr)
        print(f"\nCurrent arbServicerWallet: {current}")
        if current.lower() == NEW_SERVICER_WALLET.lower():
            print("✅ Already set to the correct wallet — nothing to do.")
            sys.exit(0)
    except Exception as e:
        print(f"⚠️  Could not read current servicer wallet: {e}")

    calldata = build_calldata(NEW_SERVICER_WALLET)
    print(f"\nCalldata: {calldata}")

    if not EXECUTE:
        print("\n[DRY-RUN] No transaction sent. Re-run with --execute to submit.")
        print(f"\nAfter sending, also update .env on the VPS:")
        print(f"  ARB_SERVICER_WALLET={NEW_SERVICER_WALLET}")
        return

    # -----------------------------------------------------------------------
    # Confirmation prompt — require explicit "YES" before broadcasting
    # -----------------------------------------------------------------------
    from eth_account import Account
    owner_account = Account.from_key(owner_key)
    print("\n" + "=" * 60)
    print("CONFIRMATION REQUIRED")
    print("=" * 60)
    print(f"  Chain:            Base mainnet (chainId {BASE_CHAIN_ID})")
    print(f"  RPC:              {BASE_RPC}")
    print(f"  Vault:            {vault_addr}")
    print(f"  Owner (sender):   {owner_account.address}")
    print(f"  Current wallet:   {current}")
    print(f"  New wallet:       {NEW_SERVICER_WALLET}")
    print(f"  Action:           setArbServicerWallet({NEW_SERVICER_WALLET})")
    print("=" * 60)
    print("\nType YES (all caps) to broadcast, anything else to abort:")
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "YES":
        print("Aborted — no transaction sent.")
        sys.exit(0)

    print("\n⚡ Sending transaction...")
    try:
        tx_hash = send_transaction(owner_key, vault_addr, calldata)
        print(f"   TX hash: {tx_hash}")

        receipt = wait_for_receipt(tx_hash)
        status = int(receipt.get("status", "0x0"), 16)
        if status == 1:
            print(f"\n✅ Success! arbServicerWallet updated to {NEW_SERVICER_WALLET}")
            print(f"\nNow update .env on the VPS:")
            print(f"  ARB_SERVICER_WALLET={NEW_SERVICER_WALLET}")
        else:
            print(f"\n❌ Transaction reverted (status=0). Check Basescan: https://basescan.org/tx/{tx_hash}")
    except Exception as e:
        print(f"\n❌ Error sending transaction: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
