#!/usr/bin/env python3
"""
Centralized Logging Module

Features:
- File-only logging (no terminal output)
- Separate log files per process (trade, redeem, redeemall, balances)
- 3-hour file rotation
- Thread-safe message queue for terminal display
- Telegram notifications with rate limiting
"""

import os
import time
import logging
import requests
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from collections import deque
from threading import Lock, Thread
from queue import Queue, Empty

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

_loggers = {}
_message_queue = deque(maxlen=10)
_message_lock = Lock()
_telegram_notifier = None

QUIET_MODE = True

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


class TelegramNotifier:
    """
    Non-blocking Telegram notification sender.
    
    Features:
    - Background thread for sending
    - Rate limiting (5 msg/sec max)
    - Graceful error handling (never crashes main process)
    - Drop counter for monitoring queue overflow
    """
    
    LEVEL_ICONS = {
        "success": "OK",
        "error": "ERR",
        "critical": "CRIT",
        "warn": "WARN",
        "info": "INFO"
    }
    
    def __init__(self, bot_token: str, chat_id: str, rate_limit: float = 5.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.rate_limit = rate_limit
        self.min_interval = 1.0 / rate_limit
        self.last_send_time = 0.0
        self.queue = Queue(maxsize=100)
        self.running = True
        self.enabled = bool(bot_token and chat_id)
        self.dropped_count = 0
        self.last_drop_warning = 0.0
        
        if self.enabled:
            self.thread = Thread(target=self._worker, daemon=True)
            self.thread.start()
    
    def _worker(self):
        """Background worker that sends messages from queue."""
        while self.running:
            try:
                msg = self.queue.get(timeout=1.0)
                if msg is None:
                    continue
                
                now = time.time()
                elapsed = now - self.last_send_time
                if elapsed < self.min_interval:
                    time.sleep(self.min_interval - elapsed)
                
                self._send(msg)
                self.last_send_time = time.time()
                
            except Empty:
                continue
            except Exception:
                pass
    
    def _send(self, message: str):
        """Send message to Telegram (with timeout)."""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=2.0)
        except Exception:
            pass
    
    def notify(self, text: str, level: str = "info"):
        """Queue a notification (non-blocking)."""
        if not self.enabled:
            return
        
        icon = self.LEVEL_ICONS.get(level, "INFO")
        timestamp = datetime.now().strftime("%H:%M:%S")
        message = f"<b>[{icon}]</b> {timestamp}\n{text}"
        
        try:
            self.queue.put_nowait(message)
        except:
            self.dropped_count += 1
            now = time.time()
            if now - self.last_drop_warning > 60:
                self.last_drop_warning = now
                print(f"{Colors.YELLOW}[WARN]{Colors.RESET} Telegram queue full, {self.dropped_count} messages dropped")
    
    def get_dropped_count(self) -> int:
        """Get number of dropped messages."""
        return self.dropped_count
    
    def stop(self):
        """Stop the notifier."""
        self.running = False


def get_telegram_notifier() -> TelegramNotifier:
    """Get or create the global Telegram notifier (singleton). Never raises."""
    global _telegram_notifier
    if _telegram_notifier is None:
        try:
            _telegram_notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        except Exception:
            _telegram_notifier = TelegramNotifier("", "")
    return _telegram_notifier


class ThreeHourRotatingHandler(TimedRotatingFileHandler):
    """Rotate logs every 3 hours."""
    
    def __init__(self, process_name):
        self.process_name = process_name
        filename = self._get_current_filename()
        super().__init__(
            filename,
            when='H',
            interval=3,
            backupCount=24,
            encoding='utf-8'
        )
    
    def _get_current_filename(self):
        now = datetime.now()
        hour_block = (now.hour // 3) * 3
        timestamp = now.strftime(f"%Y-%m-%d_{hour_block:02d}h")
        return os.path.join(LOGS_DIR, f"{self.process_name}_{timestamp}.log")
    
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        
        self.baseFilename = self._get_current_filename()
        self.stream = self._open()


def get_logger(process_name: str) -> logging.Logger:
    """Get or create a file-only logger for a process."""
    if process_name in _loggers:
        return _loggers[process_name]
    
    logger = logging.getLogger(f"polymarket.{process_name}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    
    handler = ThreeHourRotatingHandler(process_name)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)
    
    logger.propagate = False
    
    _loggers[process_name] = logger
    return logger


class Colors:
    """Terminal colors."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    WHITE = "\033[97m"
    BLACK = "\033[30m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_MAGENTA = "\033[45m"


class TerminalMessage:
    """A message for terminal display."""
    
    def __init__(self, text: str, level: str = "info"):
        self.text = text
        self.level = level
        self.timestamp = datetime.now()
    
    def format(self) -> str:
        time_str = self.timestamp.strftime("%H:%M:%S")
        
        if self.level == "success":
            prefix = f"{Colors.GREEN}OK{Colors.RESET}"
        elif self.level == "error":
            prefix = f"{Colors.RED}ERR{Colors.RESET}"
        elif self.level == "critical":
            prefix = f"{Colors.BG_RED}{Colors.WHITE}CRIT{Colors.RESET}"
        elif self.level == "warn":
            prefix = f"{Colors.YELLOW}!{Colors.RESET}"
        else:
            prefix = f"{Colors.DIM}...{Colors.RESET}"
        
        return f"{Colors.DIM}{time_str}{Colors.RESET} [{prefix}] {self.text}"


def add_message(text: str, level: str = "info"):
    """Add a message to the terminal queue and send to Telegram."""
    with _message_lock:
        _message_queue.append(TerminalMessage(text, level))
    
    try:
        notifier = get_telegram_notifier()
        notifier.notify(text, level)
    except:
        pass


def get_messages() -> list:
    """Get all messages in the queue for display."""
    with _message_lock:
        return list(_message_queue)


def clear_messages():
    """Clear all messages."""
    with _message_lock:
        _message_queue.clear()


def format_messages_block(max_lines: int = 10) -> str:
    """Format messages as a display block for terminal."""
    messages = get_messages()
    lines = []
    
    for msg in messages[-max_lines:]:
        lines.append(msg.format())
    
    while len(lines) < max_lines:
        lines.append("")
    
    return "\n".join(lines)


def set_quiet_mode(enabled: bool):
    """Enable or disable quiet mode."""
    global QUIET_MODE
    QUIET_MODE = enabled


def is_quiet_mode() -> bool:
    """Check if quiet mode is enabled."""
    return QUIET_MODE
