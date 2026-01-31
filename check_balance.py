#!/usr/bin/env python3
"""
Check wallet USDC balance and allowances on Polygon.
Run: python3 check_balance.py [--env /path/to/.env]
"""

import os
import sys
import time
import argparse
from pathlib import Path
from dotenv import load_dotenv
from web3 import Web3

# Parse command line arguments first
parser = argparse.ArgumentParser(description='Check USDC balance and allowances')
parser.add_argument('--env', type=str, help='Path to .env file (default: .env in current dir)')
args = parser.parse_args()

# Load .env from specified path or default
if args.env:
    env_path = Path(args.env)
    if not env_path.exists():
        print(f"ERROR: .env file not found: {args.env}")
        sys.exit(1)
    load_dotenv(env_path)
    print(f"[INFO] Using .env from: {env_path.absolute()}")
else:
    load_dotenv()
    print(f"[INFO] Using .env from current directory")

try:
    from logger import get_logger
    logger = get_logger("balances")
except:
    logger = None

# Polygon RPC
POLYGON_RPC = "https://rpc.ankr.com/polygon/cc878ed5ff293701a1d80d59ceff575a7f5ee2f6ac80e1a56e29865537b490ba"

# Contract addresses
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon (6 decimals)
USDC_E_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC.e native (6 decimals)

# Polymarket contract addresses for allowances
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CTF Exchange
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # NegRisk CTF Exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # NegRisk Adapter

# ERC20 ABI (just the functions we need)
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]


def format_amount(amount, decimals=6):
    """Format token amount with decimals."""
    return amount / (10 ** decimals)


def check_balance():
    """Check USDC balance and allowances."""
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("ERROR: PRIVATE_KEY not found in .env")
        return
    
    # Determine wallet address based on SIGNATURE_TYPE
    signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))
    funder_address = os.getenv("FUNDER_ADDRESS", "")
    
    # Get wallet address from private key
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    
    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        return
    
    account = w3.eth.account.from_key(private_key)
    eoa_address = account.address  # EOA address from PRIVATE_KEY
    
    # SIGNATURE_TYPE logic:
    # Type 0: Use address from PRIVATE_KEY (standard EOA wallet)
    # Type 1/2: Use FUNDER_ADDRESS (Polymarket proxy/smart contract wallet)
    if signature_type == 0:
        wallet = eoa_address
        wallet_type = "EOA (from PRIVATE_KEY)"
        show_both = False
    else:
        if not funder_address:
            print(f"ERROR: SIGNATURE_TYPE={signature_type} requires FUNDER_ADDRESS in .env")
            return
        wallet = funder_address
        wallet_type = f"Proxy Wallet (SIGNATURE_TYPE={signature_type})"
        show_both = True  # Show both EOA and Proxy balances
    
    print(f"\n{'='*60}")
    print(f"SIGNATURE_TYPE={signature_type} - {wallet_type}")
    print(f"{'='*60}\n")
    
    if show_both:
        print(f"Proxy Wallet (TRADING):  {wallet}")
        print(f"EOA Wallet (REDEEM):     {eoa_address}")
        print(f"\n{'='*60}\n")
    
    # Helper function to check balances on an address
    def check_wallet_usdc(addr, label):
        """Check USDC balances on a specific address."""
        print(f"\n--- {label} ---")
        print(f"Address: {addr}\n")
        
        # Check MATIC balance
        matic_balance = w3.eth.get_balance(addr)
        print(f"MATIC Balance: {w3.from_wei(matic_balance, 'ether'):.4f} MATIC")
        print()
        time.sleep(0.5)
        
        # Check USDC tokens
        usdc_tokens = [
            ("USDC (Bridged)", USDC_ADDRESS),
            ("USDC.e (Native)", USDC_E_ADDRESS),
        ]
        
        total = 0
        bridged = 0
        native = 0
        
        for name, address in usdc_tokens:
            try:
                contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)
                balance = contract.functions.balanceOf(addr).call()
                formatted = format_amount(balance)
                total += formatted
                
                if address == USDC_ADDRESS:
                    bridged = formatted
                elif address == USDC_E_ADDRESS:
                    native = formatted
                
                print(f"{name}: ${formatted:,.2f}")
                time.sleep(0.5)
            except Exception as e:
                print(f"{name}: Error - {e}")
                time.sleep(0.5)
        
        print(f"\nTotal USDC: ${total:,.2f}")
        return total, bridged, native
    
    # Check balances
    if show_both:
        # Check Proxy wallet (for trading)
        total_proxy, bridged_proxy, native_proxy = check_wallet_usdc(wallet, "PROXY WALLET (for Trading)")
        print()
        
        # Check EOA wallet (for redeem)
        total_eoa, bridged_eoa, native_eoa = check_wallet_usdc(eoa_address, "EOA WALLET (for Redeem)")
        print()
        
        print(f"{'='*60}")
        print(f"TOTAL ACROSS BOTH WALLETS: ${total_proxy + total_eoa:,.2f}")
        print(f"{'='*60}")
        
        # Use proxy wallet values for allowance checks
        bridged_balance = bridged_proxy
        native_balance = native_proxy
        total_usdc = total_proxy
    else:
        # Only check one wallet (EOA)
        total_usdc, bridged_balance, native_balance = check_wallet_usdc(wallet, "EOA WALLET")
    
    print()
    
    # Check allowances for the main USDC token
    time.sleep(0.5)  # Pause before checking allowances
    print(f"{'='*60}")
    print("USDC Allowances (for trading)")
    print(f"{'='*60}\n")
    
    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    
    exchanges = [
        ("CTF Exchange", CTF_EXCHANGE),
        ("NegRisk CTF Exchange", NEG_RISK_CTF_EXCHANGE),
        ("NegRisk Adapter", NEG_RISK_ADAPTER),
    ]
    
    for name, exchange_addr in exchanges:
        try:
            allowance = usdc_contract.functions.allowance(wallet, Web3.to_checksum_address(exchange_addr)).call()
            formatted = format_amount(allowance)
            
            if allowance == 0:
                status = "NOT SET"
            elif formatted > 1000000000:  # Max uint256 / 1e6
                status = "UNLIMITED"
            else:
                status = f"${formatted:,.2f}"
            
            icon = "OK" if allowance > 0 else "NEEDS APPROVAL"
            print(f"{name}:")
            print(f"  Address: {exchange_addr}")
            print(f"  Allowance: {status} [{icon}]")
            print()
            time.sleep(0.5)  # Pause to avoid rate limits
        except Exception as e:
            print(f"{name}: Error - {e}")
            time.sleep(0.5)
    
    # Also check USDC.e allowances
    time.sleep(0.5)  # Pause before checking USDC.e
    print(f"{'='*60}")
    print("USDC.e (Native) Allowances")
    print(f"{'='*60}\n")
    
    usdc_e_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=ERC20_ABI)
    
    for name, exchange_addr in exchanges:
        try:
            allowance = usdc_e_contract.functions.allowance(wallet, Web3.to_checksum_address(exchange_addr)).call()
            formatted = format_amount(allowance)
            
            if allowance == 0:
                status = "NOT SET"
            elif formatted > 1000000000:
                status = "UNLIMITED"
            else:
                status = f"${formatted:,.2f}"
            
            icon = "OK" if allowance > 0 else "NEEDS APPROVAL"
            print(f"{name}: {status} [{icon}]")
            time.sleep(0.5)  # Pause to avoid rate limits
        except Exception as e:
            print(f"{name}: Error - {e}")
            time.sleep(0.5)
    
    # Prepare contracts for allowance check
    try:
        usdc_bridged = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    except:
        usdc_bridged = None
    
    print(f"\n{'='*60}")
    print("DIAGNOSIS")
    print(f"{'='*60}")
    
    if bridged_balance >= 5:
        print("\n[OK] You have enough USDC (Bridged) for trading!")
    elif native_balance >= 5 and bridged_balance < 5:
        print(f"""
[WARNING] WRONG USDC TOKEN!

You have ${native_balance:.2f} USDC.e (Native) but Polymarket uses USDC (Bridged).

ACTION REQUIRED: Swap USDC.e to USDC on a DEX:
  - QuickSwap: https://quickswap.exchange/#/swap
  - 1inch: https://app.1inch.io/#/137/simple/swap/USDC.e/USDC
  
Select:
  FROM: USDC.e (0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359)
  TO:   USDC   (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
""")
    else:
        print(f"""
[ERROR] Insufficient USDC balance!

You need at least $5 USDC (Bridged) to trade.
Current balance: ${bridged_balance:.2f}

Deposit USDC to your wallet on Polygon network.
""")
    
    # Check allowances
    neg_risk_allowance = 0
    if usdc_bridged:
        try:
            neg_risk_allowance = usdc_bridged.functions.allowance(wallet, Web3.to_checksum_address(NEG_RISK_CTF_EXCHANGE)).call()
        except:
            pass
    
    if neg_risk_allowance == 0:
        print("""
[WARNING] NegRisk CTF Exchange allowance not set!

Run: python3 set_allowances.py
""")


if __name__ == "__main__":
    check_balance()
