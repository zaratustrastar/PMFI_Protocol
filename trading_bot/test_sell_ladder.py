#!/usr/bin/env python3
"""
Test script for sell ladder consolidation logic
Tests different position sizes to verify proper tier calculation
"""

def test_sell_ladder_logic(buy_size: float, buy_price: float = 0.02):
    """Simulate sell ladder calculation without placing orders"""
    
    MIN_SHARES_PER_ORDER = 5
    SELL_RESERVE_RATIO = 0.10
    SELL_LADDER_CONFIG = [
        {"ratio": 0.30, "profit_multiple": 3},
        {"ratio": 0.30, "profit_multiple": 4},
        {"ratio": 0.30, "profit_multiple": 5},
    ]
    
    print(f"\n{'='*60}")
    print(f"TEST: {buy_size} shares @ ${buy_price:.4f}")
    print(f"{'='*60}")
    
    reserve_ratio = SELL_RESERVE_RATIO
    reserved_size = buy_size * reserve_ratio
    sellable_size = buy_size - reserved_size
    
    print(f"   Total shares: {buy_size:.2f}")
    print(f"   Reserved (moonbag): {reserved_size:.2f} ({reserve_ratio*100:.0f}%)")
    print(f"   Sellable: {sellable_size:.2f}")
    
    # Calculate total ratio from ladder config
    total_ratio = sum(tier["ratio"] for tier in SELL_LADDER_CONFIG)
    
    # Pre-calculate all tier sizes
    tier_sizes = []
    for tier in SELL_LADDER_CONFIG:
        tier_size = sellable_size * (tier["ratio"] / total_ratio)
        tier_sizes.append({
            "size": tier_size,
            "profit_multiple": tier["profit_multiple"],
            "meets_minimum": tier_size >= MIN_SHARES_PER_ORDER
        })
    
    valid_tiers = sum(1 for t in tier_sizes if t["meets_minimum"])
    
    print(f"\n   Tier analysis: {valid_tiers}/{len(SELL_LADDER_CONFIG)} tiers meet {MIN_SHARES_PER_ORDER} share minimum")
    
    # CONSOLIDATION LOGIC
    if valid_tiers == 0:
        if sellable_size >= MIN_SHARES_PER_ORDER:
            print(f"   🔄 CONSOLIDATION: Single sell @ 3x for {sellable_size:.2f} shares")
            consolidated_config = [{"size": sellable_size, "profit_multiple": 3}]
        else:
            print(f"   ❌ POSITION TOO SMALL: {sellable_size:.2f} < {MIN_SHARES_PER_ORDER}")
            return []
    elif valid_tiers < len(SELL_LADDER_CONFIG):
        small_tier_total = sum(t["size"] for t in tier_sizes if not t["meets_minimum"])
        valid_tier_list = [t for t in tier_sizes if t["meets_minimum"]]
        
        redistribution_per_tier = small_tier_total / len(valid_tier_list)
        
        consolidated_config = []
        for t in valid_tier_list:
            consolidated_config.append({
                "size": t["size"] + redistribution_per_tier,
                "profit_multiple": t["profit_multiple"]
            })
        
        print(f"   🔄 REDISTRIBUTION: {small_tier_total:.2f} shares from {len(SELL_LADDER_CONFIG) - valid_tiers} small tier(s)")
    else:
        consolidated_config = tier_sizes
        print(f"   ✅ ALL TIERS VALID - using standard ladder")
    
    # Output the final sell orders
    print(f"\n   SELL ORDERS TO PLACE:")
    total_sell_shares = 0
    for i, tier in enumerate(consolidated_config):
        tier_size = tier["size"]
        profit_multiple = tier["profit_multiple"]
        sell_price = buy_price * profit_multiple
        
        if tier_size >= MIN_SHARES_PER_ORDER:
            print(f"      Tier {i+1}: {tier_size:.2f} shares @ ${sell_price:.4f} ({profit_multiple}x)")
            total_sell_shares += tier_size
        else:
            print(f"      Tier {i+1}: SKIP - {tier_size:.2f} shares < {MIN_SHARES_PER_ORDER} min")
    
    print(f"\n   SUMMARY:")
    print(f"      Shares selling: {total_sell_shares:.2f}")
    print(f"      Shares reserved: {reserved_size:.2f}")
    print(f"      Total: {total_sell_shares + reserved_size:.2f}")
    
    return consolidated_config


if __name__ == "__main__":
    # Test scenarios
    test_sell_ladder_logic(15)   # Edge case - tiers barely too small
    test_sell_ladder_logic(18)   # Small position - some tiers work
    test_sell_ladder_logic(25)   # Medium - all tiers should work
    test_sell_ladder_logic(44)   # User's example
    test_sell_ladder_logic(100)  # Large position
    test_sell_ladder_logic(8)    # Very small - consolidate to single
    test_sell_ladder_logic(4)    # Too small - no sells
