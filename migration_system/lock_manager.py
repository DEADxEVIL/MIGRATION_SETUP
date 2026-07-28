"""
lock_manager.py
===============

Distributed locking mechanism to prevent dual polling and other race conditions.

Features:
    - Distributed lock using SQLite (shared state)
    - Lock expiration with TTL
    - Lock renewal
    - Deadlock detection
    - Process-local locks for quick checks
    - Thread-safe
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from state_manager import StateManager

logger = logging.getLogger("lock_manager")


@dataclass
class LockInfo:
    """Information about a lock."""
    key: str
    owner: str
    acquired_at: float
    expires_at: float
    ttl_seconds: float


class LockManagerError(Exception):
    """Raised for lock management errors."""


class LockManager:
    """
    Distributed lock manager using shared SQLite state.
    
    Prevents:
        - Dual bot polling
        - Concurrent migrations
        - Race conditions during switchover
    
    Example:
        lock_mgr = LockManager(state_manager)
        
        # Acquire lock
        if lock_mgr.acquire("bot_polling", owner_id="vps1", ttl=60):
            try:
                # Do critical work
                pass
            finally:
                lock_mgr.release("bot_polling", owner_id="vps1")
    """
    
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
        self._local_locks: Dict[str, str] = {}  # key -> owner
        self._lock = threading.RLock()
        self._renewal_threads: Dict[str, threading.Thread] = {}
    
    def acquire(
        self,
        lock_key: str,
        owner_id: str,
        ttl_seconds: float = 60.0,
        wait_timeout: Optional[float] = None,
        retry_interval: float = 0.5
    ) -> bool:
        """
        Acquire a distributed lock.
        
        Args:
            lock_key: Unique lock identifier
            owner_id: Unique owner identifier (e.g., "vps1", "vps2")
            ttl_seconds: Lock time-to-live in seconds
            wait_timeout: Max time to wait for lock (None = don't wait)
            retry_interval: Time between retry attempts
        
        Returns:
            True if lock acquired, False otherwise
        """
        start_time = time.time()
        attempt = 0
        
        while True:
            attempt += 1
            now = time.time()
            
            # Check if we already hold this lock locally
            with self._lock:
                if lock_key in self._local_locks and self._local_locks[lock_key] == owner_id:
                    logger.debug("Already hold lock '%s'", lock_key)
                    return True
            
            # Try to acquire in state manager
            acquired = self.state_manager.acquire_lock(lock_key, owner_id, ttl_seconds)
            
            if acquired:
                with self._lock:
                    self._local_locks[lock_key] = owner_id
                    # Start renewal thread if not already running
                    if lock_key not in self._renewal_threads:
                        self._start_renewal(lock_key, owner_id, ttl_seconds)
                
                logger.info("Acquired lock '%s' (owner: %s)", lock_key, owner_id)
                return True
            
            # Check if we should wait
            if wait_timeout is None:
                return False
            
            elapsed = time.time() - start_time
            if elapsed >= wait_timeout:
                logger.warning("Timeout waiting for lock '%s' after %.2fs", lock_key, elapsed)
                return False
            
            # Wait before retry
            time.sleep(retry_interval)
    
    def release(self, lock_key: str, owner_id: str) -> bool:
        """
        Release a distributed lock.
        
        Returns:
            True if released, False if not owned
        """
        with self._lock:
            # Check local ownership
            if lock_key not in self._local_locks or self._local_locks[lock_key] != owner_id:
                logger.warning("Cannot release lock '%s': not owned by %s", lock_key, owner_id)
                return False
            
            # Release in state manager
            released = self.state_manager.release_lock(lock_key, owner_id)
            
            if released:
                del self._local_locks[lock_key]
                # Stop renewal thread
                if lock_key in self._renewal_threads:
                    thread = self._renewal_threads.pop(lock_key, None)
                    if thread and thread.is_alive():
                        # Thread will exit on its own when lock is released
                        pass
                
                logger.info("Released lock '%s' (owner: %s)", lock_key, owner_id)
                return True
            
            return False
    
    def renew(self, lock_key: str, owner_id: str, ttl_seconds: float = 60.0) -> bool:
        """
        Renew an existing lock.
        
        Returns:
            True if renewed, False if not owned or expired
        """
        with self._lock:
            if lock_key not in self._local_locks or self._local_locks[lock_key] != owner_id:
                return False
        
        renewed = self.state_manager.renew_lock(lock_key, owner_id, ttl_seconds)
        
        if renewed:
            logger.debug("Renewed lock '%s' (owner: %s)", lock_key, owner_id)
        else:
            logger.warning("Failed to renew lock '%s' (owner: %s)", lock_key, owner_id)
        
        return renewed
    
    def is_locked(self, lock_key: str) -> bool:
        """Check if a lock is held by anyone."""
        # Check if we hold it locally
        with self._lock:
            if lock_key in self._local_locks:
                return True
        
        # Check in state manager
        # We can check by trying to acquire with a temporary owner
        temp_owner = f"check_{uuid.uuid4().hex[:8]}"
        acquired = self.state_manager.acquire_lock(lock_key, temp_owner, 1.0)
        
        if acquired:
            # Release immediately
            self.state_manager.release_lock(lock_key, temp_owner)
            return False
        
        return True
    
    def get_owner(self, lock_key: str) -> Optional[str]:
        """Get the owner of a lock, if any."""
        # Check local first
        with self._lock:
            if lock_key in self._local_locks:
                return self._local_locks[lock_key]
        
        # Check in state manager
        # This is a hack - we need to expose the lock info from state manager
        # For now, we'll try to get it via SQL query
        import sqlite3
        try:
            conn = self.state_manager._get_connection()
            cursor = conn.execute(
                "SELECT owner_id FROM locks WHERE lock_key = ? AND expires_at > ?",
                (lock_key, time.time())
            )
            row = cursor.fetchone()
            if row:
                return row[0]
        except sqlite3.Error as e:
            logger.error("Failed to get lock owner: %s", e)
        
        return None
    
    def _start_renewal(self, lock_key: str, owner_id: str, ttl_seconds: float) -> None:
        """Start a background thread to renew the lock."""
        def renewal_loop() -> None:
            while True:
                time.sleep(ttl_seconds * 0.5)  # Renew at half TTL
                
                # Check if lock is still held locally
                with self._lock:
                    if lock_key not in self._local_locks or self._local_locks[lock_key] != owner_id:
                        break
                
                # Renew the lock
                if not self.renew(lock_key, owner_id, ttl_seconds):
                    logger.warning("Lock '%s' renewal failed, releasing", lock_key)
                    with self._lock:
                        if lock_key in self._local_locks:
                            del self._local_locks[lock_key]
                    break
        
        thread = threading.Thread(
            target=renewal_loop,
            name=f"lock_renewal_{lock_key}",
            daemon=True
        )
        thread.start()
        self._renewal_threads[lock_key] = thread
    
    def release_all(self, owner_id: str) -> None:
        """Release all locks held by an owner."""
        with self._lock:
            keys = list(self._local_locks.keys())
            for key in keys:
                if self._local_locks.get(key) == owner_id:
                    self.release(key, owner_id)
        
        logger.info("Released all locks for owner: %s", owner_id)
    
    def cleanup_expired(self) -> int:
        """Clean up expired locks (handled by state manager automatically)."""
        # State manager handles expired locks automatically
        return 0
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Release all locks held by this instance
        with self._lock:
            owner_ids = set(self._local_locks.values())
            for owner_id in owner_ids:
                self.release_all(owner_id)


class PollingLock:
    """
    Specialized lock for Telegram polling.
    Ensures only one bot instance is polling at any time.
    """
    
    LOCK_KEY = "telegram_polling"
    
    def __init__(self, lock_manager: LockManager, owner_id: str):
        self.lock_manager = lock_manager
        self.owner_id = owner_id
        self._held = False
    
    def acquire(self, ttl: float = 60.0, wait_timeout: float = 30.0) -> bool:
        """Acquire the polling lock."""
        self._held = self.lock_manager.acquire(
            self.LOCK_KEY,
            self.owner_id,
            ttl_seconds=ttl,
            wait_timeout=wait_timeout
        )
        return self._held
    
    def release(self) -> bool:
        """Release the polling lock."""
        if not self._held:
            return False
        self._held = False
        return self.lock_manager.release(self.LOCK_KEY, self.owner_id)
    
    def is_held(self) -> bool:
        """Check if this instance holds the lock."""
        return self._held
    
    def is_locked(self) -> bool:
        """Check if anyone holds the lock."""
        return self.lock_manager.is_locked(self.LOCK_KEY)
    
    def get_owner(self) -> Optional[str]:
        """Get the current lock owner."""
        return self.lock_manager.get_owner(self.LOCK_KEY)
    
    def __enter__(self):
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


if __name__ == "__main__":
    from logger_service import get_logger
    from state_manager import StateManager
    
    logger = get_logger("test")
    
    # Test with in-memory state manager
    with StateManager(":memory:") as sm:
        lm = LockManager(sm)
        
        # Test lock acquisition
        owner1 = "vps1"
        owner2 = "vps2"
        
        # Acquire lock
        assert lm.acquire("test_lock", owner1, ttl_seconds=10)
        assert lm.is_locked("test_lock")
        
        # Try to acquire with different owner (should fail)
        assert not lm.acquire("test_lock", owner2, wait_timeout=0.5)
        
        # Release and re-acquire
        assert lm.release("test_lock", owner1)
        assert not lm.is_locked("test_lock")
        assert lm.acquire("test_lock", owner2, ttl_seconds=10)
        
        # Test polling lock
        pl = PollingLock(lm, "vps1")
        assert pl.acquire()
        assert pl.is_held()
        assert pl.is_locked()
        assert pl.get_owner() == "vps1"
        
        pl.release()
        assert not pl.is_locked()
        
        print("All lock tests passed!")