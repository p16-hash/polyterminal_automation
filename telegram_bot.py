#!/usr/bin/env python3
"""
Telegram Bot for Remote Trading Control

Commands:
- /status - Check system status (WS connections, current market)
- /balance - Check wallet USDC balance
- /redeemall - Collect all unredeemed winnings
- /stop - Stop trade.py process
- /restart - Restart trade.py process
- /help - Show available commands

Run: python3 telegram_bot.py
"""

import os
import sys
import time
import signal
import subprocess
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from logger import get_logger

logger = get_logger("telegram_bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TRADE_PID_FILE = os.path.join(os.path.dirname(__file__), ".trade.pid")

ALLOWED_CHAT_IDS = [TELEGRAM_CHAT_ID] if TELEGRAM_CHAT_ID else []


class TelegramBot:
    """Simple Telegram bot with long polling."""
    
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_update_id = 0
        self.running = True
        
        self.commands = {
            "/status": self.cmd_status,
            "/balance": self.cmd_balance,
            "/redeemall": self.cmd_redeemall,
            "/stop": self.cmd_stop,
            "/restart": self.cmd_restart,
            "/help": self.cmd_help,
            "/start": self.cmd_help,
        }
    
    def send_message(self, chat_id: str, text: str):
        """Send a message to a chat."""
        try:
            url = f"{self.base_url}/sendMessage"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
    
    def get_updates(self, timeout: int = 30) -> list:
        """Get updates from Telegram using long polling."""
        try:
            url = f"{self.base_url}/getUpdates"
            response = requests.get(url, params={
                "offset": self.last_update_id + 1,
                "timeout": timeout
            }, timeout=timeout + 5)
            
            data = response.json()
            if data.get("ok"):
                return data.get("result", [])
            return []
        except Exception as e:
            logger.debug(f"Get updates error: {e}")
            return []
    
    def is_authorized(self, chat_id: str) -> bool:
        """Check if chat ID is authorized."""
        return str(chat_id) in ALLOWED_CHAT_IDS
    
    def cmd_help(self, chat_id: str):
        """Show available commands."""
        text = """<b>Trading Bot Commands</b>

/status - System status
/balance - Wallet balance
/redeemall - Collect winnings
/stop - Stop trading
/restart - Restart trading
/help - This message"""
        self.send_message(chat_id, text)
    
    def cmd_status(self, chat_id: str):
        """Check system status."""
        pid = self._get_trade_pid()
        
        if pid and self._is_process_running(pid):
            status = "RUNNING"
            status_icon = "ON"
        else:
            status = "STOPPED"
            status_icon = "OFF"
        
        now = int(time.time())
        current_slot = (now // 900) * 900
        time_in_slot = now - current_slot
        time_remaining = 900 - time_in_slot
        
        text = f"""<b>System Status</b>

Trade.py: <b>{status_icon}</b> {status}
PID: {pid or 'N/A'}

<b>Current Market</b>
Slot: {current_slot}
Time remaining: {time_remaining}s"""
        
        self.send_message(chat_id, text)
    
    def cmd_balance(self, chat_id: str):
        """Check wallet balance."""
        self.send_message(chat_id, "Checking balance...")
        
        try:
            result = subprocess.run(
                ["python3", "check_balance.py"],
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=30
            )
            
            output = result.stdout[-1500:] if len(result.stdout) > 1500 else result.stdout
            output = output.replace("<", "&lt;").replace(">", "&gt;")
            
            if result.returncode == 0:
                self.send_message(chat_id, f"<pre>{output}</pre>")
            else:
                self.send_message(chat_id, f"Error:\n<pre>{result.stderr[:500]}</pre>")
                
        except subprocess.TimeoutExpired:
            self.send_message(chat_id, "Timeout - balance check took too long")
        except Exception as e:
            self.send_message(chat_id, f"Error: {str(e)[:100]}")
    
    def cmd_redeemall(self, chat_id: str):
        """Run redeemall.py to collect all winnings."""
        self.send_message(chat_id, "Starting redeemall...")
        
        try:
            result = subprocess.run(
                ["python3", "redeemall.py"],
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=300
            )
            
            output = result.stdout[-1500:] if len(result.stdout) > 1500 else result.stdout
            output = output.replace("<", "&lt;").replace(">", "&gt;")
            
            if result.returncode == 0:
                self.send_message(chat_id, f"Redeemall complete:\n<pre>{output}</pre>")
            else:
                self.send_message(chat_id, f"Redeemall error:\n<pre>{result.stderr[:500]}</pre>")
                
        except subprocess.TimeoutExpired:
            self.send_message(chat_id, "Timeout - redeemall took too long")
        except Exception as e:
            self.send_message(chat_id, f"Error: {str(e)[:100]}")
    
    def cmd_stop(self, chat_id: str) -> bool:
        """Stop trade.py process. Returns True if stopped."""
        pid = self._get_trade_pid()
        
        if not pid:
            self.send_message(chat_id, "Trade.py not running (no PID file)")
            self._remove_pid_file()
            return True
        
        if not self._is_process_running(pid):
            self.send_message(chat_id, f"Trade.py not running (stale PID {pid})")
            self._remove_pid_file()
            return True
        
        try:
            os.kill(pid, signal.SIGTERM)
            self.send_message(chat_id, f"STOP signal sent to trade.py (PID {pid})")
            logger.info(f"Sent SIGTERM to trade.py PID {pid}")
            
            for i in range(10):
                time.sleep(1)
                if not self._is_process_running(pid):
                    self.send_message(chat_id, "Trade.py stopped successfully")
                    self._remove_pid_file()
                    return True
            
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
            self._remove_pid_file()
            self.send_message(chat_id, "Trade.py force killed (SIGKILL)")
            return True
                
        except ProcessLookupError:
            self.send_message(chat_id, "Process already stopped")
            self._remove_pid_file()
            return True
        except PermissionError:
            self.send_message(chat_id, "Permission denied - cannot stop process")
            return False
        except Exception as e:
            self.send_message(chat_id, f"Error: {str(e)[:100]}")
            return False
    
    def cmd_restart(self, chat_id: str):
        """Restart trade.py process."""
        stopped = self.cmd_stop(chat_id)
        
        if not stopped:
            self.send_message(chat_id, "Cannot restart - stop failed")
            return
        
        time.sleep(2)
        
        self.send_message(chat_id, "Starting trade.py...")
        
        try:
            process = subprocess.Popen(
                ["python3", "trade.py"],
                cwd=os.path.dirname(__file__),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            
            time.sleep(2)
            if self._is_process_running(process.pid):
                self.send_message(chat_id, f"Trade.py started (PID {process.pid})")
                logger.info(f"Started trade.py with PID {process.pid}")
            else:
                self.send_message(chat_id, "Trade.py failed to start (exited immediately)")
            
        except Exception as e:
            self.send_message(chat_id, f"Failed to start: {str(e)[:100]}")
    
    def _get_trade_pid(self) -> int:
        """Get trade.py PID from file."""
        try:
            if os.path.exists(TRADE_PID_FILE):
                with open(TRADE_PID_FILE) as f:
                    return int(f.read().strip())
        except:
            pass
        return None
    
    def _is_process_running(self, pid: int) -> bool:
        """Check if a process is running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    
    def _remove_pid_file(self):
        """Remove PID file."""
        try:
            if os.path.exists(TRADE_PID_FILE):
                os.remove(TRADE_PID_FILE)
        except:
            pass
    
    def handle_update(self, update: dict):
        """Handle a single update from Telegram."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()
        
        if not chat_id or not text:
            return
        
        if not self.is_authorized(chat_id):
            logger.warning(f"Unauthorized access attempt from chat_id: {chat_id}")
            return
        
        command = text.split()[0].lower()
        
        if command in self.commands:
            logger.info(f"Executing command: {command}")
            try:
                self.commands[command](chat_id)
            except Exception as e:
                logger.error(f"Command error: {e}")
                self.send_message(chat_id, f"Command error: {str(e)[:100]}")
    
    def run(self):
        """Main polling loop."""
        logger.info("Telegram bot started")
        print(f"Telegram bot running. Authorized chat: {TELEGRAM_CHAT_ID}")
        print("Press Ctrl+C to stop\n")
        
        self.send_message(TELEGRAM_CHAT_ID, "Bot started. Send /help for commands.")
        
        while self.running:
            try:
                updates = self.get_updates(timeout=30)
                
                for update in updates:
                    self.last_update_id = update.get("update_id", self.last_update_id)
                    self.handle_update(update)
                    
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Polling error: {e}")
                time.sleep(5)
        
        logger.info("Telegram bot stopped")


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in environment")
        sys.exit(1)
    
    if not TELEGRAM_CHAT_ID:
        print("Error: TELEGRAM_CHAT_ID not set in environment")
        sys.exit(1)
    
    bot = TelegramBot(TELEGRAM_BOT_TOKEN)
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nBot stopped")


if __name__ == "__main__":
    main()
