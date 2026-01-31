#!/usr/bin/env python3
"""
Manual Redeem - Enter market slug to redeem

Usage:
    python3 redeem.py
    
Then enter the market slug like: btc-updown-15m-1765309500
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

logger = get_logger("redeem")

_silent_context = {"silent": False}

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
RPC_URL = os.getenv("RPC_URL", "https://polygon-rpc.com")
GAMMA_API = "https://gamma-api.polymarket.com"

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
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256", "name": "index", "type": "uint256"}
        ],
        "name": "payoutNumerators",
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

def print_status(message, status="info"):
    """Print status message (respects silent mode via context dict)."""
    logger.info(f"[{status.upper()}] {message}")
    
    if _silent_context.get("silent", False):
        return
    
    if status == "success":
        print(f"{Colors.GREEN}[OK]{Colors.RESET} {message}")
    elif status == "error":
        print(f"{Colors.RED}[ERR]{Colors.RESET} {message}")
    elif status == "warn":
        print(f"{Colors.YELLOW}[!]{Colors.RESET} {message}")
    else:
        print(f"{Colors.DIM}[...]{Colors.RESET} {message}")


def get_market_info(slug):
    """Get market info from Gamma API."""
    logger.debug(f"Fetching market info for: {slug}")
    try:
        url = f"{GAMMA_API}/events?slug={slug}"
        logger.debug(f"API URL: {url}")
        response = requests.get(url, timeout=10)
        logger.debug(f"API response status: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"API returned non-200 status: {response.status_code}")
            return None
        
        events = response.json()
        logger.debug(f"API returned {len(events)} events")
        
        if not events:
            logger.warning("No events found")
            return None
        
        event = events[0]
        logger.debug(f"Event closed: {event.get('closed')}, active: {event.get('active')}")
        markets = event.get("markets", [])
        
        for market in markets:
            condition_id = market.get("conditionId")
            clob_token_ids = market.get("clobTokenIds", [])
            outcomes = market.get("outcomes", [])
            
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            if not condition_id or len(clob_token_ids) < 2:
                continue
            
            up_index = outcomes.index("Up") if "Up" in outcomes else 0
            down_index = outcomes.index("Down") if "Down" in outcomes else 1
            
            return {
                "slug": slug,
                "condition_id": condition_id,
                "up_token_id": clob_token_ids[up_index],
                "down_token_id": clob_token_ids[down_index],
                "closed": market.get("closed", False),
                "active": market.get("active", False),
                "neg_risk": market.get("negRisk", False),
            }
        
        return None
    except Exception as e:
        print_status(f"API error: {e}", "error")
        return None


def get_token_balance(w3, ctf, wallet, token_id):
    try:
        balance = ctf.functions.balanceOf(wallet, int(token_id)).call()
        return balance
    except:
        return 0


def check_oracle_resolution(w3, ctf, condition_id):
    """Check if oracle has resolved the market.
    
    Returns:
        tuple: (is_resolved, winning_outcome, payout_denominator)
        - is_resolved: True if market is resolved
        - winning_outcome: 0 for UP, 1 for DOWN, None if not resolved
        - payout_denominator: The denominator value
    """
    try:
        condition_bytes = Web3.to_bytes(hexstr=condition_id)
        payout_denom = ctf.functions.payoutDenominator(condition_bytes).call()
        
        logger.debug(f"Oracle check - condition: {condition_id[:20]}..., payoutDenominator: {payout_denom}")
        
        if payout_denom == 0:
            return False, None, 0
        
        # Check which outcome won (0=UP, 1=DOWN)
        up_payout = ctf.functions.payoutNumerators(condition_bytes, 0).call()
        down_payout = ctf.functions.payoutNumerators(condition_bytes, 1).call()
        
        logger.debug(f"Payout numerators - UP: {up_payout}, DOWN: {down_payout}")
        
        winning = None
        if up_payout > 0:
            winning = 0  # UP won
        elif down_payout > 0:
            winning = 1  # DOWN won
        
        return True, winning, payout_denom
        
    except Exception as e:
        logger.error(f"Oracle check error: {e}")
        return False, None, 0


def redeem(w3, wallet, private_key, market_info):
    """Redeem position for a market."""
    logger.info(f"Starting redeem for market: {market_info.get('slug', 'unknown')}")
    logger.debug(f"Condition ID: {market_info.get('condition_id', 'N/A')}")
    logger.debug(f"UP token: {market_info.get('up_token_id', 'N/A')}")
    logger.debug(f"DOWN token: {market_info.get('down_token_id', 'N/A')}")
    logger.debug(f"Market closed: {market_info.get('closed', False)}")
    
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    
    up_balance = get_token_balance(w3, ctf, wallet, market_info["up_token_id"])
    down_balance = get_token_balance(w3, ctf, wallet, market_info["down_token_id"])
    
    logger.info(f"Token balances - UP: {up_balance / 1e6:.6f}, DOWN: {down_balance / 1e6:.6f}")
    
    print(f"\n  UP tokens:   {up_balance / 1e6:.2f}")
    print(f"  DOWN tokens: {down_balance / 1e6:.2f}")
    print(f"  Total value: ${(up_balance + down_balance) / 1e6:.2f}")
    
    if up_balance == 0 and down_balance == 0:
        print_status("No tokens to redeem", "warn")
        return False
    
    if not market_info.get("closed"):
        print_status("Market not yet closed/resolved!", "warn")
        logger.warning("Market not closed - oracle may not have resolved yet")
        print("  Wait for oracle to resolve the market (~1-2 min after close)")
        return False
    
    # Check oracle resolution
    condition_id = market_info["condition_id"]
    is_resolved, winning_outcome, payout_denom = check_oracle_resolution(w3, ctf, condition_id)
    
    if not is_resolved:
        print_status("Oracle has NOT resolved this market yet!", "warn")
        logger.warning(f"Oracle not resolved - payoutDenominator=0 for condition {condition_id[:20]}...")
        print("  The market is closed but oracle hasn't determined the winner.")
        print("  This usually takes 1-2 minutes after market close.")
        print("  Try again in a minute.")
        return False
    
    # Show oracle result
    winner_str = "UP" if winning_outcome == 0 else "DOWN" if winning_outcome == 1 else "UNKNOWN"
    print(f"  {Colors.GREEN}Oracle resolved: {winner_str} won!{Colors.RESET}")
    logger.info(f"Oracle resolved - winner: {winner_str} (outcome={winning_outcome})")
    
    print()
    confirm = input(f"{Colors.YELLOW}Redeem? (y/n): {Colors.RESET}").strip().lower()
    logger.info(f"User confirmation: {confirm}")
    if confirm != 'y':
        print_status("Cancelled", "warn")
        return False
    
    try:
        print_status("Sending redeem transaction...")
        
        condition_id = market_info["condition_id"]
        is_neg_risk = market_info.get("neg_risk", False)
        
        logger.debug(f"Market type: {'NegRisk' if is_neg_risk else 'Standard CTF'}")
        
        nonce = w3.eth.get_transaction_count(wallet)
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
                "gasPrice": int(gas_price * 1.5),
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
                "gasPrice": int(gas_price * 1.5),
            })
        
        logger.debug(f"Built transaction: {json.dumps({k: str(v) for k, v in tx.items()})}")
        
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
        logger.info("Transaction signed, broadcasting...")
        
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"TX broadcast: {tx_hash.hex()}")
        
        print(f"  TX: {tx_hash.hex()}")
        print_status("Waiting for confirmation...")
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        logger.debug(f"TX receipt: status={receipt.get('status')}, gas_used={receipt.get('gasUsed')}")
        
        if receipt.get("status") == 1:
            winning_balance = up_balance if winning_outcome == 0 else down_balance
            print_status(f"Redeemed ${winning_balance / 1e6:.2f} USDC!", "success")
            return True
        else:
            print_status("Transaction reverted", "error")
            logger.error(f"TX reverted - receipt: {dict(receipt)}")
            print("  Possible causes:")
            print("  - Market not resolved yet (oracle delay)")
            print("  - Already redeemed")
            print("  - Insufficient gas")
            return False
        
    except Exception as e:
        logger.exception(f"Redeem error: {e}")
        print_status(f"Error: {e}", "error")
        return False


def redeem_specific(condition_id, up_token_id=None, down_token_id=None, neg_risk=True, auto_confirm=True, silent=False):
    """
    Redeem position for a specific condition ID (called from trade.py).
    
    Uses file lock to prevent concurrent redemptions (nonce collisions).
    
    Args:
        condition_id: The market condition ID to redeem
        up_token_id: Token ID for UP position (required)
        down_token_id: Token ID for DOWN position (required)
        neg_risk: If True, use NegRisk adapter; else use CTF directly
        auto_confirm: If True, skip confirmation prompt
        silent: If True, suppress terminal output (for auto-redeem)
    
    Returns:
        True if redeemed, False otherwise
    """
    _silent_context["silent"] = silent
    
    if not up_token_id or not down_token_id:
        print_status("Token IDs not provided", "error")
        logger.error("redeem_specific called without token IDs")
        return False
    
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print_status("PRIVATE_KEY not set in .env", "error")
        return False
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print_status("Cannot connect to Polygon", "error")
        return False
    
    account = Account.from_key(PRIVATE_KEY)
    wallet = account.address
    
    # Get balances directly using token IDs (no API lookup needed)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    up_balance = get_token_balance(w3, ctf, wallet, up_token_id)
    down_balance = get_token_balance(w3, ctf, wallet, down_token_id)
    
    logger.info(f"Token balances - UP: {up_balance / 1e6:.2f}, DOWN: {down_balance / 1e6:.2f}")
    if not _silent_context.get("silent"):
        print(f"  UP tokens:   {up_balance / 1e6:.2f}")
        print(f"  DOWN tokens: {down_balance / 1e6:.2f}")
    
    if up_balance == 0 and down_balance == 0:
        logger.info("redeem_specific: No tokens to redeem (both balances are 0)")
        print_status("No tokens to redeem", "warn")
        return False
    
    # Check oracle resolution (skip closed check - we know market ended)
    is_resolved, winning_outcome, payout_denom = check_oracle_resolution(w3, ctf, condition_id)
    logger.info(f"Oracle check: resolved={is_resolved}, outcome={winning_outcome}, denominator={payout_denom}")
    if not is_resolved:
        print_status("Oracle has not resolved yet (payoutDenominator=0)", "warn")
        logger.info("Oracle not resolved yet - redemption not possible")
        return False
    
    winner_str = "UP" if winning_outcome == 0 else "DOWN" if winning_outcome == 1 else "UNKNOWN"
    logger.info(f"Oracle resolved: {winner_str} won!")
    if not _silent_context.get("silent"):
        print(f"  Oracle resolved: {winner_str} won!")
    
    # Auto-confirm for automated redemption
    if not auto_confirm:
        confirm = input(f"{Colors.YELLOW}Redeem? (y/n): {Colors.RESET}").strip().lower()
        if confirm != 'y':
            print_status("Cancelled", "warn")
            return False
    
    # Acquire lock to prevent concurrent redemptions
    lock = RedeemLock(timeout=120.0)
    if not lock.acquire():
        print_status("Another redeem in progress, try later", "warn")
        return False
    
    try:
        max_attempts = 3
        retry_delay = 10
        
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    logger.info(f"Retry {attempt-1}/{max_attempts-1} for redeem...")
                    if not _silent_context.get("silent"):
                        print_status(f"Retry {attempt-1}/{max_attempts-1}...", "warn")
                else:
                    print_status("Sending redeem transaction...")
                
                nonce = w3.eth.get_transaction_count(wallet)
                gas_price = w3.eth.gas_price
                
                logger.debug(f"TX params - nonce: {nonce}, gas_price: {gas_price}, neg_risk: {neg_risk}")
                
                if neg_risk:
                    # NegRisk markets use NegRisk Adapter
                    adapter = w3.eth.contract(
                        address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                        abi=NEG_RISK_ABI
                    )
                    amounts = [up_balance, down_balance]
                    logger.debug(f"NegRisk redeem - condition: {condition_id[:20]}..., amounts: {amounts}")
                    
                    tx = adapter.functions.redeemPositions(
                        Web3.to_bytes(hexstr=condition_id),
                        amounts
                    ).build_transaction({
                        "chainId": 137,
                        "from": wallet,
                        "nonce": nonce,
                        "gas": 500000,
                        "gasPrice": int(gas_price * 1.5),
                    })
                else:
                    # Standard CTF markets use CTF Exchange directly
                    logger.debug(f"Standard CTF redeem - condition: {condition_id[:20]}...")
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
                        "gasPrice": int(gas_price * 1.5),
                    })
                
                signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                
                logger.info(f"TX sent: {tx_hash.hex()}")
                if not _silent_context.get("silent"):
                    print(f"  TX: {tx_hash.hex()}")
                print_status("Waiting for confirmation...")
                
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
                
                if receipt.status == 1:
                    if attempt > 1:
                        print_status(f"Redeemed ${(up_balance + down_balance) / 1e6:.2f} after retry!", "success")
                        logger.info(f"Redeemed after {attempt} attempts")
                    else:
                        print_status(f"Redeemed ${(up_balance + down_balance) / 1e6:.2f} USDC!", "success")
                    return True
                else:
                    raise Exception("Transaction reverted")
                
            except Exception as e:
                logger.error(f"Redeem attempt {attempt} failed: {e}")
                if attempt < max_attempts:
                    logger.info(f"Waiting {retry_delay}s before retry...")
                    if not _silent_context.get("silent"):
                        print_status(f"Failed, retrying in {retry_delay}s...", "warn")
                    time.sleep(retry_delay)
                else:
                    print_status(f"Redeem failed after {max_attempts} attempts", "error")
                    logger.error(f"Redeem failed after {max_attempts} attempts: {e}")
                    return False
        
        return False
    finally:
        lock.release()


def main():
    logger.info("=" * 50)
    logger.info("REDEEM STARTED")
    logger.info("=" * 50)
    
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print_status("PRIVATE_KEY not set in .env", "error")
        sys.exit(1)
    
    logger.info(f"Connecting to Polygon RPC: {RPC_URL}")
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print_status("Cannot connect to Polygon", "error")
        sys.exit(1)
    
    logger.info("Connected to Polygon")
    
    account = Account.from_key(PRIVATE_KEY)
    wallet = account.address
    
    logger.info(f"Wallet: {wallet}")
    
    print(f"\n{Colors.BOLD}{Colors.CYAN}Manual Redeem{Colors.RESET}")
    print(f"Wallet: {wallet}\n")
    
    # Show current time slot for reference
    now = int(time.time())
    current_slot = (now // 900) * 900
    last_slot = current_slot - 900
    print(f"{Colors.DIM}Recent markets:{Colors.RESET}")
    print(f"  Current: btc-updown-15m-{current_slot}")
    print(f"  Last:    btc-updown-15m-{last_slot}")
    print()
    
    slug = input("Enter market slug: ").strip()
    
    if not slug:
        print_status("No slug entered", "error")
        return
    
    print_status(f"Fetching market: {slug}")
    market_info = get_market_info(slug)
    
    if not market_info:
        print_status("Market not found", "error")
        return
    
    print_status(f"Found market", "success")
    print(f"  Condition ID: {market_info['condition_id'][:20]}...")
    print(f"  Closed: {market_info['closed']}")
    
    redeem(w3, wallet, PRIVATE_KEY, market_info)
    print()


if __name__ == "__main__":
    main()
