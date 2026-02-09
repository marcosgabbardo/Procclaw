"""Job dependency management for ProcClaw."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from loguru import logger

from procclaw.models import DependencyCondition, JobConfig, JobDependency


class DependencyStatus(Enum):
    """Status of a dependency check."""

    SATISFIED = "satisfied"
    WAITING = "waiting"
    FAILED = "failed"
    TIMEOUT = "timeout"


class DependencyResult:
    """Result of a dependency check."""

    def __init__(self, status: DependencyStatus, message: str = ""):
        self.status = status
        self.message = message

    @property
    def is_satisfied(self) -> bool:
        return self.status == DependencyStatus.SATISFIED

    def __bool__(self) -> bool:
        return self.is_satisfied


class DependencyManager:
    """Manages job dependencies."""

    def __init__(
        self,
        is_job_running: Callable[[str], bool],
        get_job_status: Callable[[str], str | None],
        get_last_run: Callable[[str], tuple[datetime | None, int | None] | None] | None = None,
    ):
        """Initialize the dependency manager.

        Args:
            is_job_running: Callback to check if a job is running.
            get_job_status: Callback to get job status (running, stopped, failed, etc.).
            get_last_run: Optional callback to get last run info (finished_at, exit_code).
        """
        self._is_job_running = is_job_running
        self._get_job_status = get_job_status
        self._get_last_run = get_last_run
        self._job_start_times: dict[str, datetime] = {}
        self._job_complete_times: dict[str, datetime] = {}

    def record_job_start(self, job_id: str) -> None:
        """Record when a job started."""
        self._job_start_times[job_id] = datetime.now()
        # Clear complete time when job starts
        self._job_complete_times.pop(job_id, None)

    def record_job_complete(self, job_id: str) -> None:
        """Record when a job completed."""
        self._job_complete_times[job_id] = datetime.now()

    def check_dependency(self, dep: JobDependency) -> DependencyResult:
        """Check a single dependency.

        Args:
            dep: The dependency to check.

        Returns:
            DependencyResult indicating if the dependency is satisfied.
        """
        job_id = dep.job
        condition = dep.condition

        if condition == DependencyCondition.AFTER_START:
            # Job must have started
            if job_id in self._job_start_times or self._is_job_running(job_id):
                return DependencyResult(DependencyStatus.SATISFIED)
            return DependencyResult(
                DependencyStatus.WAITING,
                f"Waiting for '{job_id}' to start",
            )

        elif condition == DependencyCondition.AFTER_COMPLETE:
            # Job must have completed
            if job_id in self._job_complete_times:
                return DependencyResult(DependencyStatus.SATISFIED)

            # Check from DB if callback is available
            if self._get_last_run:
                last_run_info = self._get_last_run(job_id)
                if last_run_info:
                    finished_at, exit_code = last_run_info
                    if finished_at is not None:
                        # Job has completed (check exit code)
                        if exit_code == 0:
                            return DependencyResult(DependencyStatus.SATISFIED)
                        else:
                            return DependencyResult(
                                DependencyStatus.FAILED,
                                f"Dependency '{job_id}' failed (exit code: {exit_code})",
                            )

            # Check if job is running (waiting) or never ran (failed)
            if self._is_job_running(job_id):
                return DependencyResult(
                    DependencyStatus.WAITING,
                    f"Waiting for '{job_id}' to complete",
                )

            status = self._get_job_status(job_id)
            if status == "failed":
                return DependencyResult(
                    DependencyStatus.FAILED,
                    f"Dependency '{job_id}' failed",
                )

            # Check if job has successfully run before (from status)
            if status == "stopped":
                # Stopped usually means completed successfully
                return DependencyResult(DependencyStatus.SATISFIED)

            return DependencyResult(
                DependencyStatus.WAITING,
                f"Waiting for '{job_id}' to run and complete",
            )

        elif condition == DependencyCondition.BEFORE_COMPLETE:
            # This job must complete before the dependent job completes
            # This is checked when the dependent job tries to complete
            # For now, we just check if the job is not waiting to complete
            return DependencyResult(DependencyStatus.SATISFIED)

        return DependencyResult(
            DependencyStatus.FAILED,
            f"Unknown dependency condition: {condition}",
        )

    def check_all_dependencies(
        self,
        job_id: str,
        job: JobConfig,
    ) -> tuple[bool, list[DependencyResult]]:
        """Check all dependencies for a job.

        Args:
            job_id: The job to check dependencies for.
            job: The job configuration.

        Returns:
            Tuple of (all_satisfied, list of results).
        """
        if not job.depends_on:
            return True, []

        results = []
        all_satisfied = True

        for dep in job.depends_on:
            result = self.check_dependency(dep)
            results.append(result)

            if not result.is_satisfied:
                all_satisfied = False

                # Check for hard failure
                if result.status == DependencyStatus.FAILED:
                    if dep.fail_on_dependency_failure:
                        logger.error(f"Dependency failed for '{job_id}': {result.message}")
                        return False, results

        return all_satisfied, results

    async def wait_for_dependencies(
        self,
        job_id: str,
        job: JobConfig,
        check_interval: float = 1.0,
    ) -> DependencyResult:
        """Wait for all dependencies to be satisfied.

        Args:
            job_id: The job to wait for.
            job: The job configuration.
            check_interval: How often to check dependencies (seconds).

        Returns:
            DependencyResult indicating final status.
        """
        if not job.depends_on:
            return DependencyResult(DependencyStatus.SATISFIED)

        # Calculate timeout from dependencies
        max_timeout = max(dep.timeout for dep in job.depends_on)
        deadline = datetime.now() + timedelta(seconds=max_timeout)

        logger.info(f"Waiting for dependencies of '{job_id}' (timeout: {max_timeout}s)")

        while datetime.now() < deadline:
            all_satisfied, results = self.check_all_dependencies(job_id, job)

            if all_satisfied:
                logger.info(f"All dependencies satisfied for '{job_id}'")
                return DependencyResult(DependencyStatus.SATISFIED)

            # Check for hard failures
            for result in results:
                if result.status == DependencyStatus.FAILED:
                    return result

            # Log waiting status
            waiting = [r for r in results if r.status == DependencyStatus.WAITING]
            if waiting:
                logger.debug(f"'{job_id}' waiting: {waiting[0].message}")

            await asyncio.sleep(check_interval)

        # Timeout
        return DependencyResult(
            DependencyStatus.TIMEOUT,
            f"Dependency timeout after {max_timeout}s",
        )

    def validate_no_cycles(
        self,
        jobs: dict[str, JobConfig],
    ) -> tuple[bool, str | None]:
        """Validate that there are no circular dependencies.

        Args:
            jobs: All job configurations.

        Returns:
            Tuple of (valid, error_message).
        """
        # Build dependency graph
        graph: dict[str, set[str]] = {job_id: set() for job_id in jobs}

        for job_id, job in jobs.items():
            for dep in job.depends_on:
                if dep.job in jobs:
                    graph[job_id].add(dep.job)

        # DFS to detect cycles
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {job_id: WHITE for job_id in jobs}
        path: list[str] = []

        def dfs(node: str) -> str | None:
            color[node] = GRAY
            path.append(node)

            for neighbor in graph[node]:
                if color[neighbor] == GRAY:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    return " -> ".join(cycle)
                elif color[neighbor] == WHITE:
                    result = dfs(neighbor)
                    if result:
                        return result

            color[node] = BLACK
            path.pop()
            return None

        for job_id in jobs:
            if color[job_id] == WHITE:
                cycle = dfs(job_id)
                if cycle:
                    return False, f"Circular dependency detected: {cycle}"

        return True, None

    def get_start_order(self, jobs: dict[str, JobConfig]) -> list[str]:
        """Get the order in which jobs should be started based on dependencies.

        Args:
            jobs: All job configurations.

        Returns:
            List of job IDs in start order.
        """
        # Topological sort
        in_degree = {job_id: 0 for job_id in jobs}

        for job_id, job in jobs.items():
            for dep in job.depends_on:
                if dep.job in jobs:
                    in_degree[job_id] += 1

        # Start with jobs that have no dependencies
        queue = [job_id for job_id, degree in in_degree.items() if degree == 0]
        result = []

        while queue:
            job_id = queue.pop(0)
            result.append(job_id)

            # Reduce in-degree of dependent jobs
            for other_id, other_job in jobs.items():
                for dep in other_job.depends_on:
                    if dep.job == job_id:
                        in_degree[other_id] -= 1
                        if in_degree[other_id] == 0:
                            queue.append(other_id)

        return result
