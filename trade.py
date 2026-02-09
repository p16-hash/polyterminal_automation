#!/usr/bin/env python3
"""
Polymarket Crypto 15-minute Auto Trader (Python)

Automatic trading tool for Polymarket 15-minute crypto markets.
Supports BTC, ETH, SOL, and XRP.
Press 1 to BUY UP, 2 to BUY DOWN, Q to quit.

Features:
- Real-time crypto price from Polymarket Chainlink WebSocket
- Real-time UP/DOWN ask prices from Polymarket orderbook
- Live countdown timer
- Compact terminal display
- Multi-cryptocurrency support (BTC, ETH, SOL, XRP)
"""

import os
import sys
import time
import json
import signal
import requests
import threading
import websocket
from datetime import datetime
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

load_dotenv()

# Centralized logging (file-only, no terminal spam)
from logger import get_logger, add_message, format_messages_block, Colors, is_quiet_mode

logger = get_logger("trade")


def format_error_short(error) -> str:
    """Format error for terminal display (short), full error goes to log file.
    
    Detects common errors and returns user-friendly short messages.
    """
    error_str = str(error)
    
    # Cloudflare block (403 with HTML)
    if "403" in error_str and ("cloudflare" in error_str.lower() or "cf-" in error_str.lower() or "enable_cookies" in error_str.lower()):
        return "IP blocked by Cloudflare (use VPN)"
    
    # DNS resolution failure
    if "nodename nor servname" in error_str or "Failed to resolve" in error_str or "NameResolutionError" in error_str:
        return "DNS blocked (change DNS to 1.1.1.1)"
    
    # Connection refused
    if "Connection refused" in error_str:
        return "Connection refused"
    
    # Timeout
    if "timeout" in error_str.lower() or "timed out" in error_str.lower():
        return "Request timeout"
    
    # Rate limit
    if "429" in error_str or "rate limit" in error_str.lower():
        return "Rate limited (slow down)"
    
    # Generic - truncate to 50 chars
    if len(error_str) > 50:
        return error_str[:47] + "..."
    return error_str


# Configuration
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
TRADE_SIZE_USDC = float(os.getenv("TRADE_SIZE_USDC", "5"))  # Legacy, kept for compatibility

# Contracts-based trading (new)
DEFAULT_CONTRACTS_SIZE = int(os.getenv("CONTRACTS_SIZE", "10"))  # Default from env
current_contracts_size = DEFAULT_CONTRACTS_SIZE  # Mutable, adjusted with S/D keys

# Order mode: FOK (Fill-Or-Kill) or FAK (Fill-And-Kill for partial fills)
order_mode = "FOK"  # Toggle with F key

# Selected cryptocurrency (set at startup)
SELECTED_CRYPTO_SLUG = "btc"
SELECTED_CRYPTO_NAME = "BTC"
SELECTED_CRYPTO_SYMBOL = "btc/usd"


def flush_stdin():
    """Flush any buffered input from stdin to prevent stale keypresses."""
    import select
    try:
        while select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.read(1)
    except Exception:
        pass  # Ignore errors on non-Unix systems

POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYMARKET_USER_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
RTDS_WS = "wss://ws-live-data.polymarket.com"  # Polymarket RTDS for Chainlink prices

# Chainlink BTC/USD on Polygon
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
CHAINLINK_ABI = [
    {'inputs': [], 'name': 'latestRoundData', 'outputs': [{'name': 'roundId', 'type': 'uint80'}, {'name': 'answer', 'type': 'int256'}, {'name': 'startedAt', 'type': 'uint256'}, {'name': 'updatedAt', 'type': 'uint256'}, {'name': 'answeredInRound', 'type': 'uint80'}], 'stateMutability': 'view', 'type': 'function'},
    {'inputs': [{'name': '_roundId', 'type': 'uint80'}], 'name': 'getRoundData', 'outputs': [{'name': 'roundId', 'type': 'uint80'}, {'name': 'answer', 'type': 'int256'}, {'name': 'startedAt', 'type': 'uint256'}, {'name': 'updatedAt', 'type': 'uint256'}, {'name': 'answeredInRound', 'type': 'uint80'}], 'stateMutability': 'view', 'type': 'function'},
    {'inputs': [], 'name': 'decimals', 'outputs': [{'name': '', 'type': 'uint8'}], 'stateMutability': 'view', 'type': 'function'}
]

# USDC contracts on Polygon
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (PoS bridged)
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Native USDC
ERC20_ABI = [
    {'constant': True, 'inputs': [{'name': '_owner', 'type': 'address'}], 'name': 'balanceOf', 'outputs': [{'name': 'balance', 'type': 'uint256'}], 'type': 'function'},
    {'constant': True, 'inputs': [], 'name': 'decimals', 'outputs': [{'name': '', 'type': 'uint8'}], 'type': 'function'}
]


class BalanceState:
    """Track wallet balance for session P/L display."""
    session_start_balance = 0.0  # Balance when terminal opened
    current_balance = 0.0        # Latest balance
    last_update = 0              # Timestamp of last refresh
    wallet_address = ""          # Cached wallet address


balance_state = BalanceState()

# Colors are imported from logger module

# Global state for real-time prices
PRICE_STALE_THRESHOLD = 5.0  # seconds

shutdown_requested = False
feed_manager = None
binance_ws = None


def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global shutdown_requested
    shutdown_requested = True
    logger.info(f"Received signal {signum}, shutting down...")
    add_message("Shutdown signal received", "warn")
    
    if feed_manager:
        try:
            feed_manager.stop()
        except:
            pass
    
    if binance_ws:
        try:
            binance_ws.close()
        except:
            pass


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def get_chainlink_btc_at_timestamp(target_timestamp):
    """Get Chainlink BTC/USD price at specific timestamp.
    
    Uses binary search through Chainlink rounds to find price closest to target time.
    
    Args:
        target_timestamp: Unix timestamp (seconds) to query
        
    Returns:
        BTC price as float, or None if failed
    """
    try:
        from web3 import Web3
        
        rpc_url = os.getenv("RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/IZ9LcPHnEBGEAQxYrTZkk")
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 10}))
        
        if not w3.is_connected():
            logger.warning("Chainlink query failed: cannot connect to RPC")
            return None
        
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_BTC_USD), 
            abi=CHAINLINK_ABI
        )
        decimals = feed.functions.decimals().call()
        round_id, _, _, _, _ = feed.functions.latestRoundData().call()
        
        # Binary search for round at target timestamp
        low = max(1, round_id - 500)
        high = round_id
        best_round = round_id
        best_diff = float('inf')
        
        for _ in range(20):  # Max 20 iterations
            if low > high:
                break
            mid = (low + high) // 2
            try:
                _, _, _, mid_ts, _ = feed.functions.getRoundData(mid).call()
                diff = abs(mid_ts - target_timestamp)
                if diff < best_diff:
                    best_diff = diff
                    best_round = mid
                
                if mid_ts < target_timestamp:
                    low = mid + 1
                else:
                    high = mid - 1
            except:
                break
        
        # Get price at best round
        _, answer, _, round_time, _ = feed.functions.getRoundData(best_round).call()
        price = answer / (10 ** decimals)
        
        logger.info(f"Chainlink PTB: ${price:,.0f} at {time.strftime('%H:%M:%S', time.gmtime(round_time))} UTC (diff {best_diff}s)")
        return round(price)  # Return rounded to integer
        
    except Exception as e:
        logger.error(f"Chainlink query error: {e}")
        return None


def get_wallet_usdc_balance(wallet_address=None):
    """Get total USDC balance (bridged + native) for wallet.
    
    Args:
        wallet_address: Wallet address (0x...). If None, determines based on SIGNATURE_TYPE.
        
    Returns:
        Total USDC balance as float, or None if failed
    """
    try:
        from web3 import Web3
        from eth_account import Account
        
        # Get wallet address based on SIGNATURE_TYPE
        if not wallet_address:
            pk = PRIVATE_KEY
            if not pk:
                return None
            
            # Type 0: Use address from PRIVATE_KEY (EOA)
            # Type 1/2: Use FUNDER_ADDRESS (Proxy wallet)
            if SIGNATURE_TYPE == 0:
                wallet_address = Account.from_key(pk).address
            else:
                if not FUNDER_ADDRESS:
                    logger.error(f"SIGNATURE_TYPE={SIGNATURE_TYPE} requires FUNDER_ADDRESS")
                    return None
                wallet_address = FUNDER_ADDRESS
        
        balance_state.wallet_address = wallet_address
        
        rpc_url = os.getenv("RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/IZ9LcPHnEBGEAQxYrTZkk")
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 10}))
        
        if not w3.is_connected():
            logger.warning("Balance query failed: cannot connect to RPC")
            return None
        
        total = 0.0
        
        # USDC.e (bridged) - main Polymarket token
        usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_BRIDGED), abi=ERC20_ABI)
        checksum_wallet = Web3.to_checksum_address(wallet_address)
        balance_e = usdc_e.functions.balanceOf(checksum_wallet).call()
        decimals_e = usdc_e.functions.decimals().call()
        total += balance_e / (10 ** decimals_e)
        
        # Native USDC
        usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=ERC20_ABI)
        balance_n = usdc_n.functions.balanceOf(checksum_wallet).call()
        decimals_n = usdc_n.functions.decimals().call()
        total += balance_n / (10 ** decimals_n)
        
        # POL/MATIC native token - convert to USD
        native_balance = w3.eth.get_balance(checksum_wallet) / (10 ** 18)
        pol_price_usd = 0.10  # fallback
        try:
            import requests
            r = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=polygon-ecosystem-token&vs_currencies=usd', timeout=3)
            if r.status_code == 200:
                pol_price_usd = r.json().get('polygon-ecosystem-token', {}).get('usd', 0.10)
        except:
            pass
        total += native_balance * pol_price_usd
        
        balance_state.current_balance = total
        balance_state.last_update = time.time()
        
        logger.info(f"Wallet balance: ${total:.2f}")
        return total
        
    except Exception as e:
        logger.error(f"Balance query error: {e}")
        return None


class TokenBalanceState:
    """Store real token balances from Data API."""
    up_balance = 0.0
    down_balance = 0.0
    up_avg_price = 0.0
    down_avg_price = 0.0
    up_invested = 0.0
    down_invested = 0.0
    up_current_value = 0.0
    down_current_value = 0.0
    last_update = 0
    
token_balance_state = TokenBalanceState()


def get_token_balances_from_api(condition_id):
    """Get token positions from Polymarket Data API with full details."""
    try:
        from eth_account import Account
        
        # Get wallet address based on SIGNATURE_TYPE
        if SIGNATURE_TYPE == 0:
            wallet = Account.from_key(PRIVATE_KEY).address
        else:
            if not FUNDER_ADDRESS:
                logger.error(f"SIGNATURE_TYPE={SIGNATURE_TYPE} requires FUNDER_ADDRESS")
                return False
            wallet = FUNDER_ADDRESS
        
        # Call Data API
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={
                "user": wallet,
                "market": condition_id,
                "sizeThreshold": 0.01
            },
            timeout=10
        )
        
        if resp.status_code != 200:
            logger.warning(f"Data API returned {resp.status_code}")
            return False
        
        positions = resp.json()
        
        # Reset state
        token_balance_state.up_balance = 0.0
        token_balance_state.down_balance = 0.0
        token_balance_state.up_avg_price = 0.0
        token_balance_state.down_avg_price = 0.0
        token_balance_state.up_invested = 0.0
        token_balance_state.down_invested = 0.0
        token_balance_state.up_current_value = 0.0
        token_balance_state.down_current_value = 0.0
        
        # Parse positions for current market
        for pos in positions:
            if pos.get("conditionId") == condition_id:
                outcome = pos.get("outcome", "").upper()
                
                if outcome in ["YES", "UP"]:
                    token_balance_state.up_balance = float(pos.get("size", 0))
                    token_balance_state.up_avg_price = float(pos.get("avgPrice", 0))
                    token_balance_state.up_invested = float(pos.get("initialValue", 0))
                    token_balance_state.up_current_value = float(pos.get("currentValue", 0))
                    
                elif outcome in ["NO", "DOWN"]:
                    token_balance_state.down_balance = float(pos.get("size", 0))
                    token_balance_state.down_avg_price = float(pos.get("avgPrice", 0))
                    token_balance_state.down_invested = float(pos.get("initialValue", 0))
                    token_balance_state.down_current_value = float(pos.get("currentValue", 0))
        
        token_balance_state.last_update = time.time()
        
        logger.info(f"Data API: UP={token_balance_state.up_balance:.2f}@${token_balance_state.up_avg_price:.2f}, DN={token_balance_state.down_balance:.2f}@${token_balance_state.down_avg_price:.2f}")
        return True
        
    except Exception as e:
        logger.error(f"Data API error: {e}")
        return False


def get_token_balances(up_token_id, down_token_id):
    """Legacy function - kept for compatibility. Returns simple balances."""
    return token_balance_state.up_balance, token_balance_state.down_balance


def refresh_balance(is_startup=False):
    """Refresh wallet balance. If startup, also sets session_start_balance."""
    try:
        balance = get_wallet_usdc_balance()
        if balance is not None:
            if is_startup or balance_state.session_start_balance == 0:
                balance_state.session_start_balance = balance
            add_message(f"Balance: ${balance:.2f}", "info")
            logger.info(f"Balance refresh OK: ${balance:.2f}")
        else:
            error_msg = "Balance refresh failed - RPC returned None"
            add_message("Balance refresh failed", "warn")
            logger.warning(error_msg)
    except Exception as e:
        error_msg = f"Balance refresh error: {e}"
        add_message("Balance refresh failed", "warn")
        logger.error(error_msg, exc_info=True)


def refresh_all_balances(market_data):
    """Refresh USDC balance AND token balances from Data API."""
    # Refresh USDC
    refresh_balance()
    
    # Refresh token balances from Data API
    if market_data:
        condition_id = market_data.get("condition_id")
        
        if condition_id:
            success = get_token_balances_from_api(condition_id)
            if success:
                add_message(f"Tokens: UP={token_balance_state.up_balance:.2f} DN={token_balance_state.down_balance:.2f}", "info")
            else:
                add_message("Token balance refresh failed", "warn")


class PriceState:
    btc_price = 0.0
    up_ask = 0.0
    down_ask = 0.0
    up_bid = 0.0
    down_bid = 0.0
    last_update = 0
    last_binance_update = 0
    last_polymarket_update = 0
    ws_connected = False
    warmup_complete = False  # Set True once BOTH feeds have reported
    
    def check_warmup(self) -> bool:
        """Check if both feeds have reported at least once.
        
        Returns True if warmup is complete (both feeds have ticked).
        """
        if not self.warmup_complete:
            if self.last_binance_update > 0 and self.last_polymarket_update > 0:
                self.warmup_complete = True
        return self.warmup_complete
    
    def is_fresh(self) -> bool:
        """Check if prices are fresh enough for trading.
        
        REQUIRES:
        1. Warmup complete (both feeds have ticked at least once)
        2. Both Polymarket AND Binance data fresh (< 5 seconds old)
        
        Trading is BLOCKED until both conditions are met.
        """
        now = time.time()
        
        # Must have warmup complete - both feeds need at least one tick
        if not self.check_warmup():
            return False
        
        # Both sources must be fresh
        polymarket_age = now - self.last_polymarket_update
        binance_age = now - self.last_binance_update
        
        return polymarket_age < PRICE_STALE_THRESHOLD and binance_age < PRICE_STALE_THRESHOLD
    
    def is_binance_fresh(self) -> bool:
        """Check if Binance BTC price is fresh."""
        if self.last_binance_update == 0:
            return False
        return (time.time() - self.last_binance_update) < PRICE_STALE_THRESHOLD
    
    def is_polymarket_fresh(self) -> bool:
        """Check if Polymarket data is fresh."""
        if self.last_polymarket_update == 0:
            return False
        return (time.time() - self.last_polymarket_update) < PRICE_STALE_THRESHOLD
    
    def get_stale_reason(self) -> str:
        """Get human-readable reason for stale data."""
        if not self.check_warmup():
            missing = []
            if self.last_binance_update == 0:
                missing.append("Binance")
            if self.last_polymarket_update == 0:
                missing.append("Polymarket")
            return f"Waiting for {', '.join(missing)} feed"
        
        now = time.time()
        stale = []
        if now - self.last_binance_update >= PRICE_STALE_THRESHOLD:
            stale.append(f"Binance ({now - self.last_binance_update:.1f}s)")
        if now - self.last_polymarket_update >= PRICE_STALE_THRESHOLD:
            stale.append(f"Polymarket ({now - self.last_polymarket_update:.1f}s)")
        
        return f"Stale: {', '.join(stale)}" if stale else "Fresh"
    
    def get_age(self) -> float:
        """Get age of oldest stale source in seconds."""
        now = time.time()
        polymarket_age = now - self.last_polymarket_update if self.last_polymarket_update else float('inf')
        binance_age = now - self.last_binance_update if self.last_binance_update else float('inf')
        return max(polymarket_age, binance_age)


price_state = PriceState()


# WebSocket Feed Manager for proper lifecycle management
class PolymarketFeedManager:
    """Manages Polymarket WebSocket connection with proper lifecycle."""
    
    def __init__(self):
        self.tokens = {"up": None, "down": None}
        self.ws_app = None
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
    
    def start(self, up_token, down_token):
        """Start WebSocket feed for given tokens."""
        self.tokens = {"up": up_token, "down": down_token}
        self.stop_event.clear()
        self.running = True
        
        def on_message(ws, message):
            if self.stop_event.is_set():
                return
            try:
                data = json.loads(message)
                event_type = data.get("event_type", "")
                asset_id = data.get("asset_id", "")
                
                # Handle book updates (full orderbook)
                if event_type == "book":
                    asks = data.get("asks", [])
                    bids = data.get("bids", [])
                    
                    if asks and len(asks) > 0:
                        ask_prices = []
                        for ask in asks:
                            try:
                                if isinstance(ask, dict):
                                    price = float(ask.get("price", 0))
                                elif isinstance(ask, list):
                                    price = float(ask[0])
                                else:
                                    price = float(ask)
                                if price > 0:
                                    ask_prices.append(price)
                            except (ValueError, IndexError):
                                continue
                        
                        if ask_prices:
                            best_ask = min(ask_prices)
                            
                            if asset_id == self.tokens["up"]:
                                price_state.up_ask = best_ask
                            elif asset_id == self.tokens["down"]:
                                price_state.down_ask = best_ask
                    
                    if bids and len(bids) > 0:
                        bid_prices = []
                        for bid in bids:
                            try:
                                if isinstance(bid, dict):
                                    price = float(bid.get("price", 0))
                                elif isinstance(bid, list):
                                    price = float(bid[0])
                                else:
                                    price = float(bid)
                                if price > 0:
                                    bid_prices.append(price)
                            except (ValueError, IndexError):
                                continue
                        
                        if bid_prices:
                            best_bid = max(bid_prices)
                            
                            if asset_id == self.tokens["up"]:
                                price_state.up_bid = best_bid
                            elif asset_id == self.tokens["down"]:
                                price_state.down_bid = best_bid
                
                # Handle price_change events (new format with price_changes array)
                elif event_type == "price_change":
                    price_changes = data.get("price_changes", [])
                    for change in price_changes:
                        change_asset_id = change.get("asset_id", "")
                        best_ask = change.get("best_ask")
                        best_bid = change.get("best_bid")
                        
                        if best_ask and best_ask != "0":
                            try:
                                ask_price = float(best_ask)
                                if change_asset_id == self.tokens["up"]:
                                    price_state.up_ask = ask_price
                                elif change_asset_id == self.tokens["down"]:
                                    price_state.down_ask = ask_price
                            except ValueError:
                                pass
                        
                        if best_bid and best_bid != "0":
                            try:
                                bid_price = float(best_bid)
                                if change_asset_id == self.tokens["up"]:
                                    price_state.up_bid = bid_price
                                elif change_asset_id == self.tokens["down"]:
                                    price_state.down_bid = bid_price
                            except ValueError:
                                pass
                
                # Handle last_trade_price
                elif event_type == "last_trade_price":
                    try:
                        price = float(data.get("price", 0))
                        if asset_id == self.tokens["up"]:
                            if price_state.up_ask == 0:
                                price_state.up_ask = price
                        elif asset_id == self.tokens["down"]:
                            if price_state.down_ask == 0:
                                price_state.down_ask = price
                    except ValueError:
                        pass
                
                price_state.last_polymarket_update = time.time()
                price_state.last_update = time.time()  # Keep for backwards compat
                
            except Exception as e:
                logger.debug(f"WS message parse error: {e}")
        
        def on_error(ws, error):
            logger.error(f"Polymarket WS error: {error}")  # Full error to log
            add_message(f"PM WS: {format_error_short(error)}", "critical")
        
        def on_close(ws, close_status_code, close_msg):
            logger.info("Polymarket WS closed")
            price_state.ws_connected = False
            add_message("Polymarket WS disconnected", "warn")
        
        def on_open(ws):
            logger.info("Polymarket WS connected")
            price_state.ws_connected = True
            subscribe_msg = {
                "auth": {},
                "type": "MARKET",
                "assets_ids": [self.tokens["up"], self.tokens["down"]]
            }
            ws.send(json.dumps(subscribe_msg))
            logger.info(f"Subscribed to UP: {self.tokens['up'][:30]}...")
            logger.info(f"Subscribed to DOWN: {self.tokens['down'][:30]}...")
        
        def run_ws():
            while not self.stop_event.is_set():
                try:
                    self.ws_app = websocket.WebSocketApp(
                        POLYMARKET_WS,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close,
                        on_open=on_open
                    )
                    self.ws_app.run_forever()
                except Exception as e:
                    logger.error(f"Polymarket WS error: {e}")
                
                if self.stop_event.is_set():
                    break
                time.sleep(2)
            
            logger.info("Polymarket WS thread exiting")
        
        self.thread = threading.Thread(target=run_ws, daemon=True)
        self.thread.start()
        
        # Fetch initial prices via REST
        self._fetch_initial_prices()
    
    def _fetch_initial_prices(self):
        """Fetch initial prices from REST API."""
        try:
            resp = requests.get(f"https://clob.polymarket.com/book?token_id={self.tokens['up']}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                asks = data.get("asks", [])
                if asks:
                    price_state.up_ask = float(asks[0].get("price", 0))
                    logger.info(f"Initial UP ask: {price_state.up_ask}")
        except Exception as e:
            logger.debug(f"Failed to fetch UP price: {e}")
        
        try:
            resp = requests.get(f"https://clob.polymarket.com/book?token_id={self.tokens['down']}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                asks = data.get("asks", [])
                if asks:
                    price_state.down_ask = float(asks[0].get("price", 0))
                    logger.info(f"Initial DOWN ask: {price_state.down_ask}")
        except Exception as e:
            logger.debug(f"Failed to fetch DOWN price: {e}")
    
    def switch_market(self, up_token, down_token):
        """Switch to new market - close old WS and start new."""
        logger.info("Switching market - stopping old WebSocket...")
        self.stop()
        
        # Reset prices for new market
        price_state.up_ask = 0.0
        price_state.down_ask = 0.0
        price_state.up_bid = 0.0
        price_state.down_bid = 0.0
        
        # Wait briefly for old thread to terminate
        time.sleep(0.5)
        
        logger.info(f"Starting new WebSocket for market...")
        self.start(up_token, down_token)
    
    def stop(self):
        """Stop WebSocket connection."""
        self.stop_event.set()
        self.running = False
        
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception as e:
                logger.debug(f"WS close error: {e}")
        
        # Wait for thread to finish
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)


# Global feed manager instance
polymarket_feed = PolymarketFeedManager()
feed_manager = polymarket_feed  # Assign to global for signal handler


# PricePoller removed - WebSocket provides real-time prices, REST polling caused Cloudflare rate limits


# Position and trade tracking
class PositionTracker:
    def __init__(self):
        self.open_positions = []  # List of open trades
        self.closed_trades = []   # List of completed trades
        self.session_trades = 0   # Total trades this session
        self.session_wins = 0     # Winning trades
        self.session_pnl = 0.0    # Total P/L in USDC
        self.user_ws_connected = False
        self.current_market_id = None  # Current market condition_id
        self.token_to_side = {}   # Map token_id -> "UP" or "DOWN"
        
        # Accumulators for average position tracking (per market)
        self._lock = threading.Lock()
        self.up_total_cost = 0.0    # Total $ spent on UP
        self.up_total_size = 0.0    # Total shares of UP
        self.down_total_cost = 0.0  # Total $ spent on DOWN
        self.down_total_size = 0.0  # Total shares of DOWN
        
        # Realized P/L tracking (per market session)
        self.realized_pnl = 0.0     # Accumulated realized P/L from sells
        self.sell_history = []       # List of all sells this session
        
        # BTC price at market start for deviation tracking
        self.start_btc_price = 0.0
        self.btc_deviation_valid = False  # Only True if captured at real market start
    
    def add_open_position(self, side, price, size, token_id, trade_id=None):
        """Add a new open position.
        
        Args:
            side: "UP" or "DOWN"
            price: Token price (e.g. 0.50)
            size: USD spent on this order
            token_id: Token ID for this side
            trade_id: Optional trade ID
        """
        # Calculate shares received: USD / price
        cost = float(size)  # USD spent
        shares = cost / float(price) if float(price) > 0 else 0  # Tokens received
        
        position = {
            "side": side,
            "price": price,
            "size": size,       # USD spent (kept for display)
            "cost": cost,       # USD spent (for P/L calculation)
            "shares": shares,   # Tokens received
            "token_id": token_id,
            "trade_id": trade_id,
            "time": time.time(),
            "status": "OPEN"
        }
        
        with self._lock:
            self.open_positions.append(position)
            self.session_trades += 1
            
            # Accumulate for average calculation
            if side == "UP":
                self.up_total_cost += cost
                self.up_total_size += shares
            elif side == "DOWN":
                self.down_total_cost += cost
                self.down_total_size += shares
        
        logger.info(f"Position opened: {side} ${size} @ {price} = {shares:.2f} shares")
        return position
    
    def remove_failed_position(self, position):
        """Remove a failed position and reverse accumulator."""
        with self._lock:
            if position in self.open_positions:
                self.open_positions.remove(position)
                self.session_trades -= 1
                
                # Reverse the accumulator using stored values
                side = position.get("side")
                cost = float(position.get("cost", 0))
                shares = float(position.get("shares", 0))
                
                if side == "UP":
                    self.up_total_cost -= cost
                    self.up_total_size -= shares
                elif side == "DOWN":
                    self.down_total_cost -= cost
                    self.down_total_size -= shares
                
                logger.info(f"Removed failed position: {side} ${cost} ({shares:.2f} shares)")
                return True
        return False
    
    def get_avg_up_price(self):
        """Get average UP position price (cost-weighted)."""
        with self._lock:
            if self.up_total_size > 0:
                return self.up_total_cost / self.up_total_size
            return 0.0
    
    def get_avg_down_price(self):
        """Get average DOWN position price (cost-weighted)."""
        with self._lock:
            if self.down_total_size > 0:
                return self.down_total_cost / self.down_total_size
            return 0.0
    
    def get_pair_cost(self):
        """Get combined pair cost (avg UP + avg DOWN)."""
        avg_up = self.get_avg_up_price()
        avg_down = self.get_avg_down_price()
        if avg_up > 0 and avg_down > 0:
            return avg_up + avg_down
        return 0.0
    
    def get_position_summary(self):
        """Get summary of current market positions."""
        with self._lock:
            up_trades = sum(1 for p in self.open_positions if p.get("side") == "UP")
            down_trades = sum(1 for p in self.open_positions if p.get("side") == "DOWN")
            
            return {
                "up_avg": self.up_total_cost / self.up_total_size if self.up_total_size > 0 else 0,
                "up_trades": up_trades,
                "up_total": self.up_total_cost,
                "down_avg": self.down_total_cost / self.down_total_size if self.down_total_size > 0 else 0,
                "down_trades": down_trades,
                "down_total": self.down_total_cost,
            }
    
    def get_pnl_scenarios(self):
        """Calculate P/L for each outcome.
        
        Returns:
            (pnl_if_up_wins, pnl_if_down_wins): Profit/loss for each outcome
        """
        with self._lock:
            total_cost = self.up_total_cost + self.down_total_cost
            
            # If UP wins: UP tokens pay $1 each, DOWN tokens worth $0
            # Payout = up_total_size × $1 (shares bought at various prices)
            payout_if_up = self.up_total_size  # Each share pays $1
            pnl_if_up_wins = payout_if_up - total_cost
            
            # If DOWN wins: DOWN tokens pay $1 each, UP tokens worth $0
            payout_if_down = self.down_total_size
            pnl_if_down_wins = payout_if_down - total_cost
            
            return pnl_if_up_wins, pnl_if_down_wins
    
    def get_paired_analysis(self):
        """Analyze positions for PAIRED/UNPAIRED contracts (fractional support).
        
        Key concept: For arbitrage lock, you need EQUAL NUMBER OF CONTRACTS
        on both sides, not equal dollar amounts.
        
        Returns dict with:
        - paired_contracts: Number of contracts that are matched UP+DOWN
        - unpaired_up: Excess UP contracts (at risk if DOWN wins)
        - unpaired_down: Excess DOWN contracts (at risk if UP wins)
        - paired_cost: Cost of paired contracts (locked arbitrage)
        - paired_payout: Payout for paired contracts ($1 per pair)
        - locked_profit: Guaranteed profit from paired contracts
        """
        with self._lock:
            up_contracts = self.up_total_size      # Fractional contracts (shares)
            down_contracts = self.down_total_size
            
            # Paired = min of both sides
            paired = min(up_contracts, down_contracts)
            unpaired_up = up_contracts - paired
            unpaired_down = down_contracts - paired
            
            # Calculate average prices
            avg_up = self.up_total_cost / up_contracts if up_contracts > 0 else 0
            avg_down = self.down_total_cost / down_contracts if down_contracts > 0 else 0
            
            # Cost of paired contracts: proportional allocation from ACTUAL costs
            # If 25.2 UP paired out of 25.2 total: use 100% of UP cost
            # If 25.2 DN paired out of 31.9 total: use (25.2/31.9) of DN cost
            if paired > 0:
                up_paired_cost = (paired / up_contracts) * self.up_total_cost if up_contracts > 0 else 0
                dn_paired_cost = (paired / down_contracts) * self.down_total_cost if down_contracts > 0 else 0
                paired_cost = up_paired_cost + dn_paired_cost
            else:
                paired_cost = 0
            paired_payout = paired * 1.0  # Each pair pays $1
            locked_profit = paired_payout - paired_cost
            
            # Risk analysis for unpaired
            unpaired_risk = None
            if unpaired_up > 0:
                unpaired_cost = unpaired_up * avg_up
                unpaired_risk = {
                    "side": "UP",
                    "contracts": unpaired_up,
                    "cost": unpaired_cost,
                    "if_wins": unpaired_up * 1.0 - unpaired_cost,  # Profit if UP wins
                    "if_loses": -unpaired_cost  # Loss if DOWN wins
                }
            elif unpaired_down > 0:
                unpaired_cost = unpaired_down * avg_down
                unpaired_risk = {
                    "side": "DOWN",
                    "contracts": unpaired_down,
                    "cost": unpaired_cost,
                    "if_wins": unpaired_down * 1.0 - unpaired_cost,  # Profit if DOWN wins
                    "if_loses": -unpaired_cost  # Loss if UP wins
                }
            
            return {
                "up_contracts": up_contracts,
                "down_contracts": down_contracts,
                "paired_contracts": paired,
                "unpaired_up": unpaired_up,
                "unpaired_down": unpaired_down,
                "paired_cost": paired_cost,
                "paired_payout": paired_payout,
                "locked_profit": locked_profit,
                "avg_up": avg_up,
                "avg_down": avg_down,
                "unpaired_risk": unpaired_risk
            }
    
    def get_buy_recommendation(self, current_up_ask, current_down_ask):
        """Get recommendation for completing paired position.
        
        Returns dict with:
        - side: Which side to buy ('UP' or 'DOWN')
        - contracts: How many to buy to complete pairs
        - price: Current ask price for that side
        - combined_price: avg existing + current ask
        - color: 'green' (< 0.98), 'yellow' (0.98-1.00), 'red' (> 1.00)
        """
        analysis = self.get_paired_analysis()
        
        if analysis["unpaired_up"] > 0 and current_down_ask > 0:
            combined = analysis["avg_up"] + current_down_ask
            contracts = analysis["unpaired_up"]
            add_cost = contracts * current_down_ask
            
            if combined < 0.98:
                color = "green"
            elif combined <= 1.00:
                color = "yellow"
            else:
                color = "red"
            
            return {
                "side": "DOWN",
                "contracts": contracts,
                "price": current_down_ask,
                "combined_price": combined,
                "add_cost": add_cost,
                "lock_profit": contracts * (1.0 - combined),
                "color": color
            }
        elif analysis["unpaired_down"] > 0 and current_up_ask > 0:
            combined = analysis["avg_down"] + current_up_ask
            contracts = analysis["unpaired_down"]
            add_cost = contracts * current_up_ask
            
            if combined < 0.98:
                color = "green"
            elif combined <= 1.00:
                color = "yellow"
            else:
                color = "red"
            
            return {
                "side": "UP",
                "contracts": contracts,
                "price": current_up_ask,
                "combined_price": combined,
                "add_cost": add_cost,
                "lock_profit": contracts * (1.0 - combined),
                "color": color
            }
        
        return None
    
    def close_position(self, token_id, won, payout_per_share=1.0):
        """Close a position and record result (used for market settlement).
        
        Args:
            token_id: Token ID to close
            won: Whether this side won
            payout_per_share: Payout per share (default $1 for winning side)
        """
        for pos in self.open_positions:
            if pos.get("token_id") == token_id:
                pos["status"] = "CLOSED"
                cost = float(pos.get("cost", pos.get("size", 0)))
                shares = float(pos.get("shares", 0))
                
                if won:
                    revenue = payout_per_share * shares
                    profit = revenue - cost
                    self.session_wins += 1
                else:
                    profit = -cost
                
                self.session_pnl += profit
                
                closed = {**pos, "profit": profit, "won": won}
                self.closed_trades.append(closed)
                self.open_positions.remove(pos)
                
                logger.info(f"Position closed: {pos['side']} P/L: ${profit:.2f}")
                return closed
        return None
    
    def close_all_side_positions(self, side, sale_price):
        """Close all positions for a side after SELL, calculate P/L, reset accumulators.
        
        Args:
            side: "UP" or "DOWN"
            sale_price: Price at which positions were sold
        
        Returns:
            Total profit/loss for the closed positions
        """
        with self._lock:
            positions_to_close = [p for p in self.open_positions if p.get("side") == side]
            
            if not positions_to_close:
                return 0.0
            
            total_profit = 0.0
            wins = 0
            
            # Close each position with its individual P/L
            for pos in positions_to_close:
                cost = float(pos.get("cost", 0))
                shares = float(pos.get("shares", 0))
                
                # Revenue = sale_price × shares
                revenue = sale_price * shares
                profit = revenue - cost
                
                closed = {
                    **pos,
                    "status": "CLOSED",
                    "sale_price": sale_price,
                    "profit": profit
                }
                self.closed_trades.append(closed)
                self.open_positions.remove(pos)
                
                total_profit += profit
                if profit > 0:
                    wins += 1
            
            # Update session stats
            self.session_pnl += total_profit
            self.session_wins += wins
            
            # Reset accumulators for this side
            if side == "UP":
                self.up_total_cost = 0.0
                self.up_total_size = 0.0
            else:
                self.down_total_cost = 0.0
                self.down_total_size = 0.0
            
            logger.info(f"Closed all {side} positions: {len(positions_to_close)} trades, P/L: ${total_profit:.2f}")
            return total_profit
    
    def get_open_positions_display(self):
        """Get formatted string for open positions."""
        if not self.open_positions:
            return "No open positions"
        
        lines = []
        for pos in self.open_positions:
            side = pos["side"]
            price = float(pos["price"])
            size = float(pos["size"])
            lines.append(f"{side} ${size:.0f} @ {price:.2f}")
        return " | ".join(lines)
    
    def get_stats_display(self):
        """Get formatted stats string."""
        win_rate = (self.session_wins / self.session_trades * 100) if self.session_trades > 0 else 0
        pnl_color = Colors.GREEN if self.session_pnl >= 0 else Colors.RED
        pnl_sign = "+" if self.session_pnl >= 0 else ""
        return f"Trades: {self.session_trades}  Won: {self.session_wins} ({win_rate:.0f}%)  P/L: {pnl_color}{pnl_sign}${self.session_pnl:.2f}{Colors.RESET}"
    
    def set_market_tokens(self, market_id, up_token, down_token, is_new_market=False):
        """Map tokens to sides for current market and reset accumulators.
        
        Args:
            market_id: Condition ID
            up_token: UP token ID
            down_token: DOWN token ID
            is_new_market: True if this is a market transition (not initial startup)
        """
        with self._lock:
            self.current_market_id = market_id
            self.token_to_side = {
                up_token: "UP",
                down_token: "DOWN"
            }
            
            # Reset accumulators for new market
            self.up_total_cost = 0.0
            self.up_total_size = 0.0
            self.down_total_cost = 0.0
            self.down_total_size = 0.0
            
            # Reset realized P/L for new market session
            self.realized_pnl = 0.0
            self.sell_history = []
            self.session_pnl = 0.0  # Total realized P/L this market
            
            # PTB (Price To Beat) - only set on market switch, not on startup
            # If terminal opens mid-market, PTB stays 0 until next market switch
            if is_new_market:
                # Capture current BTC price as PTB at market transition
                current_btc = price_state.btc_price
                if current_btc > 0:
                    self.start_btc_price = current_btc
                    self.btc_deviation_valid = True  # Enable deviation display
                    logger.info(f"PTB fixed at market switch: ${current_btc:,.0f}")
                    add_message(f"PTB: ${current_btc:,.0f}", "info")
                else:
                    self.start_btc_price = 0.0
                    self.btc_deviation_valid = False
            else:
                # Startup - don't show PTB until next market switch
                self.start_btc_price = 0.0
                self.btc_deviation_valid = False
            
        logger.info(f"Market tokens set, accumulators reset for: {market_id[:20] if market_id else 'N/A'}... (new_market={is_new_market})")
    
    def set_start_btc_price(self, price):
        """Set the starting BTC price for deviation tracking."""
        with self._lock:
            if self.start_btc_price == 0.0 and price > 0:
                self.start_btc_price = price
                logger.info(f"Start BTC price fixed: ${price:.2f}")
    
    def get_btc_deviation(self, current_price):
        """Get BTC price deviation from market start.
        
        Returns None if:
        - BTC price wasn't captured at real market start (terminal opened mid-cycle)
        - No start price or current price available
        """
        with self._lock:
            if not self.btc_deviation_valid:
                return None  # Don't show if terminal opened mid-cycle
            if self.start_btc_price > 0 and current_price > 0:
                return current_price - self.start_btc_price
            return None

tracker = PositionTracker()


def print_status(message, status="info", force_print=False):
    """Print compact status message and add to message queue.
    
    In quiet mode, only 'success', 'error', and 'critical' messages are shown.
    force_print=True bypasses quiet mode for startup messages.
    """
    logger.info(f"[{status.upper()}] {message}")
    
    add_message(message, status)
    
    show_in_terminal = (
        force_print or 
        not is_quiet_mode() or 
        status in ("success", "error", "critical")
    )


def validate_config():
    """Validate configuration."""
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        print_status("PRIVATE_KEY must be set and start with 0x", "error")
        sys.exit(1)
    
    if SIGNATURE_TYPE in [1, 2] and not FUNDER_ADDRESS:
        print_status("FUNDER_ADDRESS is required for signature types 1 and 2", "error")
        sys.exit(1)
    
    from eth_account import Account
    wallet = Account.from_key(PRIVATE_KEY)
    logger.info(f"Wallet: {wallet.address}, SigType: {SIGNATURE_TYPE}, Size: ${TRADE_SIZE_USDC}")


def find_active_market(crypto_slug="btc"):
    """Find active 15-minute market for specified cryptocurrency.
    
    Args:
        crypto_slug: Crypto identifier for market slug (e.g., "btc", "eth", "sol", "xrp")
    
    Returns:
        dict: Market data or None if not found
    """
    now = int(time.time())
    current_slot = (now // 900) * 900
    slots_to_try = [current_slot, current_slot - 900, current_slot + 900]
    
    for slot in slots_to_try:
        slug = f"{crypto_slug}-updown-15m-{slot}"
        logger.debug(f"Trying slot: {slug}")
        
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
                                
                                market_end_time = (slot + 900) * 1000
                                
                                logger.info(f"Found market: {slug}")
                                return {
                                    "slug": slug,
                                    "question": market.get("question", event.get("title", slug)),
                                    "condition_id": market.get("conditionId"),
                                    "up_token_id": clob_token_ids[up_index],
                                    "down_token_id": clob_token_ids[down_index],
                                    "end_time": market_end_time,
                                    "neg_risk": market.get("negRisk", True),
                                }
        except Exception as e:
            logger.debug(f"Error checking {slug}: {e}")
            continue
    
    return None


def init_client():
    """Initialize CLOB client."""
    logger.info("Initializing CLOB client...")
    
    try:
        if SIGNATURE_TYPE == 0:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
        elif SIGNATURE_TYPE == 1:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=1, funder=FUNDER_ADDRESS)
        else:
            client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=2, funder=FUNDER_ADDRESS)
        
        if POLY_API_KEY and POLY_API_SECRET and POLY_API_PASSPHRASE:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(api_key=POLY_API_KEY, api_secret=POLY_API_SECRET, api_passphrase=POLY_API_PASSPHRASE)
        else:
            creds = client.create_or_derive_api_creds()
        
        client.set_api_creds(creds)
        logger.info("Client initialized successfully")
        return client
    
    except Exception as e:
        logger.error(f"Failed to initialize client: {e}")
        return None


def start_chainlink_ws(crypto_symbol="btc/usd"):
    """Start Polymarket RTDS WebSocket for Chainlink crypto price.
    
    Args:
        crypto_symbol: Crypto symbol for Chainlink (e.g., "btc/usd", "eth/usd", "sol/usd", "xrp/usd")
    
    Subscribes to crypto_prices_chainlink topic for specified symbol.
    """
    global binance_ws  # Reuse global for shutdown handling
    
    def on_message(ws, message):
        try:
            data = json.loads(message)
            # RTDS Chainlink format: {"topic": "crypto_prices_chainlink", "payload": {"symbol": "btc/usd", "value": 104567.89}}
            if data.get("topic") == "crypto_prices_chainlink":
                payload = data.get("payload", {})
                if payload.get("symbol") == crypto_symbol:
                    crypto_price = float(payload.get("value", 0))
                    # Round to integers as requested
                    price_state.btc_price = round(crypto_price)  # Keep field name for backward compat
                    price_state.last_binance_update = time.time()  # Keep field name for compat
                    price_state.last_update = time.time()
                    
                    # Fix start price for deviation tracking
                    tracker.set_start_btc_price(round(crypto_price))
        except:
            pass
    
    def on_error(ws, error):
        logger.error(f"Chainlink WS error: {error}")
        add_message(f"Chainlink WS: {format_error_short(error)}", "critical")
    
    def on_close(ws, close_status_code, close_msg):
        logger.info("Chainlink WS closed")
        add_message("Chainlink WS disconnected", "warn")
    
    def on_open(ws):
        logger.info(f"Chainlink WS connected, subscribing to {crypto_symbol}")
        # Subscribe to Chainlink crypto price
        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": f'{{"symbol":"{crypto_symbol}"}}'
            }]
        }
        ws.send(json.dumps(subscribe_msg))
    
    def run_ws():
        global binance_ws
        logger.info("Chainlink WS loop starting")
        
        while True:
            if shutdown_requested:
                logger.info("Chainlink WS shutdown requested before connect, exiting")
                break
            
            try:
                ws = websocket.WebSocketApp(
                    RTDS_WS,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                    on_open=on_open
                )
                binance_ws = ws  # Reuse for shutdown handling
                ws.run_forever()
            except Exception as e:
                logger.error(f"Chainlink WS error: {e}")
            finally:
                binance_ws = None
            
            if shutdown_requested:
                logger.info("Chainlink WS shutdown requested after disconnect, exiting")
                break
            
            time.sleep(2)
        
        logger.info("Chainlink WS loop exited cleanly")
    
    thread = threading.Thread(target=run_ws, daemon=True)
    thread.start()


def start_user_channel_ws():
    """Start User Channel WebSocket for trade/order notifications."""
    if not POLY_API_KEY or not POLY_API_SECRET or not POLY_API_PASSPHRASE:
        logger.warning("API credentials not set, User Channel disabled")
        return
    
    def on_message(ws, message):
        try:
            data = json.loads(message)
            event_type = data.get("event_type", "")
            
            logger.debug(f"User WS: {event_type} - {json.dumps(data)[:200]}")
            
            # Handle trade events
            if event_type == "trade":
                status = data.get("status", "")
                asset_id = data.get("asset_id", "")
                side_str = data.get("side", "")
                price = data.get("price", "0")
                size = data.get("size", "0")
                trade_id = data.get("id", "")
                taker_order_id = data.get("taker_order_id", "")
                
                side = tracker.token_to_side.get(asset_id, "?")
                
                # Find matching position by taker_order_id first (this is the CLOB orderID)
                matched_pos = None
                for pos in tracker.open_positions:
                    if pos.get("trade_id") == taker_order_id:
                        matched_pos = pos
                        break
                
                if status == "MATCHED":
                    logger.info(f"Trade MATCHED: {side} ${size} @ {price} (order: {taker_order_id[:20] if taker_order_id else 'N/A'}...)")
                    if matched_pos:
                        matched_pos["ws_trade_id"] = trade_id
                        matched_pos["matched"] = True
                        logger.debug(f"Updated position with ws_trade_id: {trade_id[:20]}...")
                    
                elif status == "CONFIRMED":
                    logger.info(f"Trade CONFIRMED: {side} ${size} @ {price}")
                    if matched_pos:
                        matched_pos["confirmed"] = True
                        logger.debug(f"Position confirmed")
                    
                elif status == "FAILED":
                    logger.error(f"Trade FAILED: {side} - removing from open positions")
                    if matched_pos:
                        tracker.remove_failed_position(matched_pos)
            
            # Handle order events
            elif event_type == "order":
                order_type = data.get("type", "")
                asset_id = data.get("asset_id", "")
                side = tracker.token_to_side.get(asset_id, "?")
                
                if order_type == "PLACEMENT":
                    logger.info(f"Order placed: {side}")
                elif order_type == "CANCELLATION":
                    logger.info(f"Order cancelled: {side}")
                elif order_type == "UPDATE":
                    size_matched = data.get("size_matched", "0")
                    logger.info(f"Order updated: {side} matched={size_matched}")
            
        except Exception as e:
            logger.debug(f"User WS parse error: {e}")
    
    def on_error(ws, error):
        logger.error(f"User WS error: {error}")  # Full error to log
        tracker.user_ws_connected = False
        add_message(f"User WS: {format_error_short(error)}", "critical")
    
    def on_close(ws, close_status_code, close_msg):
        logger.info("User WS closed")
        tracker.user_ws_connected = False
        add_message("User WS disconnected", "warn")
    
    def on_open(ws):
        logger.info("User WS connected")
        tracker.user_ws_connected = True
        
        # Subscribe with authentication (lowercase field names per API docs)
        subscribe_msg = {
            "auth": {
                "apikey": POLY_API_KEY,
                "secret": POLY_API_SECRET,
                "passphrase": POLY_API_PASSPHRASE
            },
            "type": "subscribe"
        }
        ws.send(json.dumps(subscribe_msg))
        logger.info("User Channel subscribed")
    
    def run_ws():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    POLYMARKET_USER_WS,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                    on_open=on_open
                )
                ws.run_forever()
            except Exception as e:
                logger.error(f"User WS reconnect: {e}")
            time.sleep(3)
    
    thread = threading.Thread(target=run_ws, daemon=True)
    thread.start()
    logger.info("User Channel WS thread started")


def place_order(client, market_data, side):
    """Place a market order for a specified number of contracts."""
    global current_contracts_size, order_mode
    
    if not price_state.is_fresh():
        age = price_state.get_age()
        add_message(f"Prices stale ({age:.1f}s old), wait for WS", "warn")
        logger.warning(f"Order rejected: prices stale ({age:.1f}s)")
        return None
    
    token_id = market_data["up_token_id"] if side == "UP" else market_data["down_token_id"]
    
    # Get current ask price for the side we're buying
    ask_price = price_state.up_ask if side == "UP" else price_state.down_ask
    
    if ask_price <= 0:
        add_message(f"No ask price for {side}", "error")
        return None
    
    # OrderArgs expects: size = CONTRACT COUNT, price = price per contract
    # Library internally calculates: maker_amount = size * price (USDC)
    # Round price UP to 2 decimals to ensure limit >= ask (order fills)
    import math
    contracts = current_contracts_size
    normalized_price = math.ceil(ask_price * 100) / 100  # Round UP to 2 decimals
    
    # Polymarket minimum order is $1 - adjust contracts if needed
    min_contracts_for_dollar = math.ceil(1.0 / normalized_price)
    if contracts < min_contracts_for_dollar:
        contracts = min_contracts_for_dollar
        logger.info(f"Adjusted to {contracts} contracts (min $1 order)")
    
    usdc_cost = round(contracts * normalized_price, 2)
    
    logger.info(f"Placing {side} order: {contracts} contracts @ ${normalized_price:.2f} = ${usdc_cost:.2f}")
    
    try:
        start_time = time.time()
        
        order_args = OrderArgs(
            price=normalized_price,
            size=contracts,  # CONTRACT COUNT, not USDC!
            side=BUY,
            token_id=token_id,
        )
        
        signed_order = client.create_order(order_args)
        # Use selected order mode (FOK = full fill only, FAK = partial fills OK)
        selected_order_type = OrderType.FAK if order_mode == "FAK" else OrderType.FOK
        result = client.post_order(signed_order, selected_order_type)
        
        elapsed = int((time.time() - start_time) * 1000)
        
        if result.get("success"):
            print_status(f"{side} {contracts} filled! {order_mode} ({elapsed}ms)", "success")
            order_id = result.get('orderID', 'N/A')
            logger.info(f"Order filled: {order_id}, {contracts} contracts")
            
            # Track the position (size = USDC cost, contracts = shares)
            tracker.add_open_position(
                side=side,
                price=normalized_price,
                size=usdc_cost,
                token_id=token_id,
                trade_id=order_id
            )
            
            return True
        else:
            error_msg = result.get("errorMsg", "Unknown error")
            print_status(f"Order rejected: {error_msg}", "error")
            logger.error(f"Order rejected: {json.dumps(result)}")
            return False
    
    except Exception as e:
        error_str = str(e)
        # Check for FOK not filled (common case - not enough liquidity)
        if "FOK" in error_str and "fully filled" in error_str:
            print_status(f"{side} NOT FILLED (no liquidity)", "warn")
            logger.warning(f"FOK order not filled: {side} {contracts} @ ${normalized_price:.2f}")
            add_message(f"{side} not filled - try again", "warn")
        else:
            short_error = format_error_short(e)
            print_status(f"Order failed: {short_error}", "error")
            logger.exception(f"Order exception: {e}")
            add_message(f"ORDER FAILED: {short_error}", "critical")
        return False


def sell_all_position(client, market_data, side):
    """Sell all tokens of a given side (UP or DOWN)."""
    from web3 import Web3
    from py_clob_client.order_builder.constants import SELL
    
    token_id = market_data["up_token_id"] if side == "UP" else market_data["down_token_id"]
    
    # CTF contract address and ABI for balanceOf
    CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    CTF_ABI = [{"inputs": [{"name": "_owner", "type": "address"}, {"name": "_id", "type": "uint256"}], 
                "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], 
                "stateMutability": "view", "type": "function"}]
    
    try:
        # Get wallet address based on SIGNATURE_TYPE
        from eth_account import Account
        
        # For SIGNATURE_TYPE 0: tokens on PRIVATE_KEY address
        # For SIGNATURE_TYPE 1/2: tokens on FUNDER_ADDRESS (proxy wallet)
        if SIGNATURE_TYPE == 0:
            wallet = Account.from_key(PRIVATE_KEY).address
        else:
            if not FUNDER_ADDRESS:
                print_status(f"SIGNATURE_TYPE={SIGNATURE_TYPE} requires FUNDER_ADDRESS", "error")
                return False
            wallet = FUNDER_ADDRESS
        
        # DEBUG: Print which address we're checking
        logger.info(f"Checking {side} token balance on: {wallet[:10]}...{wallet[-8:]} (SIGNATURE_TYPE={SIGNATURE_TYPE})")
        print(f"{Colors.DIM}Checking balance on {wallet[:10]}...{wallet[-8:]} (type {SIGNATURE_TYPE}){Colors.RESET}")
        
        # Query token balance (with small delay to ensure blockchain state updated)
        time.sleep(0.3)
        RPC_URL = os.getenv("RPC_URL", "https://polygon-mainnet.g.alchemy.com/v2/IZ9LcPHnEBGEAQxYrTZkk")
        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={'timeout': 10}))
        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        
        balance_raw = ctf.functions.balanceOf(Web3.to_checksum_address(wallet), int(token_id)).call()
        balance = balance_raw / 1e6  # Convert from raw to USDC decimals
        
        # DEBUG: Print balance found
        logger.info(f"Token balance found: {balance:.2f} (raw: {balance_raw})")
        print(f"{Colors.DIM}Balance: {balance:.2f} tokens{Colors.RESET}")
        
        if balance <= 0:
            print_status(f"No {side} tokens to sell", "warn")
            logger.warning(f"Sell {side}: No balance to sell on {wallet}")
            return False
        
        logger.info(f"Selling {side}: {balance:.2f} tokens")
        
        # Get current bid price for selling
        bid_price = price_state.up_bid if side == "UP" else price_state.down_bid
        if bid_price <= 0:
            bid_price = 0.01  # Minimum price to ensure FOK fills
        
        start_time = time.time()
        
        order_args = OrderArgs(
            price=0.01,  # Low price to ensure FOK fills at any available price
            size=balance,
            side=SELL,
            token_id=token_id,
        )
        
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, OrderType.FOK)
        
        elapsed = int((time.time() - start_time) * 1000)
        
        if result.get("success"):
            # Close positions in tracker and calculate P/L
            profit = tracker.close_all_side_positions(side, bid_price)
            pnl_str = f"+${profit:.2f}" if profit >= 0 else f"-${abs(profit):.2f}"
            
            # Update token_balance_state after selling
            if side == "UP":
                token_balance_state.up_balance = 0
                token_balance_state.up_avg_price = 0
                token_balance_state.up_invested = 0
            else:
                token_balance_state.down_balance = 0
                token_balance_state.down_avg_price = 0
                token_balance_state.down_invested = 0
            
            print_status(f"SOLD {side} {balance:.2f} tokens! P/L: {pnl_str} ({elapsed}ms)", "success")
            logger.info(f"Sell order filled: {result.get('orderID', 'N/A')}, P/L: {pnl_str}")
            add_message(f"SOLD {side} {balance:.2f} P/L: {pnl_str}", "success")
            return True
        else:
            error_msg = result.get("errorMsg", "Unknown error")
            print_status(f"Sell rejected: {error_msg}", "error")
            logger.error(f"Sell rejected: {json.dumps(result)}")
            return False
    
    except Exception as e:
        short_error = format_error_short(e)
        print_status(f"Sell failed: {short_error}", "error")
        logger.exception(f"Sell exception: {e}")  # Full error to log
        add_message(f"SELL FAILED: {short_error}", "critical")
        return False


def format_time_remaining(end_time_ms):
    """Format time remaining until market closes."""
    now = int(time.time() * 1000)
    remaining = end_time_ms - now
    
    if remaining <= 0:
        return "CLOSED", Colors.RED
    
    minutes = remaining // 60000
    seconds = (remaining % 60000) // 1000
    
    if minutes > 0:
        text = f"{minutes:02d}:{seconds:02d}"
    else:
        text = f"00:{seconds:02d}"
    
    if remaining < 60000:
        color = Colors.RED
    elif remaining < 180000:
        color = Colors.YELLOW
    else:
        color = Colors.GREEN
    
    return text, color


def format_money(value, width=7, show_sign=False):
    """Format money with fixed width, optional sign."""
    if show_sign:
        if value >= 0:
            return f"+${value:>{width-2}.2f}"
        else:
            return f"-${abs(value):>{width-2}.2f}"
    else:
        return f"${value:>{width-1}.2f}"


def display_dashboard(market_data):
    """Display trading dashboard with clean hierarchy and visual emphasis."""
    os.system("clear" if os.name != "nt" else "cls")
    
    if not market_data:
        add_message("No market data", "error")
        return
    
    W = 60
    
    time_remaining, time_color = format_time_remaining(market_data["end_time"])
    
    btc = price_state.btc_price
    up_ask = price_state.up_ask
    down_ask = price_state.down_ask
    up_bid = price_state.up_bid
    down_bid = price_state.down_bid
    
    # Get analysis data
    analysis = tracker.get_paired_analysis()
    recommendation = tracker.get_buy_recommendation(up_ask, down_ask)
    
    up_contracts = analysis["up_contracts"]
    down_contracts = analysis["down_contracts"]
    avg_up = analysis["avg_up"]
    avg_down = analysis["avg_down"]
    unpaired_up = analysis["unpaired_up"]
    unpaired_down = analysis["unpaired_down"]
    locked_profit = analysis["locked_profit"]
    
    total_invested = tracker.up_total_cost + tracker.down_total_cost
    
    # Calculate P/L scenarios (including Data API positions)
    # Use total balances (tracked + Data API)
    total_up_for_outcome = up_contracts if up_contracts > 0 else (token_balance_state.up_balance if token_balance_state.last_update > 0 else 0)
    total_down_for_outcome = down_contracts if down_contracts > 0 else (token_balance_state.down_balance if token_balance_state.last_update > 0 else 0)
    total_invested_for_outcome = total_invested if total_invested > 0 else (token_balance_state.up_invested + token_balance_state.down_invested if token_balance_state.last_update > 0 else 0)
    
    if_up_wins_pl = (total_up_for_outcome * 1.0) - total_invested_for_outcome
    if_down_wins_pl = (total_down_for_outcome * 1.0) - total_invested_for_outcome
    
    messages_block = format_messages_block(10)
    
    # BTC deviation from market start (PTB = Price To Beat)
    ptb = tracker.start_btc_price
    deviation = tracker.get_btc_deviation(btc)
    
    if ptb > 0 and deviation is not None:
        pct = (deviation / ptb) * 100
        dev_abs = int(deviation)
        if deviation >= 0:
            dev_str = f"{Colors.GREEN}+${dev_abs:,} (+{pct:.2f}%){Colors.RESET}"
        else:
            dev_str = f"{Colors.RED}-${abs(dev_abs):,} ({pct:.2f}%){Colors.RESET}"
        ptb_str = f"{Colors.DIM}PTB ${ptb:,.0f}{Colors.RESET}"
    else:
        dev_str = ""
        ptb_str = f"{Colors.DIM}PTB ...{Colors.RESET}"
    
    # Wallet balance + session P/L
    bal = balance_state.current_balance
    start_bal = balance_state.session_start_balance
    if bal > 0:
        session_pl = bal - start_bal
        if session_pl >= 0:
            bal_str = f"${bal:.2f} {Colors.GREEN}(+${session_pl:.2f}){Colors.RESET}"
        else:
            bal_str = f"${bal:.2f} {Colors.RED}(-${abs(session_pl):.2f}){Colors.RESET}"
    else:
        bal_str = f"{Colors.DIM}$...{Colors.RESET}"
    
    # ═══════════════════════════════════════════════════════════════════════
    # HEADER - Crypto price + PTB + deviation + balance + time
    # ═══════════════════════════════════════════════════════════════════════
    print(f"{Colors.DIM}{'─' * W}{Colors.RESET}")
    print(f" {Colors.DIM}{SELECTED_CRYPTO_NAME} ${btc:,.0f}{Colors.RESET}  {ptb_str}  {dev_str}")
    print(f" {bal_str}  {time_color}T-{time_remaining}{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * W}{Colors.RESET}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # ASK PRICES - bold colored text, no background
    # ═══════════════════════════════════════════════════════════════════════
    print()
    print(f"      {Colors.GREEN}{Colors.BOLD}UP  ${up_ask:.2f}{Colors.RESET}              {Colors.RED}{Colors.BOLD}DN  ${down_ask:.2f}{Colors.RESET}")
    
    # Deviation from entry price (if positions exist)
    up_dev_str = ""
    dn_dev_str = ""
    if avg_up > 0:
        up_dev_pct = ((up_ask - avg_up) / avg_up) * 100
        up_dev_usd = up_ask - avg_up
        if up_dev_usd >= 0:
            up_dev_str = f"{Colors.GREEN}+{up_dev_pct:>5.1f}% +${up_dev_usd:.2f}{Colors.RESET}"
        else:
            up_dev_str = f"{Colors.RED}{up_dev_pct:>6.1f}% -${abs(up_dev_usd):.2f}{Colors.RESET}"
    if avg_down > 0:
        dn_dev_pct = ((down_ask - avg_down) / avg_down) * 100
        dn_dev_usd = down_ask - avg_down
        if dn_dev_usd >= 0:
            dn_dev_str = f"{Colors.GREEN}+{dn_dev_pct:>5.1f}% +${dn_dev_usd:.2f}{Colors.RESET}"
        else:
            dn_dev_str = f"{Colors.RED}{dn_dev_pct:>6.1f}% -${abs(dn_dev_usd):.2f}{Colors.RESET}"
    if up_dev_str or dn_dev_str:
        print(f"      {up_dev_str:24}     {dn_dev_str}")
    print()
    
    # ═══════════════════════════════════════════════════════════════════════
    # POSITIONS - largest block, most important
    # ═══════════════════════════════════════════════════════════════════════
    print(f"{Colors.BOLD}{'═' * W}{Colors.RESET}")
    print(f" {Colors.BOLD}{Colors.CYAN}POSITIONS{Colors.RESET}")
    print(f"{Colors.BOLD}{'─' * W}{Colors.RESET}")
    
    # Get total balances (tracked + Data API)
    total_up = up_contracts + (token_balance_state.up_balance if token_balance_state.last_update > 0 and up_contracts == 0 else 0)
    total_down = down_contracts + (token_balance_state.down_balance if token_balance_state.last_update > 0 and down_contracts == 0 else 0)
    
    # Calculate unpaired deficit for display
    paired = min(total_up, total_down)
    up_deficit = total_down - total_up if total_down > total_up else 0
    dn_deficit = total_up - total_down if total_up > total_down else 0
    
    # Calculate unrealized P/L for total (tracked + Data API)
    up_unrealized = up_contracts * (up_bid - avg_up) if up_contracts > 0 and avg_up > 0 else 0
    dn_unrealized = down_contracts * (down_bid - avg_down) if down_contracts > 0 and avg_down > 0 else 0
    
    # Add Data API unrealized if no tracked position
    if up_contracts == 0 and token_balance_state.up_balance > 0 and token_balance_state.up_avg_price > 0:
        up_unrealized = token_balance_state.up_balance * (up_bid - token_balance_state.up_avg_price)
    if down_contracts == 0 and token_balance_state.down_balance > 0 and token_balance_state.down_avg_price > 0:
        dn_unrealized = token_balance_state.down_balance * (down_bid - token_balance_state.down_avg_price)
    
    total_unrealized = up_unrealized + dn_unrealized
    
    # UP position: $invested | contracts @ avg | P/L
    if up_contracts > 0:
        # Show tracked position (bought through this script)
        up_invested = tracker.up_total_cost
        up_pnl_pct = ((up_bid - avg_up) / avg_up * 100) if avg_up > 0 else 0
        up_pnl_usd = up_unrealized
        if up_pnl_usd >= 0:
            pnl_color = Colors.GREEN
            pnl_sign = "+"
        else:
            pnl_color = Colors.RED
            pnl_sign = "-"
        deficit_str = f"{Colors.YELLOW}(-{up_deficit:.0f}){Colors.RESET}" if up_deficit > 0 else ""
        print(f" {Colors.GREEN}{Colors.BOLD}UP{Colors.RESET}  ${up_invested:>6.2f} {Colors.BOLD}{up_contracts:>4.0f}{Colors.RESET}{deficit_str} @ ${avg_up:.2f}  {pnl_color}{pnl_sign}{abs(up_pnl_pct):>5.1f}%{Colors.RESET}")
    elif token_balance_state.last_update > 0 and token_balance_state.up_balance > 0:
        # Show Data API position (from previous sessions or external trades)
        size = token_balance_state.up_balance
        avg = token_balance_state.up_avg_price
        invested = token_balance_state.up_invested
        
        # Show deficit if unpaired
        deficit_str = f"{Colors.YELLOW}(-{up_deficit:.0f}){Colors.RESET}" if up_deficit > 0 else ""
        
        # Calculate P/L from current bid price
        if avg > 0 and up_bid > 0:
            unrealized = size * (up_bid - avg)
            pnl_pct = ((up_bid - avg) / avg * 100)
            if unrealized >= 0:
                pnl_color = Colors.GREEN
                pnl_sign = "+"
            else:
                pnl_color = Colors.RED
                pnl_sign = "-"
            print(f" {Colors.GREEN}{Colors.BOLD}UP{Colors.RESET}  ${invested:>6.2f} {Colors.BOLD}{size:>4.0f}{Colors.RESET}{deficit_str} @ ${avg:.2f}  {pnl_color}{pnl_sign}{abs(pnl_pct):>5.1f}%{Colors.RESET}")
        else:
            print(f" {Colors.GREEN}{Colors.BOLD}UP{Colors.RESET}  ${invested:>6.2f} {Colors.BOLD}{size:>4.0f}{Colors.RESET}{deficit_str} @ ${avg:.2f}  {Colors.DIM}---%{Colors.RESET}")
    else:
        deficit_str = f" {Colors.YELLOW}(-{up_deficit:.0f}){Colors.RESET}" if up_deficit > 0 else ""
        print(f" {Colors.GREEN}{Colors.BOLD}UP{Colors.RESET}  {Colors.DIM}   ---{Colors.RESET}{deficit_str}")
    
    # DOWN position: $invested | contracts @ avg | P/L
    if down_contracts > 0:
        # Show tracked position (bought through this script)
        dn_invested = tracker.down_total_cost
        dn_pnl_pct = ((down_bid - avg_down) / avg_down * 100) if avg_down > 0 else 0
        dn_pnl_usd = dn_unrealized
        if dn_pnl_usd >= 0:
            pnl_color = Colors.GREEN
            pnl_sign = "+"
        else:
            pnl_color = Colors.RED
            pnl_sign = "-"
        deficit_str = f"{Colors.YELLOW}(-{dn_deficit:.0f}){Colors.RESET}" if dn_deficit > 0 else ""
        print(f" {Colors.RED}{Colors.BOLD}DN{Colors.RESET}  ${dn_invested:>6.2f} {Colors.BOLD}{down_contracts:>4.0f}{Colors.RESET}{deficit_str} @ ${avg_down:.2f}  {pnl_color}{pnl_sign}{abs(dn_pnl_pct):>5.1f}%{Colors.RESET}")
    elif token_balance_state.last_update > 0 and token_balance_state.down_balance > 0:
        # Show Data API position (from previous sessions or external trades)
        size = token_balance_state.down_balance
        avg = token_balance_state.down_avg_price
        invested = token_balance_state.down_invested
        
        # Show deficit if unpaired
        deficit_str = f"{Colors.YELLOW}(-{dn_deficit:.0f}){Colors.RESET}" if dn_deficit > 0 else ""
        
        # Calculate P/L from current bid price
        if avg > 0 and down_bid > 0:
            unrealized = size * (down_bid - avg)
            pnl_pct = ((down_bid - avg) / avg * 100)
            if unrealized >= 0:
                pnl_color = Colors.GREEN
                pnl_sign = "+"
            else:
                pnl_color = Colors.RED
                pnl_sign = "-"
            print(f" {Colors.RED}{Colors.BOLD}DN{Colors.RESET}  ${invested:>6.2f} {Colors.BOLD}{size:>4.0f}{Colors.RESET}{deficit_str} @ ${avg:.2f}  {pnl_color}{pnl_sign}{abs(pnl_pct):>5.1f}%{Colors.RESET}")
        else:
            print(f" {Colors.RED}{Colors.BOLD}DN{Colors.RESET}  ${invested:>6.2f} {Colors.BOLD}{size:>4.0f}{Colors.RESET}{deficit_str} @ ${avg:.2f}  {Colors.DIM}---%{Colors.RESET}")
    else:
        deficit_str = f" {Colors.YELLOW}(-{dn_deficit:.0f}){Colors.RESET}" if dn_deficit > 0 else ""
        print(f" {Colors.RED}{Colors.BOLD}DN{Colors.RESET}  {Colors.DIM}   ---{Colors.RESET}{deficit_str}")
    
    # (Removed separate "Blockchain:" line - now integrated in main POSITIONS display)
    
    # Total invested and P/L summary (always show if we have realized or unrealized P/L)
    realized = tracker.session_pnl
    total_pnl = realized + total_unrealized
    
    # Add Data API invested if no tracked positions
    total_invested_with_api = total_invested
    if up_contracts == 0 and token_balance_state.up_balance > 0:
        total_invested_with_api += token_balance_state.up_invested
    if down_contracts == 0 and token_balance_state.down_balance > 0:
        total_invested_with_api += token_balance_state.down_invested
    
    if total_invested_with_api > 0 or realized != 0:
        print(f"{Colors.DIM}{'─' * W}{Colors.RESET}")
        
        if total_pnl >= 0:
            pnl_color = Colors.GREEN
            pnl_sign = "+"
        else:
            pnl_color = Colors.RED
            pnl_sign = "-"
        
        if total_invested_with_api > 0:
            print(f" {Colors.DIM}Invested: ${total_invested_with_api:>6.2f}{Colors.RESET}   {pnl_color}{Colors.BOLD}P/L: {pnl_sign}${abs(total_pnl):>6.2f}{Colors.RESET}")
        else:
            # All positions sold - show realized P/L only
            print(f" {Colors.DIM}Invested: $  0.00{Colors.RESET}   {pnl_color}{Colors.BOLD}P/L: {pnl_sign}${abs(total_pnl):>6.2f}{Colors.RESET} {Colors.DIM}(realized){Colors.RESET}")
    
    print(f"{Colors.BOLD}{'═' * W}{Colors.RESET}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # OUTCOMES - aligned format
    # ═══════════════════════════════════════════════════════════════════════
    if if_up_wins_pl >= 0:
        up_out = f"{Colors.GREEN}+${if_up_wins_pl:>6.2f}{Colors.RESET}"
    else:
        up_out = f"{Colors.RED}-${abs(if_up_wins_pl):>6.2f}{Colors.RESET}"
    
    if if_down_wins_pl >= 0:
        dn_out = f"{Colors.GREEN}+${if_down_wins_pl:>6.2f}{Colors.RESET}"
    else:
        dn_out = f"{Colors.RED}-${abs(if_down_wins_pl):>6.2f}{Colors.RESET}"
    
    print(f" If UP wins: {up_out}    If DN wins: {dn_out}")
    
    # LOCKED = guaranteed outcome = paired contracts (both sides payout $1)
    # Sum of UP + DOWN position values = paired * $1.00 each = $paired total
    if paired > 0:
        guaranteed_payout = paired * 1.0  # Each paired contract pays $1
        guaranteed_profit = guaranteed_payout - total_invested
        if guaranteed_profit >= 0:
            print(f" {Colors.GREEN}{Colors.BOLD}LOCKED: +${guaranteed_profit:>6.2f}{Colors.RESET}  {Colors.DIM}(payout ${guaranteed_payout:.2f}){Colors.RESET}")
        else:
            print(f" {Colors.RED}{Colors.BOLD}LOCKED: -${abs(guaranteed_profit):>6.2f}{Colors.RESET}  {Colors.DIM}(payout ${guaranteed_payout:.2f}){Colors.RESET}")
    
    print()
    
    # ═══════════════════════════════════════════════════════════════════════
    # UNPAIRED WARNING with badge (if any) - with spacing
    # ═══════════════════════════════════════════════════════════════════════
    if unpaired_up > 0 or unpaired_down > 0:
        unpaired_side = "UP" if unpaired_up > 0 else "DN"
        unpaired_count = unpaired_up if unpaired_up > 0 else unpaired_down
        print(f" {Colors.BG_YELLOW}{Colors.BLACK} ! {Colors.RESET}  {Colors.YELLOW}{unpaired_count:>3.0f} {unpaired_side} unpaired{Colors.RESET}")
        print()
        if recommendation:
            rec_side = recommendation["side"]
            rec_price = recommendation["price"]
            rec_cost = recommendation["add_cost"]
            color = recommendation["color"]
            if color == "green":
                rec_badge = f"{Colors.BG_GREEN}{Colors.WHITE} BUY  {Colors.RESET}"
            elif color == "yellow":
                rec_badge = f"{Colors.BG_YELLOW}{Colors.BLACK} OK   {Colors.RESET}"
            else:
                rec_badge = f"{Colors.BG_RED}{Colors.WHITE} WAIT {Colors.RESET}"
            lock_profit = recommendation.get("lock_profit", 0)
            if lock_profit >= 0:
                profit_str = f"{Colors.GREEN}+${lock_profit:.2f}{Colors.RESET}"
            else:
                profit_str = f"{Colors.RED}-${abs(lock_profit):.2f}{Colors.RESET}"
            print(f"     {rec_badge}  {rec_side} @ ${rec_price:.2f} = ${rec_cost:>6.2f}  {profit_str}")
        print()
    
    # ═══════════════════════════════════════════════════════════════════════
    # ORDER SIZE - text color only (no backgrounds)
    # ═══════════════════════════════════════════════════════════════════════
    print(f"{Colors.DIM}{'─' * W}{Colors.RESET}")
    # D = +10 (green text), S = -10 (red text), F = toggle FOK/FAK
    mode_color = Colors.GREEN if order_mode == "FAK" else Colors.YELLOW
    print(f" {Colors.CYAN}{Colors.BOLD}ORDER: {current_contracts_size} contracts{Colors.RESET}  {mode_color}{order_mode}{Colors.RESET}  {Colors.DIM}[{Colors.GREEN}D{Colors.RESET}{Colors.DIM}+10] [{Colors.RED}S{Colors.RESET}{Colors.DIM}-10] [{Colors.CYAN}F{Colors.RESET}{Colors.DIM}mode]{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * W}{Colors.RESET}")
    
    # ═══════════════════════════════════════════════════════════════════════
    # CONTROLS - text color only (no backgrounds)
    # ═══════════════════════════════════════════════════════════════════════
    # 1=BUY UP (green), 2=SELL UP (red), 9=BUY DN (green), 0=SELL DN (red), R=Refresh, M=Redeem
    print(f" {Colors.GREEN}1{Colors.RESET}{Colors.DIM}=UP{Colors.RESET}  {Colors.RED}2{Colors.RESET}{Colors.DIM}=Sell{Colors.RESET}  {Colors.GREEN}9{Colors.RESET}{Colors.DIM}=DN{Colors.RESET}  {Colors.RED}0{Colors.RESET}{Colors.DIM}=Sell{Colors.RESET}  {Colors.DIM}R=Refresh  M=Redeem  Q=Menu{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * W}{Colors.RESET}")
    print(messages_block)


def adjust_contracts_size(delta):
    """Adjust the current contracts size by delta (min = DEFAULT_CONTRACTS_SIZE)."""
    global current_contracts_size
    new_size = current_contracts_size + delta
    if new_size < DEFAULT_CONTRACTS_SIZE:
        new_size = DEFAULT_CONTRACTS_SIZE
    current_contracts_size = new_size
    add_message(f"Size: {current_contracts_size} contracts", "info")


def get_key_with_timeout(timeout=0.5):
    """Get single keypress with timeout (non-blocking)."""
    try:
        import termios
        import tty
        import select
        
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if ready:
                ch = sys.stdin.read(1)
                return ch
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except ImportError:
        import msvcrt
        if msvcrt.kbhit():
            return msvcrt.getch().decode("utf-8", errors="ignore")
        time.sleep(timeout)
        return None


PID_FILE = os.path.join(os.path.dirname(__file__), ".trade.pid")


def is_pid_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def check_already_running() -> bool:
    """Check if another trade.py instance is already running."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if is_pid_running(old_pid):
                return True
            logger.info(f"Stale PID file found (pid {old_pid} not running), removing")
            os.remove(PID_FILE)
        except:
            pass
    return False


def save_pid():
    """Save current process PID atomically."""
    try:
        temp_file = PID_FILE + ".tmp"
        with open(temp_file, "w") as f:
            f.write(str(os.getpid()))
        os.rename(temp_file, PID_FILE)
        logger.info(f"PID file created: {os.getpid()}")
    except Exception as e:
        logger.error(f"Failed to save PID: {e}")


def remove_pid():
    """Remove PID file on exit."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            logger.info("PID file removed")
    except:
        pass


def run_manual_redeem_trade():
    """Run manual redeem for all positions (triggered by M key)."""
    print(f"\n{Colors.CYAN}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'MANUAL REDEEM':^60}{Colors.RESET}")
    print(f"{Colors.CYAN}{'='*60}{Colors.RESET}\n")
    
    try:
        # Import redeemall module
        import redeemall
        
        # Run with auto-confirm
        add_message("Starting redeem...", "info")
        logger.info("Manual redeem triggered by M key")
        
        print(f"{Colors.DIM}Using wallet from current .env configuration{Colors.RESET}\n")
        redeemall.main(auto_confirm=True)
        
        print(f"\n{Colors.GREEN}Redeem completed!{Colors.RESET}")
        add_message("Redeem complete", "success")
        
    except Exception as e:
        error_msg = f"Redeem failed: {e}"
        print(f"\n{Colors.RED}[ERROR] {error_msg}{Colors.RESET}")
        add_message(error_msg, "error")
        logger.error(f"Manual redeem error: {e}", exc_info=True)
    
    print(f"\n{Colors.CYAN}{'='*60}{Colors.RESET}")
    print(f"{Colors.DIM}Press any key to continue trading...{Colors.RESET}")
    print(f"{Colors.CYAN}{'='*60}{Colors.RESET}\n")
    
    # Wait for keypress
    get_key_with_timeout(timeout=10.0)
    
    # Refresh display
    add_message("Returned to trading", "info")


def cleanup_on_exit():
    """Clean up resources when exiting trading mode."""
    global shutdown_requested
    shutdown_requested = True
    
    # Stop WebSocket feeds
    if polymarket_feed:
        try:
            polymarket_feed.stop()
        except:
            pass
    
    # Remove PID file
    remove_pid()
    
    logger.info("Cleanup complete, returning to menu")


def select_cryptocurrency():
    """Select which cryptocurrency to trade.
    
    Returns:
        tuple: (crypto_slug, crypto_name, crypto_symbol)
            - crypto_slug: for market slug format (e.g., "btc", "eth")
            - crypto_name: display name (e.g., "BTC", "ETH")
            - crypto_symbol: for Chainlink WebSocket (e.g., "btc/usd", "eth/usd")
    """
    print(f"\n{Colors.BOLD}{Colors.CYAN}═══════════════════════════════════{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}   Select Cryptocurrency{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}═══════════════════════════════════{Colors.RESET}\n")
    
    print(f"  {Colors.GREEN}1{Colors.RESET}. {Colors.BOLD}BTC{Colors.RESET} (Bitcoin)")
    print(f"  {Colors.CYAN}2{Colors.RESET}. {Colors.BOLD}ETH{Colors.RESET} (Ethereum)")
    print(f"  {Colors.YELLOW}3{Colors.RESET}. {Colors.BOLD}SOL{Colors.RESET} (Solana)")
    print(f"  {Colors.MAGENTA}4{Colors.RESET}. {Colors.BOLD}XRP{Colors.RESET} (Ripple)")
    print()
    
    crypto_map = {
        "1": ("btc", "BTC", "btc/usd"),
        "2": ("eth", "ETH", "eth/usd"),
        "3": ("sol", "SOL", "sol/usd"),
        "4": ("xrp", "XRP", "xrp/usd"),
    }
    
    while True:
        try:
            choice = input(f"{Colors.BOLD}Enter choice (1-4): {Colors.RESET}").strip()
            
            if choice in crypto_map:
                crypto_slug, crypto_name, crypto_symbol = crypto_map[choice]
                print(f"\n{Colors.GREEN}✓{Colors.RESET} Selected: {Colors.BOLD}{crypto_name}{Colors.RESET}\n")
                return crypto_slug, crypto_name, crypto_symbol
            else:
                print(f"{Colors.RED}✗ Invalid choice. Please enter 1-4.{Colors.RESET}")
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Colors.RED}[!]{Colors.RESET} Selection cancelled")
            sys.exit(0)


def main():
    """Main function."""
    global shutdown_requested, SELECTED_CRYPTO_SLUG, SELECTED_CRYPTO_NAME, SELECTED_CRYPTO_SYMBOL
    shutdown_requested = False  # Reset for re-entry from launcher
    
    if check_already_running():
        print(f"{Colors.RED}[ERR]{Colors.RESET} Another trade.py instance is already running!")
        print("  Use telegram bot /stop to stop it first, or kill the process manually.")
        sys.exit(1)
    
    save_pid()
    
    import atexit
    atexit.register(remove_pid)
    
    os.system("clear" if os.name != "nt" else "cls")
    
    # Select cryptocurrency
    SELECTED_CRYPTO_SLUG, SELECTED_CRYPTO_NAME, SELECTED_CRYPTO_SYMBOL = select_cryptocurrency()
    
    print(f"\n{Colors.BOLD}{SELECTED_CRYPTO_NAME} 15min Trader{Colors.RESET}\n")
    
    validate_config()
    
    print_status("Finding market...", "info")
    market_data = find_active_market(SELECTED_CRYPTO_SLUG)
    if not market_data:
        print_status("No active market found", "error")
        sys.exit(1)
    print_status(f"Market: {market_data['slug']}", "success")
    
    print_status("Connecting...", "info")
    client = init_client()
    if not client:
        print_status("Failed to initialize client", "error")
        sys.exit(1)
    print_status("Client ready", "success")
    
    # Set up position tracker with market info
    tracker.set_market_tokens(
        market_data.get("condition_id", ""),
        market_data["up_token_id"],
        market_data["down_token_id"]
    )
    
    # Fetch initial wallet balance and token balances from Data API
    print_status("Fetching balances...", "info")
    refresh_balance(is_startup=True)
    
    # Get initial token balances from Data API
    success = get_token_balances_from_api(market_data["condition_id"])
    if success:
        print_status(f"Tokens: UP={token_balance_state.up_balance:.2f}@${token_balance_state.up_avg_price:.2f} DN={token_balance_state.down_balance:.2f}@${token_balance_state.down_avg_price:.2f}", "info")
    else:
        print_status("No token positions found", "info")
    
    # Start WebSocket connections
    print_status("Starting price feeds...", "info")
    start_chainlink_ws(SELECTED_CRYPTO_SYMBOL)  # Polymarket RTDS Chainlink for crypto price
    polymarket_feed.start(market_data["up_token_id"], market_data["down_token_id"])
    start_user_channel_ws()
    time.sleep(1)
    print_status("Price feeds connected", "success")
    
    time.sleep(0.5)
    
    # Flush any buffered input before main loop
    flush_stdin()
    
    # Main loop
    last_market_check = 0
    redeem_scheduled = False
    old_market_data = None  # Store full market data for redeem
    had_positions = False   # Track if we had positions before market close
    position_check_done = False  # Flag to check positions only once near close
    
    while not shutdown_requested:
        display_dashboard(market_data)
        
        now_ms = int(time.time() * 1000)
        now_sec = time.time()
        
        # Check positions 5 seconds before market close (once per market)
        time_to_close = market_data["end_time"] - now_ms
        if not position_check_done and 0 < time_to_close <= 5000:  # Within last 5 seconds
            position_check_done = True
            # Check if we have any positions to redeem
            up_contracts = tracker.up_total_size
            down_contracts = tracker.down_total_size
            if up_contracts > 0 or down_contracts > 0:
                had_positions = True
                logger.info(f"Positions detected before close: UP={up_contracts:.0f}, DOWN={down_contracts:.0f}")
            else:
                had_positions = False
                logger.info("No positions at market close - skipping auto-redeem")
        
        # Check if market expired (check every 15 seconds to reduce API load)
        if now_ms >= market_data["end_time"] and now_sec - last_market_check > 15:
            last_market_check = now_sec
            
            # Schedule auto-redeem only if we had positions
            if not redeem_scheduled and had_positions and market_data.get("condition_id"):
                # Capture all needed data before market switches
                old_market_data = {
                    "condition_id": market_data["condition_id"],
                    "up_token_id": market_data["up_token_id"],
                    "down_token_id": market_data["down_token_id"],
                    "slug": market_data.get("slug", ""),
                    "neg_risk": market_data.get("neg_risk", True)  # BTC markets are NegRisk
                }
                redeem_scheduled = True
                
                def auto_redeem(mkt_data):
                    time.sleep(180)  # Wait 3 minutes for oracle to resolve
                    try:
                        from redeem import redeem_specific
                        logger.info(f"Auto-redeem triggered for: {mkt_data['slug']}")
                        logger.info(f"  condition_id: {mkt_data['condition_id'][:40]}...")
                        logger.info(f"  up_token: {mkt_data['up_token_id'][:20]}...")
                        logger.info(f"  down_token: {mkt_data['down_token_id'][:20]}...")
                        add_message(f"Auto-redeem starting...", "info")
                        result = redeem_specific(
                            mkt_data["condition_id"],
                            up_token_id=mkt_data["up_token_id"],
                            down_token_id=mkt_data["down_token_id"],
                            neg_risk=mkt_data.get("neg_risk", True),
                            auto_confirm=True,
                            silent=False  # Enable logging to see why it fails
                        )
                        if result:
                            add_message(f"Redeemed OK!", "success")
                            logger.info("Auto-redeem successful!")
                        else:
                            add_message(f"Redeem: no tokens or not resolved", "warn")
                            logger.warning("Auto-redeem returned False - likely no tokens or oracle not resolved")
                    except Exception as e:
                        logger.error(f"Auto-redeem exception: {e}", exc_info=True)
                        add_message(f"Redeem error: {str(e)[:25]}", "critical")
                
                redeem_thread = threading.Thread(target=auto_redeem, args=(old_market_data,), daemon=True)
                redeem_thread.start()
                print_status("Auto-redeem scheduled (3 min)", "success")
            
            # Archive open positions before switching markets
            # We don't know the actual result yet, but we need to clear them
            if tracker.open_positions:
                logger.info(f"Archiving {len(tracker.open_positions)} positions from expired market")
                for pos in tracker.open_positions[:]:
                    # Move to closed with pending status - actual P/L unknown until redeem
                    cost = float(pos["price"]) * float(pos["size"])
                    tracker.closed_trades.append({
                        **pos,
                        "status": "PENDING_SETTLE",
                        "profit": 0,  # Unknown until redeem
                        "cost": cost
                    })
                    logger.info(f"Archived: {pos['side']} ${pos['size']} @ {pos['price']}")
                tracker.open_positions.clear()
            
            print_status("Market expired, searching...", "warn")
            new_market = find_active_market(SELECTED_CRYPTO_SLUG)
            if new_market:
                market_data = new_market
                redeem_scheduled = False  # Reset for new market
                had_positions = False     # Reset position flag
                position_check_done = False  # Reset position check flag
                
                # Update tracker with new market tokens (is_new_market=True for real transition)
                tracker.set_market_tokens(
                    new_market.get("condition_id", ""),
                    new_market["up_token_id"],
                    new_market["down_token_id"],
                    is_new_market=True
                )
                
                # Switch WebSocket to new market (closes old, resets prices, starts new)
                polymarket_feed.switch_market(
                    new_market["up_token_id"],
                    new_market["down_token_id"]
                )
                
                print_status(f"Switched: {market_data['slug']}", "success")
            continue
        
        try:
            key = get_key_with_timeout(timeout=0.3)
        except Exception as e:
            logger.error(f"Key input error: {e}")
            continue
        
        if key is None:
            continue
        
        key = key.lower()
        
        if key == "1":
            place_order(client, market_data, "UP")
        elif key == "2":
            sell_all_position(client, market_data, "UP")
        elif key == "9":
            place_order(client, market_data, "DOWN")
        elif key == "0":
            sell_all_position(client, market_data, "DOWN")
        elif key == "d":
            # Increase contracts size by 10
            adjust_contracts_size(10)
        elif key == "s":
            # Decrease contracts size by 10 (min = DEFAULT_CONTRACTS_SIZE)
            adjust_contracts_size(-10)
        elif key == "r":
            # Refresh wallet balance AND token balances
            threading.Thread(target=refresh_all_balances, args=(market_data,), daemon=True).start()
        elif key == "f":
            # Toggle order mode FOK <-> FAK
            global order_mode
            order_mode = "FAK" if order_mode == "FOK" else "FOK"
            add_message(f"Mode: {order_mode}", "info")
            logger.info(f"Order mode changed to: {order_mode}")
        elif key == "m":
            # Manual redeem all positions
            run_manual_redeem_trade()
        elif key == "q" or key == "\x03":
            print(f"\n{Colors.CYAN}Returning to menu...{Colors.RESET}\n")
            # Clean shutdown
            cleanup_on_exit()
            return "menu"
    
    # Clean shutdown on loop exit
    cleanup_on_exit()
    return "menu"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Colors.CYAN}Bye!{Colors.RESET}\n")
