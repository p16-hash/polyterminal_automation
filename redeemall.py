#!/usr/bin/env python3
"""
Redeem All Winning Positions

Uses Polymarket Data API to fetch all positions on your wallet
and redeems any that are ready (oracle resolved).

Supports all market types, not just BTC 15-minute markets.

Usage:
    python3 redeemall.py
"""

import os
import sys
import time
import json
import requests
from datetime import datetime
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

load_dotenv()

from logger import get_logger, Colors
from redeem_lock import RedeemLock

logger = get_logger("redeemall")

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
RPC_URL = os.getenv("RPC_URL", "https://polygon-rpc.com")
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

CTF_ABI = json.loads('''[
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "uint256", "name": "id", "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"}
        ],
        "name": "payoutDenominator",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]''')

NEG_RISK_ABI = json.loads('''[
    {
        "inputs": [
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]''')

GNOSIS_SAFE_ABI = json.loads('''[
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"}
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"}
        ],
        "name": "getTransactionHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function"
    }
]''')

def print_status(message, status="info"):
    """Print status message and log to file."""
    logger.info(f"[{status.upper()}] {message}")
    
    if status == "success":
        print(f"{Colors.GREEN}[OK]{Colors.RESET} {message}")
    elif status == "error":
        print(f"{Colors.RED}[ERR]{Colors.RESET} {message}")
    elif status == "warn":
        print(f"{Colors.YELLOW}[!]{Colors.RESET} {message}")
    else:
        print(f"{Colors.DIM}[...]{Colors.RESET} {message}")


def get_token_balance(w3, ctf, wallet, token_id):
    try:
        balance = ctf.functions.balanceOf(wallet, int(token_id)).call()
        return balance
    except:
        return 0


def check_oracle_resolution(w3, ctf, condition_id):
    """Check if oracle has resolved the market.
    
    Returns:
        bool: True if payoutDenominator > 0 (market resolved)
    """
    try:
        condition_bytes = Web3.to_bytes(hexstr=condition_id)
        payout_denom = ctf.functions.payoutDenominator(condition_bytes).call()
        logger.debug(f"Oracle check - condition: {condition_id[:20]}..., payoutDenominator: {payout_denom}")
        return payout_denom > 0
    except Exception as e:
        logger.error(f"Oracle check error: {e}")
        return False


def find_all_positions(w3, wallet):
    """Find all markets with positions using Polymarket Data API.
    
    Returns 3 lists:
    - active: Market still trading (not closed yet)
    - pending: Market closed but oracle not resolved
    - redeemable: Oracle resolved, ready to redeem
    """
    print_status("Fetching positions from Polymarket Data API...")
    logger.info(f"Fetching positions for wallet: {wallet}")
    
    time.sleep(0.5)  # Pause before API request
    
    active = []          # Still trading
    pending = []         # Closed but oracle not resolved
    redeemable = []      # Ready to redeem
    
    try:
        # Get all positions for the user (no filters first to categorize them)
        url = f"{DATA_API}/positions"
        params = {
            "user": wallet,
            "limit": 500,  # Get all positions
            "sizeThreshold": 0.01  # Ignore dust positions < $0.01
        }
        
        logger.debug(f"Requesting: {url} with params: {params}")
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Data API returned {response.status_code}: {response.text}")
            print_status(f"API Error: {response.status_code}", "error")
            return active, pending, redeemable
        
        positions = response.json()
        logger.info(f"Found {len(positions)} positions from API")
        
        if not positions:
            logger.info("No positions found on wallet")
            return active, pending, redeemable
        
        # Group positions by conditionId to handle both outcomes together
        positions_by_condition = {}
        for pos in positions:
            condition_id = pos.get("conditionId")
            if not condition_id:
                continue
            
            if condition_id not in positions_by_condition:
                positions_by_condition[condition_id] = {
                    "slug": pos.get("slug", "unknown"),
                    "title": pos.get("title", "Unknown Market"),
                    "condition_id": condition_id,
                    "neg_risk": pos.get("negativeRisk", False),
                    "end_date": pos.get("endDate"),
                    "redeemable": pos.get("redeemable", False),
                    "mergeable": pos.get("mergeable", False),
                    "outcomes": {}
                }
            
            # Store outcome data
            outcome = pos.get("outcome", "")
            positions_by_condition[condition_id]["outcomes"][outcome] = {
                "asset": pos.get("asset"),
                "size": int(float(pos.get("size", 0)) * 1e6),  # Convert to Wei
                "cur_price": pos.get("curPrice", 0),
            }
        
        # Now categorize each position
        now = int(time.time())
        
        for condition_id, pos_data in positions_by_condition.items():
            # Parse outcomes to get up/down tokens
            outcomes = pos_data["outcomes"]
            
            # Try to identify Up/Down outcomes
            up_data = outcomes.get("Up") or outcomes.get("YES") or outcomes.get("Higher")
            down_data = outcomes.get("Down") or outcomes.get("NO") or outcomes.get("Lower")
            
            # If we can't identify, just take first two outcomes
            if not up_data and not down_data:
                outcome_list = list(outcomes.values())
                up_data = outcome_list[0] if len(outcome_list) > 0 else None
                down_data = outcome_list[1] if len(outcome_list) > 1 else None
            
            up_balance = up_data.get("size", 0) if up_data else 0
            down_balance = down_data.get("size", 0) if down_data else 0
            
            # Skip if no balance (shouldn't happen with API, but safety check)
            if up_balance == 0 and down_balance == 0:
                continue
            
            position_data = {
                "slug": pos_data["slug"],
                "title": pos_data["title"],
                "condition_id": condition_id,
                "up_token_id": up_data.get("asset") if up_data else None,
                "down_token_id": down_data.get("asset") if down_data else None,
                "up_balance": up_balance,
                "down_balance": down_balance,
                "neg_risk": pos_data["neg_risk"],
            }
            
            # Categorize by API flags
            end_date = pos_data.get("end_date")
            is_closed = False
            if end_date:
                try:
                    end_timestamp = datetime.fromisoformat(end_date.replace('Z', '+00:00')).timestamp()
                    is_closed = now >= end_timestamp
                except:
                    pass
            
            if pos_data["redeemable"]:
                # Ready to redeem - oracle has resolved
                logger.info(f"Found redeemable: {pos_data['slug']} - UP={up_balance/1e6:.2f}, DOWN={down_balance/1e6:.2f}")
                redeemable.append(position_data)
            elif is_closed:
                # Market closed but not yet redeemable
                logger.info(f"Found pending: {pos_data['slug']} - UP={up_balance/1e6:.2f}, DOWN={down_balance/1e6:.2f} (waiting oracle)")
                pending.append(position_data)
            else:
                # Still active
                logger.info(f"Found active: {pos_data['slug']} - UP={up_balance/1e6:.2f}, DOWN={down_balance/1e6:.2f}")
                active.append(position_data)
        
        logger.info(f"Categorization complete. Active: {len(active)}, Pending: {len(pending)}, Redeemable: {len(redeemable)}")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching positions: {e}")
        print_status(f"Network error: {e}", "error")
    except Exception as e:
        logger.exception(f"Error processing positions: {e}")
        print_status(f"Error: {e}", "error")
    
    return active, pending, redeemable


def redeem_position(w3, wallet, private_key, position):
    """Redeem a position with file lock to prevent concurrent operations."""
    condition_id = position["condition_id"]
    up_balance = position["up_balance"]
    down_balance = position["down_balance"]
    is_neg_risk = position.get("neg_risk", False)
    
    logger.info(f"Starting redeem for: {position['slug']}")
    logger.debug(f"Condition ID: {condition_id}")
    logger.debug(f"Balances - UP: {up_balance}, DOWN: {down_balance}")
    logger.debug(f"Market type: {'NegRisk' if is_neg_risk else 'Standard CTF'}")
    
    print_status(f"Redeeming: {position['slug']}")
    print(f"    UP: {up_balance / 1e6:.2f}  DOWN: {down_balance / 1e6:.2f}")
    
    # Pause to avoid rate limits
    time.sleep(0.5)
    
    # Check oracle resolution first
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    if not check_oracle_resolution(w3, ctf, condition_id):
        print_status(f"Skipping - oracle not resolved yet", "warn")
        logger.warning(f"Skipping {position['slug']} - oracle has not resolved (payoutDenominator=0)")
        return False
    
    time.sleep(0.5)  # Pause after oracle check
    
    # Acquire lock to prevent concurrent redemptions
    lock = RedeemLock(timeout=60.0)
    if not lock.acquire():
        print_status("Another redeem in progress, skipping", "warn")
        return False
    
    try:
        # Determine if we need to use Gnosis Safe execTransaction (for Proxy wallet)
        use_proxy_safe = (SIGNATURE_TYPE in [1, 2] and FUNDER_ADDRESS)
        
        if use_proxy_safe:
            # FOR PROXY WALLET: Call execTransaction on Gnosis Safe
            logger.info("Using Gnosis Safe execTransaction for Proxy wallet redeem")
            print(f"{Colors.YELLOW}    [Proxy Wallet] Using Gnosis Safe execTransaction{Colors.RESET}")
            
            # Step 1: Encode the redeemPositions call
            # Use FUNDER_ADDRESS (Safe wallet) as "from" for encoding
            safe_address = Web3.to_checksum_address(FUNDER_ADDRESS)
            
            if is_neg_risk:
                adapter = w3.eth.contract(
                    address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                    abi=NEG_RISK_ABI
                )
                amounts = [up_balance, down_balance]
                # Encode using build_transaction then extract data
                temp_tx = adapter.functions.redeemPositions(
                    Web3.to_bytes(hexstr=condition_id),
                    amounts
                ).build_transaction({"from": safe_address})
                redeem_data = temp_tx['data']
                target_contract = NEG_RISK_ADAPTER
            else:
                index_sets = [1, 2]
                parent_collection_id = bytes(32)
                # Encode using build_transaction then extract data
                temp_tx = ctf.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_ADDRESS),
                    parent_collection_id,
                    Web3.to_bytes(hexstr=condition_id),
                    index_sets
                ).build_transaction({"from": safe_address})
                redeem_data = temp_tx['data']
                target_contract = CTF_ADDRESS
            
            logger.debug(f"Encoded redeem data: {redeem_data[:100]}...")
            
            # Step 2: Get EOA (owner) address and nonce
            account = Account.from_key(private_key)
            owner_address = account.address
            
            time.sleep(0.5)
            eoa_nonce = w3.eth.get_transaction_count(owner_address)
            
            time.sleep(0.3)
            gas_price = w3.eth.gas_price
            
            # Step 3: Build execTransaction on Gnosis Safe
            safe = w3.eth.contract(
                address=Web3.to_checksum_address(FUNDER_ADDRESS),
                abi=GNOSIS_SAFE_ABI
            )
            
            # Get Safe nonce
            safe_nonce = safe.functions.nonce().call()
            logger.info(f"Safe nonce: {safe_nonce}, Owner: {owner_address}")
            
            # Safe transaction parameters
            to = Web3.to_checksum_address(target_contract)
            value = 0
            data = redeem_data
            operation = 0  # CALL
            safeTxGas = 0
            baseGas = 0
            gasPrice_safe = 0
            gasToken = "0x0000000000000000000000000000000000000000"
            refundReceiver = "0x0000000000000000000000000000000000000000"
            
            # For 1-of-1 Safe, we need owner signature
            # Build signature: just sign the Safe transaction hash
            tx_hash_to_sign = safe.functions.getTransactionHash(
                to, value, data, operation,
                safeTxGas, baseGas, gasPrice_safe,
                gasToken, refundReceiver, safe_nonce
            ).call()
            
            logger.debug(f"Safe TX hash to sign: {tx_hash_to_sign.hex()}")
            
            # Sign with owner's private key (direct hash signature for Gnosis Safe)
            # Use unsafe_sign_hash to sign the raw hash without message prefix
            signed_msg = account.unsafe_sign_hash(tx_hash_to_sign)
            
            # Build signature in Gnosis Safe format: r (32 bytes) + s (32 bytes) + v (1 byte)
            r = signed_msg.r.to_bytes(32, byteorder='big')
            s = signed_msg.s.to_bytes(32, byteorder='big')
            v = signed_msg.v
            signature = r + s + bytes([v])
            
            logger.debug(f"Signature (r+s+v): {signature.hex()[:100]}...")
            
            # Build the execTransaction call
            tx = safe.functions.execTransaction(
                to, value, data, operation,
                safeTxGas, baseGas, gasPrice_safe,
                gasToken, refundReceiver, signature
            ).build_transaction({
                "chainId": 137,
                "from": owner_address,
                "nonce": eoa_nonce,
                "gas": 1000000,
                "gasPrice": int(gas_price * 1.2),
            })
            
            print(f"{Colors.DIM}    Safe: {FUNDER_ADDRESS[:10]}...{FUNDER_ADDRESS[-8:]}{Colors.RESET}")
            print(f"{Colors.DIM}    Owner: {owner_address[:10]}...{owner_address[-8:]}{Colors.RESET}")
            
        else:
            # FOR EOA WALLET: Direct call to CTF contract
            logger.info("Using direct CTF contract call for EOA wallet")
            
            time.sleep(0.5)
            nonce = w3.eth.get_transaction_count(wallet)
            
            time.sleep(0.3)
            gas_price = w3.eth.gas_price
            
            logger.debug(f"TX params - nonce: {nonce}, gas_price: {gas_price}")
            
            if is_neg_risk:
                adapter = w3.eth.contract(
                    address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                    abi=NEG_RISK_ABI
                )
                amounts = [up_balance, down_balance]
                logger.debug(f"NegRisk redeem - condition: {condition_id}, amounts: {amounts}")
                
                tx = adapter.functions.redeemPositions(
                    Web3.to_bytes(hexstr=condition_id),
                    amounts
                ).build_transaction({
                    "chainId": 137,
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 500000,
                    "gasPrice": int(gas_price * 1.2),
                })
            else:
                logger.debug(f"Standard CTF redeem - condition: {condition_id}")
                index_sets = [1, 2]
                parent_collection_id = bytes(32)
                
                tx = ctf.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_ADDRESS),
                    parent_collection_id,
                    Web3.to_bytes(hexstr=condition_id),
                    index_sets
                ).build_transaction({
                    "chainId": 137,
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 500000,
                    "gasPrice": int(gas_price * 1.2),
                })
            
            print(f"{Colors.DIM}    TX from: {wallet[:10]}...{wallet[-8:]}{Colors.RESET}")
        
        time.sleep(0.5)  # Pause before signing and sending
        
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
        logger.info("Transaction signed, broadcasting...")
        
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"TX broadcast: {tx_hash.hex()}")
        
        print(f"    TX: {tx_hash.hex()}")
        
        # Wait for receipt with retries on rate limit
        max_retries = 3
        for attempt in range(max_retries):
            try:
                time.sleep(1)  # Pause before checking receipt
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                logger.debug(f"TX receipt: status={receipt.get('status')}, gas_used={receipt.get('gasUsed')}")
                break
            except Exception as e:
                if 'rate limit' in str(e).lower() and attempt < max_retries - 1:
                    logger.warning(f"Rate limit on receipt check, retrying in 3s...")
                    time.sleep(3)
                    continue
                raise
        
        status = receipt.get("status")
        gas_used = receipt.get("gasUsed")
        
        logger.info(f"TX completed - status: {status}, gas_used: {gas_used}")
        print(f"    Status: {status} | Gas: {gas_used}")
        
        if status == 1:
            print_status("Redeemed!", "success")
            return True
        else:
            logger.error(f"TX REVERTED - receipt: {dict(receipt)}")
            print_status(f"TX FAILED (status={status})", "error")
            print(f"{Colors.RED}Transaction was sent but REVERTED on blockchain!{Colors.RESET}")
            print(f"{Colors.DIM}Check TX on: https://polygonscan.com/tx/{tx_hash.hex()}{Colors.RESET}")
            return False
        
    except Exception as e:
        logger.exception(f"Redeem error for {position['slug']}: {e}")
        print_status(f"Error: {e}", "error")
        return False
    finally:
        lock.release()


def main(auto_confirm=False):
    """Main function with optional auto-confirmation
    
    Args:
        auto_confirm: If True, automatically confirm redemption without prompting
    """
    logger.info("=" * 50)
    logger.info("REDEEMALL STARTED")
    logger.info("=" * 50)
    
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print_status("PRIVATE_KEY not set in .env", "error")
        sys.exit(1)
    
    logger.info(f"Connecting to Polygon RPC: {RPC_URL}")
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    # Add POA middleware for Polygon (POA chain)
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    
    if not w3.is_connected():
        print_status("Cannot connect to Polygon", "error")
        sys.exit(1)
    
    logger.info("Connected to Polygon")
    
    account = Account.from_key(PRIVATE_KEY)
    signer_address = account.address  # Address that signs and sends transactions
    
    # Determine wallet address based on SIGNATURE_TYPE
    # Type 0: Tokens on signer address (EOA wallet)
    # Type 1/2: Tokens on FUNDER_ADDRESS (Proxy wallet)
    if SIGNATURE_TYPE == 0:
        wallet_to_check = signer_address
        wallet_type = "EOA"
    else:
        if not FUNDER_ADDRESS:
            print_status(f"SIGNATURE_TYPE={SIGNATURE_TYPE} requires FUNDER_ADDRESS in .env", "error")
            sys.exit(1)
        wallet_to_check = FUNDER_ADDRESS
        wallet_type = f"Proxy (type {SIGNATURE_TYPE})"
    
    # Redeem transaction always FROM signer
    redeem_from_address = signer_address
    
    logger.info(f"Checking positions on: {wallet_to_check} ({wallet_type})")
    logger.info(f"Redeem transactions FROM: {redeem_from_address}")
    
    print(f"{Colors.DIM}Checking tokens on: {wallet_to_check[:10]}...{wallet_to_check[-8:]}{Colors.RESET}")
    print(f"{Colors.DIM}Redeem TX from: {redeem_from_address[:10]}...{redeem_from_address[-8:]}{Colors.RESET}")
    
    print(f"\n{Colors.BOLD}{Colors.CYAN}Redeem All Positions{Colors.RESET}")
    print(f"Checking positions on: {wallet_to_check}")
    print(f"Type: {wallet_type}\n")
    
    active, pending, redeemable = find_all_positions(w3, wallet_to_check)
    
    # Show active markets (still trading)
    if active:
        print(f"{Colors.CYAN}Active - still trading ({len(active)}):{Colors.RESET}")
        for pos in active:
            up_val = pos["up_balance"] / 1e6
            down_val = pos["down_balance"] / 1e6
            title = pos.get("title", pos["slug"])
            print(f"  {Colors.DIM}{title}: UP=${up_val:.2f} DOWN=${down_val:.2f}{Colors.RESET}")
        print()
    
    # Show pending markets (closed but oracle not resolved)
    if pending:
        print(f"{Colors.YELLOW}Pending resolution ({len(pending)}):{Colors.RESET}")
        for pos in pending:
            up_val = pos["up_balance"] / 1e6
            down_val = pos["down_balance"] / 1e6
            title = pos.get("title", pos["slug"])
            print(f"  {Colors.DIM}{title}: UP=${up_val:.2f} DOWN=${down_val:.2f} (waiting oracle){Colors.RESET}")
        print()
    
    # Show redeemable markets
    if redeemable:
        print(f"{Colors.GREEN}Ready to redeem ({len(redeemable)}):{Colors.RESET}")
        for pos in redeemable:
            up_val = pos["up_balance"] / 1e6
            down_val = pos["down_balance"] / 1e6
            total = up_val + down_val
            title = pos.get("title", pos["slug"])
            print(f"  {title}: ${total:.2f}")
        print()
    
    # If nothing found at all
    if not active and not pending and not redeemable:
        print_status("No positions found", "success")
        print("\nAll clear!\n")
        return
    
    # If no redeemable
    if not redeemable:
        print_status("No markets ready to redeem yet", "warn")
        if pending:
            print(f"{Colors.DIM}Wait for oracle to resolve pending markets.{Colors.RESET}\n")
        return
    
    # Ask for confirmation before proceeding (unless auto-confirm)
    if auto_confirm:
        print(f"{Colors.CYAN}Auto-confirming redeem...{Colors.RESET}")
        choice = "y"
    else:
        try:
            choice = input(f"{Colors.CYAN}Redeem available? (y=yes, n/q=cancel): {Colors.RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            choice = "q"
        
        if choice not in ("y", "yes"):
            print(f"\n{Colors.DIM}Cancelled. Returning to menu...{Colors.RESET}\n")
            return "menu"
    
    print()
    
    redeemed = 0
    total_value = 0
    
    for pos in redeemable:
        # Pass both signer address (for TX) and wallet_to_check (for token location)
        if redeem_position(w3, redeem_from_address, PRIVATE_KEY, pos):
            redeemed += 1
            total_value += (pos["up_balance"] + pos["down_balance"]) / 1e6
        time.sleep(2)  # Longer pause between redemptions to avoid rate limits
    
    print(f"\n{Colors.GREEN}{Colors.BOLD}Done!{Colors.RESET}")
    print(f"Redeemed: {redeemed}/{len(redeemable)}")
    print(f"Value: ~${total_value:.2f} USDC\n")


if __name__ == "__main__":
    # Check for --auto-confirm flag
    auto_confirm = "--auto-confirm" in sys.argv or "-y" in sys.argv
    main(auto_confirm=auto_confirm)
