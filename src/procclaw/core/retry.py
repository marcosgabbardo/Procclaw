"""Retry policy implementation for ProcClaw."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable

from loguru import logger

from procclaw.models import JobConfig, RETRY_PRESETS, RetryConfig


class RetryManager:
    """Manages retry logic for failed jobs."""

    def __init__(
        self,
        on_retry: Callable[[str], bool],
        on_max_retries: Callable[[str], None] | None = None,
    ):
        """Initialize the retry manager.

        Args:
            on_retry: Callback to retry a job. Returns True if started.
            on_max_retries: Callback when max retries reached.
        """
        self._on_retry = on_retry
        self._on_max_retries = on_max_retries
        self._pending_retries: dict[str, tuple[datetime, int, RetryConfig]] = {}
        self._running = False

    def schedule_retry(self, job_id: str, job: JobConfig, attempt: int) -> datetime | None:
        """Schedule a retry for a failed job.

        Args:
            job_id: The job ID
            job: The job configuration
            attempt: Current attempt number (1-indexed)

        Returns:
            The scheduled retry time, or None if max retries reached.
        """
        config = job.retry

        if not config.enabled:
            logger.debug(f"Retry disabled for '{job_id}'")
            return None

        if attempt >= config.max_attempts:
            logger.warning(f"Job '{job_id}' reached max retries ({config.max_attempts})")
            if self._on_max_retries:
                self._on_max_retries(job_id)
            return None

        # Get delay for this attempt
        delays = config.get_delays()
        delay_idx = min(attempt, len(delays) - 1)
        delay_seconds = delays[delay_idx]

        retry_time = datetime.now() + timedelta(seconds=delay_seconds)
        self._pending_retries[job_id] = (retry_time, attempt + 1, config)

        logger.info(
            f"Scheduled retry for '{job_id}' at {retry_time.strftime('%H:%M:%S')} "
            f"(attempt {attempt + 1}/{config.max_attempts}, delay {delay_seconds}s)"
        )

        return retry_time

    def cancel_retry(self, job_id: str) -> None:
        """Cancel a pending retry."""
        if job_id in self._pending_retries:
            del self._pending_retries[job_id]
            logger.debug(f"Cancelled retry for '{job_id}'")

    def get_next_retry(self, job_id: str) -> tuple[datetime, int] | None:
        """Get the next retry time and attempt number for a job."""
        if job_id in self._pending_retries:
            retry_time, attempt, _ = self._pending_retries[job_id]
            return retry_time, attempt
        return None

    async def run(self) -> None:
        """Run the retry manager loop."""
        self._running = True
        logger.info("Retry manager started")

        while self._running:
            await self._process_retries()
            await asyncio.sleep(1)

        logger.info("Retry manager stopped")

    async def _process_retries(self) -> None:
        """Process pending retries."""
        now = datetime.now()

        for job_id in list(self._pending_retries.keys()):
            retry_time, attempt, config = self._pending_retries[job_id]

            if now >= retry_time:
                logger.info(f"Executing retry for '{job_id}' (attempt {attempt})")
                del self._pending_retries[job_id]

                if not self._on_retry(job_id):
                    logger.warning(f"Retry failed for '{job_id}'")

    def stop(self) -> None:
        """Stop the retry manager."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if retry manager is running."""
        return self._running

    @property
    def pending_count(self) -> int:
        """Get the number of pending retries."""
        return len(self._pending_retries)
