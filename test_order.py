#!/usr/bin/env python3
"""Test order placement - non-interactive"""

import os
import sys
import time
import json
import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_API = "https://gamma-api.polymarket.com"

def find_active_market():
    """Find active BTC 15-minute market."""
    now = int(time.time())
    current_slot = (now // 900) * 900
    slots_to_try = [current_slot, current_slot - 900, current_slot + 900]
    
    for slot in slots_to_try:
        slug = f"btc-updown-15m-{slot}"
        try:
            response = requests.get(f"{GAMMA_API}/events?slug={slug}", timeout=10)
            if response.status_code != 200:
                continue
            events = response.json()
            if events and len(events) > 0:
                event = events[0]
                if event.get("active") and not event.get("closed"):
                    markets = event.get("markets", [])
                    for market in markets:
                        if market.get("active") and not market.get("closed"):
                            clob_token_ids = market.get("clobTokenIds", [])
                            outcomes = market.get("outcomes", [])
                            if isinstance(clob_token_ids, str):
                                clob_token_ids = json.loads(clob_token_ids)
                            if isinstance(outcomes, str):
                                outcomes = json.loads(outcomes)
                            if len(clob_token_ids) >= 2:
                                up_index = outcomes.index("Up") if "Up" in outcomes else 0
                                down_index = outcomes.index("Down") if "Down" in outcomes else 1
                                return {
                                    "slug": slug,
                                    "up_token_id": clob_token_ids[up_index],
                                    "down_token_id": clob_token_ids[down_index],
                                    "neg_risk": market.get("negRisk", True),
                                }
        except Exception as e:
            continue
    return None

def main():
    print("=== Polymarket Order Test ===\n")
    
    # Validate
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print("ERROR: PRIVATE_KEY not set")
        return
    
    print(f"Signature Type: {SIGNATURE_TYPE}")
    print(f"Private Key: {PRIVATE_KEY[:10]}...{PRIVATE_KEY[-6:]}")
    
    # Find market
    print("\nSearching for active market...")
    market = find_active_market()
    if not market:
        print("ERROR: No active market found")
        return
    
    print(f"Found: {market['slug']}")
    print(f"UP Token: {market['up_token_id'][:30]}...")
    print(f"negRisk: {market['neg_risk']}")
    
    # Init client
    print("\nInitializing client...")
    try:
        if SIGNATURE_TYPE == 0:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
        elif SIGNATURE_TYPE == 1:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=1, funder=FUNDER_ADDRESS)
        else:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=2, funder=FUNDER_ADDRESS)
        
        print("Generating API credentials...")
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        
        print(f"API Key: {creds.api_key[:20]}...")
        print(f"API Secret: {creds.api_secret[:20]}...")
        print("Client initialized!")
        
    except Exception as e:
        print(f"ERROR initializing client: {e}")
        return
    
    # Place order
    print("\n=== Placing BUY UP order ($5 @ $0.99) ===")
    try:
        order_args = OrderArgs(
            price=0.99,
            size=5.0,
            side=BUY,
            token_id=market["up_token_id"],
        )
        
        print("Creating signed order...")
        
        # py-clob-client handles neg_risk automatically based on market
        signed_order = client.create_order(order_args)
        print(f"Order created successfully")
        
        print("\nPosting order (FOK)...")
        result = client.post_order(signed_order, OrderType.FOK)
        
        print(f"\n=== RESULT ===")
        print(json.dumps(result, indent=2))
        
        if result.get("success"):
            print("\nSUCCESS! Order executed.")
        else:
            print(f"\nOrder failed: {result.get('errorMsg', 'Unknown')}")
        
    except Exception as e:
        print(f"ERROR placing order: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
