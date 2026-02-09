"""Result aggregation for ProcClaw.

Captures and stores job outputs for use in workflows.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database


@dataclass
class JobResult:
    """Result from a job execution."""
    
    job_id: str
    run_id: int
    exit_code: int
    stdout_tail: str = ""  # Last N characters of stdout
    stderr_tail: str = ""  # Last N characters of stderr
    output_data: dict | None = None  # Custom output (from JSON file)
    duration_seconds: float = 0.0
    finished_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("UTC")))
    workflow_run_id: int | None = None
    step_index: int | None = None
    
    @property
    def success(self) -> bool:
        """Check if job succeeded."""
        return self.exit_code == 0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "output_data": self.output_data,
            "duration_seconds": self.duration_seconds,
            "finished_at": self.finished_at.isoformat(),
            "success": self.success,
        }
    
    def to_env_vars(self) -> dict[str, str]:
        """Convert to environment variables for next job."""
        return {
            "PROCCLAW_PREV_JOB_ID": self.job_id,
            "PROCCLAW_PREV_RUN_ID": str(self.run_id),
            "PROCCLAW_PREV_EXIT_CODE": str(self.exit_code),
            "PROCCLAW_PREV_SUCCESS": "1" if self.success else "0",
            "PROCCLAW_PREV_DURATION": f"{self.duration_seconds:.2f}",
            "PROCCLAW_PREV_STDOUT": self.stdout_tail[:1000],  # Limit for env var
        }


@dataclass 
class WorkflowResults:
    """Aggregated results from a workflow run."""
    
    workflow_run_id: int
    workflow_id: str
    results: dict[str, JobResult] = field(default_factory=dict)  # job_id -> result
    started_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("UTC")))
    finished_at: datetime | None = None
    
    @property
    def all_success(self) -> bool:
        """Check if all jobs succeeded."""
        return all(r.success for r in self.results.values())
    
    @property
    def any_failed(self) -> bool:
        """Check if any job failed."""
        return any(not r.success for r in self.results.values())
    
    @property
    def count_success(self) -> int:
        """Count successful jobs."""
        return sum(1 for r in self.results.values() if r.success)
    
    @property
    def count_failed(self) -> int:
        """Count failed jobs."""
        return sum(1 for r in self.results.values() if not r.success)
    
    def add_result(self, result: JobResult) -> None:
        """Add a job result."""
        self.results[result.job_id] = result
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "workflow_run_id": self.workflow_run_id,
            "workflow_id": self.workflow_id,
            "results": {k: v.to_dict() for k, v in self.results.items()},
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "all_success": self.all_success,
            "count_success": self.count_success,
            "count_failed": self.count_failed,
        }
    
    def to_env_vars(self) -> dict[str, str]:
        """Convert to environment variables for callback job."""
        return {
            "PROCCLAW_WORKFLOW_ID": self.workflow_id,
            "PROCCLAW_WORKFLOW_RUN_ID": str(self.workflow_run_id),
            "PROCCLAW_WORKFLOW_SUCCESS": "1" if self.all_success else "0",
            "PROCCLAW_WORKFLOW_COUNT_SUCCESS": str(self.count_success),
            "PROCCLAW_WORKFLOW_COUNT_FAILED": str(self.count_failed),
            "PROCCLAW_WORKFLOW_JOB_IDS": ",".join(self.results.keys()),
        }
    
    def write_results_file(self, path: Path) -> str:
        """Write results to a JSON file.
        
        Args:
            path: Directory to write to
            
        Returns:
            Path to the results file
        """
        path.mkdir(parents=True, exist_ok=True)
        results_file = path / f"workflow_{self.workflow_run_id}_results.json"
        
        with open(results_file, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        
        return str(results_file)


class ResultCollector:
    """Collects and stores job results.
    
    Features:
    - Capture stdout/stderr tail
    - Parse custom output files
    - Store in database
    - Aggregate for workflows
    """
    
    def __init__(
        self,
        db: "Database",
        logs_dir: Path,
        stdout_tail_chars: int = 10000,
        stderr_tail_chars: int = 5000,
    ):
        """Initialize the result collector.
        
        Args:
            db: Database for persistence
            logs_dir: Directory where job logs are stored
            stdout_tail_chars: Max chars to capture from stdout
            stderr_tail_chars: Max chars to capture from stderr
        """
        self._db = db
        self._logs_dir = logs_dir
        self._stdout_chars = stdout_tail_chars
        self._stderr_chars = stderr_tail_chars
        self._lock = Lock()
        
        # In-memory workflow results
        self._workflow_results: dict[int, WorkflowResults] = {}
    
    def capture(
        self,
        job_id: str,
        run_id: int,
        exit_code: int,
        duration_seconds: float,
        workflow_run_id: int | None = None,
        step_index: int | None = None,
    ) -> JobResult:
        """Capture results from a completed job.
        
        Args:
            job_id: The job ID
            run_id: The run ID
            exit_code: Exit code
            duration_seconds: Run duration
            workflow_run_id: If part of a workflow
            step_index: Step index in workflow
            
        Returns:
            The captured JobResult
        """
        # Read stdout tail
        stdout_tail = self._read_log_tail(job_id, "log", self._stdout_chars)
        
        # Read stderr tail
        stderr_tail = self._read_log_tail(job_id, "error.log", self._stderr_chars)
        
        # Check for output file
        output_data = self._read_output_file(job_id, run_id)
        
        result = JobResult(
            job_id=job_id,
            run_id=run_id,
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            output_data=output_data,
            duration_seconds=duration_seconds,
            workflow_run_id=workflow_run_id,
            step_index=step_index,
        )
        
        # Persist
        self._persist_to_db(result)
        
        # Add to workflow results if applicable
        if workflow_run_id:
            self._add_to_workflow(workflow_run_id, result)
        
        logger.debug(f"Captured result for job '{job_id}' run {run_id}: exit={exit_code}")
        return result
    
    def get_result(self, job_id: str, run_id: int) -> JobResult | None:
        """Get a specific job result."""
        try:
            rows = self._db.query(
                "SELECT * FROM job_results WHERE job_id = ? AND run_id = ?",
                (job_id, run_id),
            )
            if rows:
                return self._row_to_result(rows[0])
        except Exception as e:
            logger.error(f"Failed to get result: {e}")
        return None
    
    def get_last_result(self, job_id: str) -> JobResult | None:
        """Get the most recent result for a job."""
        try:
            rows = self._db.query(
                "SELECT * FROM job_results WHERE job_id = ? ORDER BY finished_at DESC LIMIT 1",
                (job_id,),
            )
            if rows:
                return self._row_to_result(rows[0])
        except Exception as e:
            logger.error(f"Failed to get last result: {e}")
        return None
    
    def start_workflow(self, workflow_run_id: int, workflow_id: str) -> WorkflowResults:
        """Start tracking results for a workflow run."""
        results = WorkflowResults(
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_id,
        )
        self._workflow_results[workflow_run_id] = results
        return results
    
    def get_workflow_results(self, workflow_run_id: int) -> WorkflowResults | None:
        """Get results for a workflow run."""
        return self._workflow_results.get(workflow_run_id)
    
    def finish_workflow(self, workflow_run_id: int) -> WorkflowResults | None:
        """Mark a workflow as finished and persist results."""
        results = self._workflow_results.get(workflow_run_id)
        if results:
            results.finished_at = datetime.now(ZoneInfo("UTC"))
            self._persist_workflow_results(results)
        return results
    
    def _add_to_workflow(self, workflow_run_id: int, result: JobResult) -> None:
        """Add a result to a workflow's results."""
        with self._lock:
            if workflow_run_id in self._workflow_results:
                self._workflow_results[workflow_run_id].add_result(result)
    
    def _read_log_tail(self, job_id: str, suffix: str, max_chars: int) -> str:
        """Read the tail of a log file."""
        log_path = self._logs_dir / f"{job_id}.{suffix}"
        
        if not log_path.exists():
            return ""
        
        try:
            # Read last N characters
            file_size = log_path.stat().st_size
            with open(log_path, "rb") as f:
                if file_size > max_chars:
                    f.seek(file_size - max_chars)
                content = f.read().decode("utf-8", errors="replace")
            return content[-max_chars:]
        except Exception as e:
            logger.debug(f"Failed to read log tail for {job_id}: {e}")
            return ""
    
    def _read_output_file(self, job_id: str, run_id: int) -> dict | None:
        """Read custom output file if exists."""
        # Check for output file written by job
        output_path = self._logs_dir / f"{job_id}.{run_id}.output.json"
        
        if not output_path.exists():
            # Also check for generic output file
            output_path = self._logs_dir / f"{job_id}.output.json"
        
        if not output_path.exists():
            return None
        
        try:
            with open(output_path) as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to read output file for {job_id}: {e}")
            return None
    
    def _persist_to_db(self, result: JobResult) -> None:
        """Persist a result to database."""
        try:
            self._db.execute(
                """
                INSERT INTO job_results 
                (job_id, run_id, exit_code, stdout_tail, stderr_tail, output_data,
                 duration_seconds, finished_at, workflow_run_id, step_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.job_id,
                    result.run_id,
                    result.exit_code,
                    result.stdout_tail,
                    result.stderr_tail,
                    json.dumps(result.output_data) if result.output_data else None,
                    result.duration_seconds,
                    result.finished_at.isoformat(),
                    result.workflow_run_id,
                    result.step_index,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to persist result: {e}")
    
    def _persist_workflow_results(self, results: WorkflowResults) -> None:
        """Persist workflow results to database."""
        try:
            self._db.execute(
                """
                UPDATE workflow_runs 
                SET finished_at = ?, results = ?, status = ?
                WHERE id = ?
                """,
                (
                    results.finished_at.isoformat() if results.finished_at else None,
                    json.dumps(results.to_dict()),
                    "completed" if results.all_success else "failed",
                    results.workflow_run_id,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to persist workflow results: {e}")
    
    def _row_to_result(self, row: dict) -> JobResult:
        """Convert a database row to JobResult."""
        return JobResult(
            job_id=row["job_id"],
            run_id=row["run_id"],
            exit_code=row["exit_code"],
            stdout_tail=row["stdout_tail"] or "",
            stderr_tail=row["stderr_tail"] or "",
            output_data=json.loads(row["output_data"]) if row["output_data"] else None,
            duration_seconds=row["duration_seconds"],
            finished_at=datetime.fromisoformat(row["finished_at"]).replace(tzinfo=ZoneInfo("UTC")),
            workflow_run_id=row["workflow_run_id"],
            step_index=row["step_index"],
        )
