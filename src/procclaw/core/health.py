"""Health check implementations for ProcClaw."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
import psutil
from loguru import logger

from procclaw.models import HealthCheckConfig, HealthCheckType, JobConfig


class HealthCheckResult:
    """Result of a health check."""

    def __init__(self, healthy: bool, message: str = "", details: dict | None = None):
        self.healthy = healthy
        self.message = message
        self.details = details or {}
        self.timestamp = datetime.now()

    def __bool__(self) -> bool:
        return self.healthy

    def __repr__(self) -> str:
        status = "healthy" if self.healthy else "unhealthy"
        return f"HealthCheckResult({status}, {self.message!r})"


class HealthChecker:
    """Health checker for jobs."""

    def __init__(
        self,
        on_health_fail: Callable[[str, HealthCheckResult], None] | None = None,
        on_health_recover: Callable[[str, HealthCheckResult], None] | None = None,
    ):
        """Initialize the health checker.

        Args:
            on_health_fail: Callback when a job fails health check.
            on_health_recover: Callback when a job recovers.
        """
        self._on_health_fail = on_health_fail
        self._on_health_recover = on_health_recover
        self._jobs: dict[str, tuple[JobConfig, int | None]] = {}  # job_id -> (config, pid)
        self._last_check: dict[str, datetime] = {}
        self._last_result: dict[str, HealthCheckResult] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._running = False

    def register_job(self, job_id: str, job: JobConfig, pid: int | None) -> None:
        """Register a job for health checking."""
        self._jobs[job_id] = (job, pid)
        self._consecutive_failures[job_id] = 0

    def unregister_job(self, job_id: str) -> None:
        """Unregister a job from health checking."""
        self._jobs.pop(job_id, None)
        self._last_check.pop(job_id, None)
        self._last_result.pop(job_id, None)
        self._consecutive_failures.pop(job_id, None)

    def update_pid(self, job_id: str, pid: int | None) -> None:
        """Update the PID for a job."""
        if job_id in self._jobs:
            job, _ = self._jobs[job_id]
            self._jobs[job_id] = (job, pid)

    def get_last_result(self, job_id: str) -> HealthCheckResult | None:
        """Get the last health check result for a job."""
        return self._last_result.get(job_id)

    async def check_job(self, job_id: str) -> HealthCheckResult:
        """Run health check for a specific job."""
        if job_id not in self._jobs:
            return HealthCheckResult(False, "Job not registered")

        job, pid = self._jobs[job_id]
        config = job.health_check

        # Run the appropriate check
        if config.type == HealthCheckType.NONE:
            # Health check disabled - always healthy
            result = HealthCheckResult(True, "Health check disabled")
        elif config.type == HealthCheckType.PROCESS:
            result = await self._check_process(job_id, pid)
        elif config.type == HealthCheckType.HTTP:
            result = await self._check_http(job_id, config)
        elif config.type == HealthCheckType.FILE:
            result = await self._check_file(job_id, config)
        else:
            result = HealthCheckResult(False, f"Unknown health check type: {config.type}")

        # Track result
        self._last_check[job_id] = datetime.now()
        previous = self._last_result.get(job_id)
        self._last_result[job_id] = result

        # Handle state changes
        if result.healthy:
            if self._consecutive_failures.get(job_id, 0) > 0:
                self._consecutive_failures[job_id] = 0
                if self._on_health_recover:
                    self._on_health_recover(job_id, result)
        else:
            self._consecutive_failures[job_id] = self._consecutive_failures.get(job_id, 0) + 1
            if self._on_health_fail and (not previous or previous.healthy):
                self._on_health_fail(job_id, result)

        return result

    async def _check_process(self, job_id: str, pid: int | None) -> HealthCheckResult:
        """Check if process is running."""
        if pid is None:
            return HealthCheckResult(False, "No PID")

        try:
            process = psutil.Process(pid)
            if process.is_running():
                # Get additional info
                cpu = process.cpu_percent()
                mem = process.memory_info().rss / (1024 * 1024)
                return HealthCheckResult(
                    True,
                    "Process running",
                    {"pid": pid, "cpu_percent": cpu, "memory_mb": mem},
                )
            else:
                return HealthCheckResult(False, "Process not running")
        except psutil.NoSuchProcess:
            return HealthCheckResult(False, f"Process {pid} not found")
        except Exception as e:
            return HealthCheckResult(False, f"Error checking process: {e}")

    async def _check_http(self, job_id: str, config: HealthCheckConfig) -> HealthCheckResult:
        """Check HTTP endpoint."""
        if not config.url:
            return HealthCheckResult(False, "No URL configured")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=config.method,
                    url=config.url,
                    timeout=config.timeout,
                )

                # Check status code
                if response.status_code != config.expected_status:
                    return HealthCheckResult(
                        False,
                        f"Unexpected status: {response.status_code}",
                        {"status_code": response.status_code},
                    )

                # Check body if expected
                if config.expected_body:
                    body = response.text
                    if config.expected_body not in body:
                        return HealthCheckResult(
                            False,
                            f"Body does not contain expected string",
                            {"expected": config.expected_body},
                        )

                return HealthCheckResult(
                    True,
                    "HTTP check passed",
                    {"status_code": response.status_code},
                )

        except httpx.TimeoutException:
            return HealthCheckResult(False, f"Timeout after {config.timeout}s")
        except httpx.RequestError as e:
            return HealthCheckResult(False, f"Request error: {e}")
        except Exception as e:
            return HealthCheckResult(False, f"Error: {e}")

    async def _check_file(self, job_id: str, config: HealthCheckConfig) -> HealthCheckResult:
        """Check if heartbeat file was updated recently."""
        if not config.path:
            return HealthCheckResult(False, "No file path configured")

        path = Path(config.path).expanduser()

        if not path.exists():
            return HealthCheckResult(False, f"File not found: {path}")

        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            age = (datetime.now() - mtime).total_seconds()

            if age > config.max_age:
                return HealthCheckResult(
                    False,
                    f"File too old: {age:.0f}s > {config.max_age}s",
                    {"age_seconds": age, "max_age": config.max_age},
                )

            return HealthCheckResult(
                True,
                f"File updated {age:.0f}s ago",
                {"age_seconds": age},
            )

        except Exception as e:
            return HealthCheckResult(False, f"Error checking file: {e}")

    async def run(self) -> None:
        """Run the health check loop."""
        self._running = True
        logger.info("Health checker started")

        while self._running:
            await self._check_all()
            await asyncio.sleep(1)  # Check timing every second

        logger.info("Health checker stopped")

    async def _check_all(self) -> None:
        """Check all jobs that are due for a health check."""
        now = datetime.now()

        for job_id, (job, pid) in list(self._jobs.items()):
            interval = job.health_check.interval
            last_check = self._last_check.get(job_id)

            if last_check is None or (now - last_check).total_seconds() >= interval:
                await self.check_job(job_id)

    def stop(self) -> None:
        """Stop the health checker."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if health checker is running."""
        return self._running
