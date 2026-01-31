#!/usr/bin/env python3
"""
Generate Polymarket API keys.

Usage:
1. Fill in .env with your PRIVATE_KEY
2. Run: python generate_keys.py
3. (Optional) Copy credentials to .env if you want to cache them
"""

import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def main():
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print("ERROR: PRIVATE_KEY must be set in .env and start with 0x")
        return
    
    print(f"Signature Type: {SIGNATURE_TYPE}")
    
    try:
        if SIGNATURE_TYPE == 0:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
        elif SIGNATURE_TYPE == 1:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=1, funder=FUNDER_ADDRESS)
        else:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=2, funder=FUNDER_ADDRESS)
        
        print("Generating API credentials...\n")
        
        creds = client.create_or_derive_api_creds()
        
        print("=== API Credentials ===")
        print(f"API_KEY={creds.api_key}")
        print(f"API_SECRET={creds.api_secret}")
        print(f"API_PASSPHRASE={creds.api_passphrase}")
        print("=======================\n")
        print("Note: These are auto-generated each time, no need to save them.")
        print("The trading script generates them automatically.")
        
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
