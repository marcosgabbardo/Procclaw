"""Healing queue for serialized self-healing execution.

Ensures only one healing runs at a time, with proper state tracking.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import JobConfig, JobRun


class HealingState(str, Enum):
    """State of a healing request."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class HealingRequest:
    """A request for self-healing."""
    
    id: str  # Unique ID for this request
    job_id: str
    run_id: int
    job_config: dict  # Serialized job config
    exit_code: int
    error: str | None
    logs: str
    stderr: str
    created_at: datetime = field(default_factory=datetime.now)
    state: HealingState = HealingState.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict | None = None
    previous_attempts: list[dict] = field(default_factory=list)


class HealingQueue:
    """Queue for serialized healing execution.
    
    Only one healing runs at a time. Requests are processed FIFO.
    """
    
    def __init__(self):
        self._queue: deque[HealingRequest] = deque()
        self._current: HealingRequest | None = None
        self._history: list[HealingRequest] = []  # Completed/failed requests
        self._lock = asyncio.Lock()
        self._processing = False
        self._cancelled_ids: set[str] = set()
    
    @property
    def current(self) -> HealingRequest | None:
        """Get the currently processing request."""
        return self._current
    
    @property
    def pending_count(self) -> int:
        """Number of pending requests."""
        return len(self._queue)
    
    @property
    def is_processing(self) -> bool:
        """Whether a healing is currently in progress."""
        return self._current is not None
    
    def get_status(self) -> dict:
        """Get queue status."""
        return {
            "processing": self.is_processing,
            "current": self._current.id if self._current else None,
            "current_job": self._current.job_id if self._current else None,
            "pending_count": self.pending_count,
            "pending_jobs": [r.job_id for r in self._queue],
            "history_count": len(self._history),
        }
    
    def get_previous_attempts(self, job_id: str) -> list[dict]:
        """Get previous healing attempts for a job (for context)."""
        attempts = []
        for req in self._history:
            if req.job_id == job_id and req.result:
                attempts.append({
                    "id": req.id,
                    "run_id": req.run_id,
                    "created_at": req.created_at.isoformat(),
                    "state": req.state.value,
                    "result": req.result,
                })
        return attempts[-5:]  # Last 5 attempts
    
    async def enqueue(
        self,
        job_id: str,
        run_id: int,
        job_config: dict,
        exit_code: int,
        error: str | None,
        logs: str,
        stderr: str,
    ) -> HealingRequest:
        """Add a healing request to the queue.
        
        Returns the created request.
        """
        async with self._lock:
            # Check if already queued for this job
            for req in self._queue:
                if req.job_id == job_id:
                    logger.info(f"Healing already queued for job '{job_id}', skipping")
                    return req
            
            # Check if currently processing this job
            if self._current and self._current.job_id == job_id:
                logger.info(f"Healing already in progress for job '{job_id}', skipping")
                return self._current
            
            # Get previous attempts for context
            previous = self.get_previous_attempts(job_id)
            
            # Create request
            request_id = f"heal-{job_id}-{run_id}-{datetime.now().strftime('%H%M%S')}"
            request = HealingRequest(
                id=request_id,
                job_id=job_id,
                run_id=run_id,
                job_config=job_config,
                exit_code=exit_code,
                error=error,
                logs=logs,
                stderr=stderr,
                previous_attempts=previous,
            )
            
            self._queue.append(request)
            logger.info(f"Enqueued healing request '{request_id}' for job '{job_id}' (queue size: {len(self._queue)})")
            
            return request
    
    async def dequeue(self) -> HealingRequest | None:
        """Get the next request to process.
        
        Returns None if queue is empty or already processing.
        """
        async with self._lock:
            if self._current is not None:
                return None  # Already processing
            
            if not self._queue:
                return None  # Queue empty
            
            request = self._queue.popleft()
            
            # Check if cancelled
            if request.id in self._cancelled_ids:
                self._cancelled_ids.discard(request.id)
                request.state = HealingState.CANCELLED
                self._history.append(request)
                logger.info(f"Skipped cancelled request '{request.id}'")
                return await self.dequeue()  # Try next
            
            request.state = HealingState.IN_PROGRESS
            request.started_at = datetime.now()
            self._current = request
            
            logger.info(f"Dequeued healing request '{request.id}' for job '{request.job_id}'")
            return request
    
    async def complete(self, request_id: str, result: dict, success: bool) -> None:
        """Mark a request as completed."""
        async with self._lock:
            if self._current and self._current.id == request_id:
                self._current.state = HealingState.COMPLETED if success else HealingState.FAILED
                self._current.completed_at = datetime.now()
                self._current.result = result
                self._history.append(self._current)
                logger.info(f"Completed healing request '{request_id}' (success={success})")
                self._current = None
    
    async def cancel(self, job_id: str) -> bool:
        """Cancel healing for a job.
        
        Returns True if something was cancelled.
        """
        async with self._lock:
            cancelled = False
            
            # Cancel current if matching
            if self._current and self._current.job_id == job_id:
                self._current.state = HealingState.CANCELLED
                self._current.completed_at = datetime.now()
                self._history.append(self._current)
                self._current = None
                cancelled = True
                logger.info(f"Cancelled in-progress healing for job '{job_id}'")
            
            # Cancel pending
            new_queue = deque()
            for req in self._queue:
                if req.job_id == job_id:
                    req.state = HealingState.CANCELLED
                    self._history.append(req)
                    cancelled = True
                    logger.info(f"Cancelled pending healing '{req.id}' for job '{job_id}'")
                else:
                    new_queue.append(req)
            self._queue = new_queue
            
            return cancelled
    
    def get_queue_list(self) -> list[dict]:
        """Get list of queued requests."""
        result = []
        
        if self._current:
            result.append({
                "id": self._current.id,
                "job_id": self._current.job_id,
                "run_id": self._current.run_id,
                "state": self._current.state.value,
                "created_at": self._current.created_at.isoformat(),
                "started_at": self._current.started_at.isoformat() if self._current.started_at else None,
            })
        
        for req in self._queue:
            result.append({
                "id": req.id,
                "job_id": req.job_id,
                "run_id": req.run_id,
                "state": req.state.value,
                "created_at": req.created_at.isoformat(),
                "started_at": None,
            })
        
        return result
    
    def clear_history(self, older_than_hours: int = 24) -> int:
        """Clear old history entries.
        
        Returns number of entries cleared.
        """
        cutoff = datetime.now().timestamp() - (older_than_hours * 3600)
        old_count = len(self._history)
        self._history = [
            r for r in self._history 
            if r.completed_at and r.completed_at.timestamp() > cutoff
        ]
        cleared = old_count - len(self._history)
        if cleared:
            logger.info(f"Cleared {cleared} old healing history entries")
        return cleared


# Global queue instance
_healing_queue: HealingQueue | None = None


def get_healing_queue() -> HealingQueue:
    """Get the global healing queue."""
    global _healing_queue
    if _healing_queue is None:
        _healing_queue = HealingQueue()
    return _healing_queue
