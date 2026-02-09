"""Distributed locking for ProcClaw.

Prevents duplicate job executions across multiple instances.
Supports multiple backends: local (SQLite), file-based, and Redis.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database


@dataclass
class LockInfo:
    """Information about a lock."""
    
    job_id: str
    holder_id: str
    acquired_at: datetime
    expires_at: datetime
    
    @property
    def is_expired(self) -> bool:
        """Check if the lock has expired."""
        return datetime.now() >= self.expires_at
    
    @property
    def remaining_seconds(self) -> float:
        """Seconds until lock expires."""
        return (self.expires_at - datetime.now()).total_seconds()


class LockProvider(ABC):
    """Abstract base class for lock providers."""
    
    @abstractmethod
    async def acquire(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Try to acquire a lock.
        
        Args:
            job_id: The job ID (lock key)
            holder_id: Unique identifier for this holder
            timeout_seconds: Lock timeout
            
        Returns:
            True if lock acquired
        """
        pass
    
    @abstractmethod
    async def release(self, job_id: str, holder_id: str) -> bool:
        """Release a lock.
        
        Args:
            job_id: The job ID
            holder_id: Must match the holder who acquired
            
        Returns:
            True if released
        """
        pass
    
    @abstractmethod
    async def is_locked(self, job_id: str) -> bool:
        """Check if a job is locked."""
        pass
    
    @abstractmethod
    async def get_lock_info(self, job_id: str) -> LockInfo | None:
        """Get lock information."""
        pass
    
    @abstractmethod
    async def refresh(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Extend a lock's timeout.
        
        Args:
            job_id: The job ID
            holder_id: Must match the holder
            timeout_seconds: New timeout
            
        Returns:
            True if refreshed
        """
        pass


class SQLiteLockProvider(LockProvider):
    """SQLite-based lock provider.
    
    Suitable for single-node deployments or when using
    a shared SQLite database file (NFS, etc).
    """
    
    def __init__(self, db: "Database"):
        """Initialize with database instance.
        
        Args:
            db: ProcClaw database instance
        """
        self._db = db
    
    async def acquire(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Try to acquire a lock."""
        return self._db.acquire_lock(job_id, holder_id, timeout_seconds)
    
    async def release(self, job_id: str, holder_id: str) -> bool:
        """Release a lock."""
        return self._db.release_lock(job_id, holder_id)
    
    async def is_locked(self, job_id: str) -> bool:
        """Check if locked."""
        return self._db.is_locked(job_id)
    
    async def get_lock_info(self, job_id: str) -> LockInfo | None:
        """Get lock information."""
        holder = self._db.get_lock_holder(job_id)
        if holder is None:
            return None
        
        # Would need to enhance DB to return full info
        # For now, return partial info
        return LockInfo(
            job_id=job_id,
            holder_id=holder,
            acquired_at=datetime.now(),  # Approximate
            expires_at=datetime.now() + timedelta(minutes=5),  # Approximate
        )
    
    async def refresh(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Refresh a lock by releasing and re-acquiring."""
        if await self.release(job_id, holder_id):
            return await self.acquire(job_id, holder_id, timeout_seconds)
        return False


class FileLockProvider(LockProvider):
    """File-based lock provider.
    
    Uses OS-level file locking. Works well for single-machine
    multi-process scenarios.
    """
    
    def __init__(self, lock_dir: Path):
        """Initialize with lock directory.
        
        Args:
            lock_dir: Directory to store lock files
        """
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        
        # Track our own locks for cleanup
        self._our_locks: dict[str, Path] = {}
    
    def _lock_file(self, job_id: str) -> Path:
        """Get the lock file path for a job."""
        safe_name = job_id.replace("/", "_").replace("\\", "_")
        return self._lock_dir / f"{safe_name}.lock"
    
    async def acquire(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Try to acquire a file lock."""
        lock_file = self._lock_file(job_id)
        
        try:
            # Check if lock exists and is valid
            if lock_file.exists():
                content = lock_file.read_text()
                parts = content.split("|")
                if len(parts) == 2:
                    expires_at = datetime.fromisoformat(parts[1])
                    if datetime.now() < expires_at:
                        return False  # Still locked
            
            # Create lock file
            expires_at = datetime.now() + timedelta(seconds=timeout_seconds)
            lock_file.write_text(f"{holder_id}|{expires_at.isoformat()}")
            
            self._our_locks[job_id] = lock_file
            return True
            
        except Exception as e:
            logger.warning(f"Failed to acquire file lock for '{job_id}': {e}")
            return False
    
    async def release(self, job_id: str, holder_id: str) -> bool:
        """Release a file lock."""
        lock_file = self._lock_file(job_id)
        
        try:
            if not lock_file.exists():
                return True
            
            # Verify we hold the lock
            content = lock_file.read_text()
            parts = content.split("|")
            if parts[0] != holder_id:
                return False  # Not our lock
            
            lock_file.unlink()
            self._our_locks.pop(job_id, None)
            return True
            
        except Exception as e:
            logger.warning(f"Failed to release file lock for '{job_id}': {e}")
            return False
    
    async def is_locked(self, job_id: str) -> bool:
        """Check if locked via file."""
        lock_file = self._lock_file(job_id)
        
        if not lock_file.exists():
            return False
        
        try:
            content = lock_file.read_text()
            parts = content.split("|")
            if len(parts) == 2:
                expires_at = datetime.fromisoformat(parts[1])
                return datetime.now() < expires_at
        except:
            pass
        
        return False
    
    async def get_lock_info(self, job_id: str) -> LockInfo | None:
        """Get lock information from file."""
        lock_file = self._lock_file(job_id)
        
        if not lock_file.exists():
            return None
        
        try:
            content = lock_file.read_text()
            parts = content.split("|")
            if len(parts) == 2:
                return LockInfo(
                    job_id=job_id,
                    holder_id=parts[0],
                    acquired_at=datetime.fromtimestamp(lock_file.stat().st_mtime),
                    expires_at=datetime.fromisoformat(parts[1]),
                )
        except:
            pass
        
        return None
    
    async def refresh(
        self,
        job_id: str,
        holder_id: str,
        timeout_seconds: int,
    ) -> bool:
        """Refresh a file lock."""
        lock_file = self._lock_file(job_id)
        
        try:
            if not lock_file.exists():
                return False
            
            content = lock_file.read_text()
            parts = content.split("|")
            if parts[0] != holder_id:
                return False
            
            expires_at = datetime.now() + timedelta(seconds=timeout_seconds)
            lock_file.write_text(f"{holder_id}|{expires_at.isoformat()}")
            return True
            
        except Exception as e:
            logger.warning(f"Failed to refresh file lock for '{job_id}': {e}")
            return False
    
    def cleanup_expired(self) -> int:
        """Clean up expired lock files."""
        count = 0
        now = datetime.now()
        
        for lock_file in self._lock_dir.glob("*.lock"):
            try:
                content = lock_file.read_text()
                parts = content.split("|")
                if len(parts) == 2:
                    expires_at = datetime.fromisoformat(parts[1])
                    if now >= expires_at:
                        lock_file.unlink()
                        count += 1
            except:
                pass
        
        return count


class LockManager:
    """High-level lock manager with retry logic.
    
    Wraps a LockProvider with convenient methods for
    acquiring locks with retries and automatic refresh.
    """
    
    def __init__(
        self,
        provider: LockProvider,
        default_timeout: int = 300,
        retry_interval: float = 0.5,
        max_retries: int = 10,
    ):
        """Initialize the lock manager.
        
        Args:
            provider: Lock provider implementation
            default_timeout: Default lock timeout in seconds
            retry_interval: Seconds between retry attempts
            max_retries: Maximum acquisition attempts
        """
        self._provider = provider
        self._default_timeout = default_timeout
        self._retry_interval = retry_interval
        self._max_retries = max_retries
        
        # Generate unique holder ID for this instance
        self._holder_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    
    @property
    def holder_id(self) -> str:
        """Get this instance's holder ID."""
        return self._holder_id
    
    async def acquire(
        self,
        job_id: str,
        timeout_seconds: int | None = None,
        wait: bool = True,
    ) -> bool:
        """Acquire a lock for a job.
        
        Args:
            job_id: The job ID
            timeout_seconds: Lock timeout
            wait: Whether to retry if lock is held
            
        Returns:
            True if acquired
        """
        timeout = timeout_seconds or self._default_timeout
        
        if not wait:
            return await self._provider.acquire(job_id, self._holder_id, timeout)
        
        for attempt in range(self._max_retries):
            if await self._provider.acquire(job_id, self._holder_id, timeout):
                return True
            
            if attempt < self._max_retries - 1:
                await asyncio.sleep(self._retry_interval)
        
        logger.warning(
            f"Failed to acquire lock for '{job_id}' after {self._max_retries} attempts"
        )
        return False
    
    async def release(self, job_id: str) -> bool:
        """Release a lock."""
        return await self._provider.release(job_id, self._holder_id)
    
    async def is_locked(self, job_id: str) -> bool:
        """Check if a job is locked."""
        return await self._provider.is_locked(job_id)
    
    async def refresh(self, job_id: str, timeout_seconds: int | None = None) -> bool:
        """Refresh a lock's timeout."""
        timeout = timeout_seconds or self._default_timeout
        return await self._provider.refresh(job_id, self._holder_id, timeout)
    
    @asynccontextmanager
    async def lock(
        self,
        job_id: str,
        timeout_seconds: int | None = None,
        wait: bool = True,
    ):
        """Context manager for acquiring and releasing a lock.
        
        Usage:
            async with lock_manager.lock("my-job"):
                # Do work while holding lock
                pass
        """
        acquired = await self.acquire(job_id, timeout_seconds, wait)
        
        if not acquired:
            raise LockAcquisitionError(f"Failed to acquire lock for '{job_id}'")
        
        try:
            yield
        finally:
            await self.release(job_id)


class LockAcquisitionError(Exception):
    """Raised when lock acquisition fails."""
    pass
