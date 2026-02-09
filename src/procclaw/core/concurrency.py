"""Concurrency control for ProcClaw.

Limits the number of concurrent job instances and manages queuing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING, Callable

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database
    from procclaw.models import ConcurrencyConfig


@dataclass
class ConcurrencyStats:
    """Statistics about concurrency usage."""
    
    job_id: str
    running_count: int
    max_instances: int
    queued_count: int


@dataclass
class QueuedExecution:
    """A queued job execution waiting to run."""
    
    job_id: str
    queued_at: datetime
    trigger: str
    params: dict | None = None
    idempotency_key: str | None = None
    queue_id: int | None = None


class ConcurrencyLimiter:
    """Limits concurrent executions of jobs.
    
    Features:
    - Per-job max_instances limit
    - Global max concurrent limit
    - Queue excess jobs when limits exceeded
    - Queue timeout handling
    """
    
    def __init__(
        self,
        db: "Database",
        get_running_count: Callable[[str], int],
        global_max: int = 100,
    ):
        """Initialize the concurrency limiter.
        
        Args:
            db: Database instance
            get_running_count: Callback to get running job count
            global_max: Maximum concurrent jobs globally
        """
        self._db = db
        self._get_running_count = get_running_count
        self._global_max = global_max
        
        # Per-job tracking
        self._running: dict[str, int] = {}  # job_id -> count
        self._lock = Lock()
    
    def can_start(
        self,
        job_id: str,
        config: "ConcurrencyConfig",
    ) -> tuple[bool, str]:
        """Check if a job can start given concurrency limits.
        
        Args:
            job_id: The job ID
            config: Concurrency configuration
            
        Returns:
            Tuple of (can_start, reason)
        """
        with self._lock:
            current = self._running.get(job_id, 0)
            
            # Check per-job limit
            if current >= config.max_instances:
                return False, f"At max instances ({config.max_instances})"
            
            # Check global limit
            total_running = sum(self._running.values())
            if total_running >= self._global_max:
                return False, f"At global max ({self._global_max})"
            
            return True, "OK"
    
    def record_start(self, job_id: str) -> None:
        """Record that a job has started."""
        with self._lock:
            self._running[job_id] = self._running.get(job_id, 0) + 1
            logger.debug(f"Job '{job_id}' started, count: {self._running[job_id]}")
    
    def record_stop(self, job_id: str) -> None:
        """Record that a job has stopped."""
        with self._lock:
            if job_id in self._running:
                self._running[job_id] = max(0, self._running[job_id] - 1)
                if self._running[job_id] == 0:
                    del self._running[job_id]
                logger.debug(f"Job '{job_id}' stopped, count: {self._running.get(job_id, 0)}")
    
    def get_running_count(self, job_id: str) -> int:
        """Get the number of running instances for a job."""
        with self._lock:
            return self._running.get(job_id, 0)
    
    def get_total_running(self) -> int:
        """Get the total number of running jobs."""
        with self._lock:
            return sum(self._running.values())
    
    def queue_job(
        self,
        job_id: str,
        priority: int,
        trigger: str = "manual",
        params: str | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Queue a job for later execution.
        
        Args:
            job_id: The job ID
            priority: Job priority (lower = higher priority)
            trigger: What triggered the job
            params: Optional parameters (JSON)
            idempotency_key: Optional idempotency key
            
        Returns:
            Queue entry ID
        """
        return self._db.enqueue_job(
            job_id=job_id,
            priority=priority,
            trigger=trigger,
            params=params,
            idempotency_key=idempotency_key,
        )
    
    def get_next_queued(self) -> dict | None:
        """Get the next job from the queue.
        
        Returns:
            Queue entry dict or None
        """
        return self._db.dequeue_next()
    
    def get_queue_length(self, job_id: str | None = None) -> int:
        """Get the number of queued jobs."""
        return self._db.get_queue_length(job_id)
    
    def get_stats(self, job_id: str, config: "ConcurrencyConfig") -> ConcurrencyStats:
        """Get concurrency statistics for a job."""
        return ConcurrencyStats(
            job_id=job_id,
            running_count=self.get_running_count(job_id),
            max_instances=config.max_instances,
            queued_count=self.get_queue_length(job_id),
        )


class ConcurrencyManager:
    """Manages concurrent job execution with priority queuing.
    
    This is a higher-level manager that combines:
    - Concurrency limiting
    - Priority queue processing
    - Queue timeout handling
    """
    
    def __init__(
        self,
        db: "Database",
        on_execute: Callable[[str, str, dict | None], bool],
        global_max: int = 100,
        queue_check_interval: int = 5,
    ):
        """Initialize the concurrency manager.
        
        Args:
            db: Database instance
            on_execute: Callback to execute a job (job_id, trigger, params) -> success
            global_max: Maximum concurrent jobs globally
            queue_check_interval: How often to check queue (seconds)
        """
        self._db = db
        self._on_execute = on_execute
        self._queue_check_interval = queue_check_interval
        
        self._limiter = ConcurrencyLimiter(
            db=db,
            get_running_count=lambda job_id: 0,  # Will be updated
            global_max=global_max,
        )
        
        self._running = False
        self._configs: dict[str, "ConcurrencyConfig"] = {}
    
    def register_job(self, job_id: str, config: "ConcurrencyConfig") -> None:
        """Register a job's concurrency configuration."""
        self._configs[job_id] = config
    
    def unregister_job(self, job_id: str) -> None:
        """Unregister a job."""
        self._configs.pop(job_id, None)
    
    def try_start(
        self,
        job_id: str,
        trigger: str = "manual",
        params: dict | None = None,
        idempotency_key: str | None = None,
        priority: int = 2,
    ) -> tuple[bool, str]:
        """Try to start a job, queuing if necessary.
        
        Args:
            job_id: The job ID
            trigger: What triggered the job
            params: Optional parameters
            idempotency_key: Optional idempotency key
            priority: Job priority
            
        Returns:
            Tuple of (started_or_queued, status_message)
        """
        config = self._configs.get(job_id)
        if config is None:
            # No config = no limits
            return True, "No concurrency config"
        
        can_start, reason = self._limiter.can_start(job_id, config)
        
        if can_start:
            self._limiter.record_start(job_id)
            return True, "Started"
        
        # Can't start - should we queue?
        if config.queue_excess:
            import json
            params_json = json.dumps(params) if params else None
            queue_id = self._limiter.queue_job(
                job_id=job_id,
                priority=priority,
                trigger=trigger,
                params=params_json,
                idempotency_key=idempotency_key,
            )
            return False, f"Queued (id={queue_id})"
        
        return False, f"Rejected: {reason}"
    
    def record_complete(self, job_id: str) -> None:
        """Record that a job completed."""
        self._limiter.record_stop(job_id)
    
    async def run(self) -> None:
        """Run the queue processor loop."""
        self._running = True
        logger.info("Concurrency manager started")
        
        while self._running:
            try:
                await self._process_queue()
            except Exception as e:
                logger.error(f"Queue processing error: {e}")
            
            await asyncio.sleep(self._queue_check_interval)
        
        logger.info("Concurrency manager stopped")
    
    async def _process_queue(self) -> None:
        """Process queued jobs that can now run."""
        import json
        
        while True:
            entry = self._limiter.get_next_queued()
            if entry is None:
                break
            
            job_id = entry["job_id"]
            config = self._configs.get(job_id)
            
            if config is None:
                # Job was unregistered - complete and skip
                self._db.complete_queued_job(entry["id"])
                continue
            
            can_start, _ = self._limiter.can_start(job_id, config)
            
            if can_start:
                self._limiter.record_start(job_id)
                
                params = json.loads(entry["params"]) if entry.get("params") else None
                success = self._on_execute(job_id, entry["trigger"], params)
                
                if success:
                    self._db.complete_queued_job(entry["id"])
                else:
                    self._db.fail_queued_job(entry["id"])
            else:
                # Can't start yet - put back and stop processing
                # (entry was already claimed, need to requeue)
                self._db.enqueue_job(
                    job_id=job_id,
                    priority=entry["priority"],
                    trigger=entry["trigger"],
                    params=entry.get("params"),
                    idempotency_key=entry.get("idempotency_key"),
                )
                self._db.complete_queued_job(entry["id"])  # Remove the claimed one
                break
    
    def stop(self) -> None:
        """Stop the queue processor."""
        self._running = False
    
    @property
    def is_running(self) -> bool:
        """Check if manager is running."""
        return self._running
