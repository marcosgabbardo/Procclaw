"""Dead Man's Switch / Watchdog for ProcClaw.

Monitors scheduled jobs and alerts if they don't run when expected.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable, TYPE_CHECKING

from croniter import croniter
from loguru import logger
import pytz

if TYPE_CHECKING:
    from procclaw.db import Database
    from procclaw.models import JobConfig


class MissedRunInfo:
    """Information about a missed job run."""
    
    def __init__(
        self,
        job_id: str,
        expected_time: datetime,
        grace_period_seconds: int,
        last_actual_run: datetime | None = None,
    ):
        self.job_id = job_id
        self.expected_time = expected_time
        self.grace_period_seconds = grace_period_seconds
        self.last_actual_run = last_actual_run
        self.detected_at = datetime.now()
    
    @property
    def hours_overdue(self) -> float:
        """How many hours overdue the job is."""
        if self.expected_time.tzinfo:
            now = datetime.now(self.expected_time.tzinfo)
        else:
            now = datetime.now()
        return (now - self.expected_time).total_seconds() / 3600
    
    def __repr__(self) -> str:
        return (
            f"MissedRunInfo(job_id={self.job_id!r}, "
            f"expected={self.expected_time}, "
            f"overdue={self.hours_overdue:.1f}h)"
        )


class Watchdog:
    """Monitors scheduled jobs for missed runs.
    
    The watchdog periodically checks if scheduled jobs ran when expected.
    If a job misses its expected run time by more than the grace period,
    an alert is triggered.
    
    Grace period calculation:
    - For frequent jobs (< 1h interval): 2x the interval
    - For hourly jobs: 30 minutes
    - For daily jobs: 2 hours
    - For weekly jobs: 12 hours
    """
    
    # Default grace periods by job frequency (in seconds)
    GRACE_PERIODS = {
        "minute": 120,      # 2 minutes for minute-level jobs
        "hourly": 1800,     # 30 minutes for hourly jobs
        "daily": 7200,      # 2 hours for daily jobs
        "weekly": 43200,    # 12 hours for weekly jobs
    }
    
    def __init__(
        self,
        db: "Database",
        get_jobs: Callable[[], dict[str, "JobConfig"]],
        on_missed_run: Callable[[MissedRunInfo], None] | None = None,
        check_interval: int = 300,  # 5 minutes
        default_timezone: str = "America/Sao_Paulo",
    ):
        """Initialize the watchdog.
        
        Args:
            db: Database instance for querying run history
            get_jobs: Callable that returns current job configurations
            on_missed_run: Callback when a missed run is detected
            check_interval: How often to check for missed runs (seconds)
            default_timezone: Default timezone for jobs without one
        """
        self._db = db
        self._get_jobs = get_jobs
        self._on_missed_run = on_missed_run
        self._check_interval = check_interval
        self._default_tz = default_timezone
        self._running = False
        
        # Track already-alerted missed runs to avoid duplicate alerts
        self._alerted_misses: set[tuple[str, datetime]] = set()
    
    def _get_schedule_interval_seconds(self, schedule: str) -> int:
        """Estimate the interval between runs from a cron expression.
        
        Returns the approximate interval in seconds.
        """
        try:
            now = datetime.now()
            cron = croniter(schedule, now)
            run1 = cron.get_next(datetime)
            run2 = cron.get_next(datetime)
            return int((run2 - run1).total_seconds())
        except Exception:
            return 86400  # Default to daily
    
    def _get_grace_period(self, schedule: str, job_grace: int | None = None) -> int:
        """Get the grace period for a job.
        
        Args:
            schedule: Cron expression
            job_grace: Optional job-specific grace period
            
        Returns:
            Grace period in seconds
        """
        if job_grace:
            return job_grace
        
        interval = self._get_schedule_interval_seconds(schedule)
        
        if interval < 3600:  # Sub-hourly
            return max(interval * 2, 120)  # At least 2 minutes
        elif interval < 86400:  # Sub-daily
            return self.GRACE_PERIODS["hourly"]
        elif interval < 604800:  # Sub-weekly
            return self.GRACE_PERIODS["daily"]
        else:
            return self.GRACE_PERIODS["weekly"]
    
    def _get_last_expected_run(
        self,
        schedule: str,
        timezone: str,
        since: datetime | None = None,
    ) -> datetime | None:
        """Get when the job should have last run.
        
        Args:
            schedule: Cron expression
            timezone: Job's timezone
            since: Check runs since this time (default: 24h ago)
            
        Returns:
            The most recent time the job should have run, or None
        """
        try:
            tz = pytz.timezone(timezone)
            now = datetime.now(tz)
            
            # Default to checking last 24 hours
            if since is None:
                since = now - timedelta(days=1)
            
            # Iterate backwards from now to find last expected run
            cron = croniter(schedule, now, ret_type=datetime)
            
            # Get the previous run time (before now)
            prev_run = cron.get_prev(datetime)
            
            # Make sure it's after our 'since' cutoff
            if prev_run >= since:
                return prev_run
            
            return None
            
        except Exception as e:
            logger.warning(f"Error calculating last expected run: {e}")
            return None
    
    def _get_last_actual_run(self, job_id: str) -> datetime | None:
        """Get when the job actually last ran."""
        last_run = self._db.get_last_run(job_id)
        if last_run and last_run.started_at:
            return last_run.started_at
        return None
    
    def check_job(self, job_id: str, job: "JobConfig") -> MissedRunInfo | None:
        """Check if a specific job missed its expected run.
        
        Args:
            job_id: The job ID
            job: The job configuration
            
        Returns:
            MissedRunInfo if the job missed a run, None otherwise
        """
        from procclaw.models import JobType
        
        # Only check scheduled jobs
        if job.type != JobType.SCHEDULED or not job.schedule:
            return None
        
        if not job.enabled:
            return None
        
        tz = job.timezone or self._default_tz
        grace_period = self._get_grace_period(job.schedule)
        
        # Get when job should have last run
        expected = self._get_last_expected_run(job.schedule, tz)
        if expected is None:
            return None
        
        # Get when job actually last ran
        actual = self._get_last_actual_run(job_id)
        
        # Calculate the deadline (expected + grace period)
        if expected.tzinfo:
            now = datetime.now(expected.tzinfo)
        else:
            now = datetime.now()
            expected = pytz.timezone(tz).localize(expected)
        
        deadline = expected + timedelta(seconds=grace_period)
        
        # If we're past the deadline...
        if now > deadline:
            # And job hasn't run since expected time
            # Handle timezone-naive vs aware comparison
            if actual is not None and actual.tzinfo is None and expected.tzinfo is not None:
                actual = pytz.timezone(tz).localize(actual)
            if actual is None or actual < expected:
                # Check if we already alerted for this specific miss
                miss_key = (job_id, expected)
                if miss_key in self._alerted_misses:
                    return None
                
                return MissedRunInfo(
                    job_id=job_id,
                    expected_time=expected,
                    grace_period_seconds=grace_period,
                    last_actual_run=actual,
                )
        
        return None
    
    def check_all_jobs(self) -> list[MissedRunInfo]:
        """Check all scheduled jobs for missed runs.
        
        Returns:
            List of MissedRunInfo for jobs that missed their runs
        """
        from procclaw.models import JobType
        
        missed = []
        jobs = self._get_jobs()
        
        for job_id, job in jobs.items():
            if job.type != JobType.SCHEDULED:
                continue
            
            miss = self.check_job(job_id, job)
            if miss:
                missed.append(miss)
        
        return missed
    
    async def run(self) -> None:
        """Run the watchdog loop."""
        self._running = True
        logger.info(f"Watchdog started (check interval: {self._check_interval}s)")
        
        while self._running:
            try:
                await self._check_and_alert()
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
            
            await asyncio.sleep(self._check_interval)
        
        logger.info("Watchdog stopped")
    
    async def _check_and_alert(self) -> None:
        """Check for missed runs and trigger alerts."""
        missed_runs = self.check_all_jobs()
        
        for miss in missed_runs:
            logger.warning(
                f"Missed run detected: {miss.job_id} "
                f"(expected: {miss.expected_time}, "
                f"overdue: {miss.hours_overdue:.1f}h)"
            )
            
            # Mark as alerted
            miss_key = (miss.job_id, miss.expected_time)
            self._alerted_misses.add(miss_key)
            
            # Trigger callback
            if self._on_missed_run:
                try:
                    self._on_missed_run(miss)
                except Exception as e:
                    logger.error(f"Error in missed run callback: {e}")
        
        # Clean up old alerted misses (> 24 hours old)
        self._cleanup_old_alerts()
    
    def _cleanup_old_alerts(self) -> None:
        """Remove old entries from alerted_misses set."""
        cutoff = datetime.now() - timedelta(days=1)
        
        # Remove entries older than cutoff
        self._alerted_misses = {
            (job_id, expected)
            for job_id, expected in self._alerted_misses
            if expected.replace(tzinfo=None) > cutoff.replace(tzinfo=None)
        }
    
    def stop(self) -> None:
        """Stop the watchdog."""
        self._running = False
    
    @property
    def is_running(self) -> bool:
        """Check if watchdog is running."""
        return self._running
    
    def clear_alerted(self, job_id: str | None = None) -> None:
        """Clear alerted misses.
        
        Args:
            job_id: Clear only for this job, or all if None
        """
        if job_id:
            self._alerted_misses = {
                (jid, expected)
                for jid, expected in self._alerted_misses
                if jid != job_id
            }
        else:
            self._alerted_misses.clear()
