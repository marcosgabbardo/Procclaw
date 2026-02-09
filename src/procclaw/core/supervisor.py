"""Process supervisor for ProcClaw."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from loguru import logger

from procclaw.config import DEFAULT_LOGS_DIR, ensure_config_dir, load_config, load_jobs
from procclaw.db import Database
from procclaw.models import (
    JobConfig,
    JobRun,
    JobsConfig,
    JobState,
    JobStatus,
    JobType,
    Priority,
    ProcClawConfig,
)
from procclaw.core.concurrency import ConcurrencyLimiter
from procclaw.core.dedup import DeduplicationManager
from procclaw.core.dependencies import DependencyManager, DependencyStatus
from procclaw.core.dlq import DeadLetterQueue, DLQEntry
from procclaw.core.eta import ETAScheduler
from procclaw.core.health import HealthChecker, HealthCheckResult
from procclaw.core.locks import LockManager
from procclaw.core.log_utils import LogRotator
from procclaw.core.output_parser import JobOutputChecker, OutputMatch
from procclaw.core.priority import PriorityQueue, PrioritizedJob
from procclaw.core.resources import ResourceMonitor, ResourceViolation
from procclaw.core.results import ResultCollector
from procclaw.core.retry import RetryManager
from procclaw.core.revocation import RevocationManager
from procclaw.core.scheduler import Scheduler
from procclaw.core.triggers import TriggerManager, TriggerEvent
from procclaw.core.watchdog import Watchdog, MissedRunInfo
from procclaw.core.workflow import WorkflowManager, WorkflowConfig
from procclaw.openclaw import OpenClawIntegration, init_integration, AlertType


class ProcessHandle:
    """Handle to a running process."""

    def __init__(
        self,
        job_id: str,
        process: subprocess.Popen[bytes],
        stdout_file: Any,
        stderr_file: Any,
    ):
        self.job_id = job_id
        self.process = process
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file
        self.started_at = datetime.now()

    @property
    def pid(self) -> int:
        """Get the process ID."""
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """Get the return code if process has exited."""
        return self.process.returncode

    def is_running(self) -> bool:
        """Check if the process is still running."""
        return self.process.poll() is None

    def terminate(self) -> None:
        """Send SIGTERM to the process."""
        if self.is_running():
            self.process.terminate()

    def kill(self) -> None:
        """Send SIGKILL to the process."""
        if self.is_running():
            self.process.kill()

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process to finish."""
        return self.process.wait(timeout=timeout)

    def close_files(self) -> None:
        """Close log file handles."""
        if self.stdout_file:
            self.stdout_file.close()
        if self.stderr_file:
            self.stderr_file.close()


class Supervisor:
    """Process supervisor managing job execution."""

    def __init__(
        self,
        config: ProcClawConfig | None = None,
        jobs: JobsConfig | None = None,
        db: Database | None = None,
    ):
        """Initialize the supervisor."""
        self.config = config or load_config()
        self.jobs = jobs or load_jobs()
        self.db = db or Database()

        self._processes: dict[str, ProcessHandle] = {}
        self._shutdown_event = asyncio.Event()
        self._running = False

        # Initialize scheduler
        self._scheduler = Scheduler(
            on_trigger=self.start_job,
            is_job_running=self.is_job_running,
        )

        # Initialize health checker
        self._health_checker = HealthChecker(
            on_health_fail=self._on_health_fail,
            on_health_recover=self._on_health_recover,
        )

        # Initialize retry manager
        self._retry_manager = RetryManager(
            on_retry=lambda job_id: self.start_job(job_id, trigger="retry"),
            on_max_retries=self._on_max_retries,
            on_rate_limit=self._on_rate_limit,
            logs_dir=DEFAULT_LOGS_DIR,
        )

        # Initialize dependency manager
        self._dependency_manager = DependencyManager(
            is_job_running=self.is_job_running,
            get_job_status=lambda job_id: self.db.get_state(job_id).status.value if self.db.get_state(job_id) else None,
            get_last_run=self._get_last_run_info,
        )

        # Initialize OpenClaw integration
        self._openclaw = init_integration(
            enabled=self.config.openclaw.enabled,
            channels=self.config.openclaw.alerts_channels,
        )

        # Initialize watchdog (dead man's switch)
        self._watchdog = Watchdog(
            db=self.db,
            get_jobs=lambda: self.jobs.get_enabled_jobs(),
            on_missed_run=self._on_missed_run,
            check_interval=300,  # Check every 5 minutes
            default_timezone="America/Sao_Paulo",
        )

        # Initialize log rotator
        self._log_rotator = LogRotator(
            logs_dir=DEFAULT_LOGS_DIR,
            check_interval=300,  # Check every 5 minutes
            max_age_days=7,
        )
        # Register all jobs for log rotation
        for job_id, job in self.jobs.jobs.items():
            self._log_rotator.register_job(job_id, job.log)

        # Initialize output checker
        self._output_checker = JobOutputChecker(
            logs_dir=DEFAULT_LOGS_DIR,
            on_error=self._on_output_error,
            on_warning=self._on_output_warning,
            on_rate_limit=lambda job_id, match: self._on_rate_limit(job_id, match.line),
        )

        # Initialize resource monitor
        self._resource_monitor = ResourceMonitor(
            on_violation=self._on_resource_violation,
            on_kill=self._on_resource_kill,
            default_check_interval=30,
        )

        # Initialize deduplication manager
        self._dedup_manager = DeduplicationManager(
            db=self.db,
            default_window=60,
        )

        # Initialize concurrency limiter
        self._concurrency_limiter = ConcurrencyLimiter(
            db=self.db,
            on_job_queued=self._on_job_queued,
            on_queue_timeout=self._on_queue_timeout,
            global_max=self.config.daemon.max_concurrent_jobs if hasattr(self.config.daemon, 'max_concurrent_jobs') else 50,
        )

        # Initialize priority queue
        self._priority_queue = PriorityQueue(max_size=10000)

        # Initialize dead letter queue
        self._dlq = DeadLetterQueue(
            db=self.db,
            on_dlq_entry=self._on_dlq_entry,
        )

        # Initialize lock manager
        self._lock_manager = LockManager(
            db=self.db,
            holder_id=f"procclaw-{os.getpid()}",
        )

        # Initialize trigger manager
        self._trigger_manager = TriggerManager(
            on_trigger=self._on_trigger_event,
        )

        # Initialize ETA scheduler
        self._eta_scheduler = ETAScheduler(
            db=self.db,
            on_trigger=self._on_eta_trigger,
            default_timezone="America/Sao_Paulo",
        )

        # Initialize revocation manager
        self._revocation_manager = RevocationManager(
            db=self.db,
            default_expiry_seconds=3600,
        )

        # Initialize result collector
        self._result_collector = ResultCollector(
            db=self.db,
            logs_dir=DEFAULT_LOGS_DIR,
        )

        # Initialize workflow manager
        self._workflow_manager = WorkflowManager(
            db=self.db,
            result_collector=self._result_collector,
            start_job=self._start_job_for_workflow,
            is_job_running=self.is_job_running,
            stop_job=self.stop_job,
            logs_dir=DEFAULT_LOGS_DIR,
        )

        # Ensure directories exist
        ensure_config_dir()

    def reload_jobs(self) -> None:
        """Reload jobs configuration."""
        self.jobs = load_jobs()
        self._scheduler.update_jobs(self.jobs.get_enabled_jobs())
        logger.info(f"Reloaded {len(self.jobs.jobs)} jobs")

    def _get_last_run_info(self, job_id: str) -> tuple[datetime | None, int | None] | None:
        """Get last run info for dependency checking."""
        last_run = self.db.get_last_run(job_id)
        if last_run:
            return (last_run.finished_at, last_run.exit_code)
        return None

    # Process Management

    def start_job(
        self,
        job_id: str,
        trigger: str = "manual",
        params: dict | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Start a job.
        
        Args:
            job_id: The job ID to start
            trigger: What triggered this start (manual, schedule, retry, api, webhook)
            params: Optional parameters for the job
            idempotency_key: Optional idempotency key for deduplication
            
        Returns:
            True if job was started, False otherwise
        """
        job = self.jobs.get_job(job_id)
        if not job:
            logger.error(f"Job '{job_id}' not found")
            return False

        if not job.enabled:
            logger.warning(f"Job '{job_id}' is disabled")
            return False

        if job_id in self._processes and self._processes[job_id].is_running():
            logger.warning(f"Job '{job_id}' is already running")
            return False

        # Check revocation
        if self._revocation_manager.is_revoked(job_id):
            logger.info(f"Job '{job_id}' skipped - revoked")
            return False

        # Check deduplication
        if job.dedup.enabled:
            if self._dedup_manager.is_duplicate(
                job_id=job_id,
                params=params if job.dedup.use_params else None,
                idempotency_key=idempotency_key,
                window_seconds=job.dedup.window_seconds,
            ):
                logger.info(f"Job '{job_id}' skipped - duplicate within {job.dedup.window_seconds}s window")
                return False

        # Check concurrency limits
        if job.concurrency.max_instances > 0:
            running_count = self._concurrency_limiter.get_running_count(job_id)
            if running_count >= job.concurrency.max_instances:
                if job.concurrency.queue_excess:
                    # Queue the job for later execution
                    self._concurrency_limiter.queue_job(
                        job_id=job_id,
                        trigger=trigger,
                        params=params,
                        idempotency_key=idempotency_key,
                        timeout=job.concurrency.queue_timeout,
                    )
                    logger.info(f"Job '{job_id}' queued - max instances ({job.concurrency.max_instances}) reached")
                    return False
                else:
                    logger.warning(f"Job '{job_id}' rejected - max instances ({job.concurrency.max_instances}) reached")
                    return False

        # Check dependencies (sync check, not async wait)
        if job.depends_on:
            satisfied, results = self._dependency_manager.check_all_dependencies(job_id, job)
            if not satisfied:
                for result in results:
                    if result.status == DependencyStatus.FAILED:
                        logger.error(f"Cannot start '{job_id}': {result.message}")
                        return False
                    elif result.status == DependencyStatus.WAITING:
                        logger.warning(f"Cannot start '{job_id}': {result.message}")
                        return False

        # Acquire lock if enabled
        lock_acquired = False
        if job.lock.enabled:
            lock_acquired = self._lock_manager.try_acquire(
                job_id=job_id,
                timeout_seconds=job.lock.timeout_seconds,
            )
            if not lock_acquired:
                logger.warning(f"Job '{job_id}' could not acquire lock")
                return False

        try:
            handle = self._spawn_process(job_id, job)
            self._processes[job_id] = handle

            # Update state (preserve retry_attempt and restart_count from existing state)
            existing_state = self.db.get_state(job_id)
            state = JobState(
                job_id=job_id,
                status=JobStatus.RUNNING,
                pid=handle.pid,
                started_at=handle.started_at,
                retry_attempt=existing_state.retry_attempt if existing_state else 0,
                restart_count=existing_state.restart_count if existing_state else 0,
            )
            
            # Increment restart count if this is a restart/retry
            if trigger in ("restart", "retry"):
                state.restart_count += 1
            
            self.db.save_state(state)

            # Record run
            run = JobRun(
                job_id=job_id,
                started_at=handle.started_at,
                trigger=trigger,
            )
            run.id = self.db.add_run(run)

            # Record execution for deduplication
            if job.dedup.enabled:
                self._dedup_manager.record_execution(
                    job_id=job_id,
                    params=params if job.dedup.use_params else None,
                    idempotency_key=idempotency_key,
                )

            # Increment concurrency counter
            self._concurrency_limiter.increment_running(job_id)

            logger.info(f"Started job '{job_id}' (PID: {handle.pid})")
            self._audit_log(job_id, "started", f"PID: {handle.pid}, trigger: {trigger}")

            # Notify OpenClaw
            self._openclaw.on_job_started(job_id, handle.pid, trigger)

            # Register for health checking
            self._health_checker.register_job(job_id, job, handle.pid)

            # Register for resource monitoring
            if job.resources.enabled:
                self._resource_monitor.register_job(
                    job_id=job_id,
                    pid=handle.pid,
                    config=job.resources,
                    started_at=handle.started_at,
                )

            # Cancel any pending retry
            self._retry_manager.cancel_retry(job_id)

            # Record start for dependency tracking
            self._dependency_manager.record_job_start(job_id)

            return True

        except Exception as e:
            # Release lock on failure
            if lock_acquired:
                self._lock_manager.release(job_id)
            
            logger.error(f"Failed to start job '{job_id}': {e}")
            state = JobState(
                job_id=job_id,
                status=JobStatus.FAILED,
                last_error=str(e),
            )
            self.db.save_state(state)
            return False

    def stop_job(self, job_id: str, force: bool = False) -> bool:
        """Stop a running job."""
        handle = self._processes.get(job_id)
        if not handle or not handle.is_running():
            logger.warning(f"Job '{job_id}' is not running")
            return False

        job = self.jobs.get_job(job_id)
        grace_period = job.shutdown.grace_period if job else 60

        try:
            if force:
                logger.info(f"Force killing job '{job_id}'")
                handle.kill()
            else:
                logger.info(f"Stopping job '{job_id}' (grace period: {grace_period}s)")
                handle.terminate()

                try:
                    handle.wait(timeout=grace_period)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Job '{job_id}' did not stop gracefully, killing")
                    handle.kill()
                    handle.wait(timeout=5)

            self._finalize_job(job_id, handle)
            return True

        except Exception as e:
            logger.error(f"Failed to stop job '{job_id}': {e}")
            return False

    def restart_job(self, job_id: str) -> bool:
        """Restart a job."""
        self.stop_job(job_id)
        result = self.start_job(job_id, trigger="restart")
        
        if result:
            # Get restart count
            state = self.db.get_state(job_id)
            restart_count = state.restart_count if state else 0
            
            # Notify OpenClaw
            job = self.jobs.get_job(job_id)
            if job and job.alerts.on_restart:
                self._openclaw.on_job_restart(job_id, restart_count)
        
        return result

    def get_job_status(self, job_id: str) -> dict | None:
        """Get detailed status of a job."""
        job = self.jobs.get_job(job_id)
        if not job:
            return None

        state = self.db.get_state(job_id) or JobState(job_id=job_id)
        handle = self._processes.get(job_id)

        # Check if process is still running
        if handle and not handle.is_running() and state.status == JobStatus.RUNNING:
            self._finalize_job(job_id, handle)
            state = self.db.get_state(job_id) or state

        # Calculate uptime
        uptime_seconds = None
        if state.status == JobStatus.RUNNING and state.started_at:
            uptime_seconds = (datetime.now() - state.started_at).total_seconds()

        # Get resource usage
        cpu_percent = None
        memory_mb = None
        if handle and handle.is_running():
            try:
                proc = psutil.Process(handle.pid)
                cpu_percent = proc.cpu_percent()
                memory_mb = proc.memory_info().rss / (1024 * 1024)
            except psutil.NoSuchProcess:
                pass

        # Get last run
        last_run = self.db.get_last_run(job_id)

        # Get next scheduled run
        next_run = self._scheduler.get_next_run(job_id)

        return {
            "id": job_id,
            "name": job.name,
            "description": job.description,
            "type": job.type.value,
            "status": state.status.value,
            "enabled": job.enabled,
            "pid": state.pid,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "uptime_seconds": uptime_seconds,
            "restart_count": state.restart_count,
            "retry_attempt": state.retry_attempt,
            "last_exit_code": state.last_exit_code,
            "last_error": state.last_error,
            "next_run": next_run.isoformat() if next_run else (state.next_run.isoformat() if state.next_run else None),
            "cpu_percent": cpu_percent,
            "memory_mb": memory_mb,
            "last_run": {
                "started_at": last_run.started_at.isoformat() if last_run else None,
                "finished_at": last_run.finished_at.isoformat() if last_run and last_run.finished_at else None,
                "exit_code": last_run.exit_code if last_run else None,
                "duration_seconds": last_run.duration_seconds if last_run else None,
                "trigger": last_run.trigger if last_run else None,
            } if last_run else None,
            "tags": job.tags,
        }

    def list_jobs(self) -> list[dict]:
        """List all jobs with their current status."""
        result = []
        for job_id in self.jobs.jobs:
            status = self.get_job_status(job_id)
            if status:
                result.append(status)
        return result

    def is_job_running(self, job_id: str) -> bool:
        """Check if a job is currently running."""
        handle = self._processes.get(job_id)
        return handle is not None and handle.is_running()

    def wait_for_job(self, job_id: str, timeout: float | None = None) -> int | None:
        """Wait for a job to complete and return its exit code.

        Args:
            job_id: The job ID to wait for
            timeout: Optional timeout in seconds

        Returns:
            Exit code of the process, or None if not found/timeout
        """
        handle = self._processes.get(job_id)
        if not handle:
            return None

        try:
            exit_code = handle.wait(timeout=timeout)
            self._finalize_job(job_id, handle)
            return exit_code
        except subprocess.TimeoutExpired:
            return None

    # Process Spawning

    def _spawn_process(self, job_id: str, job: JobConfig) -> ProcessHandle:
        """Spawn a new process for a job."""
        # Prepare working directory
        cwd = Path(job.cwd).expanduser() if job.cwd else Path.cwd()
        if not cwd.exists():
            raise RuntimeError(f"Working directory does not exist: {cwd}")

        # Prepare environment
        env = os.environ.copy()
        env.update(job.env)
        env["PROCCLAW_JOB_ID"] = job_id

        # Prepare log files
        logs_dir = DEFAULT_LOGS_DIR
        logs_dir.mkdir(parents=True, exist_ok=True)

        stdout_path = job.get_log_stdout_path(logs_dir.parent, job_id)
        stderr_path = job.get_log_stderr_path(logs_dir.parent, job_id)

        stdout_file = open(stdout_path, "a")
        stderr_file = open(stderr_path, "a")

        # Log header
        timestamp = datetime.now().isoformat()
        stdout_file.write(f"\n{'='*60}\n")
        stdout_file.write(f"[{timestamp}] Starting job: {job_id}\n")
        stdout_file.write(f"Command: {job.cmd}\n")
        stdout_file.write(f"{'='*60}\n\n")
        stdout_file.flush()

        # Parse command
        if sys.platform == "win32":
            cmd = job.cmd
            shell = True
        else:
            cmd = job.cmd
            shell = True

        # Spawn process
        process = subprocess.Popen(
            cmd,
            shell=shell,
            cwd=cwd,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,  # Create new process group
        )

        return ProcessHandle(job_id, process, stdout_file, stderr_file)

    def _finalize_job(self, job_id: str, handle: ProcessHandle) -> None:
        """Finalize a completed job."""
        now = datetime.now()
        duration = (now - handle.started_at).total_seconds()
        exit_code = handle.returncode

        # Close file handles
        handle.close_files()

        # Unregister from health checker
        self._health_checker.unregister_job(job_id)

        # Unregister from resource monitor
        self._resource_monitor.unregister_job(job_id)

        # Record completion for dependency tracking
        self._dependency_manager.record_job_complete(job_id)

        # Release lock if held
        job = self.jobs.get_job(job_id)
        if job and job.lock.enabled:
            self._lock_manager.release(job_id)

        # Decrement concurrency counter
        self._concurrency_limiter.decrement_running(job_id)

        # Process queued jobs for this job type
        self._process_queued_jobs(job_id)

        # Update state
        state = self.db.get_state(job_id) or JobState(job_id=job_id)
        state.status = JobStatus.STOPPED if exit_code == 0 else JobStatus.FAILED
        state.stopped_at = now
        state.last_exit_code = exit_code
        if exit_code != 0:
            state.last_error = f"Process exited with code {exit_code}"

        # Update run record
        last_run = self.db.get_last_run(job_id)
        if last_run and last_run.finished_at is None:
            last_run.finished_at = now
            last_run.exit_code = exit_code
            last_run.duration_seconds = duration
            if exit_code != 0:
                last_run.error = f"Exit code: {exit_code}"
            self.db.update_run(last_run)

        # Remove from active processes
        if job_id in self._processes:
            del self._processes[job_id]

        status = "completed" if exit_code == 0 else "failed"
        logger.info(f"Job '{job_id}' {status} (exit code: {exit_code}, duration: {duration:.1f}s)")
        self._audit_log(job_id, status, f"exit_code: {exit_code}, duration: {duration:.1f}s")

        # Notify OpenClaw
        if exit_code == 0:
            self._openclaw.on_job_stopped(job_id, exit_code, duration)
        else:
            should_alert = job.alerts.on_failure if job else True
            self._openclaw.on_job_failed(
                job_id,
                exit_code,
                state.last_error,
                should_alert=should_alert,
            )

        # Check output for errors (even if exit code is 0)
        if job:
            self._output_checker.check_job_output(job_id, job, exit_code)

        # Handle retry for failed jobs
        if exit_code != 0:
            if job and job.retry.enabled:
                retry_time = self._retry_manager.schedule_retry(
                    job_id, job, state.retry_attempt
                )
                if retry_time:
                    state.retry_attempt += 1
                    state.next_retry = retry_time
                else:
                    # Max retries reached - move to DLQ
                    state.retry_attempt = 0
                    self._dlq.add_entry(
                        job_id=job_id,
                        run_id=last_run.id if last_run else None,
                        error=state.last_error or f"Exit code: {exit_code}",
                        job_config=job.model_dump() if job else None,
                        attempts=job.retry.max_attempts if job else 0,
                    )

        self.db.save_state(state)

    def _process_queued_jobs(self, job_id: str) -> None:
        """Process queued jobs after a job completes."""
        queued = self._concurrency_limiter.dequeue_job(job_id)
        if queued:
            logger.info(f"Starting queued job '{job_id}' (was queued at {queued.queued_at})")
            self.start_job(
                job_id=job_id,
                trigger=queued.trigger,
                params=queued.params,
                idempotency_key=queued.idempotency_key,
            )

    # Callbacks

    def _on_health_fail(self, job_id: str, result: HealthCheckResult) -> None:
        """Called when a job fails health check."""
        logger.warning(f"Health check failed for '{job_id}': {result.message}")
        self._audit_log(job_id, "health_fail", result.message)

        # Send alert if configured
        job = self.jobs.get_job(job_id)
        if job and job.alerts.on_health_fail:
            self._openclaw.on_health_fail(job_id, result.check_type.value, result.message)

    def _on_health_recover(self, job_id: str, result: HealthCheckResult) -> None:
        """Called when a job recovers from health failure."""
        logger.info(f"Job '{job_id}' recovered: {result.message}")
        self._audit_log(job_id, "health_recover", result.message)

        # Send alert if configured
        job = self.jobs.get_job(job_id)
        if job and job.alerts.on_recovered:
            self._openclaw.on_health_recover(job_id)

    def _on_max_retries(self, job_id: str) -> None:
        """Called when a job reaches max retries."""
        logger.error(f"Job '{job_id}' reached max retries, marking as failed")
        self._audit_log(job_id, "max_retries", "Job disabled")

        # Update state
        state = self.db.get_state(job_id) or JobState(job_id=job_id)
        state.status = JobStatus.FAILED
        state.last_error = "Max retries reached"
        state.retry_attempt = 0
        self.db.save_state(state)

        # Get retry attempts for the alert
        job = self.jobs.get_job(job_id)
        max_attempts = job.retry.max_attempts if job else 5

        # Send alert (always for max retries)
        self._openclaw.on_max_retries(job_id, max_attempts)

    def _on_missed_run(self, miss: MissedRunInfo) -> None:
        """Called when a scheduled job misses its expected run."""
        logger.error(
            f"Job '{miss.job_id}' missed scheduled run "
            f"(expected: {miss.expected_time}, overdue: {miss.hours_overdue:.1f}h)"
        )
        self._audit_log(
            miss.job_id,
            "missed_run",
            f"Expected: {miss.expected_time}, Overdue: {miss.hours_overdue:.1f}h",
        )

        # Send alert
        self._openclaw.on_missed_run(
            job_id=miss.job_id,
            expected_time=miss.expected_time,
            hours_overdue=miss.hours_overdue,
            last_run=miss.last_actual_run,
        )

    def _on_rate_limit(self, job_id: str, message: str) -> None:
        """Called when rate limiting is detected for a job."""
        logger.warning(f"Rate limit detected for '{job_id}': {message}")
        self._audit_log(job_id, "rate_limit", message)

        # Send alert
        self._openclaw.on_rate_limit(job_id, message)

    def _on_output_error(self, job_id: str, match: OutputMatch) -> None:
        """Called when error pattern is found in job output."""
        logger.warning(f"Error pattern found in '{job_id}' output: {match.pattern}")
        self._audit_log(job_id, "output_error", f"Pattern: {match.pattern}, Line: {match.line_number}")

        # Send alert
        self._openclaw.on_output_error(job_id, match.pattern, match.line)

    def _on_output_warning(self, job_id: str, match: OutputMatch) -> None:
        """Called when warning pattern is found in job output."""
        # Just log, don't alert for warnings
        logger.info(f"Warning pattern found in '{job_id}' output: {match.pattern}")

    def _on_resource_violation(self, violation: ResourceViolation) -> None:
        """Called when a resource limit is violated."""
        logger.warning(
            f"Resource violation for '{violation.job_id}': "
            f"{violation.resource} = {violation.current_value:.1f} > {violation.limit_value:.1f}"
        )
        self._audit_log(
            violation.job_id,
            "resource_violation",
            f"{violation.resource}: {violation.current_value:.1f} > {violation.limit_value:.1f}",
        )

    def _on_resource_kill(self, job_id: str, reason: str) -> None:
        """Called when a job is killed due to resource limits."""
        logger.error(f"Job '{job_id}' killed: {reason}")
        self._audit_log(job_id, "resource_kill", reason)
        
        # Send alert
        self._openclaw.queue_alert(
            job_id=job_id,
            alert_type=AlertType.FAILURE,
            message=f"Job killed due to resource limit: {reason}",
            details={"reason": reason},
        )

    def _on_job_queued(self, job_id: str, queue_position: int) -> None:
        """Called when a job is queued due to concurrency limits."""
        logger.info(f"Job '{job_id}' queued at position {queue_position}")
        self._audit_log(job_id, "queued", f"Position: {queue_position}")

    def _on_queue_timeout(self, job_id: str, waited_seconds: float) -> None:
        """Called when a queued job times out."""
        logger.warning(f"Job '{job_id}' timed out in queue after {waited_seconds:.1f}s")
        self._audit_log(job_id, "queue_timeout", f"Waited: {waited_seconds:.1f}s")
        
        # Send alert
        self._openclaw.queue_alert(
            job_id=job_id,
            alert_type=AlertType.FAILURE,
            message=f"Job timed out in queue after {waited_seconds:.1f}s",
            details={"waited_seconds": waited_seconds},
        )

    def _on_dlq_entry(self, entry: DLQEntry) -> None:
        """Called when a job is added to the dead letter queue."""
        logger.error(f"Job '{entry.job_id}' moved to DLQ after {entry.attempts} attempts")
        self._audit_log(entry.job_id, "dlq_entry", f"Attempts: {entry.attempts}, Error: {entry.last_error}")
        
        # Send alert
        self._openclaw.queue_alert(
            job_id=entry.job_id,
            alert_type=AlertType.MAX_RETRIES,
            message=f"Job moved to DLQ after {entry.attempts} failed attempts",
            details={"attempts": entry.attempts, "error": entry.last_error},
        )

    def _on_trigger_event(self, event: TriggerEvent) -> None:
        """Called when an external trigger fires."""
        logger.info(f"Trigger event for '{event.job_id}': {event.trigger_type}")
        self._audit_log(event.job_id, "trigger", f"Type: {event.trigger_type}, Source: {event.source}")
        
        # Start the job
        self.start_job(
            job_id=event.job_id,
            trigger=f"trigger:{event.trigger_type}",
            params=event.payload,
            idempotency_key=event.idempotency_key,
        )

    def _on_eta_trigger(self, job_id: str, trigger: str, params: dict | None, idempotency_key: str | None) -> None:
        """Called when an ETA-scheduled job is due."""
        logger.info(f"ETA trigger for '{job_id}'")
        self._audit_log(job_id, "eta_trigger", f"Trigger: {trigger}")
        
        # Check if revoked
        if self._revocation_manager.is_revoked(job_id):
            logger.info(f"ETA job '{job_id}' skipped - revoked")
            return
        
        # Start the job
        self.start_job(
            job_id=job_id,
            trigger=trigger,
            params=params,
            idempotency_key=idempotency_key,
        )

    def _start_job_for_workflow(self, job_id: str, trigger: str, env: dict | None) -> bool:
        """Start a job as part of a workflow (with env vars)."""
        job = self.jobs.get_job(job_id)
        if not job:
            logger.error(f"Job '{job_id}' not found for workflow")
            return False
        
        # Merge workflow env with job env
        if env:
            original_env = job.env.copy()
            job.env.update(env)
        
        success = self.start_job(job_id, trigger=trigger)
        
        # Restore original env
        if env:
            job.env = original_env
        
        return success

    # DLQ Management

    def get_dlq_entries(self, pending_only: bool = True) -> list[DLQEntry]:
        """Get entries from the dead letter queue.
        
        Args:
            pending_only: If True, only return entries not yet reinjected
            
        Returns:
            List of DLQ entries
        """
        return self._dlq.get_entries(pending_only=pending_only)

    def get_dlq_stats(self) -> dict:
        """Get DLQ statistics."""
        stats = self._dlq.get_stats()
        return {
            "total_entries": stats.total_entries,
            "pending_entries": stats.pending_entries,
            "reinjected_entries": stats.reinjected_entries,
            "by_job": stats.by_job,
        }

    def reinject_dlq_entry(self, entry_id: int) -> bool:
        """Reinject a DLQ entry for retry.
        
        Args:
            entry_id: The DLQ entry ID
            
        Returns:
            True if reinjection was successful
        """
        entry = self._dlq.get_entry(entry_id)
        if not entry:
            logger.error(f"DLQ entry {entry_id} not found")
            return False

        if entry.is_reinjected:
            logger.warning(f"DLQ entry {entry_id} already reinjected")
            return False

        # Start the job
        success = self.start_job(entry.job_id, trigger="dlq_reinject")
        
        if success:
            # Get the new run ID
            last_run = self.db.get_last_run(entry.job_id)
            self._dlq.mark_reinjected(entry_id, last_run.id if last_run else None)
            logger.info(f"Reinjected DLQ entry {entry_id} for job '{entry.job_id}'")

        return success

    def purge_dlq(self, job_id: str | None = None, older_than_days: int | None = None) -> int:
        """Purge entries from the DLQ.
        
        Args:
            job_id: Only purge entries for this job
            older_than_days: Only purge entries older than this
            
        Returns:
            Number of entries purged
        """
        return self._dlq.purge(job_id=job_id, older_than_days=older_than_days)

    # Trigger Management

    def trigger_job_webhook(
        self,
        job_id: str,
        payload: dict | None = None,
        idempotency_key: str | None = None,
        auth_token: str | None = None,
    ) -> bool:
        """Trigger a job via webhook.
        
        Args:
            job_id: The job to trigger
            payload: Optional payload data
            idempotency_key: Optional idempotency key
            auth_token: Optional auth token for validation
            
        Returns:
            True if trigger was accepted
        """
        job = self.jobs.get_job(job_id)
        if not job:
            logger.error(f"Job '{job_id}' not found")
            return False

        if not job.trigger.enabled or job.trigger.type.value != "webhook":
            logger.error(f"Job '{job_id}' does not have webhook trigger enabled")
            return False

        # Validate auth token if configured
        if job.trigger.auth_token and job.trigger.auth_token != auth_token:
            logger.warning(f"Invalid auth token for job '{job_id}' webhook trigger")
            return False

        # Start the job
        return self.start_job(
            job_id=job_id,
            trigger="webhook",
            params=payload,
            idempotency_key=idempotency_key,
        )

    # Concurrency Info

    def get_concurrency_stats(self, job_id: str) -> dict:
        """Get concurrency statistics for a job."""
        stats = self._concurrency_limiter.get_stats(job_id)
        job = self.jobs.get_job(job_id)
        return {
            "job_id": job_id,
            "running_count": stats.running_count if stats else 0,
            "max_instances": job.concurrency.max_instances if job else 1,
            "queued_count": stats.queued_count if stats else 0,
        }

    # ETA Scheduling

    def schedule_job_at(self, job_id: str, run_at: str, timezone: str | None = None):
        """Schedule a job to run at a specific time."""
        return self._eta_scheduler.schedule_at(job_id, run_at, timezone=timezone)

    def schedule_job_in(self, job_id: str, seconds: int):
        """Schedule a job to run in N seconds."""
        return self._eta_scheduler.schedule_in(job_id, seconds)

    def cancel_eta(self, job_id: str) -> bool:
        """Cancel an ETA schedule."""
        return self._eta_scheduler.cancel(job_id)

    def get_eta_jobs(self):
        """Get all pending ETA jobs."""
        return self._eta_scheduler.get_all_pending()

    # Revocation

    def revoke_job(self, job_id: str, reason: str | None = None, terminate: bool = False, expires_in: int | None = None):
        """Revoke a job."""
        revocation = self._revocation_manager.revoke(job_id, reason=reason, terminate=terminate, expires_in=expires_in)
        
        # If terminate and job is running, stop it
        if terminate and self.is_job_running(job_id):
            self.stop_job(job_id, force=True)
        
        # Cancel from ETA
        self._eta_scheduler.cancel(job_id)
        
        # Remove from concurrency queue
        self._concurrency_limiter.remove_from_queue(job_id)
        
        return revocation

    def unrevoke_job(self, job_id: str) -> bool:
        """Remove a job revocation."""
        return self._revocation_manager.unrevoke(job_id)

    def get_revocations(self):
        """Get all active revocations."""
        return self._revocation_manager.get_all_active()

    def is_revoked(self, job_id: str) -> bool:
        """Check if a job is revoked."""
        return self._revocation_manager.is_revoked(job_id)

    # Workflows

    def list_workflows(self):
        """List all registered workflows."""
        return self._workflow_manager.list_workflows()

    def get_workflow(self, workflow_id: str):
        """Get a workflow configuration."""
        return self._workflow_manager.get_workflow(workflow_id)

    def start_workflow(self, workflow_id: str):
        """Start a workflow run."""
        return self._workflow_manager.start_workflow(workflow_id)

    def list_workflow_runs(self, workflow_id: str | None = None):
        """List workflow runs."""
        return self._workflow_manager.list_runs(workflow_id)

    def get_workflow_run(self, run_id: int):
        """Get a workflow run."""
        return self._workflow_manager.get_run(run_id)

    def cancel_workflow(self, run_id: int) -> bool:
        """Cancel a workflow run."""
        return self._workflow_manager.cancel_workflow(run_id)

    # Results

    def get_job_result(self, job_id: str, run_id: int):
        """Get a specific job result."""
        return self._result_collector.get_result(job_id, run_id)

    def get_last_job_result(self, job_id: str):
        """Get the last result for a job."""
        return self._result_collector.get_last_result(job_id)

    # Audit Logging

    def _audit_log(self, job_id: str, action: str, details: str = "") -> None:
        """Write to the audit log."""
        audit_path = DEFAULT_LOGS_DIR / "daemon.audit.log"
        timestamp = datetime.now().isoformat()

        with open(audit_path, "a") as f:
            f.write(f"[{timestamp}] {job_id}: {action}")
            if details:
                f.write(f" - {details}")
            f.write("\n")

    # Lifecycle

    async def run(self) -> None:
        """Run the supervisor main loop."""
        self._running = True
        logger.info("Supervisor started")

        # Start OpenClaw integration
        await self._openclaw.start()

        # Load scheduled jobs into scheduler
        self._scheduler.update_jobs(self.jobs.get_enabled_jobs())

        # Start continuous jobs that should auto-start
        for job_id, job in self.jobs.get_jobs_by_type(JobType.CONTINUOUS).items():
            if job.enabled:
                # Check if already running from previous session
                state = self.db.get_state(job_id)
                if state and state.status == JobStatus.RUNNING and state.pid:
                    if self.check_pid(state.pid):
                        logger.info(f"Job '{job_id}' still running from previous session")
                        continue
                # Don't auto-start continuous jobs - let user start them
                # self.start_job(job_id, trigger="auto")

        # Start API server
        from procclaw.api.server import run_server
        api_task = asyncio.create_task(
            run_server(
                self,
                host=self.config.daemon.host,
                port=self.config.daemon.port,
            )
        )

        # Register file triggers for jobs that have them configured
        for job_id, job in self.jobs.jobs.items():
            if job.trigger.enabled and job.trigger.type.value == "file" and job.trigger.watch_path:
                self._trigger_manager.register_file_trigger(
                    job_id=job_id,
                    watch_path=job.trigger.watch_path,
                    pattern=job.trigger.pattern,
                    delete_after=job.trigger.delete_after,
                )

        # Start background tasks
        scheduler_task = asyncio.create_task(self._scheduler.run())
        health_task = asyncio.create_task(self._health_checker.run())
        retry_task = asyncio.create_task(self._retry_manager.run())
        watchdog_task = asyncio.create_task(self._watchdog.run())
        log_rotator_task = asyncio.create_task(self._log_rotator.run())
        resource_task = asyncio.create_task(self._resource_monitor.run())
        trigger_task = asyncio.create_task(self._trigger_manager.run())
        concurrency_task = asyncio.create_task(self._concurrency_limiter.run())

        try:
            while not self._shutdown_event.is_set():
                # Check running processes
                await self._check_processes()

                # Short sleep
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    pass

        except Exception as e:
            logger.error(f"Supervisor error: {e}")
        finally:
            # Stop all background tasks
            self._scheduler.stop()
            self._health_checker.stop()
            self._retry_manager.stop()
            self._watchdog.stop()
            self._log_rotator.stop()
            self._resource_monitor.stop()
            self._trigger_manager.stop()
            self._concurrency_limiter.stop()
            await self._openclaw.stop()

            for task in [api_task, scheduler_task, health_task, retry_task, watchdog_task, log_rotator_task, resource_task, trigger_task, concurrency_task]:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            await self._cleanup()
            self._running = False
            logger.info("Supervisor stopped")

    async def _check_processes(self) -> None:
        """Check status of running processes."""
        for job_id, handle in list(self._processes.items()):
            if not handle.is_running():
                self._finalize_job(job_id, handle)

    async def _cleanup(self) -> None:
        """Clean up all running processes."""
        if not self._processes:
            return

        logger.info(f"Stopping {len(self._processes)} running jobs...")

        # Send SIGTERM to all
        for job_id, handle in self._processes.items():
            if handle.is_running():
                logger.debug(f"Terminating {job_id}")
                handle.terminate()

        # Wait for graceful shutdown (max 60s total)
        deadline = datetime.now().timestamp() + 60
        while self._processes and datetime.now().timestamp() < deadline:
            await asyncio.sleep(0.5)
            for job_id, handle in list(self._processes.items()):
                if not handle.is_running():
                    self._finalize_job(job_id, handle)

        # Force kill remaining
        for job_id, handle in list(self._processes.items()):
            if handle.is_running():
                logger.warning(f"Force killing {job_id}")
                handle.kill()
                self._finalize_job(job_id, handle)

    def shutdown(self) -> None:
        """Signal the supervisor to shut down."""
        logger.info("Shutdown requested")
        self._shutdown_event.set()

    @property
    def is_running(self) -> bool:
        """Check if the supervisor is running."""
        return self._running

    # Static Methods

    @staticmethod
    def check_pid(pid: int) -> bool:
        """Check if a process with given PID is running."""
        try:
            process = psutil.Process(pid)
            return process.is_running()
        except psutil.NoSuchProcess:
            return False
