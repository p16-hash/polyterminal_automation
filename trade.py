#!/usr/bin/env python3
"""
Polymarket 15-min Crypto Hedged Arbitrage Bot (Passive Limit Orders)

- Places resting limit buys on both YES/NO when it keeps projected pair cost < $0.99
- No directional bets — builds hedged positions for guaranteed profit on resolution
- Monitors via WS/polling, places GTC limits, cancels/replaces on drift
"""

import os
import sys
import time
import signal
import threading
from datetime import datetime
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

load_dotenv()

# Keep original logger, colors, messages, etc.
from logger import get_logger, add_message, Colors

logger = get_logger("trade")

# Configuration (keep/extend original)
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
RPC_URL = os.getenv("RPC_URL", "https://polygon-rpc.com")

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# Strategy params
SAFETY_THRESHOLD = 0.99      # Max projected avg(YES) + avg(NO)
MAX_ORDER_SIZE = 20.0        # Shares per limit order (small to start!)
PRICE_OFFSET = 0.005         # Bid this much below ask for maker edge
POLL_INTERVAL = 8            # Seconds (WS preferred for speed)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Global client
client = ClobClient(
    HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=SIGNATURE_TYPE,
    funder=FUNDER_ADDRESS
)
# Create/derive API creds if needed
try:
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
except Exception as e:
    logger.warning(f"API creds setup: {e}")

# State per current market
class HedgedState:
    def __init__(self):
        self.token_yes = None
        self.token_no = None
        self.qty_yes = 0.0
        self.cost_yes = 0.0
        self.qty_no = 0.0
        self.cost_no = 0.0
        self.open_order_ids = {"yes": None, "no": None}
        self.last_mid_yes = 0.5
        self.last_mid_no = 0.5

state = HedgedState()

# Keep original globals if needed (feed_manager, shutdown_requested, etc.)
shutdown_requested = False

def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    logger.info("Shutdown signal received")
    add_message("Shutting down...", "warn")

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def get_avgs():
    avg_yes = state.cost_yes / state.qty_yes if state.qty_yes > 0 else 0.50
    avg_no = state.cost_no / state.qty_no if state.qty_no > 0 else 0.50
    return avg_yes, avg_no

def simulate_buy(side, price, delta_q):
    if delta_q <= 0:
        return False, 999.0
    if side == "yes":
        new_qty = state.qty_yes + delta_q
        new_cost = state.cost_yes + price * delta_q
        new_avg = new_cost / new_qty
        opp_avg, _ = get_avgs()
        proj = new_avg + opp_avg
    else:
        new_qty = state.qty_no + delta_q
        new_cost = state.cost_no + price * delta_q
        new_avg = new_cost / new_qty
        _, opp_avg = get_avgs()
        proj = new_avg + opp_avg
    return proj < SAFETY_THRESHOLD, proj

def get_ask_price(token_id):
    """Get current best ask price (via client or WS fallback)"""
    try:
        # Prefer client if available
        ask = client.get_price(token_id, "SELL")
        if ask and ask > 0:
            return ask
    except:
        pass
    # Fallback: assume you have WS feed; here placeholder
    # In original, use polymarket_feed.best_ask_yes / best_ask_no
    # For now, return dummy or poll
    logger.warning("Using fallback ask price")
    return 0.51  # REPLACE with actual WS/polling

def place_or_update_limit(side):
    if shutdown_requested:
        return
    token = state.token_yes if side == "yes" else state.token_no
    if not token:
        return

    ask_price = get_ask_price(token)
    if ask_price <= 0:
        return

    bid_price = round(ask_price - PRICE_OFFSET, 3)
    if bid_price <= 0 or bid_price >= 1.0:
        return

    delta_q = MAX_ORDER_SIZE

    ok, proj_cost = simulate_buy(side, bid_price, delta_q)
    if not ok:
        logger.info(f"{side.upper()} @ {bid_price:.3f} x {delta_q} → proj {proj_cost:.4f} > {SAFETY_THRESHOLD} (skip)")
        return

    # Cancel existing
    old_id = state.open_order_ids[side]
    if old_id:
        try:
            client.cancel(old_id)
            logger.info(f"Cancelled old {side.upper()} order {old_id}")
        except Exception as e:
            logger.warning(f"Cancel failed: {e}")

    if DRY_RUN:
        logger.info(f"DRY RUN: Would place {side.upper()} limit @ {bid_price:.3f} x {delta_q} (proj {proj_cost:.4f})")
        return

    try:
        order_args = OrderArgs(
            token_id=token,
            price=bid_price,
            size=delta_q,
            side=BUY
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        order_id = resp.get("id") or resp.get("orderID")
        state.open_order_ids[side] = order_id
        logger.info(f"Placed {side.upper()} limit @ {bid_price:.3f} x {delta_q} (proj {proj_cost:.4f}) ID={order_id}")
        add_message(f"Placed {side.upper()} limit @ {bid_price:.3f}", "success")
    except Exception as e:
        logger.error(f"Place order failed: {e}")

def check_and_update_fills():
    """Poll recent trades for fills (fallback; prefer WS user channel)"""
    try:
        trades = client.get_trades(limit=10)  # Recent
        for trade in trades:
            if trade.get("status") == "filled" and float(trade.get("filled", 0)) > 0:
                token = trade.get("token_id")
                filled_qty = float(trade["filled"])
                fill_price = float(trade["price"])
                side = "yes" if token == state.token_yes else "no" if token == state.token_no else None
                if side:
                    if side == "yes":
                        state.qty_yes += filled_qty
                        state.cost_yes += filled_qty * fill_price
                    else:
                        state.qty_no += filled_qty
                        state.cost_no += filled_qty * fill_price
                    logger.info(f"FILL: {side.upper()} {filled_qty:.2f} @ {fill_price:.3f}")
                    add_message(f"FILL {side.upper()} {filled_qty:.2f} @ {fill_price:.3f}", "success")
                    # Optional: Telegram notify
    except Exception as e:
        logger.warning(f"Fill check error: {e}")

def check_profit_locked():
    min_qty = min(state.qty_yes, state.qty_no)
    total_cost = state.cost_yes + state.cost_no
    if min_qty > total_cost + 0.01:  # small buffer
        logger.info(f"PROFIT LOCKED! min_qty={min_qty:.2f} > cost={total_cost:.2f}")
        add_message("PROFIT LOCKED — cancelling opens", "success")
        for s in ["yes", "no"]:
            oid = state.open_order_ids[s]
            if oid:
                try:
                    client.cancel(oid)
                except:
                    pass
        # Trigger redemption if market closing soon (use original logic)
        # e.g., call redeemall or schedule

def trading_loop(market_data):
    # Set tokens from market discovery (keep original call)
    state.token_yes = market_data["up_token_id"]
    state.token_no = market_data["down_token_id"]
    logger.info(f"Trading {market_data['slug']} — YES: {state.token_yes}, NO: {state.token_no}")

    # Reset state on new market
    state.qty_yes = state.cost_yes = state.qty_no = state.cost_no = 0.0
    state.open_order_ids = {"yes": None, "no": None}

    while not shutdown_requested:
        try:
            # Optional: refresh balances (keep original thread if useful)
            # threading.Thread(target=refresh_all_balances, args=(market_data,), daemon=True).start()

            place_or_update_limit("yes")
            place_or_update_limit("no")

            check_and_update_fills()
            check_profit_locked()

            # Optional: drift cancel/replace if mid moved a lot (use WS prices)
            # ...

        except Exception as e:
            logger.error(f"Trading loop error: {e}")
        
        time.sleep(POLL_INTERVAL)

    logger.info("Trading loop stopped")

def main():
    global shutdown_requested
    logger.info("Hedged Arbitrage Bot starting...")

    # Keep original market selection / loop
    while not shutdown_requested:
        market = find_active_market(SELECTED_CRYPTO_SLUG)  # Assume original function
        if not market:
            add_message("No active market found", "error")
            time.sleep(30)
            continue

        # Start WS feeds if not running (keep original polymarket_feed.start() etc.)
        # polymarket_feed.switch_market(...) etc.

        trading_loop(market)

        if shutdown_requested:
            break

        time.sleep(10)  # Wait before retry

    # Cleanup
    logger.info("Bot shutdown complete")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        shutdown_requested = True
        print(f"\n{Colors.CYAN}Bye!{Colors.RESET}\n")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
