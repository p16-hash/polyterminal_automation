#!/usr/bin/env python3
"""
File-based lock for coordinating redemption operations.
Prevents concurrent redemptions that could cause nonce collisions.
"""

import os
import fcntl
import time

LOCK_FILE = "/tmp/redeem.lock"


class RedeemLock:
    """File-based lock for coordinating redemption operations.
    
    Uses fcntl.flock() for atomic locking across processes.
    Prevents concurrent auto-redeem and manual /redeemall from racing.
    """
    
    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._fd = None
        self._acquired = False
    
    def acquire(self) -> bool:
        """Acquire lock with timeout. Returns True if acquired."""
        start = time.time()
        
        try:
            self._fd = open(LOCK_FILE, 'w')
            
            while time.time() - start < self.timeout:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._acquired = True
                    self._fd.write(f"{os.getpid()}\n")
                    self._fd.flush()
                    return True
                except (IOError, OSError):
                    time.sleep(0.5)
            
            self._fd.close()
            self._fd = None
            return False
            
        except Exception:
            if self._fd:
                self._fd.close()
                self._fd = None
            return False
    
    def release(self):
        """Release the lock."""
        if self._fd and self._acquired:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._acquired = False
            self._fd = None
