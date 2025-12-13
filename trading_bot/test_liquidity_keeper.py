#!/usr/bin/env python3
"""
Test script for Liquidity Keeper components.

Tests:
1. Vault state reading from Base
2. Kill switch checking
3. Hourly limit tracking
4. Shortfall calculation
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from liquidity_keeper import (
    VAULT_V5_ADDRESS,
    BASE_RPC_URL,
    POLYMARKET_PROXY_ADDRESS,
    BOT_V5_URL,
    NAV_PRECISION,
    check_kill_switches,
    check_hourly_limit,
    record_liquidation,
    read_vault_state,
    PolymarketClient,
)

from web3 import Web3


def test_base_connection():
    """Test connection to Base RPC."""
    print("\n🔌 Testing Base RPC connection...")
    
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    
    if w3.is_connected():
        block = w3.eth.block_number
        print(f"   ✅ Connected to Base (block {block})")
        return True
    else:
        print("   ❌ Cannot connect to Base RPC")
        return False


def test_vault_state():
    """Test reading vault state."""
    print("\n📊 Testing vault state reading...")
    
    if not VAULT_V5_ADDRESS:
        print("   ⚠️  VAULT_V5_ADDRESS not set, skipping")
        return True
    
    from liquidity_keeper import w3_base, vault_contract, usdc_base, VAULT_V5_ABI, ERC20_ABI, USDC_BASE_ADDRESS
    
    global w3_base, vault_contract, usdc_base
    
    w3_base = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    
    vault_contract = w3_base.eth.contract(
        address=Web3.to_checksum_address(VAULT_V5_ADDRESS),
        abi=VAULT_V5_ABI
    )
    
    usdc_base = w3_base.eth.contract(
        address=Web3.to_checksum_address(USDC_BASE_ADDRESS),
        abi=ERC20_ABI
    )
    
    try:
        import liquidity_keeper
        liquidity_keeper.w3_base = w3_base
        liquidity_keeper.vault_contract = vault_contract
        liquidity_keeper.usdc_base = usdc_base
        
        state = read_vault_state()
        
        if state:
            print(f"   ✅ Vault state read successfully")
            print(f"      Pending shares: ${state.pending_shares_usdc:.2f}")
            print(f"      Buffer: ${state.buffer_usdc:.2f}")
            print(f"      Shortfall: ${state.shortfall_usdc:.2f}")
            print(f"      NAV: {state.last_nav / NAV_PRECISION:.6f}")
            return True
        else:
            print("   ❌ Could not read vault state")
            return False
            
    except Exception as e:
        print(f"   ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_kill_switches():
    """Test kill switch checking."""
    print("\n🔍 Testing kill switch check...")
    
    is_safe, reason = check_kill_switches()
    
    print(f"   Status: {'✅ Safe' if is_safe else '🚫 Stopped'}")
    print(f"   Reason: {reason}")
    
    return True


def test_hourly_limit():
    """Test hourly limit tracking."""
    print("\n⏱️ Testing hourly limit...")
    
    allowed, max_amount = check_hourly_limit(1000.0)
    print(f"   Can liquidate $1000? {allowed} (max: ${max_amount:.2f})")
    
    record_liquidation(100.0)
    
    allowed, max_amount = check_hourly_limit(1000.0)
    print(f"   After $100 recorded - max now: ${max_amount:.2f}")
    
    return True


def test_pm_client():
    """Test Polymarket client."""
    print("\n💰 Testing Polymarket client...")
    
    if not POLYMARKET_PROXY_ADDRESS:
        print("   ⚠️  POLYMARKET_PROXY_ADDRESS not set, skipping")
        return True
    
    try:
        pm_client = PolymarketClient(POLYMARKET_PROXY_ADDRESS)
        
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


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("🧪 LIQUIDITY KEEPER TESTS")
    print("=" * 60)
    
    results = []
    
    results.append(("Base Connection", test_base_connection()))
    results.append(("Kill Switches", test_kill_switches()))
    results.append(("Hourly Limit", test_hourly_limit()))
    results.append(("PM Client", test_pm_client()))
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
