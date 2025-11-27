"""
Polymarket Market Utilities
Get token IDs and market information from market slugs or condition IDs
"""

import json
import requests
from typing import Optional, Dict, List


def get_market_info_from_job(job: Dict) -> Optional[Dict]:
    """
    Get market info directly from job data (no API call needed).
    
    This is the preferred method when trading from the job queue, since
    the job already contains the condition_id, token IDs, etc.
    
    Args:
        job: Job dict with condition_id, clob_token_ids, outcomes, etc.
        
    Returns:
        Dictionary with market data and token IDs, or None if missing data
    """
    condition_id = job.get("market_id", "")  # condition_id is stored as market_id
    clob_token_ids_str = job.get("clob_token_ids", "[]")
    outcomes_str = job.get("outcomes", "[]")
    question = job.get("question", "")
    event_slug = job.get("event_slug", "")
    
    if not condition_id:
        print(f"❌ No condition_id in job")
        return None
    
    # Parse token IDs
    try:
        clob_token_ids = json.loads(clob_token_ids_str) if clob_token_ids_str else []
    except:
        clob_token_ids = []
    
    # Parse outcomes
    try:
        outcomes = json.loads(outcomes_str) if outcomes_str else []
    except:
        outcomes = []
    
    if len(clob_token_ids) < 2:
        print(f"❌ Missing token IDs for {question[:40]}...")
        print(f"   clob_token_ids_str: {clob_token_ids_str}")
        return None
    
    return {
        "slug": event_slug,
        "question": question,
        "market_id": condition_id,
        "condition_id": condition_id,
        "yes_token_id": clob_token_ids[0],
        "no_token_id": clob_token_ids[1],
        "outcomes": outcomes,
        "active": True,
        "closed": False,
    }


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


def get_token_ids(market_data: Dict, condition_id: Optional[str] = None) -> Dict[str, str]:
    """
    Extract YES and NO token IDs from market data.
    
    Args:
        market_data: Market data from Gamma API (event level)
        condition_id: If provided, find this specific sub-market. Otherwise, use first market.
        
    Returns:
        Dictionary with 'yes' and 'no' token IDs
    """
    token_ids = {"yes": "", "no": ""}
    
    # Handle nested structure - event.markets[] contains sub-markets
    markets = market_data.get("markets", [])
    if not markets:
        return token_ids
    
    # Find the right market
    market = None
    if condition_id:
        # Find specific sub-market by condition_id
        for m in markets:
            m_condition_id = m.get("conditionId", m.get("condition_id", ""))
            if m_condition_id == condition_id:
                market = m
                break
        
        if not market:
            print(f"⚠️  Sub-market with condition_id {condition_id[:16]}... not found")
            # Fall back to first market
            market = markets[0]
    else:
        # Use first market (backwards compatible)
        market = markets[0]
    
    # Extract clobTokenIds (JSON string array)
    clob_token_ids_str = market.get("clobTokenIds", "[]")
    try:
        clob_token_ids = json.loads(clob_token_ids_str) if isinstance(clob_token_ids_str, str) else clob_token_ids_str
    except:
        clob_token_ids = []
    
    # Map tokens to outcomes
    # For binary markets: first token = YES (or first outcome), second = NO (or second outcome)
    if len(clob_token_ids) >= 2:
        token_ids["yes"] = clob_token_ids[0]
        token_ids["no"] = clob_token_ids[1]
    
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
