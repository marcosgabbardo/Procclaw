"""Workflow composition for ProcClaw.

Provides chain, group, and chord primitives for complex job workflows.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database
    from procclaw.core.results import JobResult, ResultCollector, WorkflowResults


class WorkflowType(str, Enum):
    """Type of workflow."""
    
    CHAIN = "chain"  # Sequential execution
    GROUP = "group"  # Parallel execution
    CHORD = "chord"  # Group + callback


class WorkflowStatus(str, Enum):
    """Status of a workflow run."""
    
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OnFailure(str, Enum):
    """Behavior when a job fails in workflow."""
    
    STOP = "stop"      # Stop workflow immediately
    CONTINUE = "continue"  # Continue with remaining jobs
    RETRY = "retry"    # Retry the failed job


@dataclass
class WorkflowConfig:
    """Configuration for a workflow."""
    
    id: str
    name: str
    type: WorkflowType
    jobs: list[str]  # Job IDs
    callback: str | None = None  # For chord: job to run after group
    on_failure: OnFailure = OnFailure.STOP
    pass_results: bool = True  # Pass results to next job
    max_parallel: int = 10  # Max parallel jobs for group/chord
    timeout_seconds: int = 3600  # Workflow timeout
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "jobs": self.jobs,
            "callback": self.callback,
            "on_failure": self.on_failure.value,
            "pass_results": self.pass_results,
            "max_parallel": self.max_parallel,
            "timeout_seconds": self.timeout_seconds,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowConfig":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            type=WorkflowType(data["type"]),
            jobs=data["jobs"],
            callback=data.get("callback"),
            on_failure=OnFailure(data.get("on_failure", "stop")),
            pass_results=data.get("pass_results", True),
            max_parallel=data.get("max_parallel", 10),
            timeout_seconds=data.get("timeout_seconds", 3600),
        )


@dataclass
class WorkflowRun:
    """A running workflow instance."""
    
    id: int
    workflow_id: str
    config: WorkflowConfig
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_step: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("UTC")))
    finished_at: datetime | None = None
    job_run_ids: dict[str, int] = field(default_factory=dict)  # job_id -> run_id
    error: str | None = None
    
    @property
    def is_running(self) -> bool:
        """Check if workflow is running."""
        return self.status == WorkflowStatus.RUNNING
    
    @property
    def is_finished(self) -> bool:
        """Check if workflow has finished."""
        return self.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "current_step": self.current_step,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "job_run_ids": self.job_run_ids,
            "error": self.error,
        }


class WorkflowManager:
    """Manages workflow execution.
    
    Features:
    - Chain: Sequential job execution with result passing
    - Group: Parallel job execution
    - Chord: Group + callback
    - Failure handling
    - Persistence
    """
    
    def __init__(
        self,
        db: "Database",
        result_collector: "ResultCollector",
        start_job: Callable[[str, str, dict | None], bool],
        is_job_running: Callable[[str], bool],
        stop_job: Callable[[str], bool],
        logs_dir: Path,
    ):
        """Initialize the workflow manager.
        
        Args:
            db: Database for persistence
            result_collector: Result collector for aggregation
            start_job: Function to start a job (job_id, trigger, env) -> success
            is_job_running: Function to check if job is running
            stop_job: Function to stop a job
            logs_dir: Directory for workflow result files
        """
        self._db = db
        self._results = result_collector
        self._start_job = start_job
        self._is_job_running = is_job_running
        self._stop_job = stop_job
        self._logs_dir = logs_dir
        self._lock = Lock()
        
        # Active workflow runs
        self._active_runs: dict[int, WorkflowRun] = {}
        self._run_tasks: dict[int, asyncio.Task] = {}
        
        # Workflow configs
        self._workflows: dict[str, WorkflowConfig] = {}
        
        # Job completion tracking
        self._job_completions: dict[str, asyncio.Event] = {}
        
        self._running = False
        self._next_run_id = 1
        
        self._load_workflows()
        self._load_active_runs()
    
    def _load_workflows(self) -> None:
        """Load workflow configs from database."""
        try:
            rows = self._db.query("SELECT * FROM workflows")
            for row in rows:
                config = WorkflowConfig.from_dict(json.loads(row["config"]))
                self._workflows[config.id] = config
            logger.debug(f"Loaded {len(self._workflows)} workflows")
        except Exception as e:
            logger.debug(f"No workflows table yet: {e}")
    
    def _load_active_runs(self) -> None:
        """Load active workflow runs from database."""
        try:
            rows = self._db.query(
                "SELECT * FROM workflow_runs WHERE status IN ('pending', 'running')"
            )
            for row in rows:
                config = WorkflowConfig.from_dict(json.loads(row["config"]))
                run = WorkflowRun(
                    id=row["id"],
                    workflow_id=row["workflow_id"],
                    config=config,
                    status=WorkflowStatus(row["status"]),
                    current_step=row["current_step"],
                    started_at=datetime.fromisoformat(row["started_at"]).replace(tzinfo=ZoneInfo("UTC")),
                    job_run_ids=json.loads(row["job_run_ids"]) if row["job_run_ids"] else {},
                )
                self._active_runs[run.id] = run
                if row["id"] >= self._next_run_id:
                    self._next_run_id = row["id"] + 1
            logger.debug(f"Loaded {len(self._active_runs)} active workflow runs")
        except Exception as e:
            logger.debug(f"No workflow_runs table yet: {e}")
    
    def register_workflow(self, config: WorkflowConfig) -> None:
        """Register a workflow configuration."""
        self._workflows[config.id] = config
        self._persist_workflow(config)
        logger.info(f"Registered workflow '{config.id}'")
    
    def get_workflow(self, workflow_id: str) -> WorkflowConfig | None:
        """Get a workflow configuration."""
        return self._workflows.get(workflow_id)
    
    def list_workflows(self) -> list[WorkflowConfig]:
        """List all registered workflows."""
        return list(self._workflows.values())
    
    def start_workflow(self, workflow_id: str) -> WorkflowRun | None:
        """Start a workflow run.
        
        Args:
            workflow_id: The workflow to run
            
        Returns:
            The WorkflowRun or None if workflow not found
        """
        config = self._workflows.get(workflow_id)
        if not config:
            logger.error(f"Workflow '{workflow_id}' not found")
            return None
        
        with self._lock:
            run_id = self._next_run_id
            self._next_run_id += 1
        
        run = WorkflowRun(
            id=run_id,
            workflow_id=workflow_id,
            config=config,
            status=WorkflowStatus.RUNNING,
        )
        
        self._active_runs[run_id] = run
        self._persist_run(run)
        
        # Start result tracking
        self._results.start_workflow(run_id, workflow_id)
        
        # Launch execution task
        if self._running:
            task = asyncio.create_task(self._execute_workflow(run))
            self._run_tasks[run_id] = task
        
        logger.info(f"Started workflow '{workflow_id}' run {run_id}")
        return run
    
    def cancel_workflow(self, run_id: int) -> bool:
        """Cancel a running workflow.
        
        Args:
            run_id: The workflow run ID
            
        Returns:
            True if cancelled
        """
        run = self._active_runs.get(run_id)
        if not run or run.is_finished:
            return False
        
        run.status = WorkflowStatus.CANCELLED
        run.finished_at = datetime.now(ZoneInfo("UTC"))
        run.error = "Cancelled by user"
        
        # Cancel the task
        task = self._run_tasks.get(run_id)
        if task:
            task.cancel()
        
        # Stop any running jobs
        for job_id in run.config.jobs:
            if self._is_job_running(job_id):
                self._stop_job(job_id)
        
        self._persist_run(run)
        self._results.finish_workflow(run_id)
        
        logger.info(f"Cancelled workflow run {run_id}")
        return True
    
    def get_run(self, run_id: int) -> WorkflowRun | None:
        """Get a workflow run."""
        return self._active_runs.get(run_id)
    
    def list_runs(self, workflow_id: str | None = None) -> list[WorkflowRun]:
        """List workflow runs."""
        runs = list(self._active_runs.values())
        if workflow_id:
            runs = [r for r in runs if r.workflow_id == workflow_id]
        return runs
    
    def notify_job_complete(self, job_id: str, run_id: int, result: "JobResult") -> None:
        """Notify that a job has completed.
        
        Args:
            job_id: The completed job
            run_id: The job run ID
            result: The job result
        """
        # Signal completion
        event = self._job_completions.get(job_id)
        if event:
            event.set()
    
    async def _execute_workflow(self, run: WorkflowRun) -> None:
        """Execute a workflow run.
        
        Args:
            run: The workflow run
        """
        try:
            if run.config.type == WorkflowType.CHAIN:
                await self._execute_chain(run)
            elif run.config.type == WorkflowType.GROUP:
                await self._execute_group(run)
            elif run.config.type == WorkflowType.CHORD:
                await self._execute_chord(run)
            
            # Mark completed
            if run.status == WorkflowStatus.RUNNING:
                run.status = WorkflowStatus.COMPLETED
                run.finished_at = datetime.now(ZoneInfo("UTC"))
                
        except asyncio.CancelledError:
            run.status = WorkflowStatus.CANCELLED
            run.finished_at = datetime.now(ZoneInfo("UTC"))
            raise
            
        except Exception as e:
            logger.error(f"Workflow {run.id} failed: {e}")
            run.status = WorkflowStatus.FAILED
            run.finished_at = datetime.now(ZoneInfo("UTC"))
            run.error = str(e)
        
        finally:
            self._persist_run(run)
            self._results.finish_workflow(run.id)
            
            # Clean up
            if run.id in self._run_tasks:
                del self._run_tasks[run.id]
    
    async def _execute_chain(self, run: WorkflowRun) -> None:
        """Execute a chain workflow (sequential).
        
        Args:
            run: The workflow run
        """
        prev_result: JobResult | None = None
        
        for i, job_id in enumerate(run.config.jobs):
            run.current_step = i
            self._persist_run(run)
            
            # Prepare environment with previous result
            env: dict[str, str] = {}
            if prev_result and run.config.pass_results:
                env = prev_result.to_env_vars()
            
            # Start job
            success = await self._start_and_wait(job_id, run, env)
            
            if not success:
                if run.config.on_failure == OnFailure.STOP:
                    run.status = WorkflowStatus.FAILED
                    run.error = f"Job '{job_id}' failed at step {i}"
                    return
                elif run.config.on_failure == OnFailure.CONTINUE:
                    logger.warning(f"Job '{job_id}' failed, continuing chain")
            
            # Get result for next job
            prev_result = self._results.get_last_result(job_id)
        
        logger.info(f"Chain workflow {run.id} completed")
    
    async def _execute_group(self, run: WorkflowRun) -> None:
        """Execute a group workflow (parallel).
        
        Args:
            run: The workflow run
        """
        # Start all jobs in parallel (with limit)
        semaphore = asyncio.Semaphore(run.config.max_parallel)
        
        async def run_job(job_id: str) -> bool:
            async with semaphore:
                return await self._start_and_wait(job_id, run, {})
        
        tasks = [run_job(job_id) for job_id in run.config.jobs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Check for failures
        failed = sum(1 for r in results if r is False or isinstance(r, Exception))
        
        if failed > 0:
            if run.config.on_failure == OnFailure.STOP:
                run.status = WorkflowStatus.FAILED
                run.error = f"{failed} jobs failed in group"
                return
        
        logger.info(f"Group workflow {run.id} completed ({len(run.config.jobs)} jobs)")
    
    async def _execute_chord(self, run: WorkflowRun) -> None:
        """Execute a chord workflow (group + callback).
        
        Args:
            run: The workflow run
        """
        # Run group phase
        await self._execute_group(run)
        
        if run.status == WorkflowStatus.FAILED:
            return
        
        # Run callback
        if run.config.callback:
            # Get aggregated results
            workflow_results = self._results.get_workflow_results(run.id)
            
            env: dict[str, str] = {}
            if workflow_results and run.config.pass_results:
                env = workflow_results.to_env_vars()
                # Write results file
                results_file = workflow_results.write_results_file(self._logs_dir)
                env["PROCCLAW_RESULTS_FILE"] = results_file
            
            success = await self._start_and_wait(run.config.callback, run, env)
            
            if not success:
                run.status = WorkflowStatus.FAILED
                run.error = f"Callback job '{run.config.callback}' failed"
                return
        
        logger.info(f"Chord workflow {run.id} completed")
    
    async def _start_and_wait(
        self,
        job_id: str,
        run: WorkflowRun,
        env: dict[str, str],
    ) -> bool:
        """Start a job and wait for completion.
        
        Args:
            job_id: The job to run
            run: The workflow run
            env: Environment variables to pass
            
        Returns:
            True if job succeeded
        """
        # Create completion event
        event = asyncio.Event()
        self._job_completions[job_id] = event
        
        try:
            # Add workflow context to env
            env["PROCCLAW_WORKFLOW_ID"] = run.workflow_id
            env["PROCCLAW_WORKFLOW_RUN_ID"] = str(run.id)
            
            # Start job
            success = self._start_job(job_id, f"workflow:{run.workflow_id}", env)
            
            if not success:
                logger.error(f"Failed to start job '{job_id}' in workflow {run.id}")
                return False
            
            # Wait for completion
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=run.config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.error(f"Job '{job_id}' timed out in workflow {run.id}")
                self._stop_job(job_id)
                return False
            
            # Check result
            result = self._results.get_last_result(job_id)
            if result:
                run.job_run_ids[job_id] = result.run_id
                return result.success
            
            return False
            
        finally:
            del self._job_completions[job_id]
    
    def _persist_workflow(self, config: WorkflowConfig) -> None:
        """Persist a workflow config to database."""
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO workflows (id, name, type, config)
                VALUES (?, ?, ?, ?)
                """,
                (config.id, config.name, config.type.value, json.dumps(config.to_dict())),
            )
        except Exception as e:
            logger.error(f"Failed to persist workflow: {e}")
    
    def _persist_run(self, run: WorkflowRun) -> None:
        """Persist a workflow run to database."""
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO workflow_runs 
                (id, workflow_id, config, status, current_step, started_at, finished_at, job_run_ids, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.workflow_id,
                    json.dumps(run.config.to_dict()),
                    run.status.value,
                    run.current_step,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                    json.dumps(run.job_run_ids),
                    run.error,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to persist workflow run: {e}")
    
    async def run(self) -> None:
        """Run the workflow manager loop."""
        self._running = True
        logger.info("Workflow manager started")
        
        # Resume any pending workflows
        for run in list(self._active_runs.values()):
            if run.status == WorkflowStatus.RUNNING:
                task = asyncio.create_task(self._execute_workflow(run))
                self._run_tasks[run.id] = task
        
        while self._running:
            # Cleanup completed runs
            for run_id, run in list(self._active_runs.items()):
                if run.is_finished:
                    del self._active_runs[run_id]
            
            await asyncio.sleep(1)
        
        logger.info("Workflow manager stopped")
    
    def stop(self) -> None:
        """Stop the workflow manager."""
        self._running = False
        
        # Cancel all running tasks
        for task in self._run_tasks.values():
            task.cancel()
