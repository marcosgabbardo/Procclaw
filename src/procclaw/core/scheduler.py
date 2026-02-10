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
        get_last_run: Callable[[str], datetime | None] | None = None,
        timezone: str = "America/Sao_Paulo",
        missed_run_grace_minutes: int = 60,
    ):
        """Initialize the scheduler.

        Args:
            on_trigger: Callback when job should run. Returns True if started.
            is_job_running: Callback to check if job is running.
            get_last_run: Callback to get last run time for a job.
            timezone: Default timezone for schedules.
            missed_run_grace_minutes: Grace period to catch up missed runs.
        """
        self._on_trigger = on_trigger
        self._is_job_running = is_job_running
        self._get_last_run = get_last_run
        self._default_tz = timezone
        self._missed_run_grace_minutes = missed_run_grace_minutes
        self._jobs: dict[str, JobConfig] = {}
        self._next_runs: dict[str, datetime] = {}
        self._queued: dict[str, int] = {}  # job_id -> queue count
        self._pending_catchup: set[str] = set()  # jobs that need catchup run
        self._running = False
        self._operating_hours = OperatingHoursChecker(default_timezone=timezone)
        self._paused_jobs: set[str] = set()  # Jobs that are temporarily paused

    def add_job(self, job_id: str, job: JobConfig) -> None:
        """Add a scheduled or oneshot job."""
        # Handle scheduled jobs (cron)
        if job.type in (JobType.SCHEDULED, JobType.OPENCLAW) and job.schedule:
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
        self._paused_jobs.discard(job_id)

    def pause_job(self, job_id: str) -> None:
        """Pause a scheduled job (skip runs until resumed)."""
        self._paused_jobs.add(job_id)
        logger.info(f"Job '{job_id}' paused")

    def resume_job(self, job_id: str) -> None:
        """Resume a paused job."""
        self._paused_jobs.discard(job_id)
        # Recalculate next run from now
        if job_id in self._jobs:
            self._calculate_next_run(job_id, check_missed=False)
        logger.info(f"Job '{job_id}' resumed, next run: {self._next_runs.get(job_id)}")

    def is_paused(self, job_id: str) -> bool:
        """Check if a job is paused."""
        return job_id in self._paused_jobs

    def update_jobs(self, jobs: dict[str, JobConfig]) -> None:
        """Update all scheduled and oneshot jobs."""
        # Remove jobs that no longer exist
        current_ids = set(self._jobs.keys())
        new_ids = {
            jid for jid, j in jobs.items() 
            if (j.type in (JobType.SCHEDULED, JobType.OPENCLAW) and j.schedule) or (j.type == JobType.ONESHOT and j.run_at)
        }

        for job_id in current_ids - new_ids:
            self.remove_job(job_id)

        # Add/update jobs
        for job_id, job in jobs.items():
            if job.enabled:
                if (job.type in (JobType.SCHEDULED, JobType.OPENCLAW) and job.schedule) or (job.type == JobType.ONESHOT and job.run_at):
                    self.add_job(job_id, job)

    def _calculate_next_run(self, job_id: str, check_missed: bool = True) -> None:
        """Calculate the next run time for a job.
        
        Args:
            job_id: The job ID.
            check_missed: If True, check for missed runs within grace period.
        """
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
            
            # Check for missed runs within grace period
            if check_missed and self._get_last_run:
                prev_cron = croniter(job.schedule, now)
                prev_run = prev_cron.get_prev(datetime)
                
                # Get the job's last actual run
                last_run = self._get_last_run(job_id)
                
                grace_cutoff = now - timedelta(minutes=self._missed_run_grace_minutes)
                
                # If prev_run is within grace period AND job hasn't run since before prev_run
                if prev_run >= grace_cutoff:
                    should_catchup = False
                    if last_run is None:
                        # Never ran - skip catchup (no history to catch up on)
                        should_catchup = False
                        logger.debug(f"Job '{job_id}' has no history, skipping catchup")
                    elif last_run.tzinfo is None:
                        last_run = tz.localize(last_run)
                    
                    if last_run is not None and last_run < prev_run:
                        # Last run was before the scheduled time - catch up
                        should_catchup = True
                        logger.info(f"Job '{job_id}' last ran at {last_run}, missed run at {prev_run}")
                    
                    if should_catchup:
                        self._pending_catchup.add(job_id)
                        # Set next_run to now to trigger immediately
                        self._next_runs[job_id] = now
                        return
            
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

            # Skip paused jobs
            if job_id in self._paused_jobs:
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

        is_catchup = job_id in self._pending_catchup

        # Check operating hours (skip for catchup runs - they were already due)
        if not is_catchup:
            should_run, reason = self._operating_hours.should_run_job(job)
            if not should_run:
                logger.info(f"Skipping '{job_id}': {reason}")
                self._calculate_next_run(job_id, check_missed=False)
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
            trigger_type = "catchup" if is_catchup else ("oneshot" if job.type == JobType.ONESHOT else "scheduled")
            logger.info(f"Triggering {trigger_type} job '{job_id}'")
            self._on_trigger(job_id, trigger_type)

        # Clear catchup flag
        self._pending_catchup.discard(job_id)

        # For oneshot jobs, remove from scheduler after triggering
        if job.type == JobType.ONESHOT:
            logger.info(f"Oneshot job '{job_id}' triggered - removing from scheduler")
            self.remove_job(job_id)
        else:
            # Calculate next run for recurring jobs (don't check missed again)
            self._calculate_next_run(job_id, check_missed=False)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running
