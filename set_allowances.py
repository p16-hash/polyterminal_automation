#!/usr/bin/env python3
"""
Set Polymarket contract allowances.

Run this ONCE before trading to approve Polymarket contracts
to spend your USDC and CTF tokens.

Usage:
1. Fill in .env with your PRIVATE_KEY
2. Make sure you have some POL (MATIC) for gas
3. Run: python set_allowances.py
"""

import os
import time
from dotenv import load_dotenv
from web3 import Web3
from web3.constants import MAX_INT
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
RPC_URL = "https://rpc.ankr.com/polygon/cc878ed5ff293701a1d80d59ceff575a7f5ee2f6ac80e1a56e29865537b490ba"

USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC (main for Polymarket)
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # USDC.e Native
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

SPENDERS = [
    ("CTF Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk CTF Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]

USDC_TOKENS = [
    ("USDC (Bridged)", USDC_BRIDGED),
    ("USDC.e (Native)", USDC_NATIVE),
]

ERC20_ABI = """[{"constant": false,"inputs": [{"name": "_spender","type": "address" },{ "name": "_value", "type": "uint256" }],"name": "approve","outputs": [{ "name": "", "type": "bool" }],"payable": false,"stateMutability": "nonpayable","type": "function"}]"""

ERC1155_ABI = """[{"inputs": [{ "internalType": "address", "name": "operator", "type": "address" },{ "internalType": "bool", "name": "approved", "type": "bool" }],"name": "setApprovalForAll","outputs": [],"stateMutability": "nonpayable","type": "function"}]"""

def main():
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print("ERROR: PRIVATE_KEY must be set in .env and start with 0x")
        return
    
    web3 = Web3(Web3.HTTPProvider(RPC_URL))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    
    account = web3.eth.account.from_key(PRIVATE_KEY)
    pub_key = account.address
    
    print(f"Wallet: {pub_key}")
    
    balance = web3.eth.get_balance(pub_key)
    print(f"POL balance: {web3.from_wei(balance, 'ether')} POL")
    
    if balance < web3.to_wei(0.01, "ether"):
        print("WARNING: Low POL balance, you need gas for transactions")
    
    time.sleep(0.5)  # Pause to avoid rate limits
    
    ctf = web3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)
    
    chain_id = 137
    
    print("\nSetting allowances for all USDC tokens and CTF...\n")
    
    for spender_name, spender in SPENDERS:
        print(f"\n=== {spender_name} ===")
        print(f"    Address: {spender}")
        time.sleep(0.5)  # Pause between spenders
        
        # Approve all USDC tokens
        for token_name, token_addr in USDC_TOKENS:
            try:
                usdc = web3.eth.contract(address=token_addr, abi=ERC20_ABI)
                nonce = web3.eth.get_transaction_count(pub_key)
                
                tx = usdc.functions.approve(spender, int(MAX_INT, 0)).build_transaction({
                    "chainId": chain_id,
                    "from": pub_key,
                    "nonce": nonce,
                    "gas": 500000,
                    "gasPrice": web3.eth.gas_price,
                })
                signed_tx = web3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
                tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
                web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                print(f"  {token_name} approved: {tx_hash.hex()}")
                time.sleep(1)  # Pause between transactions
                
            except Exception as e:
                print(f"  {token_name} error: {e}")
                time.sleep(1)
        
        # Approve CTF
        try:
            nonce = web3.eth.get_transaction_count(pub_key)
            
            tx = ctf.functions.setApprovalForAll(spender, True).build_transaction({
                "chainId": chain_id,
                "from": pub_key,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": web3.eth.gas_price,
            })
            signed_tx = web3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            print(f"  CTF approved: {tx_hash.hex()}")
            time.sleep(1)  # Pause between transactions
            
        except Exception as e:
            print(f"  CTF error: {e}")
            time.sleep(1)
    
    print("\n" + "="*60)
    print("Done! All allowances set.")
    print("="*60)
    print("""
IMPORTANT: Polymarket primarily uses BRIDGED USDC!
If you have USDC.e (Native), you need to swap it to USDC (Bridged).

Swap here:
- QuickSwap: https://quickswap.exchange/#/swap
- 1inch: https://app.1inch.io/#/137/simple/swap/USDC.e/USDC
""")


if __name__ == "__main__":
    main()
