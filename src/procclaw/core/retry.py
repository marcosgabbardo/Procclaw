"""Retry policy implementation for ProcClaw."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.models import JobConfig, RetryConfig


# Patterns that indicate rate limiting in output
RATE_LIMIT_PATTERNS = [
    r"429",
    r"rate.?limit",
    r"too.?many.?requests",
    r"quota.?exceeded",
    r"throttl",
    r"slow.?down",
]

# Compiled regex for rate limit detection
RATE_LIMIT_REGEX = re.compile(
    "|".join(RATE_LIMIT_PATTERNS),
    re.IGNORECASE,
)


class RetryInfo:
    """Information about a pending retry."""
    
    def __init__(
        self,
        retry_time: datetime,
        attempt: int,
        config: "RetryConfig",
        is_rate_limit: bool = False,
    ):
        self.retry_time = retry_time
        self.attempt = attempt
        self.config = config
        self.is_rate_limit = is_rate_limit


class RetryManager:
    """Manages retry logic for failed jobs.
    
    Features:
    - Standard exponential/webhook backoff
    - Special handling for rate-limited jobs (longer delays)
    - Detection of rate limit errors from output
    """

    def __init__(
        self,
        on_retry: Callable[[str], bool],
        on_max_retries: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[str, str], None] | None = None,
        logs_dir: Path | None = None,
    ):
        """Initialize the retry manager.

        Args:
            on_retry: Callback to retry a job. Returns True if started.
            on_max_retries: Callback when max retries reached.
            on_rate_limit: Callback when rate limit is detected.
            logs_dir: Directory where job logs are stored.
        """
        self._on_retry = on_retry
        self._on_max_retries = on_max_retries
        self._on_rate_limit = on_rate_limit
        self._logs_dir = logs_dir
        self._pending_retries: dict[str, RetryInfo] = {}
        self._rate_limited_jobs: set[str] = set()  # Jobs currently rate-limited
        self._running = False

    def is_rate_limit_error(self, job_id: str, error_log: str | None = None) -> bool:
        """Check if a job failure was due to rate limiting.
        
        Args:
            job_id: The job ID
            error_log: Optional error output to check
            
        Returns:
            True if rate limit was detected
        """
        # Check the error log content
        if error_log and RATE_LIMIT_REGEX.search(error_log):
            return True
        
        # Check the job's error log file if logs_dir is set
        if self._logs_dir:
            error_file = self._logs_dir / f"{job_id}.error.log"
            if error_file.exists():
                try:
                    # Read last 5KB of error log
                    with open(error_file, "rb") as f:
                        f.seek(0, 2)  # End
                        size = f.tell()
                        f.seek(max(0, size - 5120))
                        content = f.read().decode("utf-8", errors="ignore")
                    
                    if RATE_LIMIT_REGEX.search(content):
                        return True
                except Exception as e:
                    logger.warning(f"Failed to check error log for rate limit: {e}")
        
        return False

    def schedule_retry(
        self,
        job_id: str,
        job: "JobConfig",
        attempt: int,
        error_output: str | None = None,
    ) -> datetime | None:
        """Schedule a retry for a failed job.

        Args:
            job_id: The job ID
            job: The job configuration
            attempt: Current attempt number (0-indexed)
            error_output: Optional error output to check for rate limit

        Returns:
            The scheduled retry time, or None if max retries reached.
        """
        config = job.retry

        if not config.enabled:
            logger.debug(f"Retry disabled for '{job_id}'")
            return None

        # Check if this is a rate limit error
        is_rate_limit = self.is_rate_limit_error(job_id, error_output)
        
        if is_rate_limit:
            # Use rate limit specific settings
            max_attempts = config.rate_limit_max_attempts
            delays = config.get_rate_limit_delays()
            self._rate_limited_jobs.add(job_id)
            
            # Notify about rate limit
            if self._on_rate_limit:
                self._on_rate_limit(job_id, "Rate limit detected in output")
            
            logger.warning(f"Rate limit detected for '{job_id}', using extended delays")
        else:
            # Normal retry
            max_attempts = config.max_attempts
            delays = config.get_delays()

        if attempt >= max_attempts:
            logger.warning(f"Job '{job_id}' reached max retries ({max_attempts})")
            if self._on_max_retries:
                self._on_max_retries(job_id)
            # Clear rate limit flag
            self._rate_limited_jobs.discard(job_id)
            return None

        # Get delay for this attempt
        delay_idx = min(attempt, len(delays) - 1)
        delay_seconds = delays[delay_idx]

        retry_time = datetime.now() + timedelta(seconds=delay_seconds)
        
        self._pending_retries[job_id] = RetryInfo(
            retry_time=retry_time,
            attempt=attempt + 1,
            config=config,
            is_rate_limit=is_rate_limit,
        )

        retry_type = "rate-limit" if is_rate_limit else "standard"
        logger.info(
            f"Scheduled {retry_type} retry for '{job_id}' at {retry_time.strftime('%H:%M:%S')} "
            f"(attempt {attempt + 1}/{max_attempts}, delay {delay_seconds}s)"
        )

        return retry_time

    def cancel_retry(self, job_id: str) -> None:
        """Cancel a pending retry."""
        if job_id in self._pending_retries:
            del self._pending_retries[job_id]
            logger.debug(f"Cancelled retry for '{job_id}'")
        self._rate_limited_jobs.discard(job_id)

    def get_next_retry(self, job_id: str) -> tuple[datetime, int] | None:
        """Get the next retry time and attempt number for a job."""
        if job_id in self._pending_retries:
            info = self._pending_retries[job_id]
            return info.retry_time, info.attempt
        return None

    def is_job_rate_limited(self, job_id: str) -> bool:
        """Check if a job is currently being rate limited."""
        return job_id in self._rate_limited_jobs

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
            info = self._pending_retries[job_id]

            if now >= info.retry_time:
                retry_type = "rate-limit" if info.is_rate_limit else "standard"
                logger.info(f"Executing {retry_type} retry for '{job_id}' (attempt {info.attempt})")
                del self._pending_retries[job_id]

                try:
                    if not self._on_retry(job_id):
                        logger.warning(f"Retry failed for '{job_id}'")
                except Exception as e:
                    logger.error(f"Retry callback error for '{job_id}': {e}")

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
    
    @property
    def rate_limited_count(self) -> int:
        """Get the number of rate-limited jobs."""
        return len(self._rate_limited_jobs)
