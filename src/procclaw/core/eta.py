"""ETA (Estimated Time of Arrival) scheduling for ProcClaw.

Allows jobs to be scheduled for a specific datetime, not just cron.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database


@dataclass
class ETAJob:
    """A job scheduled for a specific time."""
    
    job_id: str
    run_at: datetime  # UTC
    scheduled_at: datetime
    trigger: str = "eta"
    params: dict | None = None
    idempotency_key: str | None = None
    one_shot: bool = True  # Remove after triggering
    
    @property
    def is_due(self) -> bool:
        """Check if the job is due to run."""
        return datetime.now(ZoneInfo("UTC")) >= self.run_at
    
    @property
    def seconds_until(self) -> float:
        """Seconds until the job is due."""
        now = datetime.now(ZoneInfo("UTC"))
        return (self.run_at - now).total_seconds()


class ETAScheduler:
    """Manages ETA-scheduled jobs.
    
    Features:
    - Schedule jobs for specific datetime
    - Schedule jobs relative to now (run_in seconds)
    - Timezone support
    - Persistence across restarts
    """
    
    def __init__(
        self,
        db: "Database",
        on_trigger: Callable[[str, str, dict | None, str | None], None] | None = None,
        default_timezone: str = "America/Sao_Paulo",
    ):
        """Initialize the ETA scheduler.
        
        Args:
            db: Database for persistence
            on_trigger: Callback when job is due (job_id, trigger, params, idempotency_key)
            default_timezone: Default timezone for parsing datetimes
        """
        self._db = db
        self._on_trigger = on_trigger
        self._default_tz = ZoneInfo(default_timezone)
        self._lock = Lock()
        self._eta_jobs: dict[str, ETAJob] = {}
        self._running = False
        
        # Load persisted ETA jobs
        self._load_from_db()
    
    def _load_from_db(self) -> None:
        """Load ETA jobs from database."""
        try:
            rows = self._db.query(
                "SELECT job_id, run_at, scheduled_at, trigger, params, idempotency_key, one_shot "
                "FROM eta_jobs WHERE triggered = 0"
            )
            for row in rows:
                job = ETAJob(
                    job_id=row["job_id"],
                    run_at=datetime.fromisoformat(row["run_at"]).replace(tzinfo=ZoneInfo("UTC")),
                    scheduled_at=datetime.fromisoformat(row["scheduled_at"]).replace(tzinfo=ZoneInfo("UTC")),
                    trigger=row["trigger"] or "eta",
                    params=eval(row["params"]) if row["params"] else None,
                    idempotency_key=row["idempotency_key"],
                    one_shot=bool(row["one_shot"]),
                )
                self._eta_jobs[job.job_id] = job
            logger.debug(f"Loaded {len(self._eta_jobs)} ETA jobs from database")
        except Exception as e:
            logger.debug(f"No ETA jobs table yet: {e}")
    
    def schedule_at(
        self,
        job_id: str,
        run_at: datetime | str,
        timezone: str | None = None,
        trigger: str = "eta",
        params: dict | None = None,
        idempotency_key: str | None = None,
        one_shot: bool = True,
    ) -> ETAJob:
        """Schedule a job to run at a specific time.
        
        Args:
            job_id: The job to schedule
            run_at: When to run (datetime or ISO string)
            timezone: Timezone for the datetime
            trigger: Trigger type for logging
            params: Optional parameters
            idempotency_key: Optional idempotency key
            one_shot: If True, remove after triggering
            
        Returns:
            The scheduled ETAJob
        """
        # Parse datetime
        if isinstance(run_at, str):
            run_at = datetime.fromisoformat(run_at)
        
        # Apply timezone
        tz = ZoneInfo(timezone) if timezone else self._default_tz
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=tz)
        
        # Convert to UTC
        run_at_utc = run_at.astimezone(ZoneInfo("UTC"))
        
        job = ETAJob(
            job_id=job_id,
            run_at=run_at_utc,
            scheduled_at=datetime.now(ZoneInfo("UTC")),
            trigger=trigger,
            params=params,
            idempotency_key=idempotency_key,
            one_shot=one_shot,
        )
        
        with self._lock:
            # Cancel existing ETA for this job if any
            if job_id in self._eta_jobs:
                self._remove_from_db(job_id)
            
            self._eta_jobs[job_id] = job
            self._persist_to_db(job)
        
        logger.info(f"Scheduled job '{job_id}' to run at {run_at_utc.isoformat()}")
        return job
    
    def schedule_in(
        self,
        job_id: str,
        seconds: int,
        **kwargs,
    ) -> ETAJob:
        """Schedule a job to run in N seconds from now.
        
        Args:
            job_id: The job to schedule
            seconds: Seconds from now
            **kwargs: Additional args for schedule_at
            
        Returns:
            The scheduled ETAJob
        """
        run_at = datetime.now(ZoneInfo("UTC")) + timedelta(seconds=seconds)
        return self.schedule_at(job_id, run_at, timezone="UTC", **kwargs)
    
    def cancel(self, job_id: str) -> bool:
        """Cancel an ETA-scheduled job.
        
        Args:
            job_id: The job to cancel
            
        Returns:
            True if cancelled, False if not found
        """
        with self._lock:
            if job_id in self._eta_jobs:
                del self._eta_jobs[job_id]
                self._remove_from_db(job_id)
                logger.info(f"Cancelled ETA schedule for job '{job_id}'")
                return True
        return False
    
    def get_eta(self, job_id: str) -> ETAJob | None:
        """Get the ETA job for a job ID."""
        return self._eta_jobs.get(job_id)
    
    def get_all_pending(self) -> list[ETAJob]:
        """Get all pending ETA jobs."""
        return list(self._eta_jobs.values())
    
    def get_due_jobs(self) -> list[ETAJob]:
        """Get all jobs that are due to run now."""
        return [job for job in self._eta_jobs.values() if job.is_due]
    
    def check_and_trigger(self) -> list[str]:
        """Check for due jobs and trigger them.
        
        Returns:
            List of job IDs that were triggered
        """
        triggered = []
        
        with self._lock:
            due_jobs = self.get_due_jobs()
            
            for job in due_jobs:
                if self._on_trigger:
                    try:
                        self._on_trigger(
                            job.job_id,
                            job.trigger,
                            job.params,
                            job.idempotency_key,
                        )
                        triggered.append(job.job_id)
                        logger.info(f"Triggered ETA job '{job.job_id}'")
                    except Exception as e:
                        logger.error(f"Failed to trigger ETA job '{job.job_id}': {e}")
                        continue
                
                # Remove if one-shot
                if job.one_shot:
                    del self._eta_jobs[job.job_id]
                    self._mark_triggered_in_db(job.job_id)
        
        return triggered
    
    def _persist_to_db(self, job: ETAJob) -> None:
        """Persist an ETA job to database."""
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO eta_jobs 
                (job_id, run_at, scheduled_at, trigger, params, idempotency_key, one_shot, triggered)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    job.job_id,
                    job.run_at.isoformat(),
                    job.scheduled_at.isoformat(),
                    job.trigger,
                    str(job.params) if job.params else None,
                    job.idempotency_key,
                    1 if job.one_shot else 0,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to persist ETA job: {e}")
    
    def _remove_from_db(self, job_id: str) -> None:
        """Remove an ETA job from database."""
        try:
            self._db.execute("DELETE FROM eta_jobs WHERE job_id = ?", (job_id,))
        except Exception as e:
            logger.debug(f"Failed to remove ETA job from db: {e}")
    
    def _mark_triggered_in_db(self, job_id: str) -> None:
        """Mark an ETA job as triggered in database."""
        try:
            self._db.execute(
                "UPDATE eta_jobs SET triggered = 1, triggered_at = ? WHERE job_id = ?",
                (datetime.now(ZoneInfo("UTC")).isoformat(), job_id),
            )
        except Exception as e:
            logger.debug(f"Failed to mark ETA job triggered: {e}")
    
    async def run(self) -> None:
        """Run the ETA scheduler loop."""
        import asyncio
        
        self._running = True
        logger.info("ETA scheduler started")
        
        while self._running:
            try:
                self.check_and_trigger()
            except Exception as e:
                logger.error(f"ETA scheduler error: {e}")
            
            # Check every second
            await asyncio.sleep(1)
        
        logger.info("ETA scheduler stopped")
    
    def stop(self) -> None:
        """Stop the ETA scheduler."""
        self._running = False
