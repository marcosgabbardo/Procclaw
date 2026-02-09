"""Deduplication manager for ProcClaw.

Prevents duplicate job executions within a configurable time window.
Supports idempotency keys for webhook triggers.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database


@dataclass
class ExecutionFingerprint:
    """Fingerprint of a job execution for deduplication."""
    
    job_id: str
    fingerprint: str
    started_at: datetime
    idempotency_key: str | None = None
    
    @classmethod
    def create(
        cls,
        job_id: str,
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> "ExecutionFingerprint":
        """Create a fingerprint for deduplication.
        
        Args:
            job_id: The job ID
            params: Optional parameters that affect execution
            idempotency_key: Optional explicit idempotency key
            
        Returns:
            ExecutionFingerprint instance
        """
        # Build fingerprint from job_id and params
        data = f"{job_id}"
        if params:
            # Sort keys for consistent hashing
            sorted_params = sorted(params.items())
            data += f":{sorted_params}"
        
        fingerprint = hashlib.sha256(data.encode()).hexdigest()[:16]
        
        return cls(
            job_id=job_id,
            fingerprint=fingerprint,
            started_at=datetime.now(),
            idempotency_key=idempotency_key,
        )


@dataclass
class DedupConfig:
    """Deduplication configuration."""
    
    enabled: bool = True
    window_seconds: int = 60  # Default 1 minute window
    use_idempotency_key: bool = True  # Honor idempotency keys


@dataclass
class DedupResult:
    """Result of a deduplication check."""
    
    is_duplicate: bool
    reason: str | None = None
    original_run_id: int | None = None
    original_started_at: datetime | None = None


class DeduplicationManager:
    """Manages deduplication of job executions.
    
    Features:
    - Time-window based deduplication
    - Idempotency key support for webhooks
    - In-memory cache with DB persistence
    - Thread-safe operations
    """
    
    def __init__(
        self,
        db: "Database",
        default_window: int = 60,
        max_cache_size: int = 10000,
    ):
        """Initialize the deduplication manager.
        
        Args:
            db: Database instance
            default_window: Default dedup window in seconds
            max_cache_size: Maximum entries in memory cache
        """
        self._db = db
        self._default_window = default_window
        self._max_cache_size = max_cache_size
        
        # In-memory cache: fingerprint -> (started_at, run_id)
        self._cache: dict[str, tuple[datetime, int]] = {}
        self._idempotency_cache: dict[str, tuple[datetime, int]] = {}
        self._lock = Lock()
        
        # Load recent executions from DB
        self._load_recent_from_db()
    
    def _load_recent_from_db(self) -> None:
        """Load recent executions from database into cache."""
        try:
            # Get runs from last hour
            cutoff = datetime.now() - timedelta(hours=1)
            recent = self._db.get_recent_executions(since=cutoff)
            
            with self._lock:
                for run in recent:
                    key = f"{run.job_id}:{run.fingerprint}" if hasattr(run, 'fingerprint') else run.job_id
                    self._cache[key] = (run.started_at, run.run_id)
                    
                    if hasattr(run, 'idempotency_key') and run.idempotency_key:
                        self._idempotency_cache[run.idempotency_key] = (run.started_at, run.run_id)
            
            logger.debug(f"Loaded {len(recent)} recent executions for dedup")
        except Exception as e:
            logger.warning(f"Failed to load recent executions: {e}")
    
    def check(
        self,
        job_id: str,
        params: dict | None = None,
        idempotency_key: str | None = None,
        window_seconds: int | None = None,
    ) -> DedupResult:
        """Check if a job execution would be a duplicate.
        
        Args:
            job_id: The job ID
            params: Optional parameters affecting execution
            idempotency_key: Optional idempotency key
            window_seconds: Override dedup window
            
        Returns:
            DedupResult indicating if this is a duplicate
        """
        window = window_seconds or self._default_window
        cutoff = datetime.now() - timedelta(seconds=window)
        
        with self._lock:
            # Check idempotency key first (exact match, no window)
            if idempotency_key and idempotency_key in self._idempotency_cache:
                started_at, run_id = self._idempotency_cache[idempotency_key]
                return DedupResult(
                    is_duplicate=True,
                    reason=f"Idempotency key '{idempotency_key}' already used",
                    original_run_id=run_id,
                    original_started_at=started_at,
                )
            
            # Check fingerprint-based dedup
            fingerprint = ExecutionFingerprint.create(job_id, params, idempotency_key)
            cache_key = f"{job_id}:{fingerprint.fingerprint}"
            
            if cache_key in self._cache:
                started_at, run_id = self._cache[cache_key]
                if started_at >= cutoff:
                    return DedupResult(
                        is_duplicate=True,
                        reason=f"Same execution within {window}s window",
                        original_run_id=run_id,
                        original_started_at=started_at,
                    )
        
        return DedupResult(is_duplicate=False)
    
    def record(
        self,
        job_id: str,
        run_id: int,
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Record an execution for future deduplication.
        
        Args:
            job_id: The job ID
            run_id: The run ID
            params: Optional parameters
            idempotency_key: Optional idempotency key
        """
        fingerprint = ExecutionFingerprint.create(job_id, params, idempotency_key)
        now = datetime.now()
        
        with self._lock:
            # Add to cache
            cache_key = f"{job_id}:{fingerprint.fingerprint}"
            self._cache[cache_key] = (now, run_id)
            
            if idempotency_key:
                self._idempotency_cache[idempotency_key] = (now, run_id)
            
            # Evict old entries if cache is too large
            self._evict_if_needed()
        
        # Persist to DB
        try:
            self._db.record_execution(
                job_id=job_id,
                run_id=run_id,
                fingerprint=fingerprint.fingerprint,
                idempotency_key=idempotency_key,
            )
        except Exception as e:
            logger.warning(f"Failed to persist execution record: {e}")
    
    def _evict_if_needed(self) -> None:
        """Evict old entries if cache exceeds max size."""
        if len(self._cache) <= self._max_cache_size:
            return
        
        # Evict oldest entries
        cutoff = datetime.now() - timedelta(hours=1)
        
        keys_to_remove = [
            key for key, (started_at, _) in self._cache.items()
            if started_at < cutoff
        ]
        
        for key in keys_to_remove:
            del self._cache[key]
        
        # Also clean idempotency cache
        keys_to_remove = [
            key for key, (started_at, _) in self._idempotency_cache.items()
            if started_at < cutoff
        ]
        
        for key in keys_to_remove:
            del self._idempotency_cache[key]
        
        logger.debug(f"Evicted {len(keys_to_remove)} old dedup entries")
    
    def clear_job(self, job_id: str) -> int:
        """Clear dedup entries for a specific job.
        
        Args:
            job_id: The job ID
            
        Returns:
            Number of entries cleared
        """
        with self._lock:
            keys_to_remove = [
                key for key in self._cache.keys()
                if key.startswith(f"{job_id}:")
            ]
            
            for key in keys_to_remove:
                del self._cache[key]
            
            return len(keys_to_remove)
    
    def clear_all(self) -> int:
        """Clear all dedup entries.
        
        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._cache) + len(self._idempotency_cache)
            self._cache.clear()
            self._idempotency_cache.clear()
            return count
    
    def get_stats(self) -> dict:
        """Get deduplication statistics.
        
        Returns:
            Dict with stats
        """
        with self._lock:
            return {
                "cache_size": len(self._cache),
                "idempotency_cache_size": len(self._idempotency_cache),
                "max_cache_size": self._max_cache_size,
            }
