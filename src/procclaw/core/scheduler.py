"""Cron scheduler for ProcClaw."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable

from croniter import croniter
from loguru import logger
import pytz

from procclaw.models import JobConfig, JobType, OnOverlap
from procclaw.core.operating_hours import OperatingHoursChecker


class Scheduler:
    """Cron-based job scheduler."""

    def __init__(
        self,
        on_trigger: Callable[[str, str], bool],
        is_job_running: Callable[[str], bool],
        timezone: str = "America/Sao_Paulo",
    ):
        """Initialize the scheduler.

        Args:
            on_trigger: Callback when job should run. Returns True if started.
            is_job_running: Callback to check if job is running.
            timezone: Default timezone for schedules.
        """
        self._on_trigger = on_trigger
        self._is_job_running = is_job_running
        self._default_tz = timezone
        self._jobs: dict[str, JobConfig] = {}
        self._next_runs: dict[str, datetime] = {}
        self._queued: dict[str, int] = {}  # job_id -> queue count
        self._running = False
        self._operating_hours = OperatingHoursChecker(default_timezone=timezone)

    def add_job(self, job_id: str, job: JobConfig) -> None:
        """Add a scheduled or oneshot job."""
        # Handle scheduled jobs (cron)
        if job.type == JobType.SCHEDULED and job.schedule:
            self._jobs[job_id] = job
            self._calculate_next_run(job_id)
            logger.debug(f"Scheduled job '{job_id}' - next run: {self._next_runs.get(job_id)}")
        # Handle oneshot jobs (run_at datetime)
        elif job.type == JobType.ONESHOT and job.run_at:
            self._jobs[job_id] = job
            self._calculate_next_run(job_id)
            logger.debug(f"Oneshot job '{job_id}' - run at: {self._next_runs.get(job_id)}")

    def remove_job(self, job_id: str) -> None:
        """Remove a scheduled job."""
        self._jobs.pop(job_id, None)
        self._next_runs.pop(job_id, None)
        self._queued.pop(job_id, None)

    def update_jobs(self, jobs: dict[str, JobConfig]) -> None:
        """Update all scheduled and oneshot jobs."""
        # Remove jobs that no longer exist
        current_ids = set(self._jobs.keys())
        new_ids = {
            jid for jid, j in jobs.items() 
            if (j.type == JobType.SCHEDULED and j.schedule) or (j.type == JobType.ONESHOT and j.run_at)
        }

        for job_id in current_ids - new_ids:
            self.remove_job(job_id)

        # Add/update jobs
        for job_id, job in jobs.items():
            if job.enabled:
                if (job.type == JobType.SCHEDULED and job.schedule) or (job.type == JobType.ONESHOT and job.run_at):
                    self.add_job(job_id, job)

    def _calculate_next_run(self, job_id: str) -> None:
        """Calculate the next run time for a job."""
        job = self._jobs.get(job_id)
        if not job:
            return

        tz = pytz.timezone(job.timezone or self._default_tz)
        now = datetime.now(tz)

        # Oneshot jobs use run_at directly
        if job.type == JobType.ONESHOT and job.run_at:
            run_at = job.run_at
            # Make timezone-aware if needed
            if run_at.tzinfo is None:
                run_at = tz.localize(run_at)
            self._next_runs[job_id] = run_at
            return

        # Scheduled jobs use cron expression
        if not job.schedule:
            return

        try:
            cron = croniter(job.schedule, now)
            next_run = cron.get_next(datetime)
            self._next_runs[job_id] = next_run
        except Exception as e:
            logger.error(f"Invalid cron expression for '{job_id}': {e}")

    def get_next_run(self, job_id: str) -> datetime | None:
        """Get the next scheduled run time for a job."""
        return self._next_runs.get(job_id)

    def get_all_next_runs(self) -> dict[str, datetime]:
        """Get all next run times."""
        return self._next_runs.copy()

    async def run(self) -> None:
        """Run the scheduler loop."""
        self._running = True
        logger.info(f"Scheduler started with {len(self._jobs)} jobs")

        while self._running:
            await self._check_schedules()
            await asyncio.sleep(1)  # Check every second

        logger.info("Scheduler stopped")

    async def _check_schedules(self) -> None:
        """Check if any jobs need to run."""
        now = datetime.now(pytz.timezone(self._default_tz))

        for job_id, next_run in list(self._next_runs.items()):
            if next_run is None:
                continue

            # Make next_run timezone-aware if it isn't
            if next_run.tzinfo is None:
                next_run = pytz.timezone(self._default_tz).localize(next_run)

            if now >= next_run:
                await self._trigger_job(job_id)

        # Process queued jobs
        for job_id in list(self._queued.keys()):
            if self._queued[job_id] > 0 and not self._is_job_running(job_id):
                logger.info(f"Running queued job '{job_id}'")
                if self._on_trigger(job_id, "scheduled"):
                    self._queued[job_id] -= 1
                    if self._queued[job_id] <= 0:
                        del self._queued[job_id]

    async def _trigger_job(self, job_id: str) -> None:
        """Trigger a scheduled job."""
        job = self._jobs.get(job_id)
        if not job:
            return

        # Check operating hours
        should_run, reason = self._operating_hours.should_run_job(job)
        if not should_run:
            logger.info(f"Skipping '{job_id}': {reason}")
            self._calculate_next_run(job_id)
            return

        is_running = self._is_job_running(job_id)

        if is_running:
            # Handle overlap
            overlap = job.on_overlap

            if overlap == OnOverlap.SKIP:
                logger.info(f"Skipping '{job_id}' - still running")
            elif overlap == OnOverlap.QUEUE:
                logger.info(f"Queuing '{job_id}' - still running")
                self._queued[job_id] = self._queued.get(job_id, 0) + 1
            elif overlap == OnOverlap.KILL_RESTART:
                logger.info(f"Kill+restart '{job_id}'")
                # The supervisor should handle the kill
                self._on_trigger(job_id, "scheduled")
        else:
            trigger_type = "oneshot" if job.type == JobType.ONESHOT else "scheduled"
            logger.info(f"Triggering {trigger_type} job '{job_id}'")
            self._on_trigger(job_id, trigger_type)

        # For oneshot jobs, remove from scheduler after triggering
        if job.type == JobType.ONESHOT:
            logger.info(f"Oneshot job '{job_id}' triggered - removing from scheduler")
            self.remove_job(job_id)
        else:
            # Calculate next run for recurring jobs
            self._calculate_next_run(job_id)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running
