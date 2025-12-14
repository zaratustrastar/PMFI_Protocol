#!/usr/bin/env python3
"""
Test script for Liquidity Keeper components.

Tests:
1. Base RPC connection
2. Treasury balance reading
3. Kill switch checking
4. Hourly limit tracking
5. PM client
6. Vault state reading
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web3 import Web3


def test_base_connection():
    """Test connection to Base RPC."""
    print("\n🔌 Testing Base RPC connection...")
    
    base_rpc = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
    w3 = Web3(Web3.HTTPProvider(base_rpc))
    
    if w3.is_connected():
        block = w3.eth.block_number
        print(f"   ✅ Connected to Base (block {block})")
        return True
    else:
        print("   ❌ Cannot connect to Base RPC")
        return False


def test_kill_switches():
    """Test kill switch checking."""
    print("\n🔍 Testing kill switch check...")
    
    from liquidity_keeper import check_kill_switches
    
    is_safe, reason = check_kill_switches()
    
    print(f"   Status: {'✅ Safe' if is_safe else '🚫 Stopped'}")
    print(f"   Reason: {reason}")
    
    return True


def test_hourly_limit():
    """Test hourly limit tracking."""
    print("\n⏱️ Testing hourly limit...")
    
    from liquidity_keeper import check_hourly_limit, record_liquidation, MAX_HOURLY_LIQUIDATION_USDC
    
    allowed, max_amount = check_hourly_limit(1000.0)
    print(f"   Can liquidate $1000? {allowed} (max: ${max_amount:.2f})")
    
    record_liquidation(100.0)
    
    allowed, max_amount = check_hourly_limit(1000.0)
    print(f"   After $100 recorded - max now: ${max_amount:.2f}")
    
    return True


def test_pm_client():
    """Test Polymarket client."""
    print("\n💰 Testing Polymarket client...")
    
    pm_address = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    if not pm_address:
        print("   ⚠️  POLYMARKET_PROXY_ADDRESS not set, skipping")
        return True
    
    try:
        from liquidity_keeper import PolymarketClient
        
        pm_client = PolymarketClient(pm_address)
        
        cash = pm_client.fetch_cash_balance()
        print(f"   PM Cash balance: ${cash:.2f}")
        
        positions = pm_client.fetch_positions()
        print(f"   PM Positions: {len(positions)}")
        
        for pos in positions[:3]:
            print(f"      • {pos['outcome']}: {pos['size']:.1f} (${pos['current_value']:.2f})")
        
        return True
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False


def test_treasury():
    """Test treasury balance reading."""
    print("\n💎 Testing treasury balance...")
    
    from liquidity_keeper import (
        VAULT_V5_ABI, ERC20_ABI, USDC_BASE_ADDRESS,
        TREASURY_ADDRESS, VAULT_V5_ADDRESS
    )
    
    base_rpc = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
    w3 = Web3(Web3.HTTPProvider(base_rpc))
    
    if not w3.is_connected():
        print("   ❌ Cannot connect to Base")
        return False
    
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_BASE_ADDRESS),
        abi=ERC20_ABI
    )
    
    treasury_addr = TREASURY_ADDRESS or os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    if treasury_addr:
        try:
            balance = usdc.functions.balanceOf(
                Web3.to_checksum_address(treasury_addr)
            ).call()
            print(f"   Treasury balance: ${balance/1e6:.2f} USDC")
        except Exception as e:
            print(f"   ⚠️  Could not read treasury: {e}")
    else:
        print("   ⚠️  No treasury address configured")
    
    return True


def test_vault_state():
    """Test reading vault state."""
    print("\n📊 Testing vault state reading...")
    
    vault_addr = os.getenv("VAULT_V5_ADDRESS", "")
    if not vault_addr:
        print("   ⚠️  VAULT_V5_ADDRESS not set, skipping")
        return True
    
    from liquidity_keeper import (
        VAULT_V5_ABI, ERC20_ABI, USDC_BASE_ADDRESS, NAV_PRECISION
    )
    
    base_rpc = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL", "https://mainnet.base.org")
    w3 = Web3(Web3.HTTPProvider(base_rpc))
    
    if not w3.is_connected():
        print("   ❌ Cannot connect to Base")
        return False
    
    try:
        vault = w3.eth.contract(
            address=Web3.to_checksum_address(vault_addr),
            abi=VAULT_V5_ABI
        )
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_BASE_ADDRESS),
            abi=ERC20_ABI
        )
        
        pending = vault.functions.getPendingWithdrawalShares().call()
        buffer = usdc.functions.balanceOf(vault_addr).call()
        state = vault.functions.getVaultState().call()
        last_nav = state[0]
        
        pending_usdc = (pending * last_nav) / NAV_PRECISION / 1e6 if last_nav > 0 else 0
        shortfall = max(0, (pending * last_nav // NAV_PRECISION) - buffer) / 1e6
        
        print(f"   ✅ Vault state read successfully")
        print(f"      Pending shares: ${pending_usdc:.2f}")
        print(f"      Buffer: ${buffer/1e6:.2f}")
        print(f"      Shortfall: ${shortfall:.2f}")
        print(f"      NAV: {last_nav / NAV_PRECISION:.6f}")
        return True
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("🧪 LIQUIDITY KEEPER TESTS (Treasury Architecture)")
    print("=" * 60)
    
    results = []
    
    results.append(("Base Connection", test_base_connection()))
    results.append(("Kill Switches", test_kill_switches()))
    results.append(("Hourly Limit", test_hourly_limit()))
    results.append(("PM Client", test_pm_client()))
    results.append(("Treasury", test_treasury()))
    results.append(("Vault State", test_vault_state()))
    
    print("\n" + "=" * 60)
    print("📋 TEST RESULTS")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1
    
    print(f"\n   Total: {passed} passed, {failed} failed")
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
