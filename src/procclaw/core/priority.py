"""Priority queue management for ProcClaw.

Manages job execution order based on priority levels.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import Priority


@dataclass(order=True)
class PrioritizedJob:
    """A job with priority for queue ordering.
    
    Lower priority number = higher importance (CRITICAL=0).
    Secondary sort by queue time (FIFO within priority).
    """
    
    priority: int
    queued_at: datetime = field(compare=True)
    job_id: str = field(compare=False)
    trigger: str = field(compare=False, default="manual")
    params: dict | None = field(compare=False, default=None)
    idempotency_key: str | None = field(compare=False, default=None)


class PriorityQueue:
    """Thread-safe priority queue for job execution.
    
    Jobs are ordered by:
    1. Priority (CRITICAL=0 > HIGH=1 > NORMAL=2 > LOW=3)
    2. Queue time (FIFO within same priority)
    
    Features:
    - O(log n) enqueue/dequeue
    - Priority preemption info
    - Queue stats by priority
    """
    
    def __init__(self, max_size: int = 10000):
        """Initialize the priority queue.
        
        Args:
            max_size: Maximum queue size
        """
        self._max_size = max_size
        self._heap: list[PrioritizedJob] = []
        self._lock = Lock()
        
        # Track by job_id for fast lookup
        self._by_job: dict[str, list[PrioritizedJob]] = {}
    
    def enqueue(
        self,
        job_id: str,
        priority: int,
        trigger: str = "manual",
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Add a job to the queue.
        
        Args:
            job_id: The job ID
            priority: Priority level (0=CRITICAL to 3=LOW)
            trigger: What triggered the job
            params: Optional parameters
            idempotency_key: Optional idempotency key
            
        Returns:
            True if enqueued, False if queue full
        """
        with self._lock:
            if len(self._heap) >= self._max_size:
                logger.warning(f"Priority queue full ({self._max_size})")
                return False
            
            job = PrioritizedJob(
                priority=priority,
                queued_at=datetime.now(),
                job_id=job_id,
                trigger=trigger,
                params=params,
                idempotency_key=idempotency_key,
            )
            
            heapq.heappush(self._heap, job)
            
            # Track by job_id
            if job_id not in self._by_job:
                self._by_job[job_id] = []
            self._by_job[job_id].append(job)
            
            logger.debug(
                f"Enqueued '{job_id}' with priority {priority}, "
                f"queue size: {len(self._heap)}"
            )
            
            return True
    
    def dequeue(self) -> PrioritizedJob | None:
        """Remove and return the highest priority job.
        
        Returns:
            PrioritizedJob or None if queue empty
        """
        with self._lock:
            if not self._heap:
                return None
            
            job = heapq.heappop(self._heap)
            
            # Update tracking
            if job.job_id in self._by_job:
                try:
                    self._by_job[job.job_id].remove(job)
                    if not self._by_job[job.job_id]:
                        del self._by_job[job.job_id]
                except ValueError:
                    pass
            
            return job
    
    def peek(self) -> PrioritizedJob | None:
        """Look at the highest priority job without removing.
        
        Returns:
            PrioritizedJob or None if queue empty
        """
        with self._lock:
            return self._heap[0] if self._heap else None
    
    def remove(self, job_id: str) -> int:
        """Remove all queued instances of a job.
        
        Args:
            job_id: The job ID
            
        Returns:
            Number of entries removed
        """
        with self._lock:
            if job_id not in self._by_job:
                return 0
            
            to_remove = self._by_job.pop(job_id)
            
            # Rebuild heap without these entries
            self._heap = [j for j in self._heap if j.job_id != job_id]
            heapq.heapify(self._heap)
            
            return len(to_remove)
    
    def get_position(self, job_id: str) -> int | None:
        """Get the queue position for a job (0 = next to run).
        
        Args:
            job_id: The job ID
            
        Returns:
            Position or None if not in queue
        """
        with self._lock:
            if job_id not in self._by_job:
                return None
            
            # Sort heap to get true order
            sorted_heap = sorted(self._heap)
            
            for i, job in enumerate(sorted_heap):
                if job.job_id == job_id:
                    return i
            
            return None
    
    def get_count(self, job_id: str | None = None) -> int:
        """Get the number of queued jobs.
        
        Args:
            job_id: Optional filter by job ID
            
        Returns:
            Count of queued jobs
        """
        with self._lock:
            if job_id:
                return len(self._by_job.get(job_id, []))
            return len(self._heap)
    
    def get_stats(self) -> dict:
        """Get queue statistics.
        
        Returns:
            Dict with stats by priority level
        """
        with self._lock:
            stats = {
                "total": len(self._heap),
                "by_priority": {0: 0, 1: 0, 2: 0, 3: 0},
                "unique_jobs": len(self._by_job),
            }
            
            for job in self._heap:
                stats["by_priority"][job.priority] = (
                    stats["by_priority"].get(job.priority, 0) + 1
                )
            
            return stats
    
    def clear(self) -> int:
        """Clear the entire queue.
        
        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._heap)
            self._heap.clear()
            self._by_job.clear()
            return count
    
    def should_preempt(self, current_priority: int) -> bool:
        """Check if a higher priority job is waiting.
        
        Useful for deciding whether to pause current work
        to handle something more urgent.
        
        Args:
            current_priority: Priority of currently running job
            
        Returns:
            True if a higher priority job is queued
        """
        with self._lock:
            if not self._heap:
                return False
            
            return self._heap[0].priority < current_priority
    
    def get_waiting_by_priority(self, priority: int) -> list[PrioritizedJob]:
        """Get all jobs waiting at a specific priority.
        
        Args:
            priority: Priority level to filter
            
        Returns:
            List of jobs at that priority
        """
        with self._lock:
            return [j for j in self._heap if j.priority == priority]


class PriorityScheduler:
    """Schedules job execution respecting priorities.
    
    Integrates with the concurrency limiter to execute
    jobs in priority order when slots are available.
    """
    
    def __init__(
        self,
        max_queue_size: int = 10000,
        preemption_enabled: bool = False,
    ):
        """Initialize the priority scheduler.
        
        Args:
            max_queue_size: Maximum queue size
            preemption_enabled: Allow high priority to preempt low
        """
        self._queue = PriorityQueue(max_size=max_queue_size)
        self._preemption_enabled = preemption_enabled
        
        # Callbacks
        self._on_execute: callable = None
        self._on_preempt: callable = None
    
    def set_execute_callback(self, callback: callable) -> None:
        """Set the callback for executing jobs."""
        self._on_execute = callback
    
    def set_preempt_callback(self, callback: callable) -> None:
        """Set the callback for preempting jobs."""
        self._on_preempt = callback
    
    def submit(
        self,
        job_id: str,
        priority: int,
        trigger: str = "manual",
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Submit a job for prioritized execution.
        
        Args:
            job_id: The job ID
            priority: Priority level
            trigger: What triggered the job
            params: Optional parameters
            idempotency_key: Optional idempotency key
            
        Returns:
            True if submitted successfully
        """
        return self._queue.enqueue(
            job_id=job_id,
            priority=priority,
            trigger=trigger,
            params=params,
            idempotency_key=idempotency_key,
        )
    
    def get_next(self) -> PrioritizedJob | None:
        """Get the next job to execute."""
        return self._queue.dequeue()
    
    def cancel(self, job_id: str) -> int:
        """Cancel all queued executions of a job."""
        return self._queue.remove(job_id)
    
    def get_stats(self) -> dict:
        """Get scheduler statistics."""
        return self._queue.get_stats()
    
    def check_preemption(self, current_priority: int) -> bool:
        """Check if preemption is needed."""
        if not self._preemption_enabled:
            return False
        return self._queue.should_preempt(current_priority)
