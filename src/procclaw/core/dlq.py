"""Dead Letter Queue (DLQ) for ProcClaw.

Stores jobs that have exhausted their retry attempts for later inspection
and manual reinjection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database
    from procclaw.models import JobConfig


@dataclass
class DLQEntry:
    """An entry in the dead letter queue."""
    
    id: int
    job_id: str
    original_run_id: int | None
    failed_at: datetime
    attempts: int
    last_error: str
    job_config: dict | None
    trigger_params: dict | None
    reinjected_at: datetime | None = None
    reinjected_run_id: int | None = None
    
    @property
    def is_reinjected(self) -> bool:
        """Check if this entry has been reinjected."""
        return self.reinjected_at is not None


@dataclass
class DLQStats:
    """Statistics about the dead letter queue."""
    
    total_entries: int
    pending_entries: int  # Not yet reinjected
    reinjected_entries: int
    by_job: dict[str, int]  # Entries per job


class DeadLetterQueue:
    """Manages the dead letter queue for failed jobs.
    
    Jobs are moved to the DLQ when they exhaust their retry attempts.
    They can be inspected, manually fixed, and reinjected for retry.
    
    Features:
    - Automatic capture of failed jobs
    - Job config snapshot for debugging
    - Reinjection with new run tracking
    - Retention policy with cleanup
    """
    
    def __init__(
        self,
        db: "Database",
        retention_days: int = 30,
        on_dlq_add: Callable[[DLQEntry], None] | None = None,
    ):
        """Initialize the dead letter queue.
        
        Args:
            db: Database instance
            retention_days: How long to keep DLQ entries
            on_dlq_add: Callback when job is added to DLQ
        """
        self._db = db
        self._retention_days = retention_days
        self._on_dlq_add = on_dlq_add
    
    def add(
        self,
        job_id: str,
        run_id: int | None,
        error: str,
        attempts: int,
        job_config: "JobConfig" | None = None,
        trigger_params: dict | None = None,
    ) -> DLQEntry:
        """Add a failed job to the dead letter queue.
        
        Args:
            job_id: The job ID
            run_id: The failed run ID
            error: Error message
            attempts: Number of attempts made
            job_config: Job configuration snapshot
            trigger_params: Parameters that triggered the job
            
        Returns:
            The created DLQ entry
        """
        # Serialize config and params
        config_json = None
        if job_config:
            config_json = job_config.model_dump_json()
        
        params_json = None
        if trigger_params:
            params_json = json.dumps(trigger_params)
        
        # Add to database
        dlq_id = self._db.add_to_dlq(
            job_id=job_id,
            run_id=run_id,
            error=error,
            attempts=attempts,
            job_config=config_json,
            trigger_params=params_json,
        )
        
        entry = DLQEntry(
            id=dlq_id,
            job_id=job_id,
            original_run_id=run_id,
            failed_at=datetime.now(),
            attempts=attempts,
            last_error=error,
            job_config=job_config.model_dump() if job_config else None,
            trigger_params=trigger_params,
        )
        
        logger.warning(
            f"Job '{job_id}' added to DLQ after {attempts} attempts: {error}"
        )
        
        if self._on_dlq_add:
            try:
                self._on_dlq_add(entry)
            except Exception as e:
                logger.error(f"DLQ callback error: {e}")
        
        return entry
    
    def get(self, dlq_id: int) -> DLQEntry | None:
        """Get a specific DLQ entry.
        
        Args:
            dlq_id: The DLQ entry ID
            
        Returns:
            DLQEntry or None if not found
        """
        # Include reinjected to find all entries
        entries = self._db.get_dlq_entries(include_reinjected=True, limit=10000)
        for entry in entries:
            if entry["id"] == dlq_id:
                return self._dict_to_entry(entry)
        return None
    
    def list(
        self,
        job_id: str | None = None,
        include_reinjected: bool = False,
        limit: int = 100,
    ) -> list[DLQEntry]:
        """List DLQ entries.
        
        Args:
            job_id: Filter by job ID
            include_reinjected: Include reinjected entries
            limit: Maximum entries to return
            
        Returns:
            List of DLQ entries
        """
        entries = self._db.get_dlq_entries(
            job_id=job_id,
            include_reinjected=include_reinjected,
            limit=limit,
        )
        return [self._dict_to_entry(e) for e in entries]
    
    def reinject(
        self,
        dlq_id: int,
        on_reinject: Callable[[str, dict | None], int],
    ) -> int | None:
        """Reinject a job from the DLQ.
        
        Args:
            dlq_id: The DLQ entry ID
            on_reinject: Callback to start the job, returns new run_id
            
        Returns:
            New run ID or None if failed
        """
        entry = self.get(dlq_id)
        if entry is None:
            logger.error(f"DLQ entry {dlq_id} not found")
            return None
        
        if entry.is_reinjected:
            logger.warning(f"DLQ entry {dlq_id} already reinjected")
            return entry.reinjected_run_id
        
        try:
            # Start the job
            new_run_id = on_reinject(entry.job_id, entry.trigger_params)
            
            # Mark as reinjected
            self._db.reinject_from_dlq(dlq_id, new_run_id)
            
            logger.info(
                f"Reinjected job '{entry.job_id}' from DLQ "
                f"(dlq_id={dlq_id}, new_run_id={new_run_id})"
            )
            
            return new_run_id
            
        except Exception as e:
            logger.error(f"Failed to reinject DLQ entry {dlq_id}: {e}")
            return None
    
    def delete(self, dlq_id: int) -> bool:
        """Delete a DLQ entry.
        
        Args:
            dlq_id: The DLQ entry ID
            
        Returns:
            True if deleted
        """
        result = self._db.delete_dlq_entry(dlq_id)
        if result:
            logger.info(f"Deleted DLQ entry {dlq_id}")
        return result
    
    def cleanup(self, days: int | None = None) -> int:
        """Clean up old DLQ entries.
        
        Args:
            days: Override retention days
            
        Returns:
            Number of entries deleted
        """
        retention = days if days is not None else self._retention_days
        count = self._db.cleanup_old_dlq(days=retention)
        if count > 0:
            logger.info(f"Cleaned up {count} old DLQ entries")
        return count
    
    def get_stats(self) -> DLQStats:
        """Get DLQ statistics.
        
        Returns:
            DLQStats object
        """
        all_entries = self._db.get_dlq_entries(include_reinjected=True, limit=10000)
        
        pending = 0
        reinjected = 0
        by_job: dict[str, int] = {}
        
        for entry in all_entries:
            if entry.get("reinjected_at"):
                reinjected += 1
            else:
                pending += 1
            
            job_id = entry["job_id"]
            by_job[job_id] = by_job.get(job_id, 0) + 1
        
        return DLQStats(
            total_entries=len(all_entries),
            pending_entries=pending,
            reinjected_entries=reinjected,
            by_job=by_job,
        )
    
    def _dict_to_entry(self, d: dict) -> DLQEntry:
        """Convert a database dict to DLQEntry."""
        job_config = None
        if d.get("job_config"):
            try:
                job_config = json.loads(d["job_config"])
            except:
                pass
        
        trigger_params = None
        if d.get("trigger_params"):
            try:
                trigger_params = json.loads(d["trigger_params"])
            except:
                pass
        
        return DLQEntry(
            id=d["id"],
            job_id=d["job_id"],
            original_run_id=d.get("original_run_id"),
            failed_at=datetime.fromisoformat(d["failed_at"]) if d.get("failed_at") else datetime.now(),
            attempts=d.get("attempts", 0),
            last_error=d.get("last_error", ""),
            job_config=job_config,
            trigger_params=trigger_params,
            reinjected_at=datetime.fromisoformat(d["reinjected_at"]) if d.get("reinjected_at") else None,
            reinjected_run_id=d.get("reinjected_run_id"),
        )
