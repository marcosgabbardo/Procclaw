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
    HealingStatus,
    JobConfig,
    JobRun,
    JobsConfig,
    JobState,
    JobStatus,
    JobType,
    Priority,
    ProcClawConfig,
    SessionTriggerEvent,
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
from procclaw.core.composite import CompositeExecutor
from procclaw.core.watchdog import Watchdog, MissedRunInfo
from procclaw.core.workflow import WorkflowManager, WorkflowConfig
from procclaw.core.self_healing import SelfHealer, HealingResult
from procclaw.core.healing_engine import HealingEngine, ProactiveScheduler
from procclaw.core.queue_manager import QueueManager
from procclaw.openclaw import (
    OpenClawIntegration, 
    init_integration, 
    AlertType,
    send_to_session,
    render_trigger_template,
)
from procclaw.secrets import resolve_secret_ref
from procclaw.core.job_wrapper import JobWrapperManager, get_wrapper_manager


class ProcessHandle:
    """Handle to a running process."""

    def __init__(
        self,
        job_id: str,
        process: subprocess.Popen[bytes],
        stdout_file: Any,
        stderr_file: Any,
        run_id: int | None = None,
    ):
        self.job_id = job_id
        self.process = process
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file
        self.run_id = run_id
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


class OrphanProcessHandle:
    """Handle to an orphaned process (from previous daemon session).
    
    Uses psutil to manage the process since we don't have the original Popen.
    """

    def __init__(self, job_id: str, pid: int, started_at: datetime | None = None):
        self.job_id = job_id
        self._pid = pid
        self.started_at = started_at or datetime.now()
        self.stdout_file = None
        self.stderr_file = None
        self._process: psutil.Process | None = None
        self._returncode: int | None = None
        
        try:
            self._process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            self._returncode = -1

    @property
    def pid(self) -> int:
        """Get the process ID."""
        return self._pid

    @property
    def returncode(self) -> int | None:
        """Get the return code if process has exited."""
        if self._returncode is not None:
            return self._returncode
        if self._process and not self.is_running():
            self._returncode = -15  # Assume SIGTERM if we can't get actual code
        return self._returncode

    def is_running(self) -> bool:
        """Check if the process is still running."""
        if not self._process:
            return False
        try:
            return self._process.is_running() and self._process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def terminate(self) -> None:
        """Send SIGTERM to the process."""
        if self._process and self.is_running():
            try:
                self._process.terminate()
            except psutil.NoSuchProcess:
                pass

    def kill(self) -> None:
        """Send SIGKILL to the process."""
        if self._process and self.is_running():
            try:
                self._process.kill()
            except psutil.NoSuchProcess:
                pass

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process to finish."""
        if not self._process:
            return self._returncode or -1
        try:
            self._process.wait(timeout=timeout)
            self._returncode = -15  # psutil doesn't give us exit code
            return self._returncode
        except psutil.NoSuchProcess:
            self._returncode = -1
            return self._returncode
        except psutil.TimeoutExpired:
            raise subprocess.TimeoutExpired(cmd="", timeout=timeout or 0)

    def close_files(self) -> None:
        """Close log file handles (no-op for orphans)."""
        pass
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
        self._manually_stopped: set[str] = set()  # Jobs explicitly stopped by user (skip retry)
        self._shutdown_event = asyncio.Event()
        self._running = False

        # Initialize scheduler
        self._scheduler = Scheduler(
            on_trigger=self.start_job,
            is_job_running=self.is_job_running,
            get_last_run=self._get_job_last_run_time,
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
        
        # Initialize composite executor (chain, group, chord)
        self._composite = CompositeExecutor(self)

        # Initialize deduplication manager
        self._dedup_manager = DeduplicationManager(
            db=self.db,
            default_window=60,
        )

        # Initialize concurrency limiter
        self._concurrency_limiter = ConcurrencyLimiter(
            db=self.db,
            get_running_count=self._get_running_instance_count,
            global_max=self.config.daemon.max_concurrent_jobs if hasattr(self.config.daemon, 'max_concurrent_jobs') else 50,
        )

        # Initialize priority queue
        self._priority_queue = PriorityQueue(max_size=10000)

        # Initialize dead letter queue
        self._dlq = DeadLetterQueue(
            db=self.db,
            on_dlq_add=self._on_dlq_entry,
        )

        # Initialize lock manager
        from procclaw.core.locks import SQLiteLockProvider
        lock_provider = SQLiteLockProvider(db=self.db)
        self._lock_manager = LockManager(
            provider=lock_provider,
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

        # Initialize self-healer (reactive - on failure)
        self._self_healer = SelfHealer(
            db=self.db,
            logs_dir=DEFAULT_LOGS_DIR,
        )

        # Initialize healing engine (proactive - scheduled reviews)
        self._healing_engine = HealingEngine(
            db=self.db,
            supervisor=self,
        )
        self._proactive_scheduler = ProactiveScheduler(
            engine=self._healing_engine,
            supervisor=self,
        )

        # Initialize queue manager for sequential job execution
        self._queue_manager = QueueManager(
            on_job_ready=self._on_queue_job_ready,
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

    def _get_job_last_run_time(self, job_id: str) -> datetime | None:
        """Get last run start time for scheduler catchup detection."""
        last_run = self.db.get_last_run(job_id)
        if last_run:
            return last_run.started_at
        return None

    # Process Management

    def _adopt_orphan_process(self, job_id: str, pid: int) -> None:
        """Adopt an orphaned process from a previous daemon session.
        
        Creates an OrphanProcessHandle so the process can be managed (stopped, etc.)
        without needing the original Popen object.
        """
        state = self.db.get_state(job_id)
        started_at = state.started_at if state else None
        
        handle = OrphanProcessHandle(job_id, pid, started_at)
        self._processes[job_id] = handle
        
        logger.info(f"Adopted orphan process for job '{job_id}' (PID {pid})")

    def start_job(
        self,
        job_id: str,
        trigger: str = "manual",
        params: dict | None = None,
        idempotency_key: str | None = None,
        composite_id: str | None = None,
        _from_queue: bool = False,
    ) -> bool:
        """Start a job.
        
        Args:
            job_id: The job ID to start
            trigger: What triggered this start (manual, schedule, retry, api, webhook)
            params: Optional parameters for the job
            idempotency_key: Optional idempotency key for deduplication
            composite_id: Workflow ID if run as part of chain/group/chord
            _from_queue: Internal flag - job is starting from queue (skip queue check)
            
        Returns:
            True if job was started (or queued), False otherwise
        """
        job = self.jobs.get_job(job_id)
        if not job:
            logger.error(f"Job '{job_id}' not found")
            return False

        if not job.enabled:
            logger.warning(f"Job '{job_id}' is disabled")
            return False

        if job_id in self._processes and self._processes[job_id].is_running():
            logger.warning(f"Job '{job_id}' is already running (in memory)")
            return False
        
        # Also check database state and PID (for orphaned processes)
        state = self.db.get_state(job_id)
        if state and state.status == JobStatus.RUNNING and state.pid:
            if self.check_pid(state.pid):
                logger.warning(f"Job '{job_id}' is already running (PID {state.pid} from DB)")
                # Adopt the orphan so we can manage it
                self._adopt_orphan_process(job_id, state.pid)
                return False

        # Check revocation
        if self._revocation_manager.is_revoked(job_id):
            logger.info(f"Job '{job_id}' skipped - revoked")
            return False

        # Check deduplication
        if job.dedup.enabled:
            dedup_result = self._dedup_manager.check(
                job_id=job_id,
                params=params if job.dedup.use_params else None,
                idempotency_key=idempotency_key,
                window_seconds=job.dedup.window_seconds,
            )
            if dedup_result.is_duplicate:
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

        # Check execution queue (jobs in same queue run sequentially)
        # Manual starts bypass the queue
        if job.queue and not _from_queue:
            force_bypass = (trigger == "manual")
            can_run = self._queue_manager.try_acquire(job_id, job, trigger=trigger, force=force_bypass)
            if not can_run:
                # Job was queued - update state to QUEUED
                state = self.db.get_state(job_id) or JobState(job_id=job_id)
                state.status = JobStatus.QUEUED
                state.queued_at = datetime.now()
                self.db.save_state(state)
                logger.info(f"Job '{job_id}' queued in '{job.queue}' (waiting for slot)")
                return True  # Return True because job was successfully queued

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
            # Create run record BEFORE spawning so we have run_id for the wrapper
            now = datetime.now()
            run = JobRun(
                job_id=job_id,
                started_at=now,
                trigger=trigger,
                composite_id=composite_id,
            )
            run.id = self.db.add_run(run)
            
            # Spawn process with run_id for wrapper tracking
            handle = self._spawn_process(job_id, job, run_id=run.id)
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
            
            # Update run with actual start time and PID info
            run.started_at = handle.started_at
            self.db.update_run(run)

            # Record execution for deduplication
            if job.dedup.enabled:
                self._dedup_manager.record(
                    job_id=job_id,
                    run_id=run.id,
                    params=params if job.dedup.use_params else None,
                    idempotency_key=idempotency_key,
                )

            # Record job start for concurrency tracking
            self._concurrency_limiter.record_start(job_id)

            logger.info(f"Started job '{job_id}' (PID: {handle.pid})")
            self._audit_log(job_id, "started", f"PID: {handle.pid}, trigger: {trigger}")

            # Notify OpenClaw
            self._openclaw.on_job_started(job_id, handle.pid, trigger)

            # Execute ON_START session triggers (async, fire-and-forget)
            if job.session_triggers:
                asyncio.create_task(self._execute_session_triggers(
                    job_id=job_id,
                    job=job,
                    event=SessionTriggerEvent.ON_START,
                    exit_code=None,
                    duration=None,
                    error=None,
                    started_at=handle.started_at,
                    finished_at=None,
                ))

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

    # Composite Jobs (chain, group, chord)
    
    async def run_chain(self, composite_id: str, job_ids: list[str], trigger: str = "manual"):
        """Run jobs sequentially. Stops on first failure."""
        return await self._composite.run_chain(composite_id, job_ids, trigger)
    
    async def run_group(self, composite_id: str, job_ids: list[str], trigger: str = "manual"):
        """Run jobs in parallel. Waits for all to complete."""
        return await self._composite.run_group(composite_id, job_ids, trigger)
    
    async def run_chord(self, composite_id: str, job_ids: list[str], callback: str, trigger: str = "manual"):
        """Run jobs in parallel, then run callback."""
        return await self._composite.run_chord(composite_id, job_ids, callback, trigger)
    
    def get_composite_run(self, composite_id: str):
        """Get a composite run by ID."""
        return self._composite.get_run(composite_id)
    
    def list_composite_runs(self):
        """List all composite runs."""
        return self._composite.list_runs()

    def stop_job(self, job_id: str, force: bool = False) -> bool:
        """Stop a running job."""
        handle = self._processes.get(job_id)
        if not handle or not handle.is_running():
            logger.warning(f"Job '{job_id}' is not running")
            return False

        job = self.jobs.get_job(job_id)
        grace_period = job.shutdown.grace_period if job else 60

        try:
            # Mark as manually stopped to prevent auto-retry
            self._manually_stopped.add(job_id)
            
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

    def set_job_enabled(self, job_id: str, enabled: bool) -> bool:
        """Enable or disable a job.
        
        Args:
            job_id: The job to enable/disable
            enabled: True to enable, False to disable
            
        Returns:
            True if the job was updated
        """
        import yaml
        
        job = self.jobs.get_job(job_id)
        if not job:
            return False
        
        from procclaw.config import DEFAULT_JOBS_FILE
        
        jobs_file = DEFAULT_JOBS_FILE
        if not jobs_file.exists():
            return False
        
        try:
            with open(jobs_file) as f:
                config = yaml.safe_load(f)
            
            if "jobs" not in config or job_id not in config["jobs"]:
                return False
            
            config["jobs"][job_id]["enabled"] = enabled
            
            with open(jobs_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
            # Reload config
            self.reload_jobs()
            
            logger.info(f"Job '{job_id}' {'enabled' if enabled else 'disabled'}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to set job '{job_id}' enabled={enabled}: {e}")
            return False

    def delete_job(self, job_id: str) -> bool:
        """Delete a job from the configuration.
        
        Also stops any running processes and finalizes pending runs.
        
        Args:
            job_id: The job to delete
            
        Returns:
            True if the job was deleted
        """
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        
        job = self.jobs.get_job(job_id)
        if not job:
            return False
        
        # Stop if running
        if self.is_job_running(job_id):
            self.stop_job(job_id, force=True)
        
        # Finalize any pending runs for this job
        self._finalize_job_runs(job_id)
        
        # Remove from jobs config
        jobs_file = DEFAULT_JOBS_FILE
        if not jobs_file.exists():
            return False
        
        try:
            with open(jobs_file) as f:
                config = yaml.safe_load(f)
            
            if "jobs" not in config or job_id not in config["jobs"]:
                return False
            
            # Remove the job
            del config["jobs"][job_id]
            
            # Write back
            with open(jobs_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
            # Reload config
            self.reload_jobs()
            
            logger.info(f"Deleted job '{job_id}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete job '{job_id}': {e}")
            return False

    def update_job(self, job_id: str, updates: dict) -> bool:
        """Update job attributes in the configuration.
        
        Args:
            job_id: The job to update
            updates: Dictionary of attributes to update (name, description, tags, cmd, cwd, schedule, etc.)
            
        Returns:
            True if the job was updated
        """
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        
        job = self.jobs.get_job(job_id)
        if not job:
            return False
        
        jobs_file = DEFAULT_JOBS_FILE
        if not jobs_file.exists():
            return False
        
        # Allowed fields to update
        allowed_fields = {
            "name", "description", "tags", "cmd", "cwd", "env", "enabled",
            "schedule", "run_at", "timezone", "type", "priority", "self_healing",
            "queue",  # Execution queue for sequential job execution
            "sla",  # SLA monitoring settings
            "model",  # OpenClaw model override
            "thinking",  # OpenClaw thinking level
        }
        
        try:
            with open(jobs_file) as f:
                config = yaml.safe_load(f)
            
            if "jobs" not in config or job_id not in config["jobs"]:
                return False
            
            # Apply updates
            for key, value in updates.items():
                if key in allowed_fields:
                    config["jobs"][job_id][key] = value
            
            # Track modification time in metadata
            if "_metadata" not in config["jobs"][job_id]:
                config["jobs"][job_id]["_metadata"] = {}
            config["jobs"][job_id]["_metadata"]["updated_at"] = datetime.now().isoformat()
            
            with open(jobs_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
            # Reload config
            self.reload_jobs()
            
            logger.info(f"Updated job '{job_id}': {list(updates.keys())}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update job '{job_id}': {e}")
            return False

    def _disable_oneshot_job(self, job_id: str) -> None:
        """Disable a oneshot job after it has been executed.
        
        This is called automatically when a oneshot job completes successfully.
        """
        try:
            success = self.update_job(job_id, {"enabled": False})
            if success:
                logger.info(f"Oneshot job '{job_id}' auto-disabled after execution")
            else:
                logger.warning(f"Failed to auto-disable oneshot job '{job_id}'")
        except Exception as e:
            logger.error(f"Error disabling oneshot job '{job_id}': {e}")

    def _save_run_logs(self, job_id: str, run_id: int, started_at: datetime) -> None:
        """Save logs from log file to SQLite for a specific run.
        
        Reads the log file and extracts lines from this run based on timestamps.
        """
        try:
            job = self.jobs.get_job(job_id)
            if not job:
                return
            
            # Get log file path
            log_path = job.get_log_stdout_path(DEFAULT_LOGS_DIR, job_id)
            if not log_path.exists():
                return
            
            # Read log file and find lines for this run
            lines_to_save = []
            in_run = False
            run_marker = f"[{started_at.strftime('%Y-%m-%dT%H:%M:%S')}"
            
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Look for the start marker of this run
                    if run_marker in line or (in_run and "Starting job:" in line and job_id in line):
                        # Found start of next run, stop collecting
                        if in_run and "Starting job:" in line:
                            break
                        in_run = True
                    
                    if in_run:
                        lines_to_save.append(line.rstrip('\n'))
                    
                    # Also check for exact timestamp match in header
                    if f"[{started_at.isoformat().split('.')[0]}" in line:
                        in_run = True
            
            # If we couldn't find by marker, just save the last N lines
            if not lines_to_save:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    # Take last 500 lines as fallback
                    lines_to_save = [l.rstrip('\n') for l in all_lines[-500:]]
            
            # Save to database
            if lines_to_save:
                self.db.add_log_lines(run_id, job_id, lines_to_save, level="stdout")
                logger.debug(f"Saved {len(lines_to_save)} log lines for run {run_id} of job '{job_id}'")
                
            # Also save stderr if exists
            stderr_path = job.get_log_stderr_path(DEFAULT_LOGS_DIR, job_id)
            if stderr_path.exists():
                with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
                    stderr_lines = [l.rstrip('\n') for l in f.readlines()[-500:]]
                if stderr_lines:
                    self.db.add_log_lines(run_id, job_id, stderr_lines, level="stderr")
            
            # Delete log files if configured
            from procclaw.models import FileLogMode
            if hasattr(self.config.daemon, 'file_log_mode') and self.config.daemon.file_log_mode == FileLogMode.DELETE:
                if log_path.exists():
                    log_path.unlink()
                    logger.debug(f"Deleted stdout log file for job '{job_id}'")
                if stderr_path.exists():
                    stderr_path.unlink()
                    logger.debug(f"Deleted stderr log file for job '{job_id}'")
                    
        except Exception as e:
            logger.error(f"Error saving logs for run {run_id}: {e}")

    def _extract_session_info(self, job_id: str, run: "JobRun") -> None:
        """Extract OpenClaw session info from job logs or direct lookup.
        
        First tries to find SESSION_KEY= and SESSION_TRANSCRIPT= lines in logs.
        If not found and job is OpenClaw type, does direct lookup via CLI.
        """
        from procclaw.models import JobRun, JobType
        
        try:
            session_key = None
            session_transcript = None
            
            # Method 1: Try to extract from logs
            logs = self.db.get_logs(run_id=run.id, limit=5000)
            if logs:
                for log_entry in logs:
                    line = log_entry.get("line", "").strip()
                    if line.startswith("SESSION_KEY="):
                        session_key = line.split("=", 1)[1]
                    elif line.startswith("SESSION_TRANSCRIPT="):
                        session_transcript = line.split("=", 1)[1]
            
            # Method 2: Direct lookup if not found in logs and job is OpenClaw type
            if not session_key and not session_transcript:
                job = self.jobs.get_job(job_id)
                if job and job.type == JobType.OPENCLAW:
                    session_key, session_transcript = self._lookup_openclaw_session(job_id, run)
            
            # Update run if we found session info
            if session_key or session_transcript:
                run.session_key = session_key
                run.session_transcript = session_transcript
                
                # Read and persist session messages in DB (so we don't depend on JSONL files)
                if session_transcript:
                    try:
                        from pathlib import Path
                        import json
                        transcript_path = Path(session_transcript)
                        if transcript_path.exists():
                            messages = []
                            with open(transcript_path, "r", encoding="utf-8") as f:
                                for line in f:
                                    if line.strip():
                                        try:
                                            msg = json.loads(line)
                                            messages.append(msg)
                                        except json.JSONDecodeError:
                                            pass
                            if messages:
                                run.session_messages = json.dumps(messages)
                                logger.debug(f"Saved {len(messages)} session messages for job '{job_id}'")
                    except Exception as e:
                        logger.warning(f"Failed to read transcript for '{job_id}': {e}")
                
                self.db.update_run(run)
                logger.debug(f"Extracted session info for job '{job_id}': key={session_key}")
                
        except Exception as e:
            logger.error(f"Error extracting session info for job '{job_id}': {e}")
    
    def _lookup_openclaw_session(self, job_id: str, run: "JobRun") -> tuple[str | None, str | None]:
        """Direct lookup of OpenClaw session info via CLI.
        
        Used when job was killed or logs don't contain session markers.
        Extracts cron job ID from the job config command and looks up the session.
        
        Returns:
            Tuple of (session_key, session_transcript_path)
        """
        import subprocess
        import json
        import re
        from pathlib import Path
        
        try:
            # Get command from job config (not stored in JobRun)
            job = self.jobs.get_job(job_id)
            if not job:
                logger.debug(f"Job '{job_id}' not found for session lookup")
                return None, None
            
            # Extract OpenClaw cron job ID from the job command
            # Format: python3 oc-runner.py <cron_job_id> <timeout>
            cmd = job.cmd or ""
            match = re.search(r'oc-runner\.py\s+([a-f0-9-]{36})', cmd)
            if not match:
                logger.debug(f"Could not extract cron job ID from command: {cmd}")
                return None, None
            
            cron_job_id = match.group(1)
            expected_session_key = f"agent:main:cron:{cron_job_id}"
            
            logger.debug(f"Looking up OpenClaw session: {expected_session_key}")
            
            # Query OpenClaw for sessions
            result = subprocess.run(
                ["openclaw", "sessions", "--json"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.warning(f"openclaw sessions command failed: {result.stderr}")
                return None, None
            
            data = json.loads(result.stdout)
            sessions = data.get("sessions", [])
            
            # Find matching session
            for session in sessions:
                if session.get("key") == expected_session_key:
                    session_id = session.get("sessionId")
                    if session_id:
                        # Build transcript path
                        transcript_path = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
                        if transcript_path.exists():
                            logger.info(f"Found OpenClaw session via direct lookup: {expected_session_key}")
                            return expected_session_key, str(transcript_path)
                        else:
                            logger.debug(f"Session found but transcript not at: {transcript_path}")
                            return expected_session_key, None
            
            logger.debug(f"No matching OpenClaw session found for: {expected_session_key}")
            return None, None
            
        except subprocess.TimeoutExpired:
            logger.warning("Timeout looking up OpenClaw session")
            return None, None
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON from openclaw sessions: {e}")
            return None, None
        except Exception as e:
            logger.error(f"Error looking up OpenClaw session: {e}")
            return None, None

    async def _execute_session_triggers(
        self,
        job_id: str,
        job: JobConfig,
        event: SessionTriggerEvent,
        exit_code: int | None,
        duration: float | None,
        error: str | None,
        started_at: datetime | None,
        finished_at: datetime | None,
    ) -> None:
        """Execute session triggers for a job event.
        
        Sends messages to OpenClaw sessions based on configured triggers.
        """
        if not job.session_triggers:
            return
        
        status = "success" if exit_code == 0 else "failed"
        
        for trigger in job.session_triggers:
            if not trigger.enabled:
                continue
            
            # Check if this trigger matches the event
            should_fire = False
            if trigger.event == event:
                should_fire = True
            elif trigger.event == SessionTriggerEvent.ON_COMPLETE:
                # ON_COMPLETE fires for both success and failure
                if event in (SessionTriggerEvent.ON_SUCCESS, SessionTriggerEvent.ON_FAILURE):
                    should_fire = True
            
            if not should_fire:
                continue
            
            # Render the message template
            message = render_trigger_template(
                template=trigger.message,
                job_id=job_id,
                job_name=job.name,
                status=status,
                exit_code=exit_code,
                duration=duration,
                error=error,
                started_at=started_at,
                finished_at=finished_at,
            )
            
            # Send to session
            logger.info(f"Firing session trigger for job '{job_id}' -> {trigger.session}" + 
                       (" (immediate)" if trigger.immediate else ""))
            try:
                await send_to_session(trigger.session, message, immediate=trigger.immediate)
            except Exception as e:
                logger.error(f"Failed to send session trigger for job '{job_id}': {e}")

    async def _run_healing_queue(self) -> None:
        """Background task to process healing queue.
        
        Runs every 30 seconds and processes one healing at a time.
        """
        logger.info("Healing queue processor started")
        
        while not self._shutdown_event.is_set():
            try:
                # Process queue (only runs if not already processing)
                await self._self_healer.process_queue()
            except Exception as e:
                logger.error(f"Error processing healing queue: {e}")
            
            # Wait 30 seconds before checking again
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=30.0,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass
        
        logger.info("Healing queue processor stopped")

    async def _trigger_self_healing(
        self,
        job_id: str,
        job: JobConfig,
        run: JobRun,
    ) -> None:
        """Trigger self-healing for a failed job.
        
        This is called asynchronously after a job fails and retries are exhausted.
        """
        async def retry_callback(jid: str) -> bool:
            """Callback to retry the job after a fix is applied."""
            try:
                # Start the job synchronously and wait for completion
                started = self.start_job(jid, trigger="healing_retry")
                if not started:
                    return False
                
                # Wait for job to complete (with timeout)
                for _ in range(60):  # Wait up to 5 minutes
                    await asyncio.sleep(5)
                    if not self.is_job_running(jid):
                        # Check the result
                        last_run = self.db.get_last_run(jid)
                        if last_run and last_run.exit_code == 0:
                            return True
                        return False
                
                # Timeout
                return False
            except Exception as e:
                logger.error(f"Error in healing retry callback: {e}")
                return False
        
        try:
            result = await self._self_healer.trigger_healing(
                job_id=job_id,
                job=job,
                run=run,
                on_retry=retry_callback,
            )
            logger.info(f"Self-healing completed for job '{job_id}': {result.status.value}")
        except Exception as e:
            logger.error(f"Self-healing failed for job '{job_id}': {e}")

    def add_job_tag(self, job_id: str, tag: str) -> bool:
        """Add a tag to a job.
        
        Args:
            job_id: The job to update
            tag: Tag to add
            
        Returns:
            True if the tag was added
        """
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        
        job = self.jobs.get_job(job_id)
        if not job:
            return False
        
        if tag in job.tags:
            return True  # Already has tag
        
        jobs_file = DEFAULT_JOBS_FILE
        if not jobs_file.exists():
            return False
        
        try:
            with open(jobs_file) as f:
                config = yaml.safe_load(f)
            
            if "jobs" not in config or job_id not in config["jobs"]:
                return False
            
            tags = config["jobs"][job_id].get("tags", [])
            if tag not in tags:
                tags.append(tag)
                config["jobs"][job_id]["tags"] = tags
            
            with open(jobs_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
            self.reload_jobs()
            logger.info(f"Added tag '{tag}' to job '{job_id}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add tag to job '{job_id}': {e}")
            return False

    def remove_job_tag(self, job_id: str, tag: str) -> bool:
        """Remove a tag from a job.
        
        Args:
            job_id: The job to update
            tag: Tag to remove
            
        Returns:
            True if the tag was removed
        """
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        
        job = self.jobs.get_job(job_id)
        if not job:
            return False
        
        if tag not in job.tags:
            return True  # Doesn't have tag
        
        jobs_file = DEFAULT_JOBS_FILE
        if not jobs_file.exists():
            return False
        
        try:
            with open(jobs_file) as f:
                config = yaml.safe_load(f)
            
            if "jobs" not in config or job_id not in config["jobs"]:
                return False
            
            tags = config["jobs"][job_id].get("tags", [])
            if tag in tags:
                tags.remove(tag)
                config["jobs"][job_id]["tags"] = tags
            
            with open(jobs_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
            self.reload_jobs()
            logger.info(f"Removed tag '{tag}' from job '{job_id}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to remove tag from job '{job_id}': {e}")
            return False

    def get_job_dependencies_graph(self) -> dict:
        """Get job dependencies as a graph structure.
        
        Returns:
            Dictionary with job dependency relationships
        """
        graph = {"nodes": [], "edges": []}
        
        for job_id, job in self.jobs.jobs.items():
            graph["nodes"].append({
                "id": job_id,
                "name": job.name,
                "type": job.type.value,
                "tags": job.tags,
            })
            
            for dep in job.depends_on:
                graph["edges"].append({
                    "from": dep.job,
                    "to": job_id,
                    "condition": dep.condition.value,
                })
        
        return graph

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

        # Get dependencies
        depends_on = [
            {
                "job": dep.job,
                "condition": dep.condition.value,
            }
            for dep in job.depends_on
        ]
        
        # Find jobs that depend on this one
        dependents = []
        for other_id, other_job in self.jobs.jobs.items():
            for dep in other_job.depends_on:
                if dep.job == job_id:
                    dependents.append({
                        "job": other_id,
                        "condition": dep.condition.value,
                    })
        
        # Get metadata from YAML (created_at, updated_at)
        import yaml
        from procclaw.config import DEFAULT_JOBS_FILE
        created_at = None
        updated_at = None
        try:
            with open(DEFAULT_JOBS_FILE) as f:
                config = yaml.safe_load(f)
            if config and "jobs" in config and job_id in config["jobs"]:
                metadata = config["jobs"][job_id].get("_metadata", {})
                created_at = metadata.get("created_at")
                updated_at = metadata.get("updated_at")
        except Exception:
            pass
        
        # Check if job is paused
        is_paused = state.paused or self._scheduler.is_paused(job_id)
        
        # Compute display status
        display_status = state.status.value
        if not job.enabled:
            display_status = "disabled"
        elif is_paused:
            display_status = "disabled"
        elif state.status == JobStatus.QUEUED:
            display_status = JobStatus.QUEUED.value
        elif state.status == JobStatus.RUNNING:
            display_status = JobStatus.RUNNING.value
        elif (job.schedule and next_run) or (job.run_at and next_run):
            # Scheduled or oneshot job with future run = PLANNED
            display_status = JobStatus.PLANNED.value
        
        return {
            "id": job_id,
            "name": job.name,
            "description": job.description,
            "type": job.type.value,
            "status": display_status,
            "enabled": job.enabled,
            "paused": is_paused,
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
            # Additional fields for detail view
            "cmd": job.cmd,
            "cwd": job.cwd,
            "schedule": job.schedule,
            "run_at": job.run_at.isoformat() if job.run_at else None,
            "max_retries": job.retry.max_attempts if job.retry else 0,
            "timeout_seconds": job.shutdown.grace_period if job.shutdown else 60,
            # Dependencies
            "depends_on": depends_on,
            "dependents": dependents,
            # Execution queue
            "queue": job.queue,
            "queue_status": self._queue_manager.get_job_queue_status(job_id, job) if job.queue else None,
            # Metadata
            "created_at": created_at,
            "updated_at": updated_at,
            # OpenClaw-specific
            "model": job.model,
            "thinking": job.thinking,
            # SLA settings
            "sla": {
                "enabled": job.sla.enabled if job.sla else False,
                "success_rate": job.sla.success_rate if job.sla else None,
                "schedule_tolerance": job.sla.schedule_tolerance if job.sla else None,
                "max_duration": job.sla.max_duration if job.sla else None,
                "evaluation_period": job.sla.evaluation_period if job.sla else None,
                "alert_threshold": job.sla.alert_threshold if job.sla else None,
                "alert_on_breach": job.sla.alert_on_breach if job.sla else None,
            } if job.sla else None,
            # Self-healing settings (full config for edit modal)
            "self_healing": {
                "enabled": job.self_healing.enabled,
                "analysis": {
                    "include_logs": job.self_healing.analysis.include_logs if job.self_healing.analysis else True,
                    "log_lines": job.self_healing.analysis.log_lines if job.self_healing.analysis else 200,
                    "include_stderr": job.self_healing.analysis.include_stderr if job.self_healing.analysis else True,
                    "include_history": job.self_healing.analysis.include_history if job.self_healing.analysis else 5,
                    "include_config": job.self_healing.analysis.include_config if job.self_healing.analysis else True,
                } if job.self_healing.analysis else None,
                "remediation": {
                    "enabled": job.self_healing.remediation.enabled if job.self_healing.remediation else True,
                    "max_attempts": job.self_healing.remediation.max_attempts if job.self_healing.remediation else 3,
                    "allowed_actions": job.self_healing.remediation.allowed_actions if job.self_healing.remediation else ["restart_job"],
                    "forbidden_paths": job.self_healing.remediation.forbidden_paths if job.self_healing.remediation else [],
                    "require_approval": job.self_healing.remediation.require_approval if job.self_healing.remediation else False,
                } if job.self_healing.remediation else None,
                "notify": {
                    "on_analysis": job.self_healing.notify.on_analysis if job.self_healing.notify else False,
                    "on_fix_attempt": job.self_healing.notify.on_fix_attempt if job.self_healing.notify else True,
                    "on_success": job.self_healing.notify.on_success if job.self_healing.notify else True,
                    "on_give_up": job.self_healing.notify.on_give_up if job.self_healing.notify else True,
                    "session": job.self_healing.notify.session if job.self_healing.notify else "main",
                } if job.self_healing.notify else None,
            } if job.self_healing else None,
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

    def _get_running_instance_count(self, job_id: str) -> int:
        """Get the count of running instances of a job.
        
        Used by ConcurrencyLimiter to track running jobs.
        
        Args:
            job_id: The job ID to check
            
        Returns:
            Number of running instances (0 or 1 for single-instance jobs)
        """
        return 1 if self.is_job_running(job_id) else 0

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

    def _spawn_process(self, job_id: str, job: JobConfig, run_id: int | None = None) -> ProcessHandle:
        """Spawn a new process for a job.
        
        Args:
            job_id: The job ID
            job: Job configuration
            run_id: Run ID for wrapper tracking (completion markers & heartbeat)
        """
        # Prepare working directory
        cwd = Path(job.cwd).expanduser() if job.cwd else Path.cwd()
        if not cwd.exists():
            raise RuntimeError(f"Working directory does not exist: {cwd}")

        # Prepare environment (resolve secrets)
        env = os.environ.copy()
        for key, value in job.env.items():
            env[key] = resolve_secret_ref(value)
        env["PROCCLAW_JOB_ID"] = job_id
        if run_id is not None:
            env["PROCCLAW_RUN_ID"] = str(run_id)

        # Prepare log files
        logs_dir = DEFAULT_LOGS_DIR
        logs_dir.mkdir(parents=True, exist_ok=True)

        stdout_path = job.get_log_stdout_path(logs_dir, job_id)
        stderr_path = job.get_log_stderr_path(logs_dir, job_id)

        stdout_file = open(stdout_path, "a")
        stderr_file = open(stderr_path, "a")

        # Log header
        timestamp = datetime.now().isoformat()
        stdout_file.write(f"\n{'='*60}\n")
        stdout_file.write(f"[{timestamp}] Starting job: {job_id}\n")
        stdout_file.write(f"Command: {job.cmd}\n")
        if run_id:
            stdout_file.write(f"Run ID: {run_id}\n")
        stdout_file.write(f"{'='*60}\n\n")
        stdout_file.flush()

        # Parse command (resolve secrets) and wrap with completion tracker
        resolved_cmd = resolve_secret_ref(job.cmd)
        
        # Use wrapper for reliable completion tracking (heartbeat + marker)
        wrapper_manager = get_wrapper_manager()
        wrapped_cmd = wrapper_manager.wrap_command(resolved_cmd)
        
        if sys.platform == "win32":
            cmd = resolved_cmd  # Windows: don't use wrapper (bash script)
            shell = True
        else:
            cmd = wrapped_cmd
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

        return ProcessHandle(job_id, process, stdout_file, stderr_file, run_id=run_id)

    def _finalize_job(self, job_id: str, handle: ProcessHandle) -> None:
        """Finalize a completed job."""
        now = datetime.now()
        duration = (now - handle.started_at).total_seconds()
        exit_code = handle.returncode

        # Close file handles
        handle.close_files()
        
        # Clean up completion marker and heartbeat (wrapper files)
        if handle.run_id is not None:
            wrapper_manager = get_wrapper_manager()
            wrapper_manager.cleanup_marker(job_id, handle.run_id)
            wrapper_manager.cleanup_heartbeat(job_id, handle.run_id)

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

        # Release execution queue slot (triggers next job in queue if any)
        if job and job.queue:
            self._queue_manager.release(job_id, job)

        # Record job stop for concurrency tracking
        self._concurrency_limiter.record_stop(job_id)

        # Process queued jobs for this job type
        self._process_queued_jobs(job_id)

        # Update state
        state = self.db.get_state(job_id) or JobState(job_id=job_id)
        state.status = JobStatus.STOPPED if exit_code == 0 else JobStatus.FAILED
        state.stopped_at = now
        state.last_exit_code = exit_code
        if exit_code != 0:
            state.last_error = f"Process exited with code {exit_code}"

        # Check if this was a manual stop (before setting run error)
        was_manually_stopped = job_id in self._manually_stopped

        # Update run record
        last_run = self.db.get_last_run(job_id)
        if last_run and last_run.finished_at is None:
            last_run.finished_at = now
            last_run.exit_code = exit_code
            last_run.duration_seconds = duration
            if was_manually_stopped:
                last_run.error = "Manually stopped"
            elif exit_code != 0:
                last_run.error = f"Exit code: {exit_code}"
            self.db.update_run(last_run)
            
            # Save logs to SQLite for this run
            self._save_run_logs(job_id, last_run.id, handle.started_at)
            
            # Extract OpenClaw session info for openclaw jobs
            if job and job.type == JobType.OPENCLAW:
                self._extract_session_info(job_id, last_run)
            
            # Check SLA and alert if breached
            if job and job.sla.enabled:
                try:
                    from procclaw.sla import check_and_alert_sla_breach
                    sla_breached = check_and_alert_sla_breach(self.db, job_id, job, last_run)
                    
                    # Trigger proactive review on SLA breach
                    if sla_breached:
                        asyncio.create_task(
                            self._proactive_scheduler.trigger_on_event(job_id, "sla_breach")
                        )
                except Exception as e:
                    logger.warning(f"Failed to check SLA for job '{job_id}': {e}")

        # Remove from active processes
        if job_id in self._processes:
            del self._processes[job_id]

        if was_manually_stopped:
            status = "stopped"
        elif exit_code == 0:
            status = "completed"
        else:
            status = "failed"
        logger.info(f"Job '{job_id}' {status} (exit code: {exit_code}, duration: {duration:.1f}s)")
        self._audit_log(job_id, status, f"exit_code: {exit_code}, duration: {duration:.1f}s")

        # Auto-disable oneshot jobs after completion
        if job and job.type == JobType.ONESHOT and exit_code == 0:
            logger.info(f"Oneshot job '{job_id}' completed - auto-disabling")
            self._disable_oneshot_job(job_id)

        # Notify OpenClaw (but not for manual stops)
        if was_manually_stopped:
            # No alert for manual stops
            self._openclaw.on_job_stopped(job_id, exit_code, duration)
        elif exit_code == 0:
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

        # Execute session triggers (async, fire-and-forget)
        # For manual stops, use ON_COMPLETE instead of ON_FAILURE
        if job and job.session_triggers:
            if was_manually_stopped:
                trigger_event = SessionTriggerEvent.ON_COMPLETE
            elif exit_code == 0:
                trigger_event = SessionTriggerEvent.ON_SUCCESS
            else:
                trigger_event = SessionTriggerEvent.ON_FAILURE
            asyncio.create_task(self._execute_session_triggers(
                job_id=job_id,
                job=job,
                event=trigger_event,
                exit_code=exit_code,
                duration=duration,
                error=state.last_error if exit_code != 0 else None,
                started_at=handle.started_at,
                finished_at=now,
            ))

        # Handle retry for failed jobs (but not if manually stopped)
        should_try_healing = False
        
        if was_manually_stopped:
            # Job was explicitly stopped by user - clear the flag and skip retry
            self._manually_stopped.discard(job_id)
            logger.debug(f"Job '{job_id}' was manually stopped, skipping retry")
            # Update state to show it was stopped, not failed
            state.status = JobStatus.STOPPED
            state.last_error = "Manually stopped"
        elif exit_code != 0:
            if job and job.retry.enabled:
                retry_time = self._retry_manager.schedule_retry(
                    job_id, job, state.retry_attempt
                )
                if retry_time:
                    state.retry_attempt += 1
                    state.next_retry = retry_time
                else:
                    # Max retries reached - move to DLQ and try self-healing
                    state.retry_attempt = 0
                    self._dlq.add_entry(
                        job_id=job_id,
                        run_id=last_run.id if last_run else None,
                        error=state.last_error or f"Exit code: {exit_code}",
                        job_config=job.model_dump() if job else None,
                        attempts=job.retry.max_attempts if job else 0,
                    )
                    should_try_healing = True
            else:
                # No retry configured, try self-healing immediately
                should_try_healing = True

        self.db.save_state(state)
        
        # Trigger self-healing if enabled and applicable
        if should_try_healing and job and last_run:
            if self._self_healer.is_healing_enabled(job):
                logger.info(f"Triggering self-healing for job '{job_id}'")
                asyncio.create_task(self._trigger_self_healing(job_id, job, last_run))
            
            # Trigger proactive review on failure (if configured)
            asyncio.create_task(
                self._proactive_scheduler.trigger_on_event(job_id, "failure")
            )

    def _process_queued_jobs(self, job_id: str) -> None:
        """Process queued jobs after a job completes."""
        # Check if there are queued jobs waiting
        queued = self._concurrency_limiter.get_next_queued()
        if queued and queued.get("job_id") == job_id:
            logger.info(f"Starting queued job '{job_id}'")
            self.start_job(
                job_id=job_id,
                trigger=queued.get("trigger", "queued"),
                params=queued.get("params"),
                idempotency_key=queued.get("idempotency_key"),
            )

    # Callbacks

    def _on_queue_job_ready(self, job_id: str, trigger: str) -> None:
        """Called when a queued job is ready to run (previous job in queue finished)."""
        logger.info(f"Queue released slot for job '{job_id}', starting now")
        
        # Start the job with _from_queue=True to skip queue check
        self.start_job(job_id, trigger=trigger, _from_queue=True)

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

    def _start_job_for_workflow(
        self, job_id: str, trigger: str, env: dict | None, composite_id: str | None = None
    ) -> bool:
        """Start a job as part of a workflow (with env vars)."""
        job = self.jobs.get_job(job_id)
        if not job:
            logger.error(f"Job '{job_id}' not found for workflow")
            return False
        
        # Merge workflow env with job env
        if env:
            original_env = job.env.copy()
            job.env.update(env)
        
        success = self.start_job(job_id, trigger=trigger, composite_id=composite_id)
        
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
        return self._dlq.list(include_reinjected=not pending_only)

    def list_dlq_entries(self, pending_only: bool = True, job_id: str | None = None) -> list[dict]:
        """Get entries from the DLQ as dicts for API.
        
        Args:
            pending_only: If True, only return entries not yet reinjected
            job_id: Filter by job ID
            
        Returns:
            List of DLQ entry dicts
        """
        entries = self.get_dlq_entries(pending_only=pending_only)
        result = []
        for e in entries:
            if job_id and e.job_id != job_id:
                continue
            result.append({
                "id": str(e.id),
                "job_id": e.job_id,
                "error": e.error,
                "attempts": e.attempt_count,
                "exit_code": e.exit_code,
                "failed_at": e.failed_at.isoformat() if e.failed_at else None,
                "reinjected": e.reinjected,
            })
        return result

    def delete_dlq_entry(self, entry_id: str) -> bool:
        """Delete a single DLQ entry.
        
        Args:
            entry_id: The DLQ entry ID
            
        Returns:
            True if deletion was successful
        """
        try:
            # Mark as reinjected to effectively "delete" it from pending
            self._dlq.reinject(int(entry_id), on_reinject=lambda *args: 0)
            return True
        except Exception:
            return False

    def get_recent_stats(self, hours: int = 24) -> dict:
        """Get run statistics for the last N hours.
        
        Args:
            hours: Number of hours to look back
            
        Returns:
            Dict with success, failed, and total runs count
        """
        from datetime import timedelta
        
        cutoff = datetime.now() - timedelta(hours=hours)
        runs = self.db.get_runs(since=cutoff, limit=1000)
        
        success = sum(1 for r in runs if r.exit_code == 0)
        failed = sum(1 for r in runs if r.exit_code != 0 and r.exit_code is not None)
        
        return {
            "success": success,
            "failed": failed,
            "runs": len(runs),
        }

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
        def do_reinject(job_id: str, params: dict | None) -> int:
            """Start the job and return the new run ID."""
            success = self.start_job(job_id, trigger="dlq_reinject", params=params)
            if not success:
                raise RuntimeError(f"Failed to start job '{job_id}'")
            last_run = self.db.get_last_run(job_id)
            return last_run.id if last_run else 0
        
        new_run_id = self._dlq.reinject(entry_id, on_reinject=do_reinject)
        return new_run_id is not None

    def purge_dlq(self, job_id: str | None = None, older_than_days: int | None = None) -> int:
        """Purge entries from the DLQ.
        
        Args:
            job_id: Only purge entries for this job (not implemented - purges all matching age)
            older_than_days: Only purge entries older than this
            
        Returns:
            Number of entries purged
        """
        return self._dlq.cleanup(days=older_than_days)

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

    # Run Cleanup

    def _finalize_job_runs(self, job_id: str) -> int:
        """Finalize all pending runs for a job.
        
        Used when deleting a job to clean up any runs that are still
        marked as running.
        
        Returns:
            Number of runs finalized
        """
        running_runs = self.db.get_running_runs()
        finalized = 0
        
        for run in running_runs:
            if run.job_id == job_id:
                logger.info(f"Finalizing run {run.id} for deleted job '{job_id}'")
                run.finished_at = datetime.now()
                run.exit_code = -15  # SIGTERM
                run.error = "Job deleted"
                self.db.update_run(run)
                finalized += 1
        
        return finalized

    def _cleanup_zombie_runs(self) -> None:
        """Clean up zombie runs from previous daemon sessions.
        
        Checks completion markers first (job may have completed while daemon was down).
        Falls back to heartbeat check, then marks as killed if no info available.
        """
        zombie_runs = self.db.get_running_runs()
        cleaned = 0
        recovered = 0
        wrapper_manager = get_wrapper_manager()
        
        for run in zombie_runs:
            # Check if the process is actually running
            state = self.db.get_state(run.job_id)
            pid = state.pid if state else None
            
            if pid and self.check_pid(pid):
                # Process is still running - ADOPT it so we can manage it
                if run.job_id not in self._processes:
                    logger.info(f"Adopting orphan process for '{run.job_id}' (PID {pid}, run {run.id})")
                    self._adopt_orphan_process(run.job_id, pid)
                continue
            
            # Process is not running - check completion marker first
            marker = wrapper_manager.check_completion_marker(run.job_id, run.id)
            
            if marker is not None:
                # Job completed while daemon was down! Use marker data.
                logger.info(
                    f"Recovered run {run.id} for job '{run.job_id}' from completion marker "
                    f"(exit code: {marker.exit_code})"
                )
                run.finished_at = marker.timestamp
                run.exit_code = marker.exit_code
                if marker.exit_code == 0:
                    run.error = None
                else:
                    run.error = f"Exit code: {marker.exit_code} (recovered from marker)"
                
                # Calculate duration
                if run.started_at:
                    run.duration_seconds = (marker.timestamp - run.started_at).total_seconds()
                
                self.db.update_run(run)
                
                # Update job state
                state = self.db.get_state(run.job_id) or JobState(job_id=run.job_id)
                state.status = JobStatus.STOPPED if marker.exit_code == 0 else JobStatus.FAILED
                state.stopped_at = marker.timestamp
                state.last_exit_code = marker.exit_code
                state.pid = None
                self.db.save_state(state)
                
                # Clean up marker
                wrapper_manager.cleanup_marker(run.job_id, run.id)
                wrapper_manager.cleanup_heartbeat(run.job_id, run.id)
                
                recovered += 1
                continue
            
            # No marker - check heartbeat to see if it died recently or a while ago
            heartbeat = wrapper_manager.check_heartbeat(run.job_id, run.id)
            
            if heartbeat is not None:
                if heartbeat.is_alive:
                    # Recent heartbeat but no process - very recent crash?
                    # This shouldn't happen normally, process might just be exiting
                    logger.warning(
                        f"Job '{run.job_id}' run {run.id} has recent heartbeat but no process, "
                        f"waiting for completion marker..."
                    )
                    continue  # Give it a chance to write marker
                else:
                    # Stale heartbeat - job died mid-execution
                    logger.info(
                        f"Job '{run.job_id}' run {run.id} died mid-execution "
                        f"(last heartbeat: {heartbeat.age_seconds:.1f}s ago)"
                    )
                    run.error = f"Killed mid-execution (no heartbeat for {heartbeat.age_seconds:.0f}s)"
            
            # Process is dead, no marker = zombie run
            logger.info(f"Cleaning up zombie run {run.id} for job '{run.job_id}'")
            run.finished_at = datetime.now()
            run.exit_code = -15  # SIGTERM
            if not run.error:
                run.error = "Killed (daemon restart)"
            self.db.update_run(run)
            
            # Clean up any stale heartbeat
            wrapper_manager.cleanup_heartbeat(run.job_id, run.id)
            
            cleaned += 1
        
        if recovered:
            logger.info(f"Recovered {recovered} completed runs from markers")
        if cleaned:
            logger.info(f"Cleaned up {cleaned} zombie runs from previous session")

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

        # Start proactive healing scheduler
        await self._proactive_scheduler.start()

        # Load scheduled jobs into scheduler
        self._scheduler.update_jobs(self.jobs.get_enabled_jobs())

        # Restore paused state from database for scheduled jobs
        for job_id in self.jobs.get_enabled_jobs():
            state = self.db.get_state(job_id)
            if state and state.paused:
                self._scheduler.pause_job(job_id)
                logger.info(f"Restored paused state for scheduled job '{job_id}'")

        # Restore pending retries from database
        restored_retries = self._retry_manager.restore_pending_retries(
            jobs=self.jobs.get_enabled_jobs(),
            get_state=self.db.get_state
        )
        if restored_retries > 0:
            logger.info(f"Restored {restored_retries} pending retries from database")

        # Clean up zombie runs from previous session (runs marked as running but process is dead)
        self._cleanup_zombie_runs()

        # Auto-start enabled continuous jobs
        for job_id, job in self.jobs.get_jobs_by_type(JobType.CONTINUOUS).items():
            if job.enabled:
                # Skip if already adopted by _cleanup_zombie_runs
                if job_id in self._processes and self._processes[job_id].is_running():
                    logger.debug(f"Continuous job '{job_id}' already adopted, skipping auto-start")
                    continue
                    
                state = self.db.get_state(job_id)
                # Skip paused jobs
                if state and state.paused:
                    logger.info(f"Skipping paused continuous job '{job_id}'")
                    continue
                # Check if already running from previous session (belt and suspenders)
                if state and state.pid and self.check_pid(state.pid):
                    if job_id not in self._processes:
                        logger.info(f"Job '{job_id}' still running from previous session (PID {state.pid}), adopting")
                        self._adopt_orphan_process(job_id, state.pid)
                    continue
                # Not running - start it
                logger.info(f"Auto-starting continuous job '{job_id}'")
                self.start_job(job_id, trigger="auto")

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
        healing_queue_task = asyncio.create_task(self._run_healing_queue())

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
            await self._proactive_scheduler.stop()
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
