"""Queue manager for sequential job execution.

Jobs in the same queue run one at a time. Different queues run in parallel.
Jobs without a queue run immediately (parallel, current behavior).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from procclaw.models import JobConfig

logger = logging.getLogger(__name__)


@dataclass
class QueuedJob:
    """A job waiting in queue."""
    
    job_id: str
    queued_at: datetime
    trigger: str = "scheduled"  # scheduled, manual, retry


@dataclass
class QueueState:
    """State of a single queue."""
    
    name: str
    running_job: str | None = None  # Currently executing job
    running_since: datetime | None = None
    pending: list[QueuedJob] = field(default_factory=list)  # Jobs waiting
    
    @property
    def is_busy(self) -> bool:
        """Check if queue has a job running."""
        return self.running_job is not None
    
    def get_position(self, job_id: str) -> int | None:
        """Get position in pending queue (0-indexed). None if not in queue."""
        for i, qj in enumerate(self.pending):
            if qj.job_id == job_id:
                return i
        return None


class QueueManager:
    """Manages execution queues for jobs.
    
    Usage:
        qm = QueueManager()
        
        # When starting a job:
        can_run = qm.try_acquire(job_id, job_config, trigger="scheduled")
        if can_run:
            # Start the job
        else:
            # Job was queued, will be started later via callback
        
        # When job finishes:
        qm.release(job_id, job_config)  # This triggers callback for next job
    """
    
    def __init__(self, on_job_ready: Callable[[str, str], None] | None = None):
        """Initialize queue manager.
        
        Args:
            on_job_ready: Callback(job_id, trigger) when a queued job should start.
        """
        self._queues: dict[str, QueueState] = {}
        self._on_job_ready = on_job_ready
    
    def set_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set the callback for when a queued job is ready to run."""
        self._on_job_ready = callback
    
    def _get_or_create_queue(self, queue_name: str) -> QueueState:
        """Get or create a queue by name."""
        if queue_name not in self._queues:
            self._queues[queue_name] = QueueState(name=queue_name)
            logger.info(f"Created queue '{queue_name}'")
        return self._queues[queue_name]
    
    def try_acquire(
        self, 
        job_id: str, 
        job_config: "JobConfig",
        trigger: str = "scheduled",
        force: bool = False
    ) -> bool:
        """Try to acquire a slot in the job's queue.
        
        Args:
            job_id: Job identifier
            job_config: Job configuration
            trigger: What triggered this job start
            force: If True, bypass queue (for manual starts)
        
        Returns:
            True if job can run immediately, False if queued.
        """
        queue_name = job_config.queue
        
        # No queue = run immediately
        if queue_name is None:
            logger.debug(f"Job '{job_id}' has no queue, running immediately")
            return True
        
        # Force bypasses queue
        if force:
            logger.info(f"Job '{job_id}' force-started, bypassing queue '{queue_name}'")
            return True
        
        queue = self._get_or_create_queue(queue_name)
        
        # Check if job is already running or queued
        if queue.running_job == job_id:
            logger.warning(f"Job '{job_id}' is already running in queue '{queue_name}'")
            return False
        
        if queue.get_position(job_id) is not None:
            logger.warning(f"Job '{job_id}' is already queued in '{queue_name}'")
            return False
        
        # Queue is free - job can run
        if not queue.is_busy:
            queue.running_job = job_id
            queue.running_since = datetime.now()
            logger.info(f"Job '{job_id}' acquired queue '{queue_name}'")
            return True
        
        # Queue is busy - add to pending
        queued_job = QueuedJob(
            job_id=job_id,
            queued_at=datetime.now(),
            trigger=trigger,
        )
        queue.pending.append(queued_job)
        position = len(queue.pending) - 1
        logger.info(
            f"Job '{job_id}' queued in '{queue_name}' at position {position} "
            f"(waiting for '{queue.running_job}')"
        )
        return False
    
    def release(self, job_id: str, job_config: "JobConfig") -> str | None:
        """Release a job's slot in its queue and start next job if any.
        
        Args:
            job_id: Job that finished
            job_config: Job configuration
        
        Returns:
            job_id of next job to start, or None
        """
        queue_name = job_config.queue
        
        # No queue = nothing to release
        if queue_name is None:
            return None
        
        queue = self._queues.get(queue_name)
        if queue is None:
            return None
        
        # Only release if this job was the running one
        if queue.running_job != job_id:
            # Maybe job was force-started or not in queue
            # Try to remove from pending just in case
            queue.pending = [qj for qj in queue.pending if qj.job_id != job_id]
            return None
        
        # Clear running slot
        queue.running_job = None
        queue.running_since = None
        logger.info(f"Job '{job_id}' released queue '{queue_name}'")
        
        # Start next pending job if any
        if queue.pending:
            next_job = queue.pending.pop(0)
            queue.running_job = next_job.job_id
            queue.running_since = datetime.now()
            logger.info(
                f"Queue '{queue_name}': starting next job '{next_job.job_id}' "
                f"(waited {(datetime.now() - next_job.queued_at).total_seconds():.1f}s)"
            )
            
            # Callback to actually start the job
            if self._on_job_ready:
                self._on_job_ready(next_job.job_id, next_job.trigger)
            
            return next_job.job_id
        
        return None
    
    def remove_from_queue(self, job_id: str, job_config: "JobConfig") -> bool:
        """Remove a job from its queue's pending list.
        
        Use when job is paused/disabled while waiting.
        
        Returns:
            True if job was removed from pending.
        """
        queue_name = job_config.queue
        if queue_name is None:
            return False
        
        queue = self._queues.get(queue_name)
        if queue is None:
            return False
        
        original_len = len(queue.pending)
        queue.pending = [qj for qj in queue.pending if qj.job_id != job_id]
        
        if len(queue.pending) < original_len:
            logger.info(f"Job '{job_id}' removed from queue '{queue_name}' pending list")
            return True
        
        return False
    
    def get_queue_info(self, queue_name: str) -> dict | None:
        """Get info about a specific queue."""
        queue = self._queues.get(queue_name)
        if queue is None:
            return None
        
        return {
            "name": queue.name,
            "running": queue.running_job,
            "running_since": queue.running_since.isoformat() if queue.running_since else None,
            "pending": [
                {
                    "job_id": qj.job_id,
                    "queued_at": qj.queued_at.isoformat(),
                    "trigger": qj.trigger,
                    "position": i,
                }
                for i, qj in enumerate(queue.pending)
            ],
            "pending_count": len(queue.pending),
        }
    
    def get_all_queues(self) -> list[dict]:
        """Get info about all queues."""
        return [self.get_queue_info(name) for name in sorted(self._queues.keys())]
    
    def get_job_queue_status(self, job_id: str, job_config: "JobConfig") -> dict | None:
        """Get queue status for a specific job.
        
        Returns:
            Dict with queue info, or None if job has no queue.
        """
        queue_name = job_config.queue
        if queue_name is None:
            return None
        
        queue = self._queues.get(queue_name)
        if queue is None:
            return {"queue": queue_name, "status": "not_active"}
        
        if queue.running_job == job_id:
            return {
                "queue": queue_name,
                "status": "running",
                "running_since": queue.running_since.isoformat() if queue.running_since else None,
            }
        
        position = queue.get_position(job_id)
        if position is not None:
            queued_job = queue.pending[position]
            return {
                "queue": queue_name,
                "status": "queued",
                "position": position,
                "queued_at": queued_job.queued_at.isoformat(),
                "waiting_for": queue.running_job,
            }
        
        return {"queue": queue_name, "status": "idle"}
    
    def is_job_queued(self, job_id: str, job_config: "JobConfig") -> bool:
        """Check if a job is waiting in queue (not running)."""
        queue_name = job_config.queue
        if queue_name is None:
            return False
        
        queue = self._queues.get(queue_name)
        if queue is None:
            return False
        
        return queue.get_position(job_id) is not None
    
    def clear_queue(self, queue_name: str) -> int:
        """Clear all pending jobs from a queue.
        
        Returns:
            Number of jobs removed.
        """
        queue = self._queues.get(queue_name)
        if queue is None:
            return 0
        
        count = len(queue.pending)
        queue.pending = []
        logger.info(f"Cleared {count} pending jobs from queue '{queue_name}'")
        return count
