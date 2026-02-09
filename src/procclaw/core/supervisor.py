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
    ProcClawConfig,
)


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

        # Ensure directories exist
        ensure_config_dir()

    def reload_jobs(self) -> None:
        """Reload jobs configuration."""
        self.jobs = load_jobs()
        logger.info(f"Reloaded {len(self.jobs.jobs)} jobs")

    # Process Management

    def start_job(self, job_id: str, trigger: str = "manual") -> bool:
        """Start a job."""
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

        try:
            handle = self._spawn_process(job_id, job)
            self._processes[job_id] = handle

            # Update state
            state = JobState(
                job_id=job_id,
                status=JobStatus.RUNNING,
                pid=handle.pid,
                started_at=handle.started_at,
            )
            self.db.save_state(state)

            # Record run
            run = JobRun(
                job_id=job_id,
                started_at=handle.started_at,
                trigger=trigger,
            )
            run.id = self.db.add_run(run)

            logger.info(f"Started job '{job_id}' (PID: {handle.pid})")
            self._audit_log(job_id, "started", f"PID: {handle.pid}, trigger: {trigger}")

            return True

        except Exception as e:
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
        return self.start_job(job_id, trigger="restart")

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
            "next_run": state.next_run.isoformat() if state.next_run else None,
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

        # Update state
        state = self.db.get_state(job_id) or JobState(job_id=job_id)
        state.status = JobStatus.STOPPED if exit_code == 0 else JobStatus.FAILED
        state.stopped_at = now
        state.last_exit_code = exit_code
        if exit_code != 0:
            state.last_error = f"Process exited with code {exit_code}"
        self.db.save_state(state)

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
