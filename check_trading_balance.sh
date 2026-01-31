#!/bin/bash
# Check balance for the trading wallet
# Usage: ./check_trading_balance.sh [path/to/.env]

ENV_FILE="${1:-.env}"
python3 check_balance.py --env "$ENV_FILE"
