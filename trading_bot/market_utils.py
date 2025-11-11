"""
Polymarket Market Utilities
Get token IDs and market information from market slugs
"""

import requests
from typing import Optional, Dict, List


def get_market_by_slug_or_id(identifier: str) -> Optional[Dict]:
    """
    Get market data from Polymarket Gamma API by slug OR event ID
    
    Args:
        identifier: Market slug (e.g., "btc-90k-dec-31") OR event ID (e.g., "677402")
        
    Returns:
        Market data dictionary or None if not found
    """
    # Try by ID first (if it's numeric-looking)
    if identifier.isdigit():
        url = f"https://gamma-api.polymarket.com/events/{identifier}"
    else:
        url = f"https://gamma-api.polymarket.com/events?slug={identifier}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Handle different response formats
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        elif isinstance(data, dict) and data.get('id'):
            return data
        else:
            print(f"No market found for: {identifier}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Error fetching market {identifier}: {e}")
        return None


def get_token_ids(market_data: Dict) -> Dict[str, str]:
    """
    Extract YES and NO token IDs from market data
    
    Args:
        market_data: Market data from Gamma API
        
    Returns:
        Dictionary with 'yes' and 'no' token IDs
    """
    tokens = market_data.get("tokens", [])
    
    token_ids = {}
    
    for token in tokens:
        outcome = token.get("outcome", "").lower()
        token_id = token.get("token_id", "")
        
        if outcome == "yes":
            token_ids["yes"] = token_id
        elif outcome == "no":
            token_ids["no"] = token_id
    
    return token_ids


def get_market_info(identifier: str) -> Optional[Dict]:
    """
    Get complete market information including token IDs
    
    Args:
        identifier: Market slug OR event ID
        
    Returns:
        Dictionary with market data and token IDs
    """
    market_data = get_market_by_slug_or_id(identifier)
    
    if not market_data:
        return None
    
    token_ids = get_token_ids(market_data)
    
    return {
        "slug": market_data.get("slug", identifier),
        "question": market_data.get("question", market_data.get("title", "")),
        "market_id": market_data.get("id", ""),
        "condition_id": market_data.get("condition_id", ""),
        "yes_token_id": token_ids.get("yes", ""),
        "no_token_id": token_ids.get("no", ""),
        "active": market_data.get("active", False),
        "closed": market_data.get("closed", False),
    }


if __name__ == "__main__":
    # Test with a market slug
    test_slug = "bitcoin-above-100k-on-december-31"
    info = get_market_info(test_slug)
    
    if info:
        print(f"Market: {info['question']}")
        print(f"YES Token ID: {info['yes_token_id']}")
        print(f"NO Token ID: {info['no_token_id']}")
    else:
        print("Market not found")
